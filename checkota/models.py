"""Shared data models for the processing pipeline."""

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

from checkota.manager import Config


@dataclass
class VariantUpdate:
    cfg: Config
    config_path: Path
    variant_label: Optional[str]
    region_name: Optional[str]
    title: str
    url: str
    size: str
    desc: str
    is_new_update: bool
    target_fp: str
    target_incremental: Optional[str]
    sdk_message: str
    data: Dict[str, str]


@dataclass
class PendingNotification:
    msg: str
    device_title: str
    title: str
    is_new_update: bool
