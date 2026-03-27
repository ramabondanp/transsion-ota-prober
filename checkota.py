#!/usr/bin/python3

import argparse
import html
import io
import os
import re
import sys
import threading
import textwrap
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Dict, Optional, Set, Tuple

import requests

SUBMODULE_DIR = Path(__file__).resolve().parent / "google-ota-prober"
if SUBMODULE_DIR.is_dir():
    subs_path = str(SUBMODULE_DIR)
    if subs_path not in sys.path:
        sys.path.insert(0, subs_path)

from modules.manager import Config, parse_fingerprint, region_code_from_product, region_from_product, update_config_from_fingerprint
from modules.logging import Log
from modules.telegram import TgNotify
from modules.metadata import (
    build_sdk_strings,
    extract_incremental_from_fingerprint,
    get_ota_metadata,
    processed_updates_path,
)
from modules.fingerprints import load_processed_titles, save_processed_title
from modules.update_checker import UpdateChecker


@dataclass
class RunContext:
    env: Dict[str, str]
    processed_path: Path
    processed_titles: Set[str]
    dry_run: bool
    metadata_cache: Dict[str, Optional[Dict[str, str]]] = field(default_factory=dict)
    file_lock: threading.Lock = field(default_factory=threading.Lock)
    telegram_lock: threading.Lock = field(default_factory=threading.Lock)
    cache_lock: threading.Lock = field(default_factory=threading.Lock)
    notice_lock: threading.Lock = field(default_factory=threading.Lock)
    telegram_notice_printed: bool = False
    _local: threading.local = field(default_factory=threading.local, repr=False)

    def session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            self._local.session = session
        return session


@dataclass
class VariantUpdate:
    cfg: Config
    config_path: Path
    variant_label: Optional[str]
    region_name: Optional[str]
    title: str
    url: str
    size: str
    desc: str
    is_new_update: bool
    target_fp: str
    target_incremental: Optional[str]
    sdk_message: str
    data: Dict[str, str]


class TerminalParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.indent = 0
        self.bold = False
        self.list_stack = []
        self.ol_counter = []
        self.buffer = ""
        self.lines = []

    def _push(self, line: str = ""):
        self.lines.append(line)

    def handle_starttag(self, tag, attrs):
        if tag == "b":
            self.bold = True
        elif tag == "h3":
            self.flush()
        elif tag == "h4":
            self.flush()
        elif tag == "ol":
            self.flush()
            self.list_stack.append("ol")
            self.ol_counter.append(0)
            self.indent += 2
        elif tag == "ul":
            self.flush()
            self.list_stack.append("ul")
            self.indent += 2
        elif tag == "li":
            self.flush()
        elif tag == "br":
            self.flush()
            self._push("")

    def handle_endtag(self, tag):
        if tag == "b":
            self.bold = False
        elif tag in ("h3", "h4"):
            self.flush(style=tag)
        elif tag in ("ol", "ul"):
            self.flush()
            if self.list_stack:
                lst = self.list_stack.pop()
                if lst == "ol" and self.ol_counter:
                    self.ol_counter.pop()
            self.indent = max(0, self.indent - 2)

    def handle_data(self, data):
        self.buffer += html.unescape(data)

    def flush(self, style=None):
        text = self.buffer.strip()
        self.buffer = ""
        if not text:
            return

        prefix = " " * self.indent
        width = max(20, 100 - self.indent)

        if style == "h3":
            self._push("\033[1;36m" + "=" * 60 + "\033[0m")
            self._push("\033[1;36m  " + text.upper() + "\033[0m")
            self._push("\033[1;36m" + "=" * 60 + "\033[0m")
            return
        if style == "h4":
            self._push("\033[1;33m  " + text + "\033[0m")
            return

        if self.list_stack:
            lst_type = self.list_stack[-1]
            if lst_type == "ol":
                self.ol_counter[-1] += 1
                bullet = f"{self.ol_counter[-1]}."
            else:
                bullet = "•"

            lines = textwrap.wrap(text, width - len(bullet) - 1) or [text]
            for idx, line in enumerate(lines):
                if idx == 0:
                    if self.bold or text.endswith(":"):
                        self._push(prefix + f"\033[1;32m{bullet} {line}\033[0m")
                    else:
                        self._push(prefix + f"{bullet} {line}")
                else:
                    self._push(prefix + "  " + line)
            return

        if self.bold:
            self._push("\033[1m" + prefix + text + "\033[0m")
            return

        for line in textwrap.wrap(text, width) or [text]:
            self._push(prefix + line)

    def render(self, markup: str) -> str:
        self.feed(markup)
        self.flush()
        return "\n".join(self.lines).rstrip()


def format_update_description(description: str) -> str:
    parser = TerminalParser()
    return parser.render(description or "")


def config_from_fingerprint(fingerprint: str) -> Config:
    pattern = re.compile(
        r"^(?P<oem>[^/]+)/(?P<product>[^/]+)/(?P<device>[^:]+):"
        r"(?P<android_version>[^/]+)/(?P<build_tag>[^/]+)/(?P<incremental>[^:]+):.+$"
    )
    match = pattern.match(fingerprint.strip())
    if not match:
        raise ValueError(
            "Invalid fingerprint format. Expected: "
            "oem/product/device:android_version/build_tag/incremental:user/release-keys"
        )

    parsed = match.groupdict()
    return Config(
        build_tag=parsed["build_tag"],
        incremental=parsed["incremental"],
        android_version=parsed["android_version"],
        model=parsed["device"],
        device=parsed["device"],
        oem=parsed["oem"],
        product=parsed["product"],
        variant=None,
        variant_index=None,
    )


def log_variant_header(cfg: Config, variant_label: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    fingerprint = cfg.fingerprint()
    region_name = region_from_product(cfg.product)
    region_code = region_code_from_product(cfg.product)
    normalized_variant = variant_label.strip().lower() if variant_label else None
    normalized_region = region_name.strip().lower() if region_name else None

    Log.i(f"Device: {cfg.model} ({cfg.device})")
    if variant_label and region_name and normalized_variant == normalized_region:
        combined = variant_label
        if region_code:
            combined = f"{combined} ({region_code})"
        Log.i(f"Variant / Region: {combined}")
    else:
        if variant_label:
            Log.i(f"Variant: {variant_label}")
        if region_name:
            region_display = region_name
            if region_code:
                region_display = f"{region_display} ({region_code})"
            Log.i(f"Region: {region_display}")
    Log.i(f"Build: {fingerprint}")
    return region_name, region_code


def get_cached_ota_metadata(ctx: RunContext, url: str) -> Optional[Dict[str, str]]:
    with ctx.cache_lock:
        cached = ctx.metadata_cache.get(url, None)
        if url in ctx.metadata_cache:
            return cached

    ota_meta = get_ota_metadata(url, session=ctx.session())
    with ctx.cache_lock:
        ctx.metadata_cache[url] = ota_meta
    return ota_meta


def save_processed_update(ctx: RunContext, title: str) -> None:
    with ctx.file_lock:
        if title not in ctx.processed_titles:
            save_processed_title(ctx.processed_path, title)
            ctx.processed_titles.add(title)


def create_notifier(ctx: RunContext, args: argparse.Namespace) -> Optional[TgNotify]:
    if args.skip_telegram or args.register_update:
        return None

    token = ctx.env.get("bot_token")
    chat = ctx.env.get("chat_id")
    telegraph_token = ctx.env.get("telegraph_token")
    if not token or not chat or not telegraph_token:
        with ctx.notice_lock:
            if not ctx.telegram_notice_printed:
                if ctx.dry_run:
                    Log.i("Dry-run mode: Telegram env vars not set; notifications skipped.")
                else:
                    Log.w("Telegram env vars not set, skipping notifications")
                ctx.telegram_notice_printed = True
        return None

    try:
        return TgNotify(token, chat, telegraph_token, session=ctx.session())
    except ValueError as exc:
        Log.e(f"Telegram setup failed: {exc}")
        return None


def build_notification_message(update: VariantUpdate) -> str:
    region_line = f" ({update.region_name})" if update.region_name else ""
    os_line = f"<b>OS:</b> {update.sdk_message}\n" if update.sdk_message else ""
    inc = update.data.get("post_build_incremental")
    spl = update.data.get("post_security_patch_level")
    build_date = update.data.get("build_date")
    return (
        f"<blockquote><b>OTA Update Available</b></blockquote>\n\n"
        f"<b>Device:</b> {update.cfg.model}{region_line}\n"
        f"\n"
        f"<b>Title:</b> {update.title}\n"
        f"{os_line}\n"
        f"{update.desc}\n\n"
        f"<b>Size:</b> {update.size}\n"
        + (f"<b>Incremental:</b> <code>{inc}</code>\n" if inc else "")
        + (f"<b>Security patch:</b> {spl}\n" if spl else "")
        + f"<b>Fingerprint:</b> <code>{update.target_fp}</code>"
        + (f"\n<b>Build date:</b> {build_date} (CST)" if build_date else "")
        + (f"\n<b>Google OTA link:</b> {html.escape(update.url, quote=False)}" if update.url else "")
    )


def collect_update_info(
    ctx: RunContext,
    cfg: Config,
    config_path: Path,
    args: argparse.Namespace,
    variant_label: Optional[str] = None,
) -> Tuple[int, Optional[VariantUpdate]]:
    update_incremental_only = bool(getattr(args, "update_incremental", False))
    if update_incremental_only:
        args.skip_telegram = True

    region_name, _ = log_variant_header(cfg, variant_label)
    checker = UpdateChecker(cfg, session=ctx.session(), imei=args.imei)
    found, data = checker.check(args.debug)

    if not found or not data:
        Log.i("No updates found")
        return 0, None

    title = data.get("title")
    url = data.get("url")
    size = data.get("size")
    desc = data.get("description", "No description")

    if getattr(args, "gen_fp", False):
        if not url:
            Log.e("Missing OTA URL in update response; cannot fetch target fingerprint.")
            return 1, None
        ota_meta = get_cached_ota_metadata(ctx, url)
        if not ota_meta or not ota_meta.get("fingerprint"):
            Log.e("Could not determine target fingerprint from OTA metadata.")
            return 1, None
        Log.raw(ota_meta["fingerprint"])
        return 0, None

    if args.dry_run and not title and url and size:
        title = "UNKNOWN_TITLE_DRY_RUN"
        Log.w("Missing update title; continuing because --dry-run is enabled.")
    elif not all([title, url, size]):
        Log.e("Missing essential update info (title, url, or size)")
        return 1, None

    Log.s(f"New OTA update found: {title}")
    Log.i(f"Size: {size}")
    Log.i(f"URL: {url}")
    if args.dry_run and args.fp and desc:
        Log.i("Description:")
        formatted_desc = format_update_description(desc)
        Log.raw(formatted_desc if formatted_desc else desc)

    is_new_update = title not in ctx.processed_titles
    if args.register_update:
        if not is_new_update:
            Log.i("--register-update flag is set, but update title is already known. No action taken.")
            return 0, None
        Log.i("--register-update set. Skipping config incremental update.")
        if args.dry_run:
            Log.i("--register-update set. Dry-run: would save new update title without notification.")
        else:
            Log.i("--register-update flag is set. Saving new update title without notification.")
            save_processed_update(ctx, title)
            Log.s("Update check completed successfully (update title registered).")
        return 0, None

    if not is_new_update:
        if update_incremental_only and not args.force_update_incremental:
            Log.i("Update title already known; skipping incremental update due to --update-incremental.")
            return 0, None
        if update_incremental_only and args.force_update_incremental:
            Log.i("Update title already known; continuing due to --force with --update-incremental.")
        elif not args.force_notify:
            Log.i("This update has already been processed. Skipping.")
            return 0, None

    ota_meta = get_cached_ota_metadata(ctx, url)
    if not ota_meta or not ota_meta.get("fingerprint"):
        Log.e("Could not determine target fingerprint from OTA metadata. Cannot derive incremental information.")
        return 1, None

    target_fp = ota_meta["fingerprint"]
    Log.i(f"Target build: {target_fp}")
    inc = ota_meta.get("post_build_incremental")
    spl = ota_meta.get("post_security_patch_level")
    build_date = ota_meta.get("build_date")
    sdk_level = ota_meta.get("post_sdk_level")
    android_ver = ota_meta.get("android_version")
    sdk_message, sdk_log_line, _ = build_sdk_strings(sdk_level, android_ver)
    if inc:
        Log.i(f"Incremental: {inc}")
    if spl:
        Log.i(f"Security patch: {spl}")
    if build_date:
        Log.i(f"Build date: {build_date} (CST)")
    if sdk_log_line:
        Log.i(sdk_log_line)
    if not is_new_update and args.force_notify:
        Log.w(f"Forcing notification for an already processed update: {title}")

    data["fingerprint"] = target_fp
    if inc:
        data["post_build_incremental"] = inc
    if spl:
        data["post_security_patch_level"] = spl
    if build_date:
        data["build_date"] = build_date
    if sdk_level:
        data["post_sdk_level"] = sdk_level
    if android_ver:
        data["android_version"] = android_ver

    return 0, VariantUpdate(
        cfg=cfg,
        config_path=config_path,
        variant_label=variant_label,
        region_name=region_name,
        title=title,
        url=url,
        size=size,
        desc=desc,
        is_new_update=is_new_update,
        target_fp=target_fp,
        target_incremental=inc or extract_incremental_from_fingerprint(target_fp),
        sdk_message=sdk_message,
        data=data,
    )


def apply_update_actions(ctx: RunContext, update: VariantUpdate, args: argparse.Namespace) -> int:
    update_incremental_only = bool(getattr(args, "update_incremental", False))
    if update_incremental_only or update.is_new_update:
        parsed_target = parse_fingerprint(update.target_fp)
        if args.incremental:
            Log.i("--incremental override active; skipping config file update.")
        elif getattr(args, "no_config", False):
            Log.i("No config file mode; skipping incremental config update.")
        elif (
            "Tcard" in update.title
            and parsed_target
            and parsed_target["android_version"] == update.cfg.android_version
        ):
            Log.i("Skipping config update because update title contains 'Tcard' without an Android version change.")
        elif update.target_incremental:
            if args.dry_run:
                if parsed_target:
                    Log.i(
                        f"Dry-run: would update {update.config_path} "
                        f"android_version={parsed_target['android_version']}, "
                        f"build_tag={parsed_target['build_tag']}, "
                        f"incremental={parsed_target['incremental']}."
                    )
                else:
                    Log.i(f"Dry-run: would update {update.config_path} incremental to {update.target_incremental}.")
            else:
                with ctx.file_lock:
                    if update_config_from_fingerprint(update.config_path, update.cfg, update.target_fp):
                        if parsed_target:
                            update.cfg.android_version = parsed_target["android_version"]
                            update.cfg.build_tag = parsed_target["build_tag"]
                            update.cfg.incremental = parsed_target["incremental"]
        else:
            Log.w("Unable to determine new incremental value from OTA metadata; config not updated.")

    notifier = create_notifier(ctx, args)
    if notifier:
        msg = build_notification_message(update)
        if args.dry_run:
            Log.i("Dry-run: would send Telegram notification with OTA details.")
            if update.is_new_update:
                Log.i("Dry-run: would save new update title after successful notification.")
        else:
            with ctx.telegram_lock:
                sent = notifier.send(msg, truncate_desc=True, device_title=f"{update.cfg.model} - {update.title}")
            if not sent:
                Log.e("Failed to send notification. Update title will not be saved.")
                return 1
            if update.is_new_update:
                save_processed_update(ctx, update.title)

    Log.s("Update check completed successfully")
    return 0


def process_config_variant(
    ctx: RunContext,
    cfg: Config,
    config_path: Path,
    args: argparse.Namespace,
    variant_label: Optional[str] = None,
) -> int:
    status, update = collect_update_info(ctx, cfg, config_path, args, variant_label)
    if status != 0 or update is None:
        return status
    return apply_update_actions(ctx, update, args)


def process_config(config_path: Path, args: argparse.Namespace) -> int:
    try:
        configs = Config.from_yaml(config_path)
    except Exception as exc:
        Log.e(f"Config error for {config_path}: {exc}")
        return 1

    if args.region:
        region_code = args.region.strip().upper()
        Log.i(f"Filtering configuration variants by region code: {region_code}")
        filtered_configs = [
            cfg for cfg in configs if region_code_from_product(cfg.product) == region_code
        ]
        if not filtered_configs:
            Log.e(f"No configuration variants in {config_path} match region code {region_code}")
            return 1
        configs = filtered_configs

    if args.incremental and len(configs) != 1:
        Log.e(
            "--incremental requires a single configuration variant. "
            "Use --reg to select a specific region when multiple variants exist."
        )
        return 1

    exit_code = 0
    variants_total = len(configs)

    if variants_total > 1:
        Log.raw("")

    for idx, cfg in enumerate(configs, start=1):
        variant_label = cfg.variant
        display_label = variant_label or f"variant {idx}"

        if variants_total > 1 and idx > 1:
            Log.raw("")
        if variants_total > 1:
            Log.i(f"Processing variant {idx}/{variants_total}: {display_label}")

        if args.incremental:
            Log.i(f"Override incremental: {args.incremental}")
            cfg.incremental = args.incremental

        result = process_config_variant(args.run_context, cfg, config_path, args, variant_label)
        exit_code = max(exit_code, result)

    return exit_code


def create_run_context(args: argparse.Namespace) -> RunContext:
    if args.dry_run:
        Log.i("Dry-run mode enabled: no external side effects will occur.")

    processed_path = processed_updates_path()
    env = {
        "bot_token": os.environ.get("bot_token", ""),
        "chat_id": os.environ.get("chat_id", ""),
        "telegraph_token": os.environ.get("telegraph_token", ""),
    }
    return RunContext(
        env=env,
        processed_path=processed_path,
        processed_titles=load_processed_titles(processed_path),
        dry_run=args.dry_run,
    )


def main() -> int:
    if sys.version_info < (3, 7):
        Log.e("Requires Python 3.7+")
        return 1

    parser = argparse.ArgumentParser(description="Android OTA Update Checker")
    parser.add_argument("--debug", action="store_true", help="Enable debugging")
    parser.add_argument("-c", "--config", type=Path, help="Config file path")
    parser.add_argument("-d", "--config-dir", type=Path, help="Directory containing config files to process")
    parser.add_argument("--fp", help="Use this full Android fingerprint directly and skip config file loading")
    parser.add_argument("--dry-run", action="store_true", help="Simulate actions without making changes or sending notifications")
    parser.add_argument("--skip-telegram", action="store_true", help="Skip Telegram notifications")
    parser.add_argument(
        "--register-update",
        action="store_true",
        dest="register_update",
        help="Save the update title without sending a notification",
    )
    parser.add_argument(
        "--update-incremental",
        action="store_true",
        help="Update the config incremental value without notifications",
    )
    parser.add_argument("--force-notify", action="store_true", help="Send notification even if the update has been seen before")
    parser.add_argument(
        "--force",
        dest="force_update_incremental",
        action="store_true",
        help="Allow --update-incremental to update even if the update title is already known",
    )
    parser.add_argument("-i", "--incremental", help="Override incremental version")
    parser.add_argument("--imei", help="Override IMEI used in the OTA check-in request")
    parser.add_argument(
        "--gen-fp",
        action="store_true",
        help="Print update target fingerprint(s) only when OTA updates are available",
    )
    parser.add_argument("--reg", "--region", dest="region", help="Process only variants matching the given region code (e.g. OP, RU)")
    parser.add_argument(
        "--jobs",
        type=int,
        default=1,
        help="Number of config files to process in parallel when using --config-dir (default: 1)",
    )
    args = parser.parse_args()

    if args.update_incremental:
        args.skip_telegram = True
    if args.gen_fp:
        args.skip_telegram = True

    if args.config and args.config_dir:
        parser.error("Use either --config or --config-dir, not both.")

    if args.fp and (args.config or args.config_dir):
        parser.error("Use --fp alone, or use --config/--config-dir.")

    if args.region and args.fp:
        parser.error("--region cannot be used with --fp.")

    if args.fp and args.incremental:
        parser.error("--incremental cannot be used with --fp.")

    if not args.fp and not args.config and not args.config_dir:
        parser.error("Either --fp or --config/--config-dir is required.")

    if args.config and args.config.is_dir():
        parser.error("--config expects a file. Use --config-dir for directories.")

    if args.fp:
        args.no_config = True
        args.run_context = create_run_context(args)
        try:
            cfg = config_from_fingerprint(args.fp)
        except ValueError as exc:
            Log.e(str(exc))
            return 1
        Log.i("Processing direct fingerprint input")
        return process_config_variant(
            args.run_context,
            cfg=cfg,
            config_path=Path("<fingerprint>"),
            args=args,
            variant_label=None,
        )

    args.no_config = False
    args.run_context = create_run_context(args)

    if args.config:
        config_paths = [args.config]
    else:
        if not args.config_dir.exists() or not args.config_dir.is_dir():
            parser.error("--config-dir must be an existing directory.")

        config_paths = sorted(
            (
                path
                for pattern in ("*.yml", "*.yaml")
                for path in args.config_dir.glob(pattern)
                if path.is_file()
            ),
            key=lambda p: p.name.lower(),
        )

        if not config_paths:
            Log.e(f"No config files found in directory: {args.config_dir}")
            return 1

    if args.incremental and len(config_paths) != 1:
        Log.e("--incremental can only be used with a single config file")
        return 1

    if args.jobs < 1:
        Log.e("--jobs must be >= 1")
        return 1

    exit_code = 0
    total = len(config_paths)

    def run_config(index: int, path: Path) -> int:
        local_args = argparse.Namespace(**vars(args))
        if index > 1 and args.jobs == 1:
            Log.raw("")
        header = f"Processing config {index}/{total}: {path}" if total > 1 else f"Processing config: {path}"
        Log.i(header)
        return process_config(path, local_args)

    if args.jobs == 1 or total == 1:
        for idx, config_path in enumerate(config_paths, start=1):
            result = run_config(idx, config_path)
            exit_code = max(exit_code, result)
    else:
        def run_config_buffered(index: int, path: Path) -> Tuple[int, int, str]:
            local_args = argparse.Namespace(**vars(args))
            buffer = io.StringIO()
            with Log.capture(buffer):
                header = f"Processing config {index}/{total}: {path}" if total > 1 else f"Processing config: {path}"
                Log.i(header)
                result = process_config(path, local_args)
            return index, result, buffer.getvalue()

        with ThreadPoolExecutor(max_workers=min(args.jobs, total)) as executor:
            start_times = {}
            futures = {}
            for idx, config_path in enumerate(config_paths, start=1):
                start_times[idx] = time.monotonic()
                futures[executor.submit(run_config_buffered, idx, config_path)] = idx
            results: Dict[int, Tuple[int, str]] = {}
            remaining = set(futures.keys())
            next_index = 1
            first = True
            last_heartbeat = time.monotonic()

            while remaining or next_index <= total:
                if remaining:
                    done, remaining = wait(remaining, timeout=2, return_when=FIRST_COMPLETED)
                    for future in done:
                        index, result, output = future.result()
                        results[index] = (result, output)

                while next_index in results:
                    result, output = results.pop(next_index)
                    if not first:
                        Log.raw("")
                    first = False
                    if output:
                        print(output, end="" if output.endswith("\n") else "\n")
                    exit_code = max(exit_code, result)
                    next_index += 1

                now = time.monotonic()
                if now - last_heartbeat >= 5 and next_index <= total:
                    completed = next_index - 1
                    running = len(remaining)
                    buffered = len(results)
                    elapsed = now - start_times.get(next_index, now)
                    Log.raw(
                        f"... waiting for config {next_index}/{total} "
                        f"({completed}/{total} completed, {running} running, {buffered} buffered, {elapsed:.0f}s elapsed)"
                    )
                    last_heartbeat = now

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
