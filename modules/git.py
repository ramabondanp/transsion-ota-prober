import shutil
import subprocess
from pathlib import Path
from typing import List, Optional, Set

from modules.logging import Log


def commit_incremental_update(
    config_path: Path,
    new_incremental: str,
    variant_label: Optional[str] = None,
    extra_paths: Optional[List[Path]] = None,
) -> bool:
    git_path = shutil.which("git")
    if not git_path:
        Log.w("Git executable not found; skipping auto-commit.")
        return False

    try:
        repo_root_result = subprocess.run(
            [git_path, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
            cwd=str(config_path.parent),
        )
    except Exception as exc:
        Log.w(f"Failed to locate Git repository root: {exc}")
        return False

    if repo_root_result.returncode != 0:
        stderr = repo_root_result.stderr.strip() if repo_root_result.stderr else "Unknown error"
        Log.w(f"Could not determine Git repository root ({stderr}); skipping auto-commit.")
        return False

    repo_root = Path(repo_root_result.stdout.strip() or ".")

    paths: List[Path] = [config_path]
    if extra_paths:
        for path in extra_paths:
            if path and path.exists():
                paths.append(path)

    unique_paths: List[Path] = []
    seen: Set[Path] = set()
    for path in paths:
        try:
            resolved = path.resolve()
        except Exception:
            resolved = path
        if resolved in seen:
            continue
        seen.add(resolved)
        unique_paths.append(resolved)

    if not unique_paths:
        Log.i("No files to add for incremental update commit.")
        return False

    add_args: List[str] = []
    for path in unique_paths:
        try:
            add_args.append(str(path.resolve().relative_to(repo_root)))
        except Exception:
            add_args.append(str(path))

    add_cmd = [git_path, "add", "--"] + add_args
    add_result = subprocess.run(add_cmd, capture_output=True, text=True, cwd=str(repo_root))
    if add_result.returncode != 0:
        stderr = add_result.stderr.strip() or add_result.stdout.strip()
        Log.w(f"Failed to stage files for commit: {stderr}")
        return False

    diff_result = subprocess.run(
        [git_path, "diff", "--cached", "--quiet"],
        cwd=str(repo_root),
    )

    if diff_result.returncode == 0:
        Log.i("No staged changes detected; skipping incremental update commit.")
        return False
    if diff_result.returncode not in (0, 1):
        Log.w("Unable to inspect staged changes; skipping incremental update commit.")
        return False

    scope = config_path.stem
    if variant_label:
        scope = f"{scope} ({variant_label})"
    commit_msg = f"{scope}: update incremental to {new_incremental}"

    commit_cmd = [git_path, "commit", "-m", commit_msg]
    commit_result = subprocess.run(commit_cmd, capture_output=True, text=True, cwd=str(repo_root))
    if commit_result.returncode == 0:
        Log.s(f"Committed incremental update: {commit_msg}")
        return True

    stderr = commit_result.stderr.strip() or commit_result.stdout.strip()
    Log.w(f"Git commit failed: {stderr}")
    return False
