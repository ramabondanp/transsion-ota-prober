"""Command-line interface: argument parsing, config resolution, and the
top-level run orchestration (sequential and parallel)."""

import argparse
import io
import signal
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Tuple

from checkota.logging import Log
from checkota.manager import Config
from checkota.paths import APP_CONFIGS_DIR
from checkota.processor import (
    config_from_fingerprint,
    drain_pending_notifications,
    load_config_variants,
    process_config,
    process_config_variant,
)
from checkota.runtime import (
    RunContext,
    create_run_context,
    install_interrupt_handler,
    start_watchdog,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Android OTA Update Checker")
    parser.add_argument("--debug", action="store_true", help="Enable debugging")
    parser.add_argument("-c", "--config", type=Path, help="Config file path")
    parser.add_argument(
        "-d",
        "--config-dir",
        type=Path,
        help="Directory containing config files to process",
    )
    parser.add_argument(
        "--fp",
        help="Use this full Android fingerprint directly and skip config file loading",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate actions without making changes or sending notifications",
    )
    parser.add_argument(
        "--skip-telegram", action="store_true", help="Skip Telegram notifications"
    )
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
    parser.add_argument(
        "--force-notify",
        action="store_true",
        help="Send notification even if the update has been seen before",
    )
    parser.add_argument(
        "--force",
        dest="force_update_incremental",
        action="store_true",
        help="(Deprecated) Previously needed with --update-incremental; --update-incremental now always proceeds.",
    )
    parser.add_argument(
        "-i",
        "--incremental",
        help="Override incremental version for all selected configs",
    )
    parser.add_argument("--imei", help="Override IMEI used in the OTA check-in request")
    parser.add_argument(
        "--gen-fp",
        action="store_true",
        help="Print update target fingerprint(s) only when OTA updates are available",
    )
    parser.add_argument(
        "--reg",
        "--region",
        dest="region",
        help="Process only variants matching the given region code (e.g. OP, RU)",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=1,
        help="Number of config files to process in parallel when using --config-dir (default: 1)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=0.0,
        help="Overall wall-clock budget in seconds. When exceeded, in-flight requests are"
        " signalled to stop and the process exits (0 = no limit).",
    )
    return parser


def resolve_config_path(value: Path) -> Path:
    """Resolve a bare codename like 'X6850' to the matching config file.

    Lookup order:
      1. value as-is (covers absolute paths, ./relative, ~/... already expanded)
      2. APP_CONFIGS_DIR / f"config-{value}.yml" (bare codename)
      3. APP_CONFIGS_DIR / value (when value ends in .yml/.yaml)
    Returns the original value unchanged if nothing matches so downstream
    errors surface normally.
    """
    if value.is_file():
        return value
    val = str(value)
    candidates = []
    if val.endswith((".yml", ".yaml")):
        candidates.append(APP_CONFIGS_DIR / val)
        candidates.append(APP_CONFIGS_DIR / f"config-{val}")
    else:
        candidates.append(APP_CONFIGS_DIR / f"config-{val}.yml")
        candidates.append(APP_CONFIGS_DIR / f"{val}.yml")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return value


def _validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.timeout < 0:
        parser.error("--timeout must be >= 0")
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
    if args.config:
        args.config = resolve_config_path(args.config)


def _collect_config_paths(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> List[Path]:
    if args.config:
        return [args.config]
    if not args.config_dir.exists() or not args.config_dir.is_dir():
        parser.error("--config-dir must be an existing directory.")
    config_paths = sorted(
        (
            path
            for path in args.config_dir.iterdir()
            if path.is_file() and path.suffix in (".yml", ".yaml")
        ),
        key=lambda p: p.name.lower(),
    )
    if not config_paths:
        Log.e(f"No config files found in directory: {args.config_dir}")
    return config_paths


def _run_sequential(
    ctx: RunContext, args: argparse.Namespace, config_paths: List[Path]
) -> int:
    exit_code = 0
    total = len(config_paths)
    for idx, config_path in enumerate(config_paths, start=1):
        if ctx.stop_event.is_set():
            return 130
        local_args = argparse.Namespace(**vars(args))
        if idx > 1:
            Log.raw("")
        header = (
            f"Processing config {idx}/{total}: {config_path}"
            if total > 1
            else f"Processing config: {config_path}"
        )
        Log.i(header)
        result = process_config(config_path, local_args)
        exit_code = max(exit_code, result)
    return exit_code


def _run_global_pool(
    ctx: RunContext, args: argparse.Namespace, config_paths: List[Path]
) -> Tuple[int, ThreadPoolExecutor]:
    """Run every (config, variant) pair through a single pool sized by --jobs.

    Total in-flight requests never exceed --jobs regardless of how many variants
    a config has. Output is buffered per variant and regrouped per config in the
    original config order (variants in order within each config).
    """
    total = len(config_paths)

    @dataclass
    class _ConfigJob:
        index: int
        path: Path
        load_output: str
        status: int
        variants: List[Config]
        results: Dict[int, str] = field(default_factory=dict)

    # Load every config's variants upfront (YAML parse only, no network),
    # capturing any filter/error output into a per-config buffer.
    config_jobs: Dict[int, _ConfigJob] = {}
    for idx, config_path in enumerate(config_paths, start=1):
        buf = io.StringIO()
        with Log.capture(buf):
            status, variants = load_config_variants(config_path, args)
        config_jobs[idx] = _ConfigJob(
            index=idx,
            path=config_path,
            load_output=buf.getvalue(),
            status=status,
            variants=variants,
        )

    def variant_worker(
        config_idx: int, variant_idx: int, variants_total: int, cfg: Config, path: Path
    ) -> Tuple[int, int, int, str]:
        if ctx.stop_event.is_set():
            return config_idx, variant_idx, 130, ""
        local_args = argparse.Namespace(**vars(args))
        buffer = io.StringIO()
        try:
            with Log.capture(buffer):
                if variants_total > 1:
                    label = cfg.variant or f"variant {variant_idx}"
                    Log.i(f"Processing variant {variant_idx}/{variants_total}: {label}")
                if args.incremental:
                    Log.i(f"Override incremental: {args.incremental}")
                result = process_config_variant(ctx, cfg, path, local_args, cfg.variant)
        except Exception as exc:
            buffer.write(
                f"\033[91m✗\033[0m {path} variant {variant_idx} failed with unhandled exception: {exc}\n"
            )
            result = 1
        return config_idx, variant_idx, result, buffer.getvalue()

    exit_code = 0
    executor = ThreadPoolExecutor(max_workers=args.jobs)
    # Count of variant futures still pending per config; a config is ready to
    # flush (in order) once its count hits zero.
    pending: Dict[int, int] = {}
    futures = []
    try:
        for cj in config_jobs.values():
            if cj.status != 0 or not cj.variants:
                pending[cj.index] = 0
                exit_code = max(exit_code, cj.status)
                continue
            vt = len(cj.variants)
            pending[cj.index] = vt
            for v_idx, cfg in enumerate(cj.variants, start=1):
                futures.append(
                    executor.submit(variant_worker, cj.index, v_idx, vt, cfg, cj.path)
                )

        remaining = set(futures)
        next_index = 1
        first = True
        last_heartbeat = time.monotonic()

        def _flush_ready() -> None:
            nonlocal next_index, first
            while next_index <= total and pending.get(next_index, 0) == 0:
                cj = config_jobs[next_index]
                if not first:
                    Log.raw("")
                first = False
                header = (
                    f"Processing config {cj.index}/{total}: {cj.path}"
                    if total > 1
                    else f"Processing config: {cj.path}"
                )
                Log.i(header)
                if cj.load_output:
                    print(cj.load_output, end="")
                if len(cj.variants) > 1:
                    Log.raw("")
                for v_idx in sorted(cj.results):
                    if v_idx > 1:
                        Log.raw("")
                    output = cj.results[v_idx]
                    if output:
                        print(output, end="" if output.endswith("\n") else "\n")
                next_index += 1

        _flush_ready()
        while remaining:
            if ctx.stop_event.is_set():
                return 130, executor
            done, remaining = wait(remaining, timeout=2, return_when=FIRST_COMPLETED)
            for future in done:
                c_idx, v_idx, result, output = future.result()
                exit_code = max(exit_code, result)
                cj = config_jobs[c_idx]
                cj.results[v_idx] = output
                pending[c_idx] -= 1
            _flush_ready()

            now = time.monotonic()
            if now - last_heartbeat >= 5 and next_index <= total:
                completed = next_index - 1
                Log.raw(
                    f"... waiting for config {next_index}/{total} "
                    f"({completed}/{total} configs flushed, {len(remaining)} variant tasks in flight)"
                )
                last_heartbeat = now
        _flush_ready()
    finally:
        # Shutdown executor without waiting — just stop accepting new tasks.
        # Running tasks will be waited for in the outer finally block.
        executor.shutdown(wait=False, cancel_futures=True)

    return exit_code, executor


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    _validate_args(parser, args)

    args.run_context = create_run_context(args.dry_run, pool_size=max(1, args.jobs))
    ctx = args.run_context
    previous_sigint = install_interrupt_handler(ctx)
    watchdog = start_watchdog(ctx, args.timeout)
    executor = None
    exit_code = 0
    drain_result = 0
    # buffered_notifications_possible drives whether the outer drain runs.
    # Sweep mode buffers notifications per apply_update_actions; direct --fp
    # matches args.no_config=True and does NOT set args.config_dir, so this
    # predicate is false and direct mode never has anything in the pending buffer.
    buffered_notifications_possible = getattr(args, "config_dir", None) is not None

    try:
        if args.fp:
            args.no_config = True
            try:
                cfg = config_from_fingerprint(args.fp)
            except ValueError as exc:
                Log.e(str(exc))
                exit_code = 1
            else:
                Log.i("Processing direct fingerprint input")
                # Direct mode does not buffer; no drain needed.
                buffered_notifications_possible = False
                exit_code = process_config_variant(
                    ctx,
                    cfg=cfg,
                    config_path=Path("<fingerprint>"),
                    args=args,
                    variant_label=None,
                )
        else:
            args.no_config = False

            config_paths = _collect_config_paths(parser, args)
            if not config_paths:
                exit_code = 1
            elif args.jobs < 1:
                Log.e("--jobs must be >= 1")
                exit_code = 1
            elif args.jobs == 1:
                exit_code = _run_sequential(ctx, args, config_paths)
            else:
                exit_code, executor = _run_global_pool(ctx, args, config_paths)
    except KeyboardInterrupt:
        Log.w("Interrupted. Stopping in-flight requests and exiting.")
        ctx.stop_event.set()
        exit_code = 130
    finally:
        # Parallel mode: wait for running tasks to finish before closing sessions.
        if executor is not None:
            executor.shutdown(wait=True)
        # Drain AFTER all workers have stopped -- a worker that was still
        # mid-`apply_update_actions` could otherwise append to
        # `ctx.pending_notifications` after the drain took its snapshot.
        #
        # Only run drain in sweep mode (where notifications were buffered).
        # Direct `--fp` and config-error exits don't buffer anything, so
        # the drain would just be a no-op.
        #
        # By this point the signal handler and the `except KeyboardInterrupt`
        # arm have already set stop_event. Workers are already stopped
        # (executor.shutdown(wait=True) above), so clearing stop_event here
        # cannot resurrect them -- it only allows the drain to run. Sessions
        # are still alive (closed below) so `create_notifier(ctx, args)` from
        # inside drain still gets a usable session.
        if buffered_notifications_possible and exit_code in (0, 130):
            ctx.stop_event.clear()
            drain_result = drain_pending_notifications(ctx, args)
        # Close sessions safely (no worker threads should be using them).
        ctx.stop()
        signal.signal(signal.SIGINT, previous_sigint)
        if watchdog is not None:
            watchdog.cancel()

    return max(exit_code, drain_result)
