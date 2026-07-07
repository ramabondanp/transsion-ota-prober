"""Core OTA processing pipeline: collect update info, apply config/notification
actions, and orchestrate per-config / per-variant processing.
"""

import argparse
from pathlib import Path
from typing import Dict, List, Optional, Tuple, cast

from modules.description import format_update_description
from modules.logging import Log
from modules.manager import (
    Config,
    parse_fingerprint,
    region_code_from_product,
    region_from_product,
    update_config_from_fingerprint,
)
from modules.metadata import (
    build_sdk_strings,
    extract_incremental_from_fingerprint,
    get_ota_metadata,
)
from modules.models import PendingNotification, VariantUpdate
from modules.notifier import (
    build_notification_message,
    create_notifier,
    is_sweep_mode,
)
from modules.runtime import RunContext
from modules.fingerprints import save_processed_title
from modules.update_checker import UpdateChecker


#: Delay between consecutive Telegram notifications when draining a sweep buffer.
SWEEP_TELEGRAM_DELAY = 10


def config_from_fingerprint(fingerprint: str) -> Config:
    parsed = parse_fingerprint(fingerprint)
    if not parsed:
        raise ValueError(
            "Invalid fingerprint format. Expected: "
            "oem/product/device:android_version/build_tag/incremental:user/release-keys"
        )

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


def log_variant_header(
    cfg: Config, variant_label: Optional[str]
) -> Tuple[Optional[str], Optional[str]]:
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


_CACHE_MISS = object()


def get_cached_ota_metadata(ctx: RunContext, url: str) -> Optional[Dict[str, str]]:
    if ctx.stop_event.is_set():
        return None
    with ctx.cache_lock:
        cached = ctx.metadata_cache.get(url, _CACHE_MISS)
        if cached is not _CACHE_MISS:
            return cast(Optional[Dict[str, str]], cached)

    ota_meta = get_ota_metadata(url, session=ctx.session(), stop_event=ctx.stop_event)
    with ctx.cache_lock:
        ctx.metadata_cache[url] = ota_meta
    return ota_meta


def save_processed_update(ctx: RunContext, title: str) -> None:
    with ctx.file_lock:
        if title not in ctx.processed_titles:
            save_processed_title(ctx.processed_path, title)
            ctx.processed_titles.add(title)


def collect_update_info(
    ctx: RunContext,
    cfg: Config,
    config_path: Path,
    args: argparse.Namespace,
    variant_label: Optional[str] = None,
) -> Tuple[int, Optional[VariantUpdate]]:
    update_incremental_only = bool(getattr(args, "update_incremental", False))

    region_name, _ = log_variant_header(cfg, variant_label)
    checker = UpdateChecker(
        cfg, session=ctx.session(), imei=args.imei, stop_event=ctx.stop_event
    )
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
            Log.e(
                "Missing OTA URL in update response; cannot fetch target fingerprint."
            )
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
            Log.i(
                "--register-update flag is set, but update title is already known. No action taken."
            )
            return 0, None
        Log.i("--register-update set. Skipping config incremental update.")
        if args.dry_run:
            Log.i(
                "--register-update set. Dry-run: would save new update title without notification."
            )
        else:
            Log.i(
                "--register-update flag is set. Saving new update title without notification."
            )
            save_processed_update(ctx, title)
            Log.s("Update check completed successfully (update title registered).")
        return 0, None

    if not is_new_update:
        if update_incremental_only:
            Log.i(
                "Update title already known; proceeding to update incremental value (--update-incremental)."
            )
        elif not args.force_notify:
            Log.i("This update has already been processed. Skipping.")
            return 0, None

    ota_meta = get_cached_ota_metadata(ctx, url)
    if not ota_meta or not ota_meta.get("fingerprint"):
        Log.e(
            "Could not determine target fingerprint from OTA metadata. Cannot derive incremental information."
        )
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


def apply_update_actions(
    ctx: RunContext, update: VariantUpdate, args: argparse.Namespace
) -> int:
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
            Log.i(
                "Skipping config update because update title contains 'Tcard' without an Android version change."
            )
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
                    Log.i(
                        f"Dry-run: would update {update.config_path} incremental to {update.target_incremental}."
                    )
            else:
                with ctx.file_lock:
                    if update_config_from_fingerprint(
                        update.config_path, update.cfg, update.target_fp
                    ):
                        # Even on the no-op path (YAML already matches), we
                        # still mutate the in-memory cfg so subsequent code
                        # paths see the post-OTA values regardless of whether
                        # the file changed on disk. See the two early-return
                        # paths in manager.update_config_from_fingerprint
                        # (variants branch and single-config branch).
                        if parsed_target:
                            update.cfg.android_version = parsed_target[
                                "android_version"
                            ]
                            update.cfg.build_tag = parsed_target["build_tag"]
                            update.cfg.incremental = parsed_target["incremental"]
        else:
            Log.w(
                "Unable to determine new incremental value from OTA metadata; config not updated."
            )

    notifier = create_notifier(ctx, args)
    if notifier:
        msg = build_notification_message(update)
        device_title = f"{update.cfg.model} - {update.title}"

        if is_sweep_mode(args):
            # Sweep mode: buffer the notification; drain at end of run with a
            # SWEEP_TELEGRAM_DELAY-second gap between sends.
            with ctx.pending_lock:
                ctx.pending_notifications.append(
                    PendingNotification(
                        msg=msg,
                        device_title=device_title,
                        title=update.title,
                        is_new_update=update.is_new_update,
                    )
                )
            if args.dry_run:
                Log.i(
                    "Dry-run: would buffer Telegram notification "
                    f"(drained with {SWEEP_TELEGRAM_DELAY}s gap)."
                )
            else:
                Log.i(
                    f"Telegram notification buffered "
                    f"({len(ctx.pending_notifications)} pending)."
                )
        elif args.dry_run:
            Log.i("Dry-run: would send Telegram notification with OTA details.")
            if update.is_new_update:
                Log.i(
                    "Dry-run: would save new update title after successful notification."
                )
        else:
            with ctx.telegram_lock:
                sent = notifier.send(
                    msg,
                    truncate_desc=True,
                    device_title=device_title,
                )
            if not sent:
                Log.e("Failed to send notification. Update title will not be saved.")
                return 1
            if update.is_new_update:
                save_processed_update(ctx, update.title)

    Log.s("Update check completed successfully")
    return 0


def drain_pending_notifications(ctx: RunContext, args: argparse.Namespace) -> int:
    """Drain buffered Telegram notifications with SWEEP_TELEGRAM_DELAY-second gaps.

    Called once at the end of a sweep run after all configs have been processed.
    In dry-run mode, just lists what would have been sent (no actual delay or send).
    """
    with ctx.pending_lock:
        pending = list(ctx.pending_notifications)
        ctx.pending_notifications.clear()

    if not pending:
        return 0

    if args.dry_run:
        Log.i(
            f"Dry-run: would drain {len(pending)} buffered notification(s) "
            f"with {SWEEP_TELEGRAM_DELAY}s gap between sends."
        )
        for idx, note in enumerate(pending, start=1):
            Log.i(f"  [{idx}/{len(pending)}] {note.device_title}")
        return 0

    notifier = create_notifier(ctx, args)
    if not notifier:
        Log.e(
            f"Telegram not available; {len(pending)} buffered notification(s) dropped. "
            "Update titles will not be saved."
        )
        return 1

    total = len(pending)
    Log.i(
        f"Draining {total} buffered Telegram notification(s) with "
        f"{SWEEP_TELEGRAM_DELAY}s gap between sends..."
    )

    for idx, note in enumerate(pending, start=1):
        if ctx.stop_event.is_set():
            Log.w(f"Stop requested; aborting drain at {idx - 1}/{total}.")
            return 130
        if idx > 1:
            Log.i(f"Waiting {SWEEP_TELEGRAM_DELAY}s before next notification...")
            if ctx.stop_event.wait(SWEEP_TELEGRAM_DELAY):
                Log.w(
                    f"Stop requested during wait; aborting drain at {idx - 1}/{total}."
                )
                return 130
        Log.i(f"Sending notification {idx}/{total}: {note.device_title}")
        with ctx.telegram_lock:
            sent = notifier.send(
                note.msg, truncate_desc=True, device_title=note.device_title
            )
        if not sent:
            Log.e(
                f"Failed to send notification {idx}/{total} ({note.device_title}); "
                "title will not be saved."
            )
            continue
        if note.is_new_update:
            save_processed_update(ctx, note.title)

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


def load_config_variants(
    config_path: Path, args: argparse.Namespace
) -> Tuple[int, List[Config]]:
    """Load a config file and return its (region-filtered) variant Config list.

    Returns (status, configs). status is non-zero on a load/filter error, in
    which case configs is empty. Applies the --incremental override in place.
    """
    try:
        configs = Config.from_yaml(config_path)
    except Exception as exc:
        Log.e(f"Config error for {config_path}: {exc}")
        return 1, []

    if args.region:
        region_code = args.region.strip().upper()
        Log.i(f"Filtering configuration variants by region code: {region_code}")
        filtered_configs = [
            cfg
            for cfg in configs
            if region_code_from_product(cfg.product) == region_code
        ]
        if not filtered_configs:
            Log.e(
                f"No configuration variants in {config_path} match region code {region_code}"
            )
            return 1, []
        configs = filtered_configs

    if args.incremental:
        for cfg in configs:
            cfg.incremental = args.incremental

    return 0, configs


def process_config(config_path: Path, args: argparse.Namespace) -> int:
    status, configs = load_config_variants(config_path, args)
    if status != 0:
        return status

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

        result = process_config_variant(
            args.run_context, cfg, config_path, args, variant_label
        )
        exit_code = max(exit_code, result)

    return exit_code
