from dataclasses import replace
from pathlib import Path

from checkota.manager import (
    Config,
    _effective_region_values,
    _resolve_update_target,
    update_config_from_fingerprint,
)


def _write_compact_config(path: Path) -> None:
    path.write_text(
        """\
oem: "Infinix"
product_base: "X6873"
model: "Infinix GT 30 Pro"
android_version: "16"
regions:
  OP: "201500011"
  OP-M1: "201500012"
""",
        encoding="utf-8",
    )


def _configs(path: Path) -> list[Config]:
    return Config.from_yaml(path)


def test_update_target_is_resolved_by_exact_region_key(tmp_path):
    path = tmp_path / "config.yml"
    _write_compact_config(path)
    cfg = _configs(path)[0]

    assert _resolve_update_target(
        {
            "oem": "Infinix",
            "product_base": "X6873",
            "model": "Infinix GT 30 Pro",
            "android_version": "16",
            "regions": {"OP": "201500011", "OP-M1": "201500012"},
        },
        cfg,
        path,
    ) == (True, "OP")


def test_multi_part_region_is_not_truncated_for_update_target(tmp_path):
    path = tmp_path / "config.yml"
    _write_compact_config(path)
    cfg = _configs(path)[1]
    data = {
        "oem": "Infinix",
        "product_base": "X6873",
        "model": "Infinix GT 30 Pro",
        "android_version": "16",
        "regions": {"OP": "201500011", "OP-M1": "201500012"},
    }

    assert _resolve_update_target(data, cfg, path) == (True, "OP-M1")
    assert _effective_region_values(data, "OP-M1", path) == {
        "oem": "Infinix",
        "product": "X6873-OP-M1",
        "device": "Infinix-X6873",
        "android_version": "16",
        "build_tag": "BP2A.250605.031.A3",
        "incremental": "201500012",
    }


def test_inconsistent_stable_region_identity_fails_closed(tmp_path):
    path = tmp_path / "config.yml"
    _write_compact_config(path)
    cfg = replace(_configs(path)[0], variant="OP-M1")
    data = {
        "oem": "Infinix",
        "product_base": "X6873",
        "model": "Infinix GT 30 Pro",
        "android_version": "16",
        "regions": {"OP": "201500011", "OP-M1": "201500012"},
    }

    assert _resolve_update_target(data, cfg, path) == (False, None)


def test_removed_region_fails_closed(tmp_path):
    path = tmp_path / "config.yml"
    _write_compact_config(path)
    cfg = _configs(path)[0]
    data = {
        "oem": "Infinix",
        "product_base": "X6873",
        "model": "Infinix GT 30 Pro",
        "android_version": "16",
        "regions": {"IN": "201500011"},
    }

    assert _resolve_update_target(data, cfg, path) == (False, None)


def test_renamed_region_fails_closed(tmp_path):
    path = tmp_path / "config.yml"
    _write_compact_config(path)
    cfg = _configs(path)[0]
    data = {
        "oem": "Infinix",
        "product_base": "X6873",
        "model": "Infinix GT 30 Pro",
        "android_version": "16",
        "regions": {"GLOBAL": "201500011", "OP-M1": "201500012"},
    }

    assert _resolve_update_target(data, cfg, path) == (False, None)


def test_changed_on_disk_identity_fails_closed(tmp_path):
    path = tmp_path / "config.yml"
    _write_compact_config(path)
    cfg = _configs(path)[0]
    data = {
        "oem": "Infinix",
        "product_base": "X6873B",
        "model": "Infinix GT 30 Pro",
        "android_version": "16",
        "regions": {"OP": "201500011", "OP-M1": "201500012"},
    }

    assert _resolve_update_target(data, cfg, path) == (False, None)


def test_case_insensitive_duplicate_region_fails_closed(tmp_path):
    path = tmp_path / "config.yml"
    _write_compact_config(path)
    cfg = _configs(path)[0]
    data = {
        "oem": "Infinix",
        "product_base": "X6873",
        "model": "Infinix GT 30 Pro",
        "android_version": "16",
        "regions": {"OP": "201500011", "op": "201500013"},
    }

    assert _resolve_update_target(data, cfg, path) == (False, None)


def test_mismatched_target_fingerprint_is_rejected_before_write(tmp_path):
    path = tmp_path / "config.yml"
    _write_compact_config(path)
    cfg = _configs(path)[0]
    before = path.read_bytes()
    target = (
        "Infinix/X6873-IN/Infinix-X6873:17/"
        "CUSTOM.TAG/201500099:user/release-keys"
    )

    assert update_config_from_fingerprint(path, cfg, target) is False
    assert path.read_bytes() == before


def test_matching_compact_fingerprint_is_a_successful_noop(tmp_path):
    path = tmp_path / "config.yml"
    _write_compact_config(path)
    cfg = _configs(path)[0]
    before = path.read_bytes()

    assert update_config_from_fingerprint(path, cfg, cfg.fingerprint()) is True
    assert path.read_bytes() == before


def test_non_noop_compact_update_does_not_write_before_phase_4(tmp_path):
    path = tmp_path / "config.yml"
    _write_compact_config(path)
    cfg = _configs(path)[0]
    before = path.read_bytes()
    target = (
        "Infinix/X6873-OP/Infinix-X6873:17/"
        "CUSTOM.TAG/201500099:user/release-keys"
    )

    assert update_config_from_fingerprint(path, cfg, target) is False
    assert path.read_bytes() == before
