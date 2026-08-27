import hashlib
import runpy
from pathlib import Path

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
