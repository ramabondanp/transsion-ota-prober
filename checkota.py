#!/usr/bin/python3

import argparse
import html
import io
import os
import re
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Optional

SUBMODULE_DIR = Path(__file__).resolve().parent / "google-ota-prober"
if SUBMODULE_DIR.is_dir():
    subs_path = str(SUBMODULE_DIR)
    if subs_path not in sys.path:
        sys.path.insert(0, subs_path)

from modules.manager import Config, region_code_from_product, region_from_product, update_config_incremental
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

FILE_LOCK = threading.Lock()
TELEGRAM_LOCK = threading.Lock()


def process_config_variant(
    cfg: Config,
    config_name: str,
    config_path: Path,
    args: argparse.Namespace,
    variant_label: Optional[str] = None,
) -> int:
    tg = None
    update_incremental_only = bool(getattr(args, "update_incremental", False))
    if update_incremental_only:
        args.skip_telegram = True
    if not args.skip_telegram and not args.register_update:
        token = os.environ.get("bot_token")
        chat = os.environ.get("chat_id")
        telegraph_token = os.environ.get("telegraph_token")

        if not token or not chat or not telegraph_token:
            if args.dry_run:
                if not getattr(args, "_printed_dry_run_telegram_notice", False):
                    Log.i("Dry-run mode: Telegram env vars not set; notifications skipped.")
                    setattr(args, "_printed_dry_run_telegram_notice", True)
            else:
                Log.w("Telegram env vars not set, skipping notifications")
            args.skip_telegram = True
        else:
            try:
                tg = TgNotify(token, chat, telegraph_token)
            except ValueError as exc:
                Log.e(f"Telegram setup failed: {exc}")
                args.skip_telegram = True

    config_updated = False
    title_saved = False

    checker = UpdateChecker(cfg)
    fingerprint = cfg.fingerprint()
    Log.i(f"Device: {cfg.model} ({cfg.device})")
    region_name = region_from_product(cfg.product)
    region_code = region_code_from_product(cfg.product)
    normalized_variant = variant_label.strip().lower() if variant_label else None
    normalized_region = region_name.strip().lower() if region_name else None

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

    found, data = checker.check(args.debug)

    if not found or not data:
        Log.i("No updates found")
        return 0

    title = data.get("title")
    url = data.get("url")
    size = data.get("size")
    desc = data.get("description", "No description")

    if not all([title, url, size]):
        Log.e("Missing essential update info (title, url, or size)")
        return 1

    Log.s(f"New OTA update found: {title}")
    Log.i(f"Size: {size}")
    Log.i(f"URL: {url}")

    processed_path = processed_updates_path()
    processed_titles = load_processed_titles(processed_path)
    is_new_update = title not in processed_titles

    if args.register_update:
        if not is_new_update:
            Log.i("--register-update flag is set, but update title is already known. No action taken.")
            return 0
        Log.i("--register-update set. Skipping config incremental update.")
        if args.dry_run:
            Log.i("--register-update set. Dry-run: would save new update title without notification.")
        else:
            Log.i("--register-update flag is set. Saving new update title without notification.")
            with FILE_LOCK:
                save_processed_title(processed_path, title)
            Log.s("Update check completed successfully (update title registered).")
        return 0

    if not is_new_update:
        if update_incremental_only and not args.force_update_incremental:
            Log.i("Update title already known; skipping incremental update due to --update-incremental.")
            return 0
        if update_incremental_only and args.force_update_incremental:
            Log.i("Update title already known; continuing due to --force with --update-incremental.")
        elif not args.force_notify:
            Log.i("This update has already been processed. Skipping.")
            return 0

    ota_meta = get_ota_metadata(url)
    if not ota_meta or not ota_meta.get("fingerprint"):
        Log.e("Could not determine target fingerprint from OTA metadata. Cannot derive incremental information.")
        return 1

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

    target_incremental = inc or extract_incremental_from_fingerprint(target_fp)
    if update_incremental_only or (is_new_update and not args.register_update):
        if args.incremental:
            Log.i("--incremental override active; skipping config file update.")
        elif title and "Tcard" in title:
            Log.i("Skipping incremental update because update title contains 'Tcard'.")
        elif target_incremental:
            if args.dry_run:
                Log.i(f"Dry-run: would update {config_path} incremental to {target_incremental}.")
            else:
                with FILE_LOCK:
                    if update_config_incremental(config_path, cfg, new_incremental=target_incremental):
                        cfg.incremental = target_incremental
                        config_updated = True
        else:
            Log.w("Unable to determine new incremental value from OTA metadata; config not updated.")

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

    if not args.skip_telegram and tg:
        region_line = f" ({region_name})" if region_name else ""
        os_line = f"<b>OS:</b> {sdk_message}\n" if sdk_message else ""
        msg = (
            f"<blockquote><b>OTA Update Available</b></blockquote>\n\n"
            f"<b>Device:</b> {cfg.model}{region_line}\n"
            f"\n"
            f"<b>Title:</b> {title}\n"
            f"{os_line}\n"
            f"{desc}\n\n"
            f"<b>Size:</b> {size}\n"
            + (f"<b>Incremental:</b> <code>{inc}</code>\n" if inc else "")
            + (f"<b>Security patch:</b> {spl}\n" if spl else "")
            + f"<b>Fingerprint:</b> <code>{target_fp}</code>"
            + (f"\n<b>Build date:</b> {build_date} (CST)" if build_date else "")
            + (f"\n<b>Google OTA link:</b> {html.escape(url, quote=False)}" if url else "")
        )

        if args.dry_run:
            Log.i("Dry-run: would send Telegram notification with OTA details.")
            if is_new_update:
                Log.i("Dry-run: would save new update title after successful notification.")
        else:
            with TELEGRAM_LOCK:
                sent = tg.send(msg, truncate_desc=True, device_title=f"{cfg.model} - {title}")
            if sent:
                if is_new_update:
                    with FILE_LOCK:
                        save_processed_title(processed_path, title)
                        title_saved = True
            else:
                Log.e("Failed to send notification. Update title will not be saved.")
                return 1

    Log.s("Update check completed successfully")
    return 0


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

        slug = None
        if variant_label:
            slug = re.sub(r"[^A-Za-z0-9]+", "-", variant_label).strip("-")
        if not slug and variants_total > 1:
            slug = f"variant{idx}"

        config_name = config_path.stem
        if slug and variants_total > 1:
            config_name = f"{config_name}-{slug}"

        result = process_config_variant(cfg, config_name, config_path, args, variant_label)
        exit_code = max(exit_code, result)

    return exit_code


def main() -> int:
    if sys.version_info < (3, 7):
        Log.e("Requires Python 3.7+")
        return 1

    parser = argparse.ArgumentParser(description="Android OTA Update Checker")
    parser.add_argument("--debug", action="store_true", help="Enable debugging")
    parser.add_argument("-c", "--config", type=Path, help="Config file path")
    parser.add_argument("-d", "--config-dir", type=Path, help="Directory containing config files to process")
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

    if args.config and args.config_dir:
        parser.error("Use either --config or --config-dir, not both.")

    if not args.config and not args.config_dir:
        parser.error("Either --config or --config-dir is required.")

    if args.config and args.config.is_dir():
        parser.error("--config expects a file. Use --config-dir for directories.")

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

    if args.dry_run:
        Log.i("Dry-run mode enabled: no external side effects will occur.")
        token = os.environ.get("bot_token")
        chat = os.environ.get("chat_id")
        telegraph_token = os.environ.get("telegraph_token")
        if not token or not chat or not telegraph_token:
            Log.i("Dry-run mode: Telegram env vars not set; notifications skipped.")
            setattr(args, "_printed_dry_run_telegram_notice", True)

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
        def run_config_buffered(index: int, path: Path) -> tuple[int, int, str]:
            local_args = argparse.Namespace(**vars(args))
            header = f"Processing config {index}/{total}: {path}" if total > 1 else f"Processing config: {path}"
            buffer = io.StringIO()
            with Log.capture(buffer):
                Log.i(header)
                result = process_config(path, local_args)
            return index, result, buffer.getvalue()

        with ThreadPoolExecutor(max_workers=min(args.jobs, total)) as executor:
            start_times = {}
            futures = {}
            for idx, config_path in enumerate(config_paths, start=1):
                start_times[idx] = time.monotonic()
                futures[executor.submit(run_config_buffered, idx, config_path)] = idx
            results: dict[int, tuple[int, str]] = {}
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
