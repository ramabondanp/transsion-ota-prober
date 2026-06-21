import sys
import threading
from contextlib import contextmanager

_thread_local = threading.local()


def _stream():
    return getattr(_thread_local, "stream", sys.stdout)


class Log:
    @staticmethod
    def i(message):
        print(f"\033[94m=>\033[0m {message}", file=_stream())

    @staticmethod
    def s(message):
        print(f"\033[92m✓\033[0m {message}", file=_stream())

    @staticmethod
    def e(message):
        print(f"\033[91m✗\033[0m {message}", file=_stream())

    @staticmethod
    def w(message):
        print(f"\033[93m!\033[0m {message}", file=_stream())

    @staticmethod
    def raw(message=""):
        print(message, file=_stream())

    @staticmethod
    @contextmanager
    def capture(stream):
        prev = getattr(_thread_local, "stream", None)
        _thread_local.stream = stream
        try:
            yield
        finally:
            if prev is None:
                try:
                    delattr(_thread_local, "stream")
                except AttributeError:
                    pass
            else:
                _thread_local.stream = prev
