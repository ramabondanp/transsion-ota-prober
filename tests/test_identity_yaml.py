import argparse
from pathlib import Path
from unittest.mock import patch

import yaml

from checkota.manager import (
    Config,
    fingerprint_identity_matches_config,
    update_config_from_fingerprint,
)
from checkota.models import VariantUpdate
from checkota.processor import apply_update_actions
from checkota.runtime import RunContext

FP = "Infinix/X6873-OP/Infinix-X6873:16/BP2A.250605.031.A3/201350016:user/release-keys"


def _config(path: Path) -> Config:
    return _legacy_config(path)


def _legacy_config(path: Path, index: int = 0) -> Config:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    variants = data.get("variants")
    if isinstance(variants, list):
        merged = {**data, **variants[index]}
        variant_index = index
    else:
        merged = data
        variant_index = None
    return Config(
        build_tag=merged["build_tag"],
        incremental=merged["incremental"],
        android_version=merged["android_version"],
        model=merged["model"],
        device=merged["device"],
        oem=merged["oem"],
        product=merged["product"],
        variant=merged.get("variant"),
        variant_index=variant_index,
    )


def _write_single_config(path: Path) -> None:
    path.write_text(
        """\
oem: "Infinix"
product: "X6873-OP"
device: "Infinix-X6873"
android_version: "14"
build_tag: "OLD#TAG" # retain this comment
incremental: 'OLD#INCREMENTAL' # retain this comment too
not_android_version: "must remain unchanged"
model: "Infinix GT 30 Pro"
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
        path.read_text(encoding="utf-8").replace("X6873-OP", "X6873-IN"),
        encoding="utf-8",
    )
    before = path.read_bytes()

    assert update_config_from_fingerprint(path, cfg, FP) is False
    assert path.read_bytes() == before


def test_yaml_replacement_quotes_hashes_preserves_comments_and_crlf(tmp_path):
    path = tmp_path / "config.yml"
    content = """\
oem: "Infinix" # identity comment
product: "X6873-OP"
device: "Infinix-X6873"
android_version: "14" # version comment
build_tag: "OLD#TAG" # build comment
incremental: 'OLD#INCREMENTAL' # incremental comment
not_android_version: "must remain unchanged"
model: "Infinix GT 30 Pro"
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
    assert 'not_android_version: "must remain unchanged"' in text

    parsed = yaml.safe_load(text)
    assert parsed["android_version"] == "16"
    assert parsed["build_tag"] == 'BP2A#250605."031'
    assert parsed["incremental"] == "201350#016"


def test_variant_identity_and_effective_values_are_preserved(tmp_path):
    path = tmp_path / "config.yml"
    path.write_text(
        """\
oem: "Infinix"
device: "Infinix-X6873"
model: "Infinix GT 30 Pro"
variants:
  - variant: "Global"
    android_version: "14"
    build_tag: "GLOBAL"
    product: "X6873-OP"
    incremental: "GLOBAL-I"
  - variant: "India"
    android_version: "14"
    build_tag: "INDIA"
    product: "X6873-IN"
    incremental: "INDIA-I"
""",
        encoding="utf-8",
    )
    cfg = _legacy_config(path, 1)
    target = FP.replace("X6873-OP", "X6873-IN")

    assert update_config_from_fingerprint(path, cfg, target) is True
    parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert parsed["variants"][0]["incremental"] == "GLOBAL-I"
    assert parsed["variants"][1]["android_version"] == "16"
    assert parsed["variants"][1]["build_tag"] == "BP2A.250605.031.A3"
    assert parsed["variants"][1]["incremental"] == "201350016"


def test_variant_first_key_on_sequence_marker_is_rewritten_without_duplicate(tmp_path):
    path = tmp_path / "config.yml"
    path.write_text(
        """\
oem: "Infinix"
product: "X6873-OP"
device: "Infinix-X6873"
model: "Infinix GT 30 Pro"
variants:
  - android_version: "14" # keep marker comment
    build_tag: "OLD"
    incremental: "OLD-I"
""",
        encoding="utf-8",
    )

    assert update_config_from_fingerprint(path, _config(path), FP) is True

    text = path.read_text(encoding="utf-8")
    assert text.count("android_version:") == 1
    assert '  - android_version: "16" # keep marker comment' in text
    parsed = yaml.safe_load(text)
    assert parsed["variants"][0]["android_version"] == "16"


def test_indentless_variant_sequence_is_updated_in_place(tmp_path):
    path = tmp_path / "config.yml"
    path.write_text(
        """\
oem: "Infinix"
device: "Infinix-X6873"
model: "Infinix GT 30 Pro"
variants:
- variant: "Global"
  product: "X6873-OP"
  android_version: "14"
  build_tag: "OLD"
  incremental: "OLD-I"
- variant: "India"
  product: "X6873-IN"
  android_version: "14"
  build_tag: "INDIA"
  incremental: "INDIA-I"
""",
        encoding="utf-8",
    )

    assert update_config_from_fingerprint(path, _config(path), FP) is True

    text = path.read_text(encoding="utf-8")
    assert "\n- variant:" in text
    parsed = yaml.safe_load(text)
    assert parsed["variants"][0]["incremental"] == "201350016"
    assert parsed["variants"][1]["incremental"] == "INDIA-I"


def test_duplicate_variant_keys_fail_closed_without_writing(tmp_path):
    path = tmp_path / "config.yml"
    path.write_text(
        """\
oem: "Infinix"
product: "X6873-OP"
device: "Infinix-X6873"
model: "Infinix GT 30 Pro"
variants:
  - android_version: "13"
    android_version: "14"
    build_tag: "OLD"
    incremental: "OLD-I"
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
        variant_index=0,
    )

    assert update_config_from_fingerprint(path, cfg, FP) is False
    assert path.read_bytes() == before


def test_stale_variant_index_uses_label_for_shared_identity(tmp_path):
    path = tmp_path / "config.yml"
    original = """\
oem: "Infinix"
product: "X6873-OP"
device: "Infinix-X6873"
model: "Infinix GT 30 Pro"
variants:
  - variant: "Alpha"
    android_version: "14"
    build_tag: "ALPHA"
    incremental: "ALPHA-I"
  - variant: "Beta"
    android_version: "15"
    build_tag: "BETA"
    incremental: "BETA-I"
"""
    reordered = """\
oem: "Infinix"
product: "X6873-OP"
device: "Infinix-X6873"
model: "Infinix GT 30 Pro"
variants:
  - variant: "Beta"
    android_version: "15"
    build_tag: "BETA"
    incremental: "BETA-I"
  - variant: "Alpha"
    android_version: "14"
    build_tag: "ALPHA"
    incremental: "ALPHA-I"
"""
    path.write_text(original, encoding="utf-8")
    cfg = _legacy_config(path, 1)
    path.write_text(reordered, encoding="utf-8")

    assert update_config_from_fingerprint(path, cfg, FP) is True

    variants = yaml.safe_load(path.read_text(encoding="utf-8"))["variants"]
    assert variants[0]["variant"] == "Beta"
    assert variants[0]["incremental"] == "201350016"
    assert variants[1]["incremental"] == "ALPHA-I"


def test_stale_variant_index_uses_current_build_for_shared_identity(tmp_path):
    path = tmp_path / "config.yml"
    original = """\
oem: "Infinix"
product: "X6873-OP"
device: "Infinix-X6873"
model: "Infinix GT 30 Pro"
variants:
  - android_version: "14"
    build_tag: "ALPHA"
    incremental: "ALPHA-I"
  - android_version: "15"
    build_tag: "BETA"
    incremental: "BETA-I"
"""
    reordered = """\
oem: "Infinix"
product: "X6873-OP"
device: "Infinix-X6873"
model: "Infinix GT 30 Pro"
variants:
  - android_version: "15"
    build_tag: "BETA"
    incremental: "BETA-I"
  - android_version: "14"
    build_tag: "ALPHA"
    incremental: "ALPHA-I"
"""
    path.write_text(original, encoding="utf-8")
    cfg = _legacy_config(path, 1)
    path.write_text(reordered, encoding="utf-8")

    assert update_config_from_fingerprint(path, cfg, FP) is True

    variants = yaml.safe_load(path.read_text(encoding="utf-8"))["variants"]
    assert variants[0]["incremental"] == "201350016"
    assert variants[1]["incremental"] == "ALPHA-I"


def test_variant_index_is_not_used_when_shared_identity_is_ambiguous(tmp_path):
    path = tmp_path / "config.yml"
    path.write_text(
        """\
oem: "Infinix"
product: "X6873-OP"
device: "Infinix-X6873"
model: "Infinix GT 30 Pro"
variants:
  - android_version: "14"
    build_tag: "SAME"
    incremental: "SAME-I"
  - android_version: "14"
    build_tag: "SAME"
    incremental: "SAME-I"
""",
        encoding="utf-8",
    )
    before = path.read_bytes()
    cfg = _legacy_config(path, 1)

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
    update = VariantUpdate(
        cfg=cfg,
        config_path=tmp_path / "config.yml",
        variant_label=None,
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
