"""Latest-only OTA sweep for TecnoNaman.

The normal checker reports the next OTA reachable from a stored device baseline. If a
baseline is old, that can expose several historical/intermediate OTAs one by one. This
runner follows that chain in memory first, records intermediate titles silently, and
sends Telegram only for the final reachable OTA.

The on-disk config is advanced only after the final notification succeeds. If Telegram
fails, the old baseline remains in place so a later manual run can retry without losing
the latest notification.
"""

from __future__ import annotations

import argparse
import signal
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from checkota.fingerprints import (
    claim_processed_title,
    commit_processed_title,
    release_processed_claim,
    save_processed_title,
)
from checkota.logging import Log
from checkota.manager import Config, parse_fingerprint, update_config_from_fingerprint
from checkota.notifier import build_notification_message, create_notifier
from checkota.processor import collect_update_info, load_config_variants
from checkota.runtime import (
    RunContext,
    create_run_context,
    install_interrupt_handler,
    start_watchdog,
)

MAX_CHAIN_HOPS = 12
TELEGRAM_SEND_GAP_SECONDS = 1.2
_last_send_at = 0.0


def _worker_args(ctx: RunContext, config_dir: Path) -> argparse.Namespace:
    """Build the argument shape expected by the existing checker internals."""
    return argparse.Namespace(
        debug=False,
        config=None,
        config_dir=config_dir,
        fp=None,
        dry_run=False,
        skip_telegram=False,
        register_update=False,
        update_incremental=False,
        force_notify=True,  # probe known titles too; we decide notification separately
        incremental=None,
        imei=None,
        gen_fp=False,
        region=None,
        jobs=1,
        timeout=0.0,
        no_config=False,
        run_context=ctx,
    )


def _advance_cfg_in_memory(cfg: Config, fingerprint: str) -> bool:
    parsed = parse_fingerprint(fingerprint)
    if not parsed:
        return False
    cfg.android_version = parsed["android_version"]
    cfg.build_tag = parsed["build_tag"]
    cfg.incremental = parsed["incremental"]
    return True


def _is_processed(ctx: RunContext, title: str) -> bool:
    with ctx.file_lock:
        return title in ctx.processed_titles


def _mark_processed(ctx: RunContext, title: str) -> bool:
    """Record an intermediate OTA without notifying Telegram."""
    with ctx.file_lock:
        if title in ctx.processed_titles:
            return True
        saved = save_processed_title(ctx.processed_path, title)
        if saved:
            ctx.processed_titles.add(title)
        return saved


def _persist_target(ctx: RunContext, update) -> bool:
    """Advance the stored YAML baseline directly to the chosen final target."""
    with ctx.file_lock:
        ok = update_config_from_fingerprint(
            update.config_path, update.cfg, update.target_fp
        )
    if not ok:
        Log.e(f"Failed to persist latest baseline for {update.cfg.model}")
    return ok


def _send_latest_once(ctx: RunContext, update, args: argparse.Namespace) -> bool:
    """Send one final OTA with a process/file-safe title claim."""
    global _last_send_at

    if _is_processed(ctx, update.title):
        Log.i(f"Latest OTA already processed; duplicate skipped: {update.title}")
        return True

    try:
        claim = claim_processed_title(ctx.processed_path, update.title)
    except (OSError, UnicodeError, ValueError) as exc:
        Log.e(f"Failed to reserve OTA title {update.title}: {exc}")
        return False

    if claim is None:
        with ctx.file_lock:
            ctx.processed_titles.add(update.title)
        Log.i(f"OTA was already claimed/processed elsewhere; duplicate skipped: {update.title}")
        return True

    notifier = create_notifier(ctx, args)
    if notifier is None:
        release_processed_claim(claim)
        Log.e("Telegram notifier is unavailable; latest baseline was not advanced.")
        return False

    msg = build_notification_message(update)
    device_title = f"{update.cfg.model} - {update.title}"

    with ctx.telegram_lock:
        wait_for = TELEGRAM_SEND_GAP_SECONDS - (time.monotonic() - _last_send_at)
        if wait_for > 0 and ctx.stop_event.wait(wait_for):
            release_processed_claim(claim)
            return False
        sent = notifier.send(msg, truncate_desc=True, device_title=device_title)
        if sent:
            _last_send_at = time.monotonic()

    if not sent:
        release_processed_claim(claim)
        Log.e("Latest OTA notification failed; baseline kept unchanged for retry.")
        return False

    committed = commit_processed_title(ctx.processed_path, update.title, claim)
    release_processed_claim(claim)
    if not committed:
        Log.e("Telegram sent, but duplicate-protection state could not be saved.")
        return False

    with ctx.file_lock:
        ctx.processed_titles.add(update.title)
    return True


def process_variant_latest(
    ctx: RunContext,
    cfg: Config,
    config_path: Path,
    args: argparse.Namespace,
    variant_label: str | None = None,
) -> int:
    """Follow OTA hops in memory and notify only the final reachable update."""
    chain = []
    seen: set[tuple[str, str]] = set()
    exhausted = True

    for hop in range(1, MAX_CHAIN_HOPS + 1):
        if ctx.stop_event.is_set():
            return 130

        status, update = collect_update_info(
            ctx, cfg, config_path, args, variant_label
        )
        if status != 0:
            # Do not notify a partially explored chain. Keep the disk baseline
            # unchanged so a future manual run can retry safely.
            return status

        if update is None:
            exhausted = False
            break

        signature = (update.title, update.target_fp)
        if signature in seen:
            Log.w(
                f"Repeated OTA response after baseline advance: {update.title}. "
                "Treating the last distinct OTA as the final candidate."
            )
            exhausted = False
            break
        seen.add(signature)
        chain.append(update)

        if not _advance_cfg_in_memory(cfg, update.target_fp):
            Log.e(f"Invalid target fingerprint while chasing latest OTA: {update.target_fp}")
            return 1

        Log.i(
            f"Latest-only probe hop {hop}: advanced in memory to "
            f"{cfg.android_version}/{cfg.incremental}"
        )

    if not chain:
        return 0

    latest = chain[-1]

    if exhausted:
        # We hit the safety cap before proving there is no newer OTA. Suppress all
        # discovered builds and advance the baseline silently. A later run can
        # continue from here instead of spamming historical updates.
        Log.w(
            f"Reached {MAX_CHAIN_HOPS} OTA hops for {cfg.model}; "
            "suppressing this batch and advancing baseline without Telegram."
        )
        for item in chain:
            if not _mark_processed(ctx, item.title):
                return 1
        return 0 if _persist_target(ctx, latest) else 1

    # Every discovered update before the final one is historical/intermediate.
    for item in chain[:-1]:
        if not _mark_processed(ctx, item.title):
            Log.e(f"Could not save intermediate OTA state: {item.title}")
            return 1
        Log.i(f"Intermediate OTA suppressed: {item.title}")

    # If another run/variant already handled the final title, never send it again.
    if _is_processed(ctx, latest.title):
        Log.i(f"Final OTA already processed; duplicate Telegram skipped: {latest.title}")
        return 0 if _persist_target(ctx, latest) else 1

    Log.s(f"Final latest OTA selected for Telegram: {latest.title}")
    if not _send_latest_once(ctx, latest, args):
        return 1

    return 0 if _persist_target(ctx, latest) else 1


def _collect_jobs(config_dir: Path, ctx: RunContext, args: argparse.Namespace):
    jobs = []
    for config_path in sorted(config_dir.glob("*.y*ml"), key=lambda p: p.name.lower()):
        status, variants = load_config_variants(config_path, args)
        if status != 0:
            yield status, None
            continue
        for cfg in variants:
            jobs.append((cfg, config_path, cfg.variant))
    yield 0, jobs


def main() -> int:
    parser = argparse.ArgumentParser(description="TecnoNaman latest-only OTA sweep")
    parser.add_argument("--config-dir", type=Path, default=Path("configs"))
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=600.0)
    ns = parser.parse_args()

    if ns.jobs < 1:
        parser.error("--jobs must be >= 1")
    if not ns.config_dir.is_dir():
        parser.error("--config-dir must be an existing directory")

    ctx = create_run_context(False, pool_size=ns.jobs)
    args = _worker_args(ctx, ns.config_dir)
    previous_sigint = install_interrupt_handler(ctx)
    watchdog = start_watchdog(ctx, ns.timeout)
    exit_code = 0

    try:
        jobs = []
        for status, found_jobs in _collect_jobs(ns.config_dir, ctx, args):
            exit_code = max(exit_code, status)
            if found_jobs:
                jobs.extend(found_jobs)

        with ThreadPoolExecutor(max_workers=ns.jobs) as executor:
            futures = [
                executor.submit(
                    process_variant_latest,
                    ctx,
                    cfg,
                    config_path,
                    args,
                    variant_label,
                )
                for cfg, config_path, variant_label in jobs
            ]
            for future in as_completed(futures):
                try:
                    exit_code = max(exit_code, future.result())
                except Exception as exc:
                    Log.e(f"Unhandled latest-only worker error: {exc}")
                    exit_code = max(exit_code, 1)
    except KeyboardInterrupt:
        Log.w("Interrupted latest-only OTA sweep.")
        exit_code = 130
    finally:
        ctx.stop()
        signal.signal(signal.SIGINT, previous_sigint)
        if watchdog is not None:
            watchdog.cancel()

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
