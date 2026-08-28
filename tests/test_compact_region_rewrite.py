from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from checkota import manager
from checkota.manager import Config, update_config_from_fingerprint


def _write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8", newline="")


def _config(path: Path, region_index: int = 0) -> Config:
    return Config.from_yaml(path)[region_index]


def _target(
    cfg: Config,
    android_version: str,
    build_tag: str,
    incremental: str,
) -> str:
    return (
        f"{cfg.oem}/{cfg.product}/{cfg.device}:{android_version}/"
        f"{build_tag}/{incremental}:user/release-keys"
    )


def test_scalar_region_incremental_is_rewritten_in_place(tmp_path):
    path = tmp_path / "config.yml"
    _write(
        path,
        """\
oem: "Infinix"
product_base: "X6873"
model: "Infinix GT 30 Pro"
android_version: "16"
regions:
  OP: "OLD#VALUE" # retain region comment
  IN: "UNCHANGED"
""",
    )
    cfg = _config(path)

    assert update_config_from_fingerprint(
        path,
        cfg,
        _target(cfg, "16", "BP2A.250605.031.A3", "NEW#VALUE1"),
    )

    text = path.read_text(encoding="utf-8")
    assert '  OP: "NEW#VALUE1" # retain region comment' in text
    assert '  IN: "UNCHANGED"' in text
    assert yaml.safe_load(text)["regions"] == {
        "OP": "NEW#VALUE1",
        "IN": "UNCHANGED",
    }


def test_quoted_top_level_keys_are_rewritten_in_place(tmp_path):
    path = tmp_path / "config.yml"
    _write(
        path,
        """\
oem: "Infinix"
product_base: "X6873"
model: "Infinix GT 30 Pro"
"android_version": "16" # retain version comment
"regions": # retain section comment
  OP: "OLD"
""",
    )
    cfg = _config(path)

    assert update_config_from_fingerprint(
        path,
        cfg,
        _target(cfg, "15", "AP3A.240905.015.A2", "NEW"),
    )

    assert path.read_text(encoding="utf-8") == (
        'oem: "Infinix"\n'
        'product_base: "X6873"\n'
        'model: "Infinix GT 30 Pro"\n'
        '"android_version": "15" # retain version comment\n'
        '"regions": # retain section comment\n'
        '  OP: "NEW"\n'
    )


def test_quoted_region_key_is_rewritten_without_changing_its_spelling(tmp_path):
    path = tmp_path / "config.yml"
    _write(
        path,
        """\
oem: "Infinix"
product_base: "X6873"
model: "Infinix GT 30 Pro"
android_version: "16"
regions:
  "NO": "OLD" # retain quoted region
""",
    )
    cfg = _config(path)

    assert update_config_from_fingerprint(
        path,
        cfg,
        _target(cfg, "15", "AP3A.240905.015.A2", "NEW"),
    )

    assert path.read_text(encoding="utf-8") == (
        'oem: "Infinix"\n'
        'product_base: "X6873"\n'
        'model: "Infinix GT 30 Pro"\n'
        'android_version: "15"\n'
        'regions:\n'
        '  "NO": "NEW" # retain quoted region\n'
    )


def test_numeric_leading_region_key_is_rewritten_in_place(tmp_path):
    path = tmp_path / "config.yml"
    _write(
        path,
        """\
oem: "Infinix"
product_base: "X6873"
model: "Infinix GT 30 Pro"
android_version: "16"
regions:
  5G: "OLD"
""",
    )
    cfg = _config(path)

    assert cfg.region == "5G"
    assert update_config_from_fingerprint(
        path,
        cfg,
        _target(cfg, "16", "BP2A.250605.031.A3", "NEW"),
    )

    assert '  5G: "NEW"' in path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "replacement",
    [
        'regions: {OP: "OLD"}',
        "regions:\n  OP: &base \"OLD\"",
        "regions:\n  OP: *base",
        # Flow-style region value: single-line and multi-line both reject, so a
        # loadable config is never one the updater has to bail out of.
        'regions:\n  OP: {incremental: "OLD"}',
        'regions:\n  OP: {\n    incremental: "OLD"\n  }',
        # Multi-line scalar: continuation lines are indistinguishable from keys
        # to the line-oriented rewriter.
        'regions:\n  OP: "OLD\n    continued"',
    ],
)
def test_unsafe_layout_is_rejected_without_rewrite(tmp_path, replacement):
    path = tmp_path / "config.yml"
    base = """\
oem: "Infinix"
product_base: "X6873"
model: "Infinix GT 30 Pro"
android_version: "16"
regions:
  OP: "OLD"
"""
    _write(path, base)
    cfg = _config(path)
    _write(path, base.replace('regions:\n  OP: "OLD"', replacement))
    before = path.read_bytes()

    assert not update_config_from_fingerprint(
        path,
        cfg,
        _target(cfg, "16", "BP2A.250605.031.A3", "NEW"),
    )
    assert path.read_bytes() == before


def test_scalar_region_is_promoted_for_android_override(tmp_path):
    path = tmp_path / "config.yml"
    _write(
        path,
        """\
oem: "Infinix"
product_base: "X6873"
model: "Infinix GT 30 Pro"
android_version: "16"
regions:
  EU: "OLD" # retain promoted region comment
  OP: "UNCHANGED"
""",
    )
    cfg = _config(path)

    assert update_config_from_fingerprint(
        path,
        cfg,
        _target(cfg, "15", "AP3A.240905.015.A2", "NEW"),
    )

    assert path.read_text(encoding="utf-8") == (
        "oem: \"Infinix\"\n"
        "product_base: \"X6873\"\n"
        "model: \"Infinix GT 30 Pro\"\n"
        "android_version: \"16\"\n"
        "regions:\n"
        "  EU: # retain promoted region comment\n"
        "    android_version: \"15\"\n"
        "    incremental: \"NEW\"\n"
        "  OP: \"UNCHANGED\"\n"
    )


def test_expanded_region_reduces_to_scalar_when_overrides_redundant(tmp_path):
    path = tmp_path / "config.yml"
    _write(
        path,
        """\
oem: "Infinix"
product_base: "X6873"
model: "Infinix GT 30 Pro"
android_version: "16"
regions:
  EU:
    android_version: "15"
    incremental: "OLD"
  OP: "UNCHANGED"
""",
    )
    cfg = _config(path)

    assert update_config_from_fingerprint(
        path,
        cfg,
        _target(cfg, "16", "BP2A.250605.031.A3", "NEW"),
    )

    text = path.read_text(encoding="utf-8")
    assert '  EU: "NEW"' in text
    assert "android_version: \"15\"" not in text
    assert "build_tag:" not in text
    assert Config.from_yaml(path)[0].incremental == "NEW"


def test_expanded_region_preserves_product_base_override(tmp_path):
    path = tmp_path / "config.yml"
    _write(
        path,
        """\
oem: "Infinix"
product_base: "X6857"
model: "Infinix NOTE 50X 5G"
android_version: "16"
regions:
  IN:
    product_base: "X6857B"
    android_version: "16"
    incremental: "OLD"
""",
    )
    cfg = _config(path)

    assert update_config_from_fingerprint(
        path,
        cfg,
        _target(cfg, "16", "BP2A.250605.031.A3", "NEW"),
    )

    parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert parsed["regions"] == {
        "IN": {"product_base": "X6857B", "incremental": "NEW"}
    }
    assert Config.from_yaml(path)[0].product == "X6857B-IN"


def test_noncanonical_build_tag_is_preserved_on_promotion(tmp_path):
    path = tmp_path / "config.yml"
    _write(
        path,
        """\
oem: "TECNO"
product_base: "T1102"
model: "TECNO MEGAPAD"
android_version: "15"
regions:
  OP: "OLD"
""",
    )
    cfg = _config(path)

    assert update_config_from_fingerprint(
        path,
        cfg,
        _target(cfg, "15", "AQ3A.250226.002", "NEW"),
    )

    parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert parsed["regions"]["OP"] == {
        "build_tag": "AQ3A.250226.002",
        "incremental": "NEW",
    }


def test_rewrite_preserves_crlf_and_missing_final_newline(tmp_path):
    path = tmp_path / "config.yml"
    content = (
        "oem: \"Infinix\"\r\n"
        "product_base: \"X6873\"\r\n"
        "model: \"Infinix GT 30 Pro\"\r\n"
        "android_version: \"16\"\r\n"
        "regions:\r\n"
        "  EU: \"OLD\""
    )
    path.write_bytes(content.encode("utf-8"))
    cfg = _config(path)

    assert update_config_from_fingerprint(
        path,
        cfg,
        _target(cfg, "15", "AP3A.240905.015.A2", "NEW"),
    )

    updated = path.read_bytes()
    assert b"\r\n" in updated
    assert b"\n" not in updated.replace(b"\r\n", b"")
    assert not updated.endswith(b"\n")
    parsed = yaml.safe_load(updated.decode("utf-8"))
    assert parsed["android_version"] == "15"
    assert parsed["regions"]["EU"] == "NEW"


def test_redundant_key_comments_are_retained_as_comments(tmp_path):
    path = tmp_path / "config.yml"
    _write(
        path,
        """\
oem: "Infinix"
product_base: "X6873"
model: "Infinix GT 30 Pro"
android_version: "16"
regions:
  EU: # mapping note
    android_version: "15" # old version note
    build_tag: "OLD.TAG" # old tag note
    incremental: "OLD"

  OP: "UNCHANGED"
""",
    )
    cfg = _config(path)

    assert update_config_from_fingerprint(
        path,
        cfg,
        _target(cfg, "16", "BP2A.250605.031.A3", "NEW"),
    )

    text = path.read_text(encoding="utf-8")
    # The child keys are gone, so their comments are re-indented to the region
    # key and hoisted above it rather than left dangling at an indentation level
    # that no longer exists. Blank lines stay below as separators.
    assert text == (
        'oem: "Infinix"\n'
        'product_base: "X6873"\n'
        'model: "Infinix GT 30 Pro"\n'
        'android_version: "16"\n'
        "regions:\n"
        "  # old version note\n"
        "  # old tag note\n"
        '  EU: "NEW" # mapping note\n'
        "\n"
        '  OP: "UNCHANGED"\n'
    )
    assert yaml.safe_load(text)["regions"]["EU"] == "NEW"


def test_expanded_region_is_normalized_without_collapsing(tmp_path):
    path = tmp_path / "config.yml"
    _write(
        path,
        """\
oem: "Infinix"
product_base: "X6873"
model: "Infinix GT 30 Pro"
android_version: "16"
regions:
  EU: # retain mapping comment
    android_version: "15" # remove redundant version
    build_tag: "OLD.TAG" # retain tag comment
    incremental: "OLD#VALUE" # retain incremental comment
  OP: "UNCHANGED"
""",
    )
    cfg = _config(path)

    assert update_config_from_fingerprint(
        path,
        cfg,
        _target(cfg, "16", "CUSTOM#TAG", "NEW#VALUE1"),
    )

    text = path.read_text(encoding="utf-8")
    assert "  EU: # retain mapping comment" in text
    assert "# remove redundant version" in text
    assert 'build_tag: "CUSTOM#TAG" # retain tag comment' in text
    assert 'incremental: "NEW#VALUE1" # retain incremental comment' in text
    assert yaml.safe_load(text)["regions"]["EU"] == {
        "build_tag": "CUSTOM#TAG",
        "incremental": "NEW#VALUE1",
    }


def test_mapping_collapse_preserves_missing_final_newline(tmp_path):
    path = tmp_path / "config.yml"
    content = (
        'oem: "Infinix"\n'
        'product_base: "X6873"\n'
        'model: "Infinix GT 30 Pro"\n'
        'android_version: "16"\n'
        "regions:\n"
        "  EU:\n"
        '    android_version: "15"\n'
        '    incremental: "OLD"'
    )
    _write(path, content)
    cfg = _config(path)

    assert update_config_from_fingerprint(
        path,
        cfg,
        _target(cfg, "16", "BP2A.250605.031.A3", "NEW"),
    )

    updated = path.read_bytes()
    assert updated.endswith(b'EU: "NEW"')
    assert not updated.endswith(b"\n")


def test_mapping_normalization_preserves_missing_final_newline(tmp_path):
    path = tmp_path / "config.yml"
    content = (
        'oem: "Infinix"\n'
        'product_base: "X6873"\n'
        'model: "Infinix GT 30 Pro"\n'
        'android_version: "16"\n'
        "regions:\n"
        "  EU:\n"
        '    incremental: "OLD"\n'
        '    android_version: "15"'
    )
    _write(path, content)
    cfg = _config(path)

    assert update_config_from_fingerprint(
        path,
        cfg,
        _target(cfg, "16", "CUSTOM.TAG", "NEW"),
    )

    updated = path.read_bytes()
    assert updated.endswith(b'incremental: "NEW"')
    assert not updated.endswith(b"\n")
    assert yaml.safe_load(updated.decode("utf-8"))["regions"]["EU"] == {
        "build_tag": "CUSTOM.TAG",
        "incremental": "NEW",
    }


def test_round_trip_failure_leaves_original_byte_identical(tmp_path):
    path = tmp_path / "config.yml"
    _write(
        path,
        """\
oem: "Infinix"
product_base: "X6873"
model: "Infinix GT 30 Pro"
android_version: "16"
regions:
  EU: "OLD"
""",
    )
    cfg = _config(path)
    before = path.read_bytes()
    load_calls = 0
    original_load = manager._load_yaml

    def fail_second_load(stream):
        nonlocal load_calls
        load_calls += 1
        if load_calls == 2:
            raise yaml.YAMLError("round-trip parse failed")
        return original_load(stream)

    with patch("checkota.manager._load_yaml", side_effect=fail_second_load):
        assert not update_config_from_fingerprint(
            path,
            cfg,
            _target(cfg, "15", "AP3A.240905.015.A2", "NEW"),
        )

    assert path.read_bytes() == before
    assert not list(tmp_path.glob(".*.tmp"))


def test_atomic_replace_failure_leaves_original_byte_identical(tmp_path):
    path = tmp_path / "config.yml"
    _write(
        path,
        """\
oem: "Infinix"
product_base: "X6873"
model: "Infinix GT 30 Pro"
android_version: "16"
regions:
  EU: "OLD"
""",
    )
    cfg = _config(path)
    before = path.read_bytes()

    with patch("checkota.manager.os.replace", side_effect=OSError("replace failed")):
        assert not update_config_from_fingerprint(
            path,
            cfg,
            _target(cfg, "15", "AP3A.240905.015.A2", "NEW"),
        )

    assert path.read_bytes() == before
    assert not list(tmp_path.glob(".*.tmp"))


def test_scalar_region_expansion_inherits_dominant_child_indent(tmp_path):
    path = tmp_path / "config.yml"
    _write(
        path,
        """\
oem: "Infinix"
product_base: "X6873"
model: "Infinix GT 30 Pro"
android_version: "14"
regions:
    OP:
        build_tag: "B"
        incremental: "I"
    IN: "OTHER"
""",
    )
    cfg = _config(path, region_index=1)

    assert update_config_from_fingerprint(
        path,
        cfg,
        _target(cfg, "16", "BP2A.250605.031.A3", "NEW"),
    )

    text = path.read_text(encoding="utf-8")
    assert "    IN:\n" in text
    # Expanded children must follow the file's 4-space child indent, not +2.
    assert '\n        android_version: "16"\n' in text
    assert '\n        incremental: "NEW"\n' in text
    assert '\n      android_version' not in text
    assert yaml.safe_load(text)["regions"] == {
        "OP": {"build_tag": "B", "incremental": "I"},
        "IN": {"android_version": "16", "incremental": "NEW"},
    }
