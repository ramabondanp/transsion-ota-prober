from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from typing import TextIO

from checkota.logging import Log

# Maximum number of entries to keep in the processed updates file.
# Older entries are trimmed to prevent unbounded growth.
MAX_PROCESSED_ENTRIES = 2000

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    _fcntl = None  # type: ignore[assignment]


def _title_lock_path(path: Path, title: str) -> Path:
    digest = sha256(title.encode("utf-8")).hexdigest()
    return path.with_name(f"{path.name}.{digest}.lock")


def _open_locked(path: Path, mode: str) -> TextIO:
    handle = path.open(mode, encoding="utf-8")
    try:
        if _fcntl is not None:
            _fcntl.flock(handle.fileno(), _fcntl.LOCK_EX)
    except Exception:
        handle.close()
        raise
    return handle


def _close_locked(handle: TextIO) -> None:
    try:
        if _fcntl is not None:
            _fcntl.flock(handle.fileno(), _fcntl.LOCK_UN)
    finally:
        handle.close()


@contextmanager
def _locked_file(path: Path, mode: str = "a+"):
    """Open a file and hold an advisory exclusive lock for its whole context.

    On POSIX systems this serializes checkota processes that update the shared
    processed-updates file. On platforms without fcntl it degrades to an
    unlocked file handle.
    """
    handle = _open_locked(path, mode)
    try:
        yield handle
    finally:
        _close_locked(handle)


def _read_titles(handle: TextIO) -> tuple[list[str], set[str]]:
    handle.seek(0)
    lines = handle.readlines()
    return lines, {line.strip() for line in lines if line.strip()}


def _append_title(handle: TextIO, lines: list[str], title: str) -> None:
    handle.seek(0, 2)
    handle.write(f"{title}\n")
    handle.flush()

    all_lines = lines + [f"{title}\n"]
    if len(all_lines) > MAX_PROCESSED_ENTRIES:
        handle.seek(0)
        handle.truncate()
        handle.writelines(all_lines[-MAX_PROCESSED_ENTRIES:])
        handle.flush()


def load_processed_titles(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        with _locked_file(path, "r") as handle:
            return {line.strip() for line in handle if line.strip()}
    except FileNotFoundError:
        return set()
    except (OSError, UnicodeError, ValueError) as exc:
        Log.e(f"Error reading processed updates file {path}: {exc}")
        return set()


def claim_processed_title(path: Path, title: str) -> TextIO | None:
    """Claim a title until the returned locked handle is released.

    The title lock is held while the caller performs the notification. The
    processed file is read under its own lock so ordinary saves cannot race the
    claim decision.
    """
    claim = _open_locked(_title_lock_path(path, title), "a+")
    try:
        with _locked_file(path, "a+") as handle:
            _, existing = _read_titles(handle)
        if title in existing:
            _close_locked(claim)
            return None
        return claim
    except Exception:
        _close_locked(claim)
        raise


def commit_processed_title(path: Path, title: str, claim: TextIO) -> bool:
    """Persist a title while its caller-owned title claim remains held."""
    try:
        with _locked_file(path, "a+") as handle:
            lines, existing = _read_titles(handle)
            if title not in existing:
                _append_title(handle, lines, title)
                Log.s(f"Saved new update title to {path}")
        return True
    except (OSError, UnicodeError, ValueError) as exc:
        Log.e(f"Failed to save update title to {path}: {exc}")
        return False


def release_processed_claim(claim: TextIO) -> None:
    _close_locked(claim)


def save_processed_title(path: Path, title: str) -> bool:
    """Append a title to the processed-updates file.

    Returns True when the title is known to be in the file after this call
    (whether newly appended or already present from another process). Returns
    False on any I/O/parse error.
    """
    claim: TextIO | None = None
    try:
        claim = _open_locked(_title_lock_path(path, title), "a+")
        with _locked_file(path, "a+") as handle:
            lines, existing = _read_titles(handle)
            if title in existing:
                return True
            _append_title(handle, lines, title)
        Log.s(f"Saved new update title to {path}")
        return True
    except (OSError, UnicodeError, ValueError) as exc:
        Log.e(f"Failed to save update title to {path}: {exc}")
        return False
    finally:
        if claim is not None:
            _close_locked(claim)
