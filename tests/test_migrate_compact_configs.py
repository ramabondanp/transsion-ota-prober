import runpy
from pathlib import Path

import pytest
import yaml


_MIGRATOR = runpy.run_path("scripts/migrate_compact_configs.py")
LegacyRegion = _MIGRATOR["LegacyRegion"]
load_legacy_regions = _MIGRATOR["load_legacy_regions"]
migrate_text = _MIGRATOR["migrate_text"]
migrate_directory = _MIGRATOR["migrate_directory"]


def test_legacy_variants_are_merged_and_region_order_is_preserved(tmp_path):
    path = tmp_path / "config.yml"
    path.write_text(
        """\
oem: "Infinix"
device: "Infinix-X1"
model: "Example Phone"
variants:
  - product: "X1-OP"
    android_version: "16"
    build_tag: "BP2A.250605.031.A3"
    incremental: "OP"
  - product: "X1-EU"
    android_version: "15"
    build_tag: "AP3A.240905.015.A2"
    incremental: "EU"
""",
        encoding="utf-8",
    )

    regions = load_legacy_regions(path)
    output = migrate_text(regions)
    parsed = yaml.safe_load(output)

    assert list(parsed["regions"]) == ["OP", "EU"]
    assert parsed["regions"]["OP"] == "OP"
    assert parsed["regions"]["EU"] == {
        "android_version": "15",
        "incremental": "EU",
    }


def test_migration_keeps_only_required_overrides_and_special_cases():
    regions = [
        LegacyRegion(
            oem="Infinix",
            product="X6857-OP",
            product_base="X6857",
            region="OP",
            device="Infinix-X6857",
            model="NOTE 50X",
            android_version="16",
            build_tag="BP2A.250605.031.A3",
            incremental="OP",
        ),
        LegacyRegion(
            oem="Infinix",
            product="X6857B-IN",
            product_base="X6857B",
            region="IN",
            device="Infinix-X6857B",
            model="NOTE 50X",
            android_version="16",
            build_tag="BP2A.250605.031.A3",
            incremental="IN",
        ),
        LegacyRegion(
            oem="Infinix",
            product="X6857-RU",
            product_base="X6857",
            region="RU",
            device="Infinix-X6857",
            model="NOTE 50X",
            android_version="16",
            build_tag="CUSTOM.TAG",
            incremental="RU",
        ),
    ]

    parsed = yaml.safe_load(migrate_text(regions))

    assert parsed["product_base"] == "X6857"
    assert parsed["regions"]["OP"] == "OP"
    assert parsed["regions"]["IN"] == {
        "product_base": "X6857B",
        "incremental": "IN",
    }
    assert parsed["regions"]["RU"] == {
        "build_tag": "CUSTOM.TAG",
        "incremental": "RU",
    }
    assert migrate_text(regions).count("build_tag:") == 1


def test_migrated_scalar_values_are_quoted():
    region = LegacyRegion(
        oem="TECNO",
        product="T1-OP",
        product_base="T1",
        region="OP",
        device="TECNO-T1",
        model="Example",
        android_version="15",
        build_tag="AP3A.240905.015.A2",
        incremental="001: #value",
    )

    output = migrate_text([region])

    assert 'oem: "TECNO"' in output
    assert 'product_base: "T1"' in output
    assert 'OP: "001: #value"' in output


def test_migration_rejects_unrepresentable_identity_and_duplicate_regions():
    base = dict(
        oem="Infinix",
        product="X1-OP",
        product_base="X1",
        region="OP",
        device="wrong-X1",
        model="Example",
        android_version="15",
        build_tag="AP3A.240905.015.A2",
        incremental="1",
    )

    with pytest.raises(ValueError, match="cannot be derived"):
        migrate_text([LegacyRegion(**base)])

    base["device"] = "Infinix-X1"
    with pytest.raises(ValueError, match="duplicate legacy region"):
        migrate_text([LegacyRegion(**base), LegacyRegion(**base)])


def test_yaml_ambiguous_region_keys_are_quoted():
    region = LegacyRegion(
        oem="TECNO",
        product="T1-NO",
        product_base="T1",
        region="NO",
        device="TECNO-T1",
        model="Example",
        android_version="15",
        build_tag="AP3A.240905.015.A2",
        incremental="1",
    )

    output = migrate_text([region])

    assert '  "NO": "1"' in output
    assert list(yaml.safe_load(output)["regions"]) == ["NO"]


def test_default_ties_use_first_region_and_expanded_keys_are_ordered():
    regions = [
        LegacyRegion(
            oem="TECNO",
            product="B-OP",
            product_base="B",
            region="OP",
            device="TECNO-B",
            model="Example",
            android_version="15",
            build_tag="CUSTOM",
            incremental="1",
        ),
        LegacyRegion(
            oem="TECNO",
            product="A-IN",
            product_base="A",
            region="IN",
            device="TECNO-A",
            model="Example",
            android_version="14",
            build_tag="UP1A.231005.007",
            incremental="2",
        ),
    ]

    output = migrate_text(regions)

    assert 'product_base: "B"' in output
    assert 'android_version: "15"' in output
    assert output.index('    build_tag: "CUSTOM"') < output.index(
        '    incremental: "1"'
    )
    assert output.index('    product_base: "A"') < output.index(
        '    android_version: "14"'
    )


def test_batch_preflight_writes_nothing_when_any_legacy_config_is_invalid(tmp_path):
    valid = tmp_path / "config-A.yml"
    invalid = tmp_path / "config-B.yml"
    valid_text = """\
oem: "TECNO"
product: "T1-OP"
device: "TECNO-T1"
model: "Example"
android_version: "15"
build_tag: "AP3A.240905.015.A2"
incremental: "1"
"""
    valid.write_text(valid_text, encoding="utf-8")
    invalid.write_text("oem: TECNO\noem: Infinix\n", encoding="utf-8")

    with pytest.raises(yaml.constructor.ConstructorError, match="duplicate key"):
        migrate_directory(tmp_path, write=True)

    assert valid.read_text(encoding="utf-8") == valid_text
