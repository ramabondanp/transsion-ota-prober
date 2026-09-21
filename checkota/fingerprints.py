import contextlib
import os
import tempfile
import time
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from typing import TextIO, cast

from checkota.logging import Log

# Maximum number of entries to keep in the processed updates file.
# Older entries are trimmed to prevent unbounded growth.
MAX_PROCESSED_ENTRIES = 2000

#: Per-title lock files older than this are pruned even when their title was
#: never committed (e.g. a crashed run). Committed titles are pruned
#: immediately regardless of age.
TITLE_LOCK_MAX_AGE_SECONDS = 7 * 24 * 60 * 60

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    _fcntl = None  # type: ignore[assignment]


def _title_lock_path(path: Path, title: str) -> Path:
    digest = sha256(title.encode("utf-8")).hexdigest()
    return path.with_name(f"{path.name}.{digest}.lock")


def _database_lock_path(path: Path) -> Path:
    """Return the stable lock inode used across processed-file replacements."""
    return path.with_name(f"{path.name}.db.lock")


def _open_locked(path: Path, mode: str) -> TextIO:
    handle = cast(TextIO, path.open(mode, encoding="utf-8"))
    try:
        if _fcntl is not None:
            _fcntl.flock(handle.fileno(), _fcntl.LOCK_EX)
    except OSError:
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
    """Open a file under a stable lock that survives atomic replacement.

    The data file is replaced when the title history is trimmed, so locking
    the data inode alone is insufficient: a second process can open the new
    inode after os.replace() and bypass the first lock. The separate database
    lock inode remains stable across replacements and is held for the entire
    read/append/trim transaction. On platforms without fcntl this degrades to
    unlocked file handles.
    """
    database_lock = _open_locked(_database_lock_path(path), "a+")
    try:
        handle = _open_locked(path, mode)
        try:
            yield handle
        finally:
            _close_locked(handle)
    finally:
        _close_locked(database_lock)


def _read_titles(handle: TextIO) -> tuple[list[str], set[str]]:
    handle.seek(0)
    lines = handle.readlines()
    return lines, {line.strip() for line in lines if line.strip()}


def _append_title(handle: TextIO, lines: list[str], title: str) -> None:
    # A state file whose last line has no terminator (hand-edited, or written by
    # an older tool) would otherwise merge that title with the new one: the old
    # title is forgotten (duplicate notification later) and a bogus combined
    # title is persisted. Terminate the dangling line before appending.
    if lines and not lines[-1].endswith("\n"):
        handle.seek(0, 2)
        handle.write("\n")
        lines = [*lines[:-1], lines[-1] + "\n"]

    handle.seek(0, 2)
    handle.write(f"{title}\n")
    handle.flush()

    all_lines = lines + [f"{title}\n"]
    if len(all_lines) <= MAX_PROCESSED_ENTRIES:
        return
    _rewrite_trimmed(handle.name, all_lines[-MAX_PROCESSED_ENTRIES:])


def _rewrite_trimmed(name: str, trimmed: list[str]) -> None:
    """Replace the dedup file with its newest entries atomically.

    Truncating the locked handle in place is not crash-safe: a power loss
    mid-truncate loses the whole dedup history and causes duplicate
    notifications. A temp-file swap keeps either the old or the new content,
    never nothing. The stable database lock remains held across the swap, so
    another process cannot read or replace the path until the new inode is
    fully published.
    """
    path = Path(name)
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f"{path.name}.", suffix=".tmp"
    )
    try:
        with contextlib.suppress(OSError):
            os.chmod(tmp_name, path.stat().st_mode & 0o7777)
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            tmp.writelines(trimmed)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def _title_lock_digest(path_name: str, file_name: str) -> str | None:
    """Return the title digest if file_name is a per-title lock of path_name."""
    prefix = f"{path_name}."
    if not (file_name.startswith(prefix) and file_name.endswith(".lock")):
        return None
    digest = file_name[len(prefix) : -len(".lock")]
    if len(digest) == 64 and all(c in "0123456789abcdef" for c in digest):
        return digest
    return None


def prune_title_locks(
    path: Path, known_titles: set[str] | None = None, *, now: float | None = None
) -> int:
    """Best-effort removal of stale per-title lock files.

    Every claimed title creates a ``<path>.<sha256(title)>.lock`` file that
    would otherwise accumulate forever. A lock is removed when its title is
    already committed (present in ``known_titles``) or when the file is older
    than TITLE_LOCK_MAX_AGE_SECONDS, and only when no process currently holds
    it (LOCK_EX|LOCK_NB probe), so a live claim is never disturbed.

    Residual race (documented, accepted): a process that opened the lock file
    just before the unlink proceeds on an unlinked inode. For committed titles
    the claim path re-checks the processed file under the database lock and
    harmlessly declines; for aged-out titles the worst case is a duplicate
    notification, which the claim/commit protocol already tolerates.
    """
    if _fcntl is None:
        return 0
    committed = (
        {sha256(title.encode("utf-8")).hexdigest() for title in known_titles}
        if known_titles
        else set()
    )
    now = time.time() if now is None else now
    removed = 0
    try:
        candidates = list(path.parent.iterdir())
    except OSError:
        return 0
    for entry in candidates:
        digest = _title_lock_digest(path.name, entry.name)
        if digest is None:
            continue
        if digest not in committed:
            try:
                age = now - entry.stat().st_mtime
            except OSError:
                continue
            if age < TITLE_LOCK_MAX_AGE_SECONDS:
                continue
        try:
            handle = entry.open("a+", encoding="utf-8")
        except OSError:
            continue
        try:
            try:
                _fcntl.flock(handle.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB)
            except OSError:
                continue  # another process holds this claim; never prune it
            with contextlib.suppress(OSError):
                entry.unlink()
                removed += 1
        finally:
            _close_locked(handle)
    if removed:
        Log.i(f"Pruned {removed} stale update-title lock file(s).")
    return removed


def load_processed_titles(path: Path) -> set[str]:
    if not path.exists():
        # A pure read must not create state: _locked_file would materialize the
        # database lock beside a file that does not exist yet, so even a dry run
        # wrote into the state directory.
        return set()
    try:
        # Acquire the stable lock before checking/opening the data file so a
        # concurrent first writer or trim cannot change the path between the
        # existence check and the read.
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
    """Release a title claim, tolerating an already-closed handle.

    RunContext.stop() pops and closes claimed handles under file_lock; a race
    with _commit_claimed_update could otherwise hand us a closed fd and turn
    cleanup into an uncaught OSError or ValueError. Cleanup must never crash
    the caller.
    """
    if getattr(claim, "closed", False):
        return
    try:
        _close_locked(claim)
    except (OSError, ValueError) as exc:
        Log.w(f"Ignoring error while releasing update-title claim: {exc}")
        with contextlib.suppress(OSError, ValueError):
            claim.close()


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
