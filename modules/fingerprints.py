from pathlib import Path
from typing import Set

from modules.logging import Log


def load_processed_fingerprints(path: Path) -> Set[str]:
    if not path.exists():
        return set()
    try:
        with path.open("r") as handle:
            return {line.strip() for line in handle if line.strip()}
    except Exception as exc:
        Log.e(f"Error reading processed fingerprints file {path}: {exc}")
        return set()


def save_processed_fingerprint(path: Path, fingerprint: str) -> None:
    try:
        with path.open("a") as handle:
            handle.write(f"{fingerprint}\n")
        Log.s(f"Saved new fingerprint to {path}")
    except Exception as exc:
        Log.e(f"Failed to save fingerprint to {path}: {exc}")
