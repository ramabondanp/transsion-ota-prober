"""Crash-safe, per-title notification outbox for file-backed OTA updates.

A notification is staged *before* advancing its YAML config. It is removed only
once its title has been committed to the processed-title database. Sending is
at-least-once: a crash between Telegram accepting a send and the title commit
can cause a duplicate, but cannot silently lose the update. A ready record is
proof of a completed rewrite: later config updates or rollbacks do not cancel
its delivery. Unconfirmed records still need a matching on-disk fingerprint.
"""

import json
import os
import tempfile
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from checkota.fingerprints import claim_processed_title, release_processed_claim
from checkota.logging import Log
from checkota.manager import Config
from checkota.models import PendingNotification


def _directory(processed_path: Path) -> Path:
    return processed_path.with_name(processed_path.name + ".outbox")


def _entry_path(processed_path: Path, title: str) -> Path:
    digest = sha256(title.encode("utf-8")).hexdigest()
    return _directory(processed_path) / f"{digest}.json"


@dataclass(frozen=True)
class _Entry:
    note: PendingNotification
    config_path: Path
    target_fp: str
    ready: bool

    def is_recoverable(self) -> bool:
        # A confirmed rewrite is a historical delivery obligation, even if the
        # config has since advanced, rolled back, moved, or been deleted.
        if self.ready:
            return True
        try:
            configs = Config.from_yaml(self.config_path)
        except (OSError, TypeError, ValueError):
            return False
        return any(cfg.fingerprint() == self.target_fp for cfg in configs)


def _decode(raw: bytes, path: Path, processed_path: Path) -> _Entry:
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("Invalid notification outbox JSON") from exc
    if (
        not isinstance(data, dict)
        or not all(
            isinstance(data.get(key), str) and data[key]
            for key in ("title", "msg", "device_title", "config_path", "target_fp")
        )
        or not isinstance(data.get("ready"), bool)
    ):
        raise ValueError("Invalid notification outbox entry")
    if _entry_path(processed_path, data["title"]) != path:
        raise ValueError(f"Notification outbox entry has wrong filename: {path}")
    return _Entry(
        note=PendingNotification(
            msg=data["msg"],
            device_title=data["device_title"],
            title=data["title"],
            is_new_update=True,
        ),
        config_path=Path(data["config_path"]),
        target_fp=data["target_fp"],
        ready=data["ready"],
    )


def _confirm_entry(
    path: Path, processed_path: Path, title: str
) -> PendingNotification | None:
    """Recover a completed rewrite under its title claim, or fail closed.

    Reread after claiming: another run may have confirmed, sent, or removed the
    entry since the directory scan. A busy title raises TitleClaimBusy so startup
    cannot advance configs past an unconfirmed record owned by a live worker.
    """
    claim = claim_processed_title(processed_path, title)
    if claim is None:  # Already committed: no delivery obligation remains.
        return None
    try:
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return None
        entry = _decode(raw, path, processed_path)
        if not entry.is_recoverable():
            return None
        if not entry.ready:
            # The previous process may have failed its config directory fsync.
            # Establish durability before making the delivery unconditional.
            with entry.config_path.open("rb") as handle:
                os.fsync(handle.fileno())
            _sync_directory(entry.config_path.parent)
            if not stage_pending_notification(
                processed_path,
                entry.note,
                entry.config_path,
                entry.target_fp,
                ready=True,
            ):
                raise OSError(f"Could not confirm completed rewrite for {title}")
        return entry.note
    finally:
        release_processed_claim(claim)


def load_pending_notifications(
    processed_path: Path, *, confirm: bool = False
) -> list[PendingNotification]:
    """Read pending work; startup uses confirm=True before allowing OTA checks.

    Confirmation durably promotes recovered rewrites so another OTA cannot hide
    their pending notification. Plain reads never promote entries.
    """
    directory = _directory(processed_path)
    if not directory.exists():
        return []
    notes = []
    for entry in sorted(directory.glob("*.json")):
        try:
            raw = entry.read_bytes()
        except FileNotFoundError:
            continue  # Another process finished delivery after our scan.
        decoded = _decode(raw, entry, processed_path)
        if confirm and not decoded.ready:
            note = _confirm_entry(entry, processed_path, decoded.note.title)
        else:
            note = decoded.note if decoded.is_recoverable() else None
        if note is not None:
            notes.append(note)
    return notes


def has_pending_notification(processed_path: Path, title: str) -> bool:
    """Whether a title is confirmed or backed by its target YAML fingerprint."""
    path = _entry_path(processed_path, title)
    try:
        return _decode(path.read_bytes(), path, processed_path).is_recoverable()
    except FileNotFoundError:
        return False


def stage_pending_notification(
    processed_path: Path,
    note: PendingNotification,
    config_path: Path,
    target_fp: str,
    *,
    ready: bool = False,
) -> bool:
    """Publish a complete record, returning False without rewriting on I/O failure.

    Callers hold the processed-title claim, so two writers for a title cannot
    replace one another. fsync the entry and its directory before the YAML
    rewrite to preserve the recovery record across a crash.
    """
    path = _entry_path(processed_path, note.title)
    tmp_path = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
        tmp_path = Path(name)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "title": note.title,
                    "msg": note.msg,
                    "device_title": note.device_title,
                    "config_path": str(config_path.resolve()),
                    "target_fp": target_fp,
                    "ready": ready,
                },
                handle,
                ensure_ascii=False,
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        tmp_path = None
        _sync_directory(path.parent)
        return True
    except (OSError, ValueError, UnicodeError) as exc:
        Log.e(f"Could not persist notification outbox entry: {exc}")
        return False
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


def remove_pending_notification(processed_path: Path, title: str) -> bool:
    path = _entry_path(processed_path, title)
    try:
        path.unlink(missing_ok=True)
        if path.parent.exists():
            _sync_directory(path.parent)
        return True
    except OSError as exc:
        Log.e(f"Could not remove notification outbox entry {path}: {exc}")
        return False


def _sync_directory(path: Path) -> None:
    if os.name == "nt":  # Windows has no portable directory fsync
        return
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
