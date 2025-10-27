#!/usr/bin/python3

import argparse
import os
import re
import sys
from pathlib import Path
from typing import List, Optional

SUBMODULE_DIR = Path(__file__).resolve().parent / "google-ota-prober"
if SUBMODULE_DIR.is_dir():
    subs_path = str(SUBMODULE_DIR)
    if subs_path not in sys.path:
        sys.path.insert(0, subs_path)

from modules.manager import Config, region_from_product, update_config_incremental
from modules.logging import Log
from modules.git import commit_incremental_update
from modules.github import create_github_release
from modules.telegram import TgNotify
from modules.metadata import (
    build_sdk_strings,
    extract_incremental_from_fingerprint,
    get_ota_metadata,
    processed_fp_path,
)
from modules.fingerprints import load_processed_fingerprints, save_processed_fingerprint
from modules.update_checker import UpdateChecker


def process_config_variant(
    cfg: Config,
    config_name: str,
    config_path: Path,
    args: argparse.Namespace,
    variant_label: Optional[str] = None,
) -> int:
    tg = None
    if not args.skip_telegram and not args.register_fingerprint:
        token = os.environ.get("bot_token")
        chat = os.environ.get("chat_id")
        telegraph_token = os.environ.get("telegraph_token")

        if not token or not chat or not telegraph_token:
            Log.w("Telegram env vars not set, skipping notifications")
            args.skip_telegram = True
        else:
            try:
                tg = TgNotify(token, chat, telegraph_token)
            except ValueError as exc:
                Log.e(f"Telegram setup failed: {exc}")
                args.skip_telegram = True

    config_updated = False
    fingerprint_saved = False
    commit_incremental_value: Optional[str] = None

    checker = UpdateChecker(cfg)
    fingerprint = cfg.fingerprint()
    Log.i(f"Device: {cfg.model} ({cfg.device})")
    region_name = region_from_product(cfg.product)
    if variant_label:
        Log.i(f"Variant: {variant_label}")
    if region_name:
        Log.i(f"Region: {region_name}")
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

    ota_meta = get_ota_metadata(url)
    if not ota_meta or not ota_meta.get("fingerprint"):
        Log.e("Could not determine target fingerprint. Cannot verify if update is new.")
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

    processed_path = processed_fp_path()
    processed_fingerprints = load_processed_fingerprints(processed_path)
    is_new_update = target_fp not in processed_fingerprints
    target_incremental = inc or extract_incremental_from_fingerprint(target_fp)
    commit_incremental_value = target_incremental

    if is_new_update and not args.register_fingerprint:
        if target_incremental:
            if args.dry_run:
                Log.i(f"Dry-run: would update {config_path} incremental to {target_incremental}.")
            else:
                if update_config_incremental(config_path, cfg, new_incremental=target_incremental):
                    cfg.incremental = target_incremental
                    config_updated = True
        else:
            Log.w("Unable to determine new incremental value from OTA metadata; config not updated.")
    elif is_new_update and args.register_fingerprint:
        Log.i("--register-fingerprint set. Skipping config incremental update.")

    if not is_new_update and not args.force_notify:
        Log.i("This update has already been processed. Skipping.")
        return 0

    if args.register_fingerprint:
        if is_new_update:
            if args.dry_run:
                Log.i("--register-fingerprint set. Dry-run: would save new fingerprint without notification.")
            else:
                Log.i("--register-fingerprint flag is set. Saving new fingerprint without notification.")
                save_processed_fingerprint(processed_path, target_fp)
                fingerprint_saved = True
                Log.s("Update check completed successfully (fingerprint registered).")
        else:
            Log.i("--register-fingerprint flag is set, but fingerprint is already known. No action taken.")
        return 0

    if not is_new_update and args.force_notify:
        Log.w(f"Forcing notification for an already processed update: {target_fp}")

    if not is_new_update and args.force_release:
        Log.w(f"Forcing GitHub release for an already processed update: {target_fp}")

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
        sdk_suffix = f" ({sdk_message})" if sdk_message else ""
        msg = (
            f"<blockquote><b>OTA Update Available</b></blockquote>\n\n"
            f"<b>Device:</b> {cfg.model}{region_line}\n\n"
            f"<b>Title:</b> {title}{sdk_suffix}\n\n"
            f"{desc}\n\n"
            f"<b>Size:</b> {size}\n"
            + (f"<b>Incremental:</b> <code>{inc}</code>\n" if inc else "")
            + (f"<b>Security patch:</b> {spl}\n" if spl else "")
            + f"<b>Fingerprint:</b> <code>{target_fp}</code>"
            + (f"\n<b>Build date:</b> {build_date} (CST)" if build_date else "")
        )

        if args.dry_run:
            Log.i("Dry-run: would send Telegram notification with OTA details.")
            if is_new_update:
                Log.i("Dry-run: would save new fingerprint after successful notification.")
                Log.i("Dry-run: would create GitHub release for new update.")
        else:
            if tg.send(msg, "Google OTA Link", url, truncate_desc=True, device_title=f"{cfg.model} - {title}"):
                if is_new_update:
                    save_processed_fingerprint(processed_path, target_fp)
                    fingerprint_saved = True
                    Log.i("Creating GitHub release for new update...")
                    create_github_release(config_name, data)
            else:
                Log.e("Failed to send notification. Fingerprint will not be saved.")
                return 1

    if args.force_release:
        if args.dry_run:
            Log.i("Dry-run: would create GitHub release due to --force-release.")
        else:
            Log.i("Force release flag detected. Creating GitHub release...")
            if create_github_release(config_name, data):
                if is_new_update and not (not args.skip_telegram and tg):
                    Log.i("Skipping fingerprint save due to force release")

    if is_new_update and not args.dry_run and config_updated and commit_incremental_value:
        extra_paths: List[Path] = []
        if fingerprint_saved:
            extra_paths.append(processed_path)
        commit_incremental_update(
            config_path,
            commit_incremental_value,
            variant_label,
            extra_paths,
        )

    Log.s("Update check completed successfully")
    return 0


def process_config(config_path: Path, args: argparse.Namespace) -> int:
    try:
        configs = Config.from_yaml(config_path)
    except Exception as exc:
        Log.e(f"Config error for {config_path}: {exc}")
        return 1

    if args.incremental and len(configs) != 1:
        Log.e("--incremental can only be used when a single configuration variant is defined")
        return 1

    exit_code = 0
    variants_total = len(configs)

    for idx, cfg in enumerate(configs, start=1):
        variant_label = cfg.variant
        display_label = variant_label or f"variant {idx}"

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
    parser.add_argument("--register-fingerprint", action="store_true", help="Save the update fingerprint without sending a notification")
    parser.add_argument("--force-notify", action="store_true", help="Send notification even if the update has been seen before")
    parser.add_argument("--force-release", action="store_true", help="Create GitHub release even without Telegram token or if fingerprint already exists")
    parser.add_argument("-i", "--incremental", help="Override incremental version")
    args = parser.parse_args()

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

    exit_code = 0
    total = len(config_paths)
    for idx, config_path in enumerate(config_paths, start=1):
        if total > 1 and idx > 1:
            print()
        if total > 1:
            Log.i(f"Processing config {idx}/{total}: {config_path}")
        else:
            Log.i(f"Processing config: {config_path}")
        result = process_config(config_path, args)
        exit_code = max(exit_code, result)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
