"""Crash-safe, per-title notification outbox for file-backed OTA updates.

A notification is staged *before* advancing its YAML config. It is removed only
once its title has been committed to the processed-title database. Sending is
at-least-once: a crash between Telegram accepting a send and the title commit
can cause a duplicate, but cannot silently lose the update.
"""

import json
import os
import tempfile
from hashlib import sha256
from pathlib import Path

from checkota.logging import Log
from checkota.manager import Config
from checkota.models import PendingNotification


def _directory(processed_path: Path) -> Path:
    return processed_path.with_name(processed_path.name + ".outbox")


def _entry_path(processed_path: Path, title: str) -> Path:
    digest = sha256(title.encode("utf-8")).hexdigest()
    return _directory(processed_path) / f"{digest}.json"


def _decode(raw: bytes, path: Path, processed_path: Path) -> PendingNotification | None:
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
    # A crash can leave a record before the YAML rewrite, or preserve a ready
    # record while the config rename is lost or later rolled back. Either way,
    # replay only while the on-disk target still matches. This also recovers a
    # rewrite completed before its ready marker could be published.
    try:
        configs = Config.from_yaml(Path(data["config_path"]))
    except (OSError, TypeError, ValueError):
        return None
    if not any(cfg.fingerprint() == data["target_fp"] for cfg in configs):
        return None
    return PendingNotification(
        msg=data["msg"],
        device_title=data["device_title"],
        title=data["title"],
        is_new_update=True,
    )


def load_pending_notifications(processed_path: Path) -> list[PendingNotification]:
    directory = _directory(processed_path)
    if not directory.exists():
        return []
    notes = []
    for entry in sorted(directory.glob("*.json")):
        note = _decode(entry.read_bytes(), entry, processed_path)
        if note is not None:
            notes.append(note)
    return notes


def has_pending_notification(processed_path: Path, title: str) -> bool:
    """Whether a persisted title is backed by the target YAML fingerprint."""
    path = _entry_path(processed_path, title)
    try:
        return _decode(path.read_bytes(), path, processed_path) is not None
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
