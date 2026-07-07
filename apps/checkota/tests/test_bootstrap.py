"""M6 fix — bootstrap_vendor is idempotent and fails loud on missing vendor dir."""

import sys

import pytest

from modules.paths import bootstrap_vendor


def test_bootstrap_inserts_path_once(tmp_path, monkeypatch):
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    monkeypatch.setattr(sys, "path", [p for p in sys.path])
    bootstrap_vendor(vendor)
    assert str(vendor) in sys.path
    count_first = sys.path.count(str(vendor))
    bootstrap_vendor(vendor)
    # Idempotent: no duplicate insert on second call.
    assert sys.path.count(str(vendor)) == count_first


def test_bootstrap_exits_when_missing(tmp_path):
    missing = tmp_path / "does-not-exist"
    with pytest.raises(SystemExit) as info:
        bootstrap_vendor(missing)
    assert info.value.code == 1
