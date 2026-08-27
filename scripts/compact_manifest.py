#!/usr/bin/env python3
"""Generate the canonical manifest for compact repository configs."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from checkota.manager import Config


def config_paths(config_dir: Path) -> list[Path]:
    return sorted(
        {
            path
            for pattern in ("config-*.yml", "config-*.yaml")
            for path in config_dir.glob(pattern)
        }
    )


def rows_for_paths(paths: list[Path]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for path in paths:
        for position, config in enumerate(Config.from_yaml(path)):
            rows.append(
                {
                    "source_config": str(path),
                    "position": position,
                    "oem": config.oem,
                    "product": config.product,
                    "device": config.device,
                    "android_version": config.android_version,
                    "build_tag": config.build_tag,
                    "incremental": config.incremental,
                    "model": config.model,
                    "fingerprint": config.fingerprint(),
                }
            )
    return rows


def manifest_rows(config_dir: Path) -> list[dict[str, object]]:
    return rows_for_paths(config_paths(config_dir))


def serialize_manifest(rows: list[dict[str, object]]) -> bytes:
    return b"".join(
        json.dumps(row, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        + b"\n"
        for row in rows
    )


def manifest_bytes(config_dir: Path) -> bytes:
    return serialize_manifest(manifest_rows(config_dir))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config_dir", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    paths = config_paths(args.config_dir)
    rows = rows_for_paths(paths)
    payload = serialize_manifest(rows)
    if args.output is None:
        print(payload.decode("utf-8"), end="")
    else:
        args.output.write_bytes(payload)
    print(
        f"files={len(paths)} "
        f"entries={len(rows)} "
        f"sha256={hashlib.sha256(payload).hexdigest()}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
