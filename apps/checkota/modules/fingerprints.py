from pathlib import Path
from typing import Set

from modules.logging import Log

# Maximum number of entries to keep in the processed updates file.
# Older entries are trimmed to prevent unbounded growth.
MAX_PROCESSED_ENTRIES = 2000


def load_processed_titles(path: Path) -> Set[str]:
    if not path.exists():
        return set()
    try:
        with path.open("r", encoding="utf-8") as handle:
            return {line.strip() for line in handle if line.strip()}
    except Exception as exc:
        Log.e(f"Error reading processed updates file {path}: {exc}")
        return set()


def save_processed_title(path: Path, title: str) -> None:
    try:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{title}\n")
        Log.s(f"Saved new update title to {path}")
        _trim_processed(path)
    except Exception as exc:
        Log.e(f"Failed to save update title to {path}: {exc}")


def _trim_processed(path: Path, max_entries: int = MAX_PROCESSED_ENTRIES) -> None:
    """Trim the processed updates file, keeping only the most recent entries."""
    try:
        with path.open("r", encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) > max_entries:
            with path.open("w", encoding="utf-8") as f:
                f.writelines(lines[-max_entries:])
            Log.i(f"Trimmed {path} to {max_entries} most recent entries.")
    except Exception:
        pass
