"""Shared data models for the processing pipeline."""

from dataclasses import dataclass
from pathlib import Path

from checkota.manager import Config


@dataclass
class RegionUpdate:
    cfg: Config
    config_path: Path
    region_name: str | None
    title: str
    url: str
    size: str
    desc: str
    is_new_update: bool
    target_fp: str
    target_incremental: str | None
    sdk_message: str | None
    data: dict[str, str]


@dataclass
class PendingNotification:
    msg: str
    device_title: str
    title: str
    is_new_update: bool
