from pathlib import Path

import yaml

from checkota.manager import Config, update_config_from_fingerprint


def _write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def _target(cfg: Config, android_version: str, build_tag: str) -> str:
    return (
        f"{cfg.oem}/{cfg.product}/{cfg.device}:{android_version}/"
        f"{build_tag}/NEW:user/release-keys"
    )


def test_all_regions_converging_promotes_top_level_android(tmp_path):
    path = tmp_path / "config.yml"
    _write(
        path,
        """\
oem: "Infinix"
product_base: "X6873"
model: "Infinix GT 30 Pro"
android_version: "15"
regions:
  OP: "OLD"
  EU:
    android_version: "16"
    incremental: "EU"
""",
    )
    cfg = Config.from_yaml(path)[0]
    non_target_fingerprint = Config.from_yaml(path)[1].fingerprint()

    assert update_config_from_fingerprint(
        path, cfg, _target(cfg, "16", "BP2A.250605.031.A3")
    )

    parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert parsed["android_version"] == "16"
    assert parsed["regions"] == {"OP": "NEW", "EU": "EU"}
    assert Config.from_yaml(path)[1].fingerprint() == non_target_fingerprint


def test_temporary_majority_does_not_promote_android_default(tmp_path):
    path = tmp_path / "config.yml"
    _write(
        path,
        """\
oem: "Infinix"
product_base: "X6873"
model: "Infinix GT 30 Pro"
android_version: "15"
regions:
  OP: "OLD"
  EU:
    android_version: "16"
    incremental: "EU"
  IN: "STILL-15"
""",
    )
    cfg = Config.from_yaml(path)[0]

    assert update_config_from_fingerprint(
        path, cfg, _target(cfg, "16", "BP2A.250605.031.A3")
    )

    parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert parsed["android_version"] == "15"
    assert parsed["regions"]["OP"] == {
        "android_version": "16",
        "incremental": "NEW",
    }
    assert parsed["regions"]["EU"] == {
        "android_version": "16",
        "incremental": "EU",
    }


def test_convergence_preserves_noncanonical_build_tag(tmp_path):
    path = tmp_path / "config.yml"
    _write(
        path,
        """\
oem: "Infinix"
product_base: "X6873"
model: "Infinix GT 30 Pro"
android_version: "15"
regions:
  OP: "OLD"
  EU:
    android_version: "16"
    build_tag: "CUSTOM.TAG"
    incremental: "EU"
""",
    )
    cfg = Config.from_yaml(path)[0]

    assert update_config_from_fingerprint(
        path, cfg, _target(cfg, "16", "BP2A.250605.031.A3")
    )

    parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert parsed["android_version"] == "16"
    assert parsed["regions"] == {
        "OP": "NEW",
        "EU": {"build_tag": "CUSTOM.TAG", "incremental": "EU"},
    }


def test_already_current_region_leaves_the_file_byte_identical(tmp_path):
    """A check that finds no new build must not rewrite the config.

    The top-level default lags the (already converged) regions here, so a
    normalization pass would promote it. Convergence is a side effect of
    applying an update, not something a no-op check is allowed to trigger.
    """
    path = tmp_path / "config.yml"
    content = """\
oem: "Infinix"
product_base: "X6873"
model: "Infinix GT 30 Pro"
android_version: "15"
regions:
  OP:
    product_base: "X6873B"
    android_version: "16"
    incremental: "OP"
  EU:
    android_version: "16"
    build_tag: "CUSTOM.TAG"
    incremental: "EU"
"""
    _write(path, content)
    configs = Config.from_yaml(path)
    fingerprints = {config.region: config.fingerprint() for config in configs}

    assert update_config_from_fingerprint(path, configs[0], configs[0].fingerprint())

    assert path.read_text(encoding="utf-8") == content
    normalized = Config.from_yaml(path)
    assert {config.region: config.fingerprint() for config in normalized} == fingerprints
