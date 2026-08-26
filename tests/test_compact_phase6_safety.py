import argparse
import os
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from checkota import cli, manager
from checkota.manager import Config, update_config_from_fingerprint


def _write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def _target(cfg: Config, incremental: str) -> str:
    return (
        f"{cfg.oem}/{cfg.product}/{cfg.device}:16/"
        f"BP2A.250605.031.A3/{incremental}:user/release-keys"
    )


def _two_region_config(path: Path) -> tuple[Config, Config]:
    _write(
        path,
        """\
oem: "Infinix"
product_base: "X6873"
model: "Infinix GT 30 Pro"
android_version: "16"
regions:
  OP: "OLD-OP"
  IN: "OLD-IN"
""",
    )
    configs = Config.from_yaml(path)
    return configs[0], configs[1]


def test_global_pool_region_updates_are_serialized(tmp_path):
    path = tmp_path / "config.yml"
    _two_region_config(path)
    active = 0
    max_active = 0
    active_lock = threading.Lock()
    original_update = manager._update_config_from_fingerprint

    def tracked_update(config_path, cfg, fingerprint):
        nonlocal active, max_active
        with active_lock:
            active += 1
            max_active = max(max_active, active)
        try:
            time.sleep(0.05)
            return original_update(config_path, cfg, fingerprint)
        finally:
            with active_lock:
                active -= 1

    def process_region(_ctx, cfg, config_path, _args):
        incremental = f"NEW-{cfg.region}"
        return 0 if update_config_from_fingerprint(
            config_path, cfg, _target(cfg, incremental)
        ) else 1

    args = argparse.Namespace(region=None, incremental=None)
    ctx = argparse.Namespace(stop_event=threading.Event())

    with (
        patch("checkota.cli.process_region", side_effect=process_region),
        patch(
            "checkota.manager._update_config_from_fingerprint",
            side_effect=tracked_update,
        ),
        ThreadPoolExecutor(max_workers=2) as executor,
    ):
        result = cli._run_global_pool(ctx, args, [path], executor)

    assert result == 0
    assert max_active == 1
    assert {
        config.region: config.incremental for config in Config.from_yaml(path)
    } == {"OP": "NEW-OP", "IN": "NEW-IN"}


def test_stale_config_object_can_update_another_region(tmp_path):
    path = tmp_path / "config.yml"
    op, india = _two_region_config(path)

    assert update_config_from_fingerprint(path, op, _target(op, "NEW-OP"))
    assert update_config_from_fingerprint(path, india, _target(india, "NEW-IN"))
    assert {
        config.region: config.incremental for config in Config.from_yaml(path)
    } == {"OP": "NEW-OP", "IN": "NEW-IN"}


def test_stale_config_object_can_safely_reupdate_same_region(tmp_path):
    path = tmp_path / "config.yml"
    stale_op, _ = _two_region_config(path)

    assert update_config_from_fingerprint(path, stale_op, _target(stale_op, "FIRST"))
    assert update_config_from_fingerprint(path, stale_op, _target(stale_op, "SECOND"))

    assert {
        config.region: config.incremental for config in Config.from_yaml(path)
    } == {"OP": "SECOND", "IN": "OLD-IN"}


@pytest.mark.parametrize("failure", ["fsync", "chmod", "replace"])
def test_atomic_stage_failures_leave_original_untouched(tmp_path, failure):
    path = tmp_path / "config.yml"
    cfg, _ = _two_region_config(path)
    before = path.read_bytes()

    with patch(f"checkota.manager.os.{failure}", side_effect=OSError(failure)):
        assert not update_config_from_fingerprint(path, cfg, _target(cfg, "NEW"))

    assert path.read_bytes() == before
    assert not list(tmp_path.glob(".*.tmp"))


def test_write_failure_leaves_original_untouched(tmp_path):
    path = tmp_path / "config.yml"
    cfg, _ = _two_region_config(path)
    before = path.read_bytes()

    original_fdopen = os.fdopen

    class FailingWriter:
        def __init__(self, fd, *args, **kwargs):
            self.handle = original_fdopen(fd, *args, **kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            self.handle.close()

        def write(self, _text):
            raise OSError("write")

    with patch("checkota.manager.os.fdopen", side_effect=FailingWriter):
        assert not update_config_from_fingerprint(path, cfg, _target(cfg, "NEW"))

    assert path.read_bytes() == before
    assert not list(tmp_path.glob(".*.tmp"))


def test_fdopen_failure_closes_temp_descriptor(tmp_path):
    path = tmp_path / "config.yml"
    cfg, _ = _two_region_config(path)
    before = path.read_bytes()
    fd, tmp_name = tempfile.mkstemp(dir=tmp_path)

    with (
        patch("checkota.manager.tempfile.mkstemp", return_value=(fd, tmp_name)),
        patch("checkota.manager.os.fdopen", side_effect=OSError("fdopen")),
        patch("checkota.manager.os.close", wraps=os.close) as close,
    ):
        assert not update_config_from_fingerprint(path, cfg, _target(cfg, "NEW"))

    close.assert_called_once_with(fd)
    assert path.read_bytes() == before
    assert not Path(tmp_name).exists()


def test_temp_parse_failure_leaves_original_untouched(tmp_path):
    path = tmp_path / "config.yml"
    cfg, _ = _two_region_config(path)
    before = path.read_bytes()
    # The updater reads the source with Path.open; Path.read_text is reserved
    # for reparsing the fully written temporary output.
    with patch.object(Path, "read_text", return_value="regions: ["):
        assert not update_config_from_fingerprint(path, cfg, _target(cfg, "NEW"))

    assert path.read_bytes() == before
    assert not list(tmp_path.glob(".*.tmp"))


@pytest.mark.parametrize(
    "old,new",
    [
        ('OP: "NEW-OP"', 'OP: "WRONG-TARGET"'),
        ('IN: "OLD-IN"', 'IN: "WRONG-NON-TARGET"'),
        ('product_base: "X6873"', 'product_base: "OTHER"'),
        ('  IN: "OLD-IN"\n', ""),
    ],
    ids=["target-values", "non-target-fingerprint", "identity", "region-set"],
)
def test_prepublication_invariants_reject_tampered_output(tmp_path, old, new):
    path = tmp_path / "config.yml"
    cfg, _ = _two_region_config(path)
    before = path.read_bytes()

    def tamper(lines, _config_path, _newline):
        rewritten = "".join(lines).replace(old, new, 1)
        assert rewritten != "".join(lines)
        lines[:] = rewritten.splitlines(keepends=True)
        return True

    with patch("checkota.manager._converge_android_default", side_effect=tamper):
        assert not update_config_from_fingerprint(path, cfg, _target(cfg, "NEW-OP"))

    assert path.read_bytes() == before
    assert not list(tmp_path.glob(".*.tmp"))


def test_round_trip_preserves_all_effective_regions(tmp_path):
    path = tmp_path / "config.yml"
    cfg, other = _two_region_config(path)
    before = {config.region: config.fingerprint() for config in Config.from_yaml(path)}

    assert update_config_from_fingerprint(path, cfg, _target(cfg, "NEW-OP"))
    after = {config.region: config.fingerprint() for config in Config.from_yaml(path)}

    assert after["IN"] == before["IN"]
    assert after["OP"] != before["OP"]
    assert other.region == "IN"
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["regions"]["IN"] == (
        "OLD-IN"
    )
