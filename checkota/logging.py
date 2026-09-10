"""Thread-safe-ish logging helpers with ANSI colors.

Log.capture redirects output for the calling thread only, so parallel region
workers can buffer their output without interleaving with other threads.
"""

import re
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import IO, TextIO

_thread_local = threading.local()

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def sanitize_log_text(value: object) -> str:
    """Escape terminal-control characters before interpolating untrusted values.

    Newlines, carriage returns, and tabs are preserved because they are useful
    in multi-line output; all other C0/C1 controls and ANSI CSI sequences are
    neutralized so server-controlled metadata cannot spoof or recolor logs.
    """
    text = _ANSI_ESCAPE_RE.sub("", str(value))
    return _CONTROL_RE.sub(lambda match: f"\\x{ord(match.group(0)):02x}", text)


def _stream() -> IO[str]:
    return getattr(_thread_local, "stream", sys.stdout)


class Log:
    @staticmethod
    def i(message: str) -> None:
        print(f"\033[94m=>\033[0m {sanitize_log_text(message)}", file=_stream())

    @staticmethod
    def s(message: str) -> None:
        print(f"\033[92m✓\033[0m {sanitize_log_text(message)}", file=_stream())

    @staticmethod
    def e(message: str) -> None:
        print(f"\033[91m✗\033[0m {sanitize_log_text(message)}", file=_stream())

    @staticmethod
    def w(message: str) -> None:
        print(f"\033[93m!\033[0m {sanitize_log_text(message)}", file=_stream())

    @staticmethod
    def raw(message: str = "") -> None:
        print(message, file=_stream())

    @staticmethod
    @contextmanager
    def capture(stream: TextIO) -> Iterator[None]:
        prev = getattr(_thread_local, "stream", None)
        _thread_local.stream = stream
        try:
            yield
        finally:
            if prev is None:
                # Thread-local state: no other thread can interleave between
                # the check and the delete.
                if hasattr(_thread_local, "stream"):
                    delattr(_thread_local, "stream")
            else:
                _thread_local.stream = prev
