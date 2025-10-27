import shutil
from typing import List

from modules.logging import Log


def check_cmd(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def check_cmds(cmds: List[str]) -> bool:
    missing = [cmd for cmd in cmds if not check_cmd(cmd)]
    if missing:
        Log.e(f"Missing required command(s): {', '.join(missing)}")
        return False
    return True
