"""Runtime coordination primitives: shared run context, signal handling, and
the wall-clock watchdog.
"""

import os
import signal
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set

import requests
from requests.adapters import HTTPAdapter

from modules.fingerprints import load_processed_titles
from modules.logging import Log
from modules.metadata import processed_updates_path


@dataclass
class RunContext:
    env: Dict[str, str]
    processed_path: Path
    processed_titles: Set[str]
    dry_run: bool
    metadata_cache: Dict[str, Optional[Dict[str, str]]] = field(default_factory=dict)
    # URL -> Event for an in-flight metadata fetch, so concurrent workers sharing
    # a URL fetch exactly once (see processor.get_cached_ota_metadata).
    _metadata_inflight: Dict[str, threading.Event] = field(
        default_factory=dict, repr=False
    )
    file_lock: threading.Lock = field(default_factory=threading.Lock)
    telegram_lock: threading.Lock = field(default_factory=threading.Lock)
    cache_lock: threading.Lock = field(default_factory=threading.Lock)
    notice_lock: threading.Lock = field(default_factory=threading.Lock)
    session_lock: threading.Lock = field(default_factory=threading.Lock)
    pending_lock: threading.Lock = field(default_factory=threading.Lock)
    stop_event: threading.Event = field(default_factory=threading.Event)
    pending_notifications: List["PendingNotification"] = field(default_factory=list)
    telegram_notice_printed: bool = False
    pool_size: int = 10
    _local: threading.local = field(default_factory=threading.local, repr=False)
    _sessions: List[requests.Session] = field(default_factory=list, repr=False)

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

    def stop(self) -> None:
        self.stop_event.set()
        with self.session_lock:
            sessions = list(self._sessions)
            self._sessions.clear()
        for session in sessions:
            try:
                session.close()
            except Exception:
                pass


def create_run_context(dry_run: bool, pool_size: int = 10) -> RunContext:
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
    )


def install_interrupt_handler(ctx: RunContext):
    previous_handler = signal.getsignal(signal.SIGINT)

    def handle_interrupt(signum, frame):
        # Signal interruption: only set stop_event. Sessions are closed by
        # main()'s `finally` block AFTER drain_completes so that the drain's
        # `create_notifier(ctx, args)` call still has a usable session.
        ctx.stop_event.set()
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, handle_interrupt)
    return previous_handler


def start_watchdog(ctx: RunContext, timeout: float) -> Optional[threading.Timer]:
    """Start a daemon timer that hard-exits the process when the wall-clock
    budget is exceeded. Returns the timer (cancel it in a finally block), or
    None when no timeout is configured.
    """
    if timeout <= 0:
        return None

    def _on_timeout() -> None:
        Log.e(f"Timeout of {timeout:.0f}s exceeded; signalling stop and exiting.")
        ctx.stop_event.set()
        ctx.stop()
        # Hard-exit: in-flight socket reads (e.g. RemoteZip) may not honour
        # the stop_event mid-call, so force termination after the budget.
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(124)

    watchdog = threading.Timer(timeout, _on_timeout)
    watchdog.daemon = True
    watchdog.start()
    return watchdog
