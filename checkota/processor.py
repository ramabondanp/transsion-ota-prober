"""Core OTA processing pipeline: collect update info, apply config/notification
actions, and orchestrate per-config / per-region processing.
"""

import argparse
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple, cast

import yaml

from checkota.description import format_update_description
from checkota.fingerprints import (
    claim_processed_title,
    commit_processed_title,
    release_processed_claim,
    save_processed_title,
)
from checkota.logging import Log, sanitize_log_text
from checkota.manager import (
    Config,
    fingerprint_identity_matches_config,
    parse_fingerprint,
    region_code_from_product,
    region_from_product,
    update_config_from_fingerprint,
)
from checkota.metadata import (
    build_sdk_strings,
    extract_incremental_from_fingerprint,
    get_ota_metadata,
)
from checkota.models import PendingNotification, RegionUpdate
from checkota.notifier import (
    build_notification_message,
    create_notifier,
    is_sweep_mode,
)
from checkota.outbox import (
    has_pending_notification,
    load_pending_notifications,
    remove_pending_notification,
    stage_pending_notification,
)
from checkota.runtime import RunContext
from checkota.update_checker import UpdateChecker, UpdateCheckError

#: Delay between consecutive Telegram notifications when draining a sweep buffer.
SWEEP_TELEGRAM_DELAY = 10

#: How long a failed OTA metadata fetch is remembered before another worker
#: tries the same URL again. Prevents one persistent outage from being retried
#: serially by every region/config that shares the URL.
METADATA_FAILURE_TTL = 300.0


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
    )


def log_region_header(cfg: Config) -> tuple[str | None, str | None]:
    fingerprint = cfg.fingerprint()
    region_name = region_from_product(cfg.product)
    region_code = region_code_from_product(cfg.product)

    Log.i(f"Device: {cfg.model} ({cfg.device})")
    if region_name or region_code:
        region_display = region_name or region_code
        if region_name and region_code:
            region_display = f"{region_display} ({region_code})"
        Log.i(f"Region: {region_display}")
    Log.i(f"Build: {fingerprint}")
    return region_name, region_code


_CACHE_MISS = object()

#: Upper bound on how long a waiter blocks on the owner's per-fetch Event
#: before re-checking cache/failures/stop_event. The Event itself wakes waiters
#: immediately when the owner finishes, so this cap is NOT a completion latency:
#: it only bounds cancellation lag (a waiter that misses the wake still notices
#: stop_event within 1s) and guards against a missed set. A Condition-based
#: broadcast was evaluated and rejected: any correct Condition design also needs
#: a periodic timeout for stop responsiveness, which converges back to this.
_METADATA_WAIT_POLL_INTERVAL = 1.0


def get_cached_ota_metadata(
    ctx: RunContext,
    url: str,
    use_proxy_env: bool | None = None,
) -> dict[str, str] | None:
    while True:
        if ctx.stop_event.is_set():
            return None
        fetcher_event: threading.Event | None = None
        with ctx.cache_lock:
            cached = ctx.metadata_cache.get(url, _CACHE_MISS)
            if cached is not _CACHE_MISS:
                return cast(dict[str, str] | None, cached)
            failure_ts = ctx.metadata_failures.get(url)
            if failure_ts is not None:
                if time.monotonic() - failure_ts < METADATA_FAILURE_TTL:
                    Log.i(f"Skipping recently failed metadata fetch: {url}")
                    return None
                ctx.metadata_failures.pop(url, None)
            # Another worker is already fetching this URL; capture its event, then
            # release the lock before waiting so the fetcher can re-acquire it.
            inflight = ctx._metadata_inflight.get(url)
            if inflight is not None:
                wait_event = inflight
            else:
                # We are the fetcher: register an in-flight event under the lock so
                # no other worker decides to fetch the same URL concurrently.
                wait_event = None
                fetcher_event = threading.Event()
                ctx._metadata_inflight[url] = fetcher_event

        if wait_event is not None:
            # The owner may need several range requests and retries. Polling
            # keeps cancellation responsive without abandoning a valid fetch.
            wait_event.wait(_METADATA_WAIT_POLL_INTERVAL)
            continue

        if fetcher_event is None:
            # Unreachable: wait_event is None only on the fetcher branch.
            raise RuntimeError("metadata fetcher event was not registered")
        use_proxy = ctx.zip_proxy if use_proxy_env is None else use_proxy_env
        # Honor the explicit override instead of unconditionally using
        # ctx.zip_session(), which only knows the context-wide flag.
        zip_session = ctx.session() if use_proxy else ctx.direct_session()
        ota_meta: dict[str, str] | None = None
        valid_metadata = False
        try:
            fetch_kwargs = {"session": zip_session, "stop_event": ctx.stop_event}
            if use_proxy:
                fetch_kwargs["use_proxy_env"] = True
            ota_meta = get_ota_metadata(url, **fetch_kwargs)
        finally:
            interrupted = ctx.stop_event.is_set()
            with ctx.cache_lock:
                valid_metadata = bool(
                    not interrupted
                    and ota_meta
                    and ota_meta.get("fingerprint")
                    and parse_fingerprint(ota_meta["fingerprint"]) is not None
                )
                if valid_metadata:
                    ctx.metadata_cache[url] = ota_meta
                    ctx.metadata_failures.pop(url, None)
                elif not interrupted:
                    ctx.metadata_failures[url] = time.monotonic()
                ctx._metadata_inflight.pop(url, None)
                fetcher_event.set()
        if ctx.stop_event.is_set():
            return None
        return ota_meta if valid_metadata else None


def save_processed_update(ctx: RunContext, title: str) -> bool:
    with ctx.file_lock:
        if title in ctx.processed_titles:
            return True
        saved = save_processed_title(ctx.processed_path, title)
        if saved:
            ctx.processed_titles.add(title)
        return saved


def _claim_new_update(ctx: RunContext, title: str) -> bool | None:
    """Claim a new update title across threads and processes."""
    with ctx.file_lock:
        if title in ctx.processed_titles or title in ctx.claimed_titles:
            return False
        try:
            claim = claim_processed_title(ctx.processed_path, title)
        except (OSError, UnicodeError, ValueError) as exc:
            Log.e(f"Failed to claim update title {title}: {exc}")
            return None
        if claim is None:
            ctx.processed_titles.add(title)
            return False
        ctx.claimed_titles.add(title)
        ctx.claimed_handles[title] = claim
        return True


def _release_claimed_update(ctx: RunContext, title: str) -> None:
    with ctx.file_lock:
        ctx.claimed_titles.discard(title)
        claim = ctx.claimed_handles.pop(title, None)
    if claim is not None:
        release_processed_claim(claim)


def _commit_claimed_update(ctx: RunContext, title: str) -> bool:
    with ctx.file_lock:
        claim = ctx.claimed_handles.get(title)
    if claim is None:
        return False
    committed = commit_processed_title(ctx.processed_path, title, claim)
    with ctx.file_lock:
        ctx.claimed_handles.pop(title, None)
        if committed:
            ctx.processed_titles.add(title)
        ctx.claimed_titles.discard(title)
    release_processed_claim(claim)
    return committed


@dataclass
class _TargetMetadata:
    """Validated target-build facts derived from the OTA ZIP metadata."""

    fingerprint: str
    sdk_message: str | None
    post_build_incremental: str | None
    post_security_patch_level: str | None
    build_date: str | None
    post_sdk_level: str | None
    android_version: str | None

    @property
    def response_extras(self) -> dict[str, str]:
        """Entries merged into the update info for downstream consumers."""
        extras = {"fingerprint": self.fingerprint}
        for key, value in (
            ("post_build_incremental", self.post_build_incremental),
            ("post_security_patch_level", self.post_security_patch_level),
            ("build_date", self.build_date),
            ("post_sdk_level", self.post_sdk_level),
            ("android_version", self.android_version),
        ):
            if value:
                extras[key] = value
        return extras


def _log_target_metadata(target: _TargetMetadata) -> None:
    Log.i(f"Target build: {target.fingerprint}")
    if target.post_build_incremental:
        Log.i(f"Incremental: {target.post_build_incremental}")
    if target.post_security_patch_level:
        Log.i(f"Security patch: {target.post_security_patch_level}")
    if target.build_date:
        Log.i(f"Build date: {target.build_date} (CST)")


def _resolve_target_metadata(
    ctx: RunContext, cfg: Config, url: str
) -> tuple[int, _TargetMetadata | None]:
    """Fetch OTA metadata for the update URL and validate it against the config.

    Identity mismatches fail closed: without a matching target the caller must
    not update configs, notify, or process titles.
    """
    if ctx.stop_event.is_set():
        Log.w("Stop requested before OTA metadata resolution.")
        return 130, None
    ota_meta = get_cached_ota_metadata(ctx, url)
    if ctx.stop_event.is_set():
        Log.w("Stop requested while resolving OTA metadata.")
        return 130, None
    if not ota_meta or not ota_meta.get("fingerprint"):
        Log.e(
            "Could not determine target fingerprint from OTA metadata. Cannot derive incremental information."
        )
        return 1, None

    target_fp = ota_meta["fingerprint"]
    if not fingerprint_identity_matches_config(cfg, target_fp):
        Log.e(
            "Target fingerprint identity does not match the current config. "
            "Skipping config update, notification, and title processing."
        )
        return 1, None

    inc = ota_meta.get("post_build_incremental")
    spl = ota_meta.get("post_security_patch_level")
    build_date = ota_meta.get("build_date")
    sdk_level = ota_meta.get("post_sdk_level")
    android_ver = ota_meta.get("android_version")
    sdk_message, _, _ = build_sdk_strings(sdk_level, android_ver)
    target = _TargetMetadata(
        fingerprint=target_fp,
        sdk_message=sdk_message,
        post_build_incremental=inc,
        post_security_patch_level=spl,
        build_date=build_date,
        post_sdk_level=sdk_level,
        android_version=android_ver,
    )
    _log_target_metadata(target)
    return 0, target


def _debug_label(config_path: Path, cfg: Config) -> str:
    """Build the per-config/region label used for --debug artifact names."""
    label = config_path.stem
    if cfg.region:
        label = f"{label}-{cfg.region}"
    return label


def _check_for_updates(
    ctx: RunContext, cfg: Config, args: argparse.Namespace, debug_label: str
) -> tuple[int, dict | None]:
    """Run one check-in round-trip.

    Returns (status, info). (0, None) means a successful check-in with no
    update; non-zero status means the check failed or was interrupted.
    """
    checker = UpdateChecker(
        cfg,
        session=ctx.session(),
        imei=args.imei,
        stop_event=ctx.stop_event,
        debug_label=debug_label,
    )
    try:
        found, data = checker.check(args.debug)
    except UpdateCheckError as exc:
        Log.e(str(exc))
        return 1, None

    if ctx.stop_event.is_set():
        Log.w("Update check interrupted.")
        return 130, None

    if not found or not data:
        Log.i("No updates found")
        return 0, None
    return 0, data


def _generate_fingerprint(ctx: RunContext, cfg: Config, url: str | None) -> int:
    """--gen-fp mode: print the OTA's target fingerprint instead of acting."""
    if not url:
        Log.e("Missing OTA URL in update response; cannot fetch target fingerprint.")
        return 1
    ota_meta = get_cached_ota_metadata(ctx, url)
    if not ota_meta or not ota_meta.get("fingerprint"):
        Log.e("Could not determine target fingerprint from OTA metadata.")
        return 1
    if not fingerprint_identity_matches_config(cfg, ota_meta["fingerprint"]):
        Log.e(
            "Target fingerprint identity does not match the current config; "
            "not printing it."
        )
        return 1
    Log.raw(ota_meta["fingerprint"])
    return 0


def collect_update_info(
    ctx: RunContext,
    cfg: Config,
    config_path: Path,
    args: argparse.Namespace,
) -> tuple[int, RegionUpdate | None]:
    update_incremental_only = bool(getattr(args, "update_incremental", False))

    region_name, _ = log_region_header(cfg)
    debug_label = _debug_label(config_path, cfg)

    status, data = _check_for_updates(ctx, cfg, args, debug_label)
    if status != 0 or data is None:
        return status, None

    if getattr(args, "gen_fp", False):
        return _generate_fingerprint(ctx, cfg, data.get("url")), None

    title = data.get("title")
    url = data.get("url")
    size = data.get("size")
    # The key always exists (initialised to None), so a plain .get() default
    # would let a response without an update_description render as "None".
    desc = data.get("description") or "No description"

    if args.dry_run and not title and url and size:
        data["title"] = title = "UNKNOWN_TITLE_DRY_RUN"
        Log.w("Missing update title; continuing because --dry-run is enabled.")
    elif not all([title, url, size]):
        Log.e("Missing essential update info (title, url, or size)")
        return 1, None
    # The checks above guarantee presence; narrow the Optional lookups so the
    # rest of the pipeline can rely on plain strings.
    title = cast(str, title)
    url = cast(str, url)
    size = cast(str, size)

    Log.s(f"New OTA update found: {title}")
    Log.i(f"Size: {size}")
    Log.i(f"URL: {url}")
    if args.dry_run and args.fp and desc:
        Log.i("Description:")
        formatted_desc = format_update_description(desc)
        # The fallback prints raw text, so neutralize control bytes here: the
        # terminal parser only sanitizes what it successfully renders.
        Log.raw(formatted_desc if formatted_desc else sanitize_log_text(desc))

    with ctx.file_lock:
        is_new_update = title not in ctx.processed_titles
    if args.register_update:
        if not is_new_update:
            Log.i(
                "--register-update flag is set, but update title is already known. No action taken."
            )
            return 0, None
        # Registration is still title processing. Validate the OTA identity
        # before persisting it, just as the normal update path does.
        status, target = _resolve_target_metadata(ctx, cfg, url)
        if status != 0 or target is None:
            return status, None
        Log.i("--register-update set. Skipping config incremental update.")
        if args.dry_run:
            Log.i(
                "--register-update set. Dry-run: would save new update title without notification."
            )
        else:
            Log.i(
                "--register-update flag is set. Saving new update title without notification."
            )
            if not save_processed_update(ctx, title):
                Log.e("Failed to register update title; the update was not recorded.")
                return 1, None
            Log.s("Update check completed successfully (update title registered).")
        return 0, None

    if not is_new_update:
        if update_incremental_only:
            Log.i(
                "Update title already known; proceeding to update incremental value (--update-incremental)."
            )
        elif not getattr(args, "force_notify", False):
            Log.i("This update has already been processed. Skipping.")
            return 0, None

    status, target = _resolve_target_metadata(ctx, cfg, url)
    if status != 0 or target is None:
        return status, None

    if not is_new_update and getattr(args, "force_notify", False):
        Log.w(f"Forcing notification for an already processed update: {title}")

    data.update(target.response_extras)

    return 0, RegionUpdate(
        cfg=cfg,
        config_path=config_path,
        region_name=region_name,
        title=title,
        url=url,
        size=size,
        desc=desc,
        is_new_update=is_new_update,
        target_fp=target.fingerprint,
        target_incremental=target.post_build_incremental
        or extract_incremental_from_fingerprint(target.fingerprint),
        sdk_message=target.sdk_message,
        data=data,
    )


def _log_dry_run_config_update(
    update: RegionUpdate, parsed_target: dict[str, str] | None
) -> None:
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


def _tcard_allows_config_update(update: RegionUpdate) -> bool:
    """Tcard packages may rewrite config only for a proven Android upgrade."""
    if "Tcard" not in update.title:
        return True
    parsed = parse_fingerprint(update.target_fp)
    if parsed is None:
        return False
    try:
        return int(parsed["android_version"]) > int(update.cfg.android_version)
    except ValueError:
        # Preview/non-numeric versions cannot establish an upgrade safely.
        return False


def _apply_config_update(ctx: RunContext, update: RegionUpdate, args) -> bool:
    """Rewrite the device config for the target build. Returns success.

    Every "skip" reason logs and returns True: skipping the rewrite must not
    abort the notification pipeline.
    """
    if ctx.stop_event.is_set():
        Log.w("Stop requested; config file was not updated.")
        return False
    parsed_target = parse_fingerprint(update.target_fp)
    if getattr(args, "incremental", None):
        Log.i("--incremental override active; skipping config file update.")
        return True
    if getattr(args, "no_config", False):
        Log.i("No config file mode; skipping incremental config update.")
        return True
    if not _tcard_allows_config_update(update):
        Log.i(
            "Skipping config update because update title contains 'Tcard' "
            "without a newer Android version."
        )
        return True
    if not update.target_incremental:
        Log.w(
            "Unable to determine new incremental value from OTA metadata; config not updated."
        )
        return True
    if args.dry_run:
        _log_dry_run_config_update(update, parsed_target)
        return True

    with ctx.file_lock:
        config_updated = update_config_from_fingerprint(
            update.config_path, update.cfg, update.target_fp
        )
    if not config_updated:
        return False
    # Even on the no-op path (YAML already matches), we still mutate the
    # in-memory cfg so subsequent code paths see the post-OTA values regardless
    # of whether the file changed on disk. See the two early-return paths in
    # manager.update_config_from_fingerprint.
    if parsed_target:
        update.cfg.android_version = parsed_target["android_version"]
        update.cfg.build_tag = parsed_target["build_tag"]
        update.cfg.incremental = parsed_target["incremental"]
    return True


def _dispatch_or_buffer_notification(
    ctx: RunContext,
    notifier,
    update: RegionUpdate,
    args: argparse.Namespace,
    claimed: bool,
    *,
    allow_after_stop: bool = False,
) -> int:
    """Buffer (sweep mode) or send (direct mode) the notification.

    ``allow_after_stop`` is set once the config step has been passed. Both
    single-config (-c) and sweep (-d) runs may already have advanced the YAML,
    making the update undiscoverable on retry. After a stop, those jobs may
    only buffer locally; main's shutdown drain owns delivery. No-config --fp
    runs remain retryable and still abort. Healthy -c runs keep sending inline.
    """
    stopped = ctx.stop_event.is_set()
    buffer_after_stop = (
        stopped and allow_after_stop and not getattr(args, "no_config", False)
    )
    if stopped and not buffer_after_stop:
        Log.w("Stop requested; notification was not dispatched.")
        return 130
    msg = build_notification_message(update)
    device_title = f"{update.cfg.model} - {update.title}"

    if is_sweep_mode(args) or buffer_after_stop:
        # Sweeps and interrupted file-backed updates drain after workers stop.
        # Never start a network send here when shutdown has been requested.
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
        return 0

    if args.dry_run:
        Log.i("Dry-run: would send Telegram notification with OTA details.")
        if update.is_new_update:
            Log.i("Dry-run: would save new update title after successful notification.")
        return 0

    with ctx.telegram_lock:
        sent = notifier.send(
            msg,
            truncate_desc=True,
            device_title=device_title,
        )
    if not sent:
        if claimed:
            _release_claimed_update(ctx, update.title)
        Log.e(
            "Failed to send notification. Update title will not be saved; "
            "outbox retained for retry."
        )
        return 1
    if update.is_new_update:
        if claimed:
            if not _commit_claimed_update(ctx, update.title):
                Log.e("Notification sent, but update title could not be saved.")
                return 1
        elif not save_processed_update(ctx, update.title):
            return 1
        if not getattr(args, "no_config", False) and not remove_pending_notification(
            ctx.processed_path, update.title
        ):
            return 1
    return 0


def apply_update_actions(
    ctx: RunContext, update: RegionUpdate, args: argparse.Namespace
) -> int:
    if ctx.stop_event.is_set():
        Log.w("Stop requested before applying update actions.")
        return 130
    if not fingerprint_identity_matches_config(update.cfg, update.target_fp):
        Log.e(
            "Target fingerprint identity does not match the current config. "
            "Skipping config update and notification."
        )
        return 1

    notifier = create_notifier(ctx, args)

    # Reserve a new title BEFORE mutating the config: if claiming fails we
    # leave no partial state behind, whereas a config rewrite followed by a
    # failed claim would persist YAML changes nothing recorded or notified.
    # `--force-notify` bypasses this because the user explicitly asked for
    # notifications even for already-processed titles.
    claimed = False
    with ctx.pending_lock:
        duplicate_notification = any(
            note.title == update.title and note.is_new_update
            for note in ctx.pending_notifications
        )
    if duplicate_notification:
        Log.i("Notification already queued for this title; updating this region.")
    if (
        not duplicate_notification
        and notifier
        and update.is_new_update
        and not args.dry_run
        and not getattr(args, "force_notify", False)
    ):
        claim_result = _claim_new_update(ctx, update.title)
        if claim_result is None:
            Log.e("Could not reserve update title; notification was not sent.")
            return 1
        if not claim_result:
            Log.i(
                "Update already claimed or processed by another worker; "
                "skipping duplicate notification, but updating this region."
            )
            duplicate_notification = True
        else:
            claimed = True

    update_incremental_only = bool(getattr(args, "update_incremental", False))
    needs_config_update = update_incremental_only or update.is_new_update
    file_backed = not (
        args.dry_run
        or getattr(args, "no_config", False)
        or getattr(args, "incremental", None)
        or not _tcard_allows_config_update(update)
        or not update.target_incremental
    )
    staged = False
    note: PendingNotification | None = None
    if notifier and update.is_new_update and not duplicate_notification and file_backed:
        try:
            already_staged = has_pending_notification(ctx.processed_path, update.title)
        except (OSError, ValueError, UnicodeError) as exc:
            Log.e(f"Could not inspect notification outbox: {exc}")
            if claimed:
                _release_claimed_update(ctx, update.title)
            return 1
        if already_staged:
            duplicate_notification = True
            Log.i("Notification already in outbox; updating this region.")
            if claimed:
                _release_claimed_update(ctx, update.title)
                claimed = False

    if notifier and update.is_new_update and not duplicate_notification and file_backed:
        note = PendingNotification(
            msg=build_notification_message(update),
            device_title=f"{update.cfg.model} - {update.title}",
            title=update.title,
            is_new_update=True,
        )
        if not stage_pending_notification(
            ctx.processed_path, note, update.config_path, update.target_fp
        ):
            if claimed:
                _release_claimed_update(ctx, update.title)
            return 1
        staged = True

    if needs_config_update and not _apply_config_update(ctx, update, args):
        if staged:
            # If a rewrite failed after publication, the outbox remains
            # recoverable; replay checks that the target fingerprint is on disk.
            Log.w("Config rewrite failed; staged notification will not be sent.")
        if claimed:
            _release_claimed_update(ctx, update.title)
        if ctx.stop_event.is_set():
            Log.w("Stop requested; config update was not completed.")
            return 130
        Log.e(
            f"Failed to update config {update.config_path}; "
            "not sending notification or saving title."
        )
        return 1

    if (
        staged
        and note is not None
        and not stage_pending_notification(
            ctx.processed_path, note, update.config_path, update.target_fp, ready=True
        )
    ):
        # The first (not-ready) record can still be replayed if the YAML
        # rewrite succeeded; fail this run rather than losing the claim.
        if claimed:
            _release_claimed_update(ctx, update.title)
        return 1

    if (
        notifier
        and not duplicate_notification
        and (update.is_new_update or getattr(args, "force_notify", False))
    ):
        dispatch_result = _dispatch_or_buffer_notification(
            ctx, notifier, update, args, claimed, allow_after_stop=True
        )
        if dispatch_result != 0:
            if claimed and dispatch_result == 130:
                _release_claimed_update(ctx, update.title)
            return dispatch_result
    elif not notifier and update.is_new_update and not getattr(args, "dry_run", False):
        # The config was just advanced but nothing will announce this update
        # (--skip-telegram, or Telegram setup unavailable). Record the title so
        # the update is not left neither announced nor recorded: a later run
        # cannot re-discover it from a config that already matches the target.
        if not save_processed_update(ctx, update.title):
            Log.e("Failed to record the update title; no notification was sent.")
            return 1
        Log.i("Notifications disabled; update title recorded without notifying.")

    Log.s("Update check completed successfully")
    return 0


def _remove_pending_notification(ctx: RunContext, note: PendingNotification) -> None:
    with ctx.pending_lock:
        try:
            ctx.pending_notifications.remove(note)
        except ValueError:
            pass


def _release_pending_claim(ctx: RunContext, note: PendingNotification) -> None:
    if note.is_new_update:
        _release_claimed_update(ctx, note.title)


def load_outbox_into_context(ctx: RunContext) -> bool:
    """Queue persisted notifications on startup before any new check-in work."""
    try:
        notes = load_pending_notifications(ctx.processed_path)
    except (OSError, ValueError, UnicodeError) as exc:
        Log.e(f"Could not load notification outbox: {exc}")
        return False
    with ctx.pending_lock:
        titles = {note.title for note in ctx.pending_notifications}
        for note in notes:
            if note.title not in titles:
                ctx.pending_notifications.append(note)
                titles.add(note.title)
    return True


def drain_pending_notifications(
    ctx: RunContext,
    args: argparse.Namespace,
    *,
    max_sends: int | None = None,
    delay: float | None = None,
    ignore_stop_event: bool = False,
    deadline: float | None = None,
) -> int:
    """Drain buffered Telegram notifications, retaining unsent work for retry.

    max_sends bounds how many notifications this call will attempt; anything
    beyond it stays buffered. ``delay`` overrides the normal sweep gap (the
    watchdog's emergency drain uses zero). ``deadline`` is a ``time.monotonic``
    value after which no further sends are started. ``ignore_stop_event`` lets
    the hard-exit drain proceed without clearing the shared stop_event while
    workers may still be running.
    """
    with ctx.pending_lock:
        pending = list(ctx.pending_notifications)
    if max_sends is not None:
        pending = pending[:max_sends]

    if not pending:
        return 0

    inter_send_delay = SWEEP_TELEGRAM_DELAY if delay is None else max(0.0, delay)

    if args.dry_run:
        Log.i(
            f"Dry-run: would drain {len(pending)} buffered notification(s) "
            f"with {inter_send_delay}s gap between sends."
        )
        for idx, note in enumerate(pending, start=1):
            Log.i(f"  [{idx}/{len(pending)}] {note.device_title}")
        return 0

    notifier = create_notifier(ctx, args)
    if not notifier:
        Log.e(
            f"Telegram unavailable; retaining {len(pending)} buffered notification(s) "
            "for retry."
        )
        for note in pending:
            _release_pending_claim(ctx, note)
        return 1

    total = len(pending)
    Log.i(
        f"Draining {total} buffered Telegram notification(s) with "
        f"{inter_send_delay}s gap between sends..."
    )

    failed = False
    for idx, note in enumerate(pending, start=1):
        if deadline is not None and time.monotonic() >= deadline:
            Log.w(
                f"Notification drain deadline reached; retaining notifications "
                f"from {idx - 1}/{total}."
            )
            for remaining in pending[idx - 1 :]:
                _release_pending_claim(ctx, remaining)
            return 1
        if not ignore_stop_event and ctx.stop_event.is_set():
            Log.w(f"Stop requested; retaining notifications from {idx - 1}/{total}.")
            for remaining in pending[idx - 1 :]:
                _release_pending_claim(ctx, remaining)
            return 130
        if idx > 1:
            if inter_send_delay:
                Log.i(f"Waiting {inter_send_delay}s before next notification...")
            stop_during_wait = bool(
                inter_send_delay and ctx.stop_event.wait(inter_send_delay)
            )
            if stop_during_wait and not ignore_stop_event:
                Log.w(
                    f"Stop requested during wait; retaining notifications from "
                    f"{idx - 1}/{total}."
                )
                for remaining in pending[idx - 1 :]:
                    _release_pending_claim(ctx, remaining)
                return 130
            if deadline is not None and time.monotonic() >= deadline:
                Log.w(
                    f"Notification drain deadline reached; retaining notifications "
                    f"from {idx - 1}/{total}."
                )
                for remaining in pending[idx - 1 :]:
                    _release_pending_claim(ctx, remaining)
                return 1
        if note.is_new_update:
            with ctx.file_lock:
                already_processed = note.title in ctx.processed_titles
                has_claim = note.title in ctx.claimed_handles
            if already_processed:
                Log.i(
                    f"Skipping already-processed buffered notification: "
                    f"{note.device_title}"
                )
                if not remove_pending_notification(ctx.processed_path, note.title):
                    failed = True
                    continue
                _remove_pending_notification(ctx, note)
                _release_pending_claim(ctx, note)
                continue
            if not has_claim:
                claim_result = _claim_new_update(ctx, note.title)
                if claim_result is None:
                    failed = True
                    Log.e(
                        f"Could not reserve buffered update title: {note.device_title}"
                    )
                    continue
                if not claim_result:
                    Log.i(
                        f"Update claim unavailable; retaining buffered notification: "
                        f"{note.device_title}"
                    )
                    continue
        Log.i(f"Sending notification {idx}/{total}: {note.device_title}")
        with ctx.telegram_lock:
            sent = notifier.send(
                note.msg, truncate_desc=True, device_title=note.device_title
            )
        if not sent:
            failed = True
            _release_pending_claim(ctx, note)
            Log.e(
                f"Failed to send notification {idx}/{total} ({note.device_title}); "
                "retaining it for retry."
            )
            continue
        if note.is_new_update:
            committed = _commit_claimed_update(ctx, note.title)
            if not committed:
                failed = True
                Log.e(
                    f"Notification sent, but update title could not be saved: "
                    f"{note.title}"
                )
                _release_pending_claim(ctx, note)
                continue
        _remove_pending_notification(ctx, note)
        if note.is_new_update and not remove_pending_notification(
            ctx.processed_path, note.title
        ):
            failed = True

    return 1 if failed else 0


def process_region(
    ctx: RunContext,
    cfg: Config,
    config_path: Path,
    args: argparse.Namespace,
) -> int:
    status, update = collect_update_info(ctx, cfg, config_path, args)
    if status != 0 or update is None:
        return status
    return apply_update_actions(ctx, update, args)


def _matching_regions(configs: list[Config], region_code: str) -> list[Config]:
    """Region-filter predicate shared by the run and the sweep pre-check."""
    return [
        cfg for cfg in configs if region_code_from_product(cfg.product) == region_code
    ]


class RegionFilterScan(NamedTuple):
    """Result of a quiet --reg pre-scan of a directory sweep."""

    any_match: bool
    any_load_error: bool


def scan_region_filter(config_paths: list[Path], region_code: str) -> RegionFilterScan:
    """Quietly pre-scan a --reg sweep for selection and load health.

    A sweep skips configs that lack the region -- most files in a directory do
    not carry every region -- so the run itself must not fail per file.
    cli.main() uses this to decide how to treat an empty selection: fail
    before any work when every config loaded cleanly (nothing to do, nothing
    to report), but still run the sweep when some configs failed to load, so
    the sweep is what reports those errors instead of them being swallowed by
    the pre-check. Both flags are always computed over every path.
    """
    code = region_code.strip().upper()
    any_match = False
    any_load_error = False
    for path in config_paths:
        try:
            configs = Config.from_yaml(path)
        except (OSError, TypeError, ValueError, yaml.YAMLError):
            any_load_error = True
            continue
        if _matching_regions(configs, code):
            any_match = True
    return RegionFilterScan(any_match, any_load_error)


def load_config_regions(
    config_path: Path, args: argparse.Namespace
) -> tuple[int, list[Config]]:
    """Load a config file and return its region-filtered Config list.

    Returns (status, configs). status is non-zero on a load/filter error, in
    which case configs is empty. A --reg miss is an error for a single config
    (-c) but a silent skip for a directory sweep (-d), where most files do not
    carry the requested region; cli.main() fails the run when no config in the
    sweep matched. Applies the --incremental override in place.
    """
    try:
        configs = Config.from_yaml(config_path)
    except (OSError, TypeError, ValueError, yaml.YAMLError) as exc:
        Log.e(f"Config error for {config_path}: {exc}")
        return 1, []

    if args.region:
        region_code = args.region.strip().upper()
        Log.i(f"Filtering configuration regions by region code: {region_code}")
        filtered_configs = _matching_regions(configs, region_code)
        if not filtered_configs:
            if is_sweep_mode(args):
                # Selection, not demand: skipping is reported by the run-level
                # "no region matched anywhere" check, not per file.
                return 0, []
            Log.e(
                f"No configuration regions in {config_path} match region code {region_code}"
            )
            return 1, []
        configs = filtered_configs

    if args.incremental:
        for cfg in configs:
            cfg.incremental = args.incremental

    return 0, configs


def process_config(config_path: Path, args: argparse.Namespace) -> int:
    status, configs = load_config_regions(config_path, args)
    if status != 0:
        return status

    exit_code = 0
    regions_total = len(configs)

    if regions_total > 1:
        Log.raw("")

    for idx, cfg in enumerate(configs, start=1):
        display_label = cfg.region or f"region {idx}"

        if regions_total > 1 and idx > 1:
            Log.raw("")
        if regions_total > 1:
            Log.i(f"Processing region {idx}/{regions_total}: {display_label}")

        if args.incremental:
            Log.i(f"Override incremental: {args.incremental}")

        result = process_region(args.run_context, cfg, config_path, args)
        exit_code = max(exit_code, result)

    return exit_code
