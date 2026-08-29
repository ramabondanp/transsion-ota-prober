import hashlib
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_MANIFEST = runpy.run_path(str(_ROOT / "scripts" / "compact_manifest.py"))
config_paths = _MANIFEST["config_paths"]
manifest_bytes = _MANIFEST["manifest_bytes"]
manifest_rows = _MANIFEST["manifest_rows"]
serialize_manifest = _MANIFEST["serialize_manifest"]

_BASELINE_SHA256 = "50aa62e1eedd9994f1f763a5a9e92d10d7b840717ea3d7af0dab76b391639519"


def test_manifest_preserves_baseline_field_order_and_serialization(tmp_path):
    path = tmp_path / "config-X.yml"
    path.write_text(
        """\
oem: "Infinix"
product_base: "X1"
model: "Exämple"
android_version: "16"
regions:
  OP: "INC"
""",
        encoding="utf-8",
    )

    row = manifest_rows(tmp_path)[0]
    assert list(row) == [
        "source_config",
        "position",
        "oem",
        "product",
        "device",
        "android_version",
        "build_tag",
        "incremental",
        "model",
        "fingerprint",
    ]
    expected = (
        f'{{"source_config":"{path}","position":0,"oem":"Infinix",'
        '"product":"X1-OP","device":"Infinix-X1","android_version":"16",'
        '"build_tag":"BP2A.250605.031.A3","incremental":"INC",'
        '"model":"Exämple","fingerprint":"Infinix/X1-OP/Infinix-X1:16/'
        'BP2A.250605.031.A3/INC:user/release-keys"}\n'
    ).encode()
    assert manifest_bytes(tmp_path) == expected


@pytest.mark.parametrize("input_kind", ["missing", "empty", "wrong"])
def test_cli_rejects_inputs_without_configs_before_stdout_payload(
    tmp_path, input_kind
):
    config_dir = tmp_path / input_kind
    if input_kind != "missing":
        config_dir.mkdir()
    if input_kind == "wrong":
        (config_dir / "unrelated.yml").write_text(
            "not: a compact config\n", encoding="utf-8"
        )

    result = subprocess.run(
        [
            sys.executable,
            str(_ROOT / "scripts" / "compact_manifest.py"),
            str(config_dir),
        ],
        cwd=_ROOT,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert result.stdout == b""
    assert b"no matching config-*.yml or config-*.yaml files found" in result.stderr


def test_cli_rejects_empty_input_without_touching_output(tmp_path):
    config_dir = tmp_path / "empty"
    config_dir.mkdir()
    output = tmp_path / "manifest.jsonl"
    sentinel = b"existing manifest sentinel\x00\xff"
    output.write_bytes(sentinel)

    result = subprocess.run(
        [
            sys.executable,
            str(_ROOT / "scripts" / "compact_manifest.py"),
            str(config_dir),
            "--output",
            str(output),
        ],
        cwd=_ROOT,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert result.stdout == b""
    assert output.read_bytes() == sentinel


def test_repository_manifest_matches_preserved_baseline(monkeypatch):
    # The source paths are intentionally relative because that is part of the
    # Phase 0 manifest contract.
    monkeypatch.chdir(_ROOT)
    config_dir = Path("configs")
    rows = manifest_rows(config_dir)
    payload = serialize_manifest(rows)

    assert len(config_paths(config_dir)) == 114
    assert len(rows) == 148
    assert hashlib.sha256(payload).hexdigest() == _BASELINE_SHA256
