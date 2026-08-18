"""M2 fix — update_config_from_fingerprint is idempotent and round-trip-parses its writes."""

import os
import stat
from pathlib import Path
from textwrap import dedent
from unittest.mock import patch

import yaml

from checkota.manager import Config, update_config_from_fingerprint

FP = "Infinix/X6873-OP/Infinix-X6873:16/BP2A.250605.031.A3/201350016:user/release-keys"


def _write_config(tmp_path: Path) -> Path:
    p = tmp_path / "config-X6873.yml"
    p.write_text(
        dedent(
            """\
            oem: "Infinix"
            product: "X6873-OP"
            device: "Infinix-X6873"
            android_version: "14"
            build_tag: "B"
            incremental: "I"
            model: "Infinix GT 30 Pro"
            """
        ),
        encoding="utf-8",
    )
    return p


def _cfg(p: Path) -> Config:
    return Config.from_yaml(p)[0]


def test_idempotent_second_run_leaves_file_byte_equal(tmp_path):
    """Running update_config_from_fingerprint twice on the same target
    fingerprint must leave the YAML file byte-equal after the second call
    (the second call should be a no-op)."""
    p = _write_config(tmp_path)
    cfg = _cfg(p)

    # First call: writes new values.
    first = update_config_from_fingerprint(p, cfg, FP)
    assert first is True
    after_first = p.read_text(encoding="utf-8")

    # Reload cfg from the just-written file so the comparison is fresh.
    cfg2 = _cfg(p)
    second = update_config_from_fingerprint(p, cfg2, FP)
    after_second = p.read_text(encoding="utf-8")
    assert second is True
    assert after_first == after_second, "Second update must be a no-op (file unchanged)"


def test_post_write_yaml_round_trip_parses(tmp_path):
    """After update_config_from_fingerprint writes, the file must re-parse
    as a valid YAML dict (M2 smoke test)."""
    p = _write_config(tmp_path)
    cfg = _cfg(p)
    assert update_config_from_fingerprint(p, cfg, FP) is True
    reparsed = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert isinstance(reparsed, dict)
    assert reparsed["android_version"] == "16"
    assert reparsed["build_tag"] == "BP2A.250605.031.A3"
    assert reparsed["incremental"] == "201350016"


def test_atomic_replace_preserves_config_permissions(tmp_path):
    p = _write_config(tmp_path)
    os.chmod(p, 0o644)

    assert update_config_from_fingerprint(p, _cfg(p), FP) is True
    assert stat.S_IMODE(p.stat().st_mode) == 0o644


def test_config_lock_failure_returns_false(tmp_path):
    p = _write_config(tmp_path)
    with patch("checkota.manager._config_lock", side_effect=OSError("busy")):
        assert update_config_from_fingerprint(p, _cfg(p), FP) is False
