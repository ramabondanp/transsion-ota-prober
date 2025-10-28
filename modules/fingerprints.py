from pathlib import Path
from typing import Set

from modules.logging import Log


def load_processed_titles(path: Path) -> Set[str]:
    if not path.exists():
        return set()
    try:
        with path.open("r") as handle:
            return {line.strip() for line in handle if line.strip()}
    except Exception as exc:
        Log.e(f"Error reading processed updates file {path}: {exc}")
        return set()


def save_processed_title(path: Path, title: str) -> None:
    try:
        with path.open("a") as handle:
            handle.write(f"{title}\n")
        Log.s(f"Saved new update title to {path}")
    except Exception as exc:
        Log.e(f"Failed to save update title to {path}: {exc}")
