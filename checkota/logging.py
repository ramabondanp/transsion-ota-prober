"""Thread-safe-ish logging helpers with ANSI colors.

Log.capture redirects output for the calling thread only, so parallel variant
workers can buffer their output without interleaving with other threads.
"""

import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import IO, TextIO

_thread_local = threading.local()


def _stream() -> IO[str]:
    return getattr(_thread_local, "stream", sys.stdout)


class Log:
    @staticmethod
    def i(message: str) -> None:
        print(f"\033[94m=>\033[0m {message}", file=_stream())

    @staticmethod
    def s(message: str) -> None:
        print(f"\033[92m✓\033[0m {message}", file=_stream())

    @staticmethod
    def e(message: str) -> None:
        print(f"\033[91m✗\033[0m {message}", file=_stream())

    @staticmethod
    def w(message: str) -> None:
        print(f"\033[93m!\033[0m {message}", file=_stream())

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
