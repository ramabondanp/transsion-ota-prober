"""Runtime coordination primitives: shared run context, signal handling, and
the wall-clock watchdog.
"""

import argparse
import os
import signal
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

import requests
from requests.adapters import HTTPAdapter

from checkota.constants import EMERGENCY_DRAIN_MAX_SENDS
from checkota.fingerprints import load_processed_titles
from checkota.logging import Log
from checkota.models import PendingNotification
from checkota.paths import processed_updates_path


@dataclass
class RunContext:
    env: dict[str, str]
    processed_path: Path
    processed_titles: set[str]
    dry_run: bool
    zip_proxy: bool = False
    claimed_titles: set[str] = field(default_factory=set)
    claimed_handles: dict[str, TextIO] = field(default_factory=dict, repr=False)
    metadata_cache: dict[str, dict[str, str] | None] = field(default_factory=dict)
    metadata_failures: dict[str, float] = field(default_factory=dict)
    # URL -> Event for an in-flight metadata fetch, so concurrent workers sharing
    # a URL fetch exactly once (see processor.get_cached_ota_metadata).
    _metadata_inflight: dict[str, threading.Event] = field(
        default_factory=dict, repr=False
    )
    file_lock: threading.Lock = field(default_factory=threading.Lock)
    telegram_lock: threading.Lock = field(default_factory=threading.Lock)
    cache_lock: threading.Lock = field(default_factory=threading.Lock)
    notice_lock: threading.Lock = field(default_factory=threading.Lock)
    session_lock: threading.Lock = field(default_factory=threading.Lock)
    pending_lock: threading.Lock = field(default_factory=threading.Lock)
    stop_event: threading.Event = field(default_factory=threading.Event)
    pending_notifications: list["PendingNotification"] = field(default_factory=list)
    telegram_notice_printed: bool = False
    pool_size: int = 10
    # Parsed CLI arguments, stashed so the watchdog thread's emergency drain
    # can run without access to main()'s locals.
    cli_args: argparse.Namespace | None = None
    # Serializes notification drains: the watchdog thread's emergency drain
    # skips (instead of waiting) if main() is already draining, and main()
    # waits for an in-flight emergency drain to finish before its own.
    drain_lock: threading.Lock = field(default_factory=threading.Lock)
    _local: threading.local = field(default_factory=threading.local, repr=False)
    _sessions: list[requests.Session] = field(default_factory=list, repr=False)

    def session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            # Size the connection pool so concurrent variant/config workers that
            # share this thread's session never block on a full pool.
            adapter = HTTPAdapter(
                pool_connections=self.pool_size, pool_maxsize=self.pool_size
            )
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            self._local.session = session
            with self.session_lock:
                self._sessions.append(session)
        return session

    def direct_session(self) -> requests.Session:
        """Returns a thread-local direct Session (trust_env=False) that bypasses all
        proxy environment variables and proxy connection pools. Used for fetching
        OTA metadata directly from Google CDN.
        """
        session = getattr(self._local, "direct_session", None)
        if session is None:
            session = requests.Session()
            session.trust_env = False
            adapter = HTTPAdapter(
                pool_connections=self.pool_size, pool_maxsize=self.pool_size
            )
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            self._local.direct_session = session
            with self.session_lock:
                self._sessions.append(session)
        return session

    def zip_session(self) -> requests.Session:
        """Returns a thread-local Session for fetching ZIP metadata.
        Uses session() (trust_env=True) when zip_proxy is True to respect
        proxy environment variables, or direct_session() (trust_env=False)
        when zip_proxy is False to bypass proxies.
        """
        if self.zip_proxy:
            return self.session()
        return self.direct_session()

    def stop(self) -> None:
        self.stop_event.set()
        from checkota.fingerprints import release_processed_claim

        with self.file_lock:
            claims = list(self.claimed_handles.values())
            self.claimed_handles.clear()
            self.claimed_titles.clear()
        for claim in claims:
            try:
                release_processed_claim(claim)
            except (OSError, ValueError):
                pass
        with self.session_lock:
            sessions = list(self._sessions)
            self._sessions.clear()
        for session in sessions:
            try:
                session.close()
            except (OSError, requests.exceptions.RequestException) as exc:
                Log.w(f"Ignoring error while closing session: {exc}")


def create_run_context(
    dry_run: bool, pool_size: int = 10, zip_proxy: bool = False
) -> RunContext:
    if dry_run:
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
        dry_run=dry_run,
        # Per AGENTS.md "Per-thread session pool too small" — give each thread at
        # least 10 socket slots so concurrent variant/config workers never block
        # on a full pool when --jobs overshoots the default floor. This is the
        # *capacity* of HTTPAdapter.pool_maxsize, not eagerly-opened sockets.
        pool_size=max(10, pool_size),
        zip_proxy=zip_proxy,
    )


def install_interrupt_handler(ctx: RunContext) -> object:
    previous_handler = signal.getsignal(signal.SIGINT)

    def handle_interrupt(signum, frame):
        # Signal interruption: only set stop_event. Sessions are closed by
        # main()'s `finally` block AFTER drain_completes so that the drain's
        # `create_notifier(ctx, args)` call still has a usable session.
        ctx.stop_event.set()
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, handle_interrupt)
    return previous_handler


def start_watchdog(ctx: RunContext, timeout: float) -> threading.Timer | None:
    """Start a daemon timer that hard-exits the process when the wall-clock
    budget is exceeded. Returns the timer (cancel it in a finally block), or
    None when no timeout is configured.
    """
    if timeout <= 0:
        return None

    def _on_timeout() -> None:
        ctx.stop_event.set()
        # Flush buffered stdio: os._exit skips interpreter shutdown, so piped
        # (block-buffered) output would otherwise be lost.
        sys.stdout.flush()
        sys.stderr.flush()
        # Best-effort flush of buffered Telegram notifications before the hard
        # exit; a one-shot cron run would otherwise lose every update found by
        # this sweep. Bounded and fully guarded -- teardown must not hang or
        # crash because of it.
        _emergency_drain_notifications(ctx)
        sys.stdout.flush()
        sys.stderr.flush()
        # Hard-exit: in-flight socket reads (e.g. RemoteZip) may not honour
        # the stop_event mid-call, so force termination after the budget.
        os._exit(124)

    watchdog = threading.Timer(timeout, _on_timeout)
    watchdog.daemon = True
    watchdog.start()
    return watchdog


def _emergency_drain_notifications(ctx: RunContext) -> None:
    """Best-effort synchronous notification drain from the watchdog thread.

    Workers only BUFFER notifications during a sweep; the drain is the sole
    sender, so running it here cannot double-send. Skips silently when main()
    is already draining (its own budget covers those notifications), and caps
    the number of sends so a huge buffer cannot extend teardown indefinitely.
    Titles for unsent notifications are never committed, so whatever this does
    not get to is retried naturally by the next run.
    """
    with ctx.pending_lock:
        has_pending = bool(ctx.pending_notifications)
    if not has_pending or ctx.cli_args is None:
        return
    try:
        from checkota.processor import drain_pending_notifications
    except Exception:  # noqa: BLE001 -- teardown path; skip rather than crash
        return

    if not ctx.drain_lock.acquire(blocking=False):
        return  # main() owns the drain right now
    try:
        # drain_pending_notifications refuses to run while stop_event is set;
        # workers have already been signalled (or are stuck in socket reads
        # that ignore it), so clearing it here only lets the drain proceed.
        ctx.stop_event.clear()
        drain_pending_notifications(
            ctx,
            ctx.cli_args,
            max_sends=EMERGENCY_DRAIN_MAX_SENDS,
        )
    except Exception as exc:  # noqa: BLE001 -- teardown path
        Log.w(f"Emergency notification drain failed: {exc}")
    finally:
        ctx.stop_event.set()
        ctx.drain_lock.release()
