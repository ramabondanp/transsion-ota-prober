"""Shared data models for the processing pipeline."""

from dataclasses import dataclass
from pathlib import Path

from checkota.manager import Config


@dataclass
class VariantUpdate:
    cfg: Config
    config_path: Path
    variant_label: str | None
    region_name: str | None
    title: str
    url: str
    size: str
    desc: str
    is_new_update: bool
    target_fp: str
    target_incremental: str | None
    sdk_message: str
    data: dict[str, str]


@dataclass
class PendingNotification:
    msg: str
    device_title: str
    title: str
    is_new_update: bool
