import argparse
from pathlib import Path
from unittest.mock import patch

import yaml

from checkota.manager import (
    Config,
    fingerprint_identity_matches_config,
    update_config_from_fingerprint,
)
from checkota.models import RegionUpdate
from checkota.processor import apply_update_actions
from checkota.runtime import RunContext

FP = "Infinix/X6873-OP/Infinix-X6873:16/BP2A.250605.031.A3/201350016:user/release-keys"


def _config(path: Path) -> Config:
    return Config.from_yaml(path)[0]


def _write_single_config(path: Path) -> None:
    path.write_text(
        """\
oem: "Infinix"
model: "Infinix GT 30 Pro"
product_base: "X6873"
android_version: "14"
regions:
  OP:
    build_tag: "OLD#TAG" # retain this comment
    incremental: 'OLD#INCREMENTAL' # retain this comment too
  IN: "OTHER"
""",
        encoding="utf-8",
    )


def test_target_identity_must_match_config_before_update(tmp_path):
    path = tmp_path / "config.yml"
    _write_single_config(path)
    before = path.read_bytes()
    cfg = _config(path)

    mismatched = FP.replace("X6873-OP", "X6873-IN")
    assert fingerprint_identity_matches_config(cfg, mismatched) is False
    assert update_config_from_fingerprint(path, cfg, mismatched) is False
    assert path.read_bytes() == before


def test_changed_on_disk_identity_is_rejected(tmp_path):
    path = tmp_path / "config.yml"
    _write_single_config(path)
    cfg = _config(path)
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            'product_base: "X6873"', 'product_base: "X6874"'
        ),
        encoding="utf-8",
    )
    before = path.read_bytes()

    assert update_config_from_fingerprint(path, cfg, FP) is False
    assert path.read_bytes() == before


def test_yaml_replacement_quotes_hashes_preserves_comments_and_crlf(tmp_path):
    path = tmp_path / "config.yml"
    content = """\
oem: "Infinix" # identity comment
product_base: "X6873"
android_version: "14" # version comment
model: "Infinix GT 30 Pro"
regions:
  OP:
    build_tag: "OLD#TAG" # build comment
    incremental: 'OLD#INCREMENTAL' # incremental comment
  IN: "OTHER"
""".replace("\n", "\r\n")
    path.write_bytes(content.encode("utf-8"))
    cfg = _config(path)
    target = (
        "Infinix/X6873-OP/Infinix-X6873:16/"
        'BP2A#250605."031/201350#016:user/release-keys'
    )

    assert update_config_from_fingerprint(path, cfg, target) is True
    updated = path.read_bytes()
    assert b"\r\n" in updated
    assert b"\n" not in updated.replace(b"\r\n", b"")
    text = updated.decode("utf-8")
    assert "# identity comment" in text
    assert "# version comment" in text
    assert "# build comment" in text
    assert "# incremental comment" in text

    parsed = yaml.safe_load(text)
    assert parsed["android_version"] == "14"
    assert parsed["regions"]["OP"] == {
        "android_version": "16",
        "build_tag": 'BP2A#250605."031',
        "incremental": "201350#016",
    }
    assert parsed["regions"]["IN"] == "OTHER"


def test_region_identity_and_effective_values_are_preserved(tmp_path):
    path = tmp_path / "config.yml"
    path.write_text(
        """\
oem: "Infinix"
product_base: "X6873"
model: "Infinix GT 30 Pro"
android_version: "14"
regions:
  OP: "GLOBAL-I"
  IN:
    build_tag: "INDIA"
    incremental: "INDIA-I"
""",
        encoding="utf-8",
    )
    cfg = Config.from_yaml(path)[1]
    target = FP.replace("X6873-OP", "X6873-IN")

    assert update_config_from_fingerprint(path, cfg, target) is True
    parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert parsed["regions"]["OP"] == "GLOBAL-I"
    assert parsed["regions"]["IN"] == {
        "android_version": "16",
        "incremental": "201350016",
    }


def test_region_first_key_is_rewritten_without_duplicate(tmp_path):
    path = tmp_path / "config.yml"
    path.write_text(
        """\
oem: "Infinix"
product_base: "X6873"
android_version: "14"
model: "Infinix GT 30 Pro"
regions:
  OP:
    android_version: "14" # keep marker comment
    build_tag: "OLD"
    incremental: "OLD-I"
  IN: "INDIA-I"
""",
        encoding="utf-8",
    )

    assert update_config_from_fingerprint(path, _config(path), FP) is True

    text = path.read_text(encoding="utf-8")
    assert text.count("android_version:") == 2
    assert '    android_version: "16" # keep marker comment' in text
    parsed = yaml.safe_load(text)
    assert parsed["regions"]["OP"] == {
        "android_version": "16",
        "incremental": "201350016",
    }


def test_multiple_regions_update_only_the_target(tmp_path):
    path = tmp_path / "config.yml"
    path.write_text(
        """\
oem: "Infinix"
product_base: "X6873"
model: "Infinix GT 30 Pro"
android_version: "14"
regions:
  OP:
    build_tag: "OLD"
    incremental: "OLD-I"
  IN:
    build_tag: "INDIA"
    incremental: "INDIA-I"
""",
        encoding="utf-8",
    )

    assert update_config_from_fingerprint(path, _config(path), FP) is True

    text = path.read_text(encoding="utf-8")
    parsed = yaml.safe_load(text)
    assert parsed["regions"]["OP"] == {
        "android_version": "16",
        "incremental": "201350016",
    }
    assert parsed["regions"]["IN"] == {
        "build_tag": "INDIA",
        "incremental": "INDIA-I",
    }


def test_duplicate_region_keys_fail_closed_without_writing(tmp_path):
    path = tmp_path / "config.yml"
    path.write_text(
        """\
oem: "Infinix"
product_base: "X6873"
model: "Infinix GT 30 Pro"
android_version: "14"
regions:
  OP:
    incremental: "OLD-I"
    incremental: "DUPLICATE"
""",
        encoding="utf-8",
    )
    before = path.read_bytes()
    cfg = Config(
        build_tag="OLD",
        incremental="OLD-I",
        android_version="14",
        model="Infinix GT 30 Pro",
        device="Infinix-X6873",
        oem="Infinix",
        product="X6873-OP",
        region="OP",
    )

    assert update_config_from_fingerprint(path, cfg, FP) is False
    assert path.read_bytes() == before


def test_processor_rejects_identity_mismatch_before_actions(tmp_path):
    cfg = Config(
        build_tag="B",
        incremental="I",
        android_version="14",
        model="Infinix GT 30 Pro",
        device="Infinix-X6873",
        oem="Infinix",
        product="X6873-OP",
    )
    update = RegionUpdate(
        cfg=cfg,
        config_path=tmp_path / "config.yml",
        region_name=None,
        title="OTA",
        url="https://example.test/ota.zip",
        size="1",
        desc="description",
        is_new_update=True,
        target_fp=FP.replace("X6873-OP", "X6873-IN"),
        target_incremental="201350016",
        sdk_message="Android 16",
        data={},
    )
    ctx = RunContext(
        env={},
        processed_path=tmp_path / "processed.txt",
        processed_titles=set(),
        dry_run=False,
    )

    with (
        patch("checkota.processor.update_config_from_fingerprint") as update_config,
        patch("checkota.processor.create_notifier") as create_notifier,
        patch("checkota.processor._claim_new_update") as claim,
    ):
        result = apply_update_actions(ctx, update, argparse.Namespace())

    assert result == 1
    update_config.assert_not_called()
    create_notifier.assert_not_called()
    claim.assert_not_called()
