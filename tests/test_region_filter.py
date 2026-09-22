"""--reg filtering: a directory sweep skips configs without the region.

A sweep filter is a selection, not a demand: most configs in a directory do
not carry every region, so a per-file miss must not be an error (it used to
print a ✗ and force the whole run's exit code to 1 -- AGENTS.md even
documents `checkota -d configs/ --reg OP-M1`). The run still fails when the
filter selects nothing at all, and a single `-c` miss stays an error.
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from checkota import cli
from checkota.processor import load_config_regions, scan_region_filter


@dataclass
class _Harness:
    processed: list[Path]
    real_process_config: object


_CONFIG_A = """\
oem: "Infinix"
product_base: "X6873"
model: "Infinix GT 30 Pro"
android_version: "16"
regions:
  OP: "111"
"""
_CONFIG_B = """\
oem: "TECNO"
product_base: "CN7c"
model: "Tecno Model"
android_version: "16"
regions:
  EU: "222"
"""


def _write(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def _namespace(region: str | None, *, sweep: bool) -> argparse.Namespace:
    # Only `config_dir`'s presence matters (is_sweep_mode); the path itself is
    # never resolved by load_config_regions.
    return argparse.Namespace(
        region=region,
        incremental=None,
        config_dir=Path("configs") if sweep else None,
    )


def test_sweep_region_miss_is_a_silent_skip(tmp_path, capsys):
    config = _write(tmp_path / "config-X6873.yml", _CONFIG_A)

    status, configs = load_config_regions(config, _namespace("EU", sweep=True))

    assert (status, configs) == (0, [])
    out = capsys.readouterr().out
    assert "✗" not in out
    assert "No configuration regions" not in out


def test_single_config_region_miss_still_errors(tmp_path, capsys):
    config = _write(tmp_path / "config-X6873.yml", _CONFIG_A)

    status, configs = load_config_regions(config, _namespace("EU", sweep=False))

    assert (status, configs) == (1, [])
    assert "No configuration regions" in capsys.readouterr().out


def test_sweep_region_hit_still_filters(tmp_path):
    config = _write(tmp_path / "config-CN7c.yml", _CONFIG_B)

    status, configs = load_config_regions(config, _namespace("eu", sweep=True))

    assert status == 0
    assert [cfg.region for cfg in configs] == ["EU"]


def test_sweep_filter_miss_returns_empty_without_error(tmp_path):
    config = _write(tmp_path / "config-CN7c.yml", _CONFIG_B)

    status, configs = load_config_regions(config, _namespace("op", sweep=True))

    assert (status, configs) == (0, [])


def test_scan_region_filter_reports_selection(tmp_path):
    path_a = _write(tmp_path / "config-X6873.yml", _CONFIG_A)
    path_b = _write(tmp_path / "config-CN7c.yml", _CONFIG_B)

    assert scan_region_filter([path_a, path_b], "eu").any_match is True
    assert scan_region_filter([path_a, path_b], "op").any_match is True
    scan = scan_region_filter([path_a, path_b], "nonsense")
    assert scan == (False, False)


def test_scan_region_filter_records_load_errors(tmp_path):
    """A load error must be visible to cli: an empty selection may only fail
    before work when every config loaded cleanly."""
    broken = _write(tmp_path / "config-broken.yml", "oem: [oops\n")
    path_b = _write(tmp_path / "config-CN7c.yml", _CONFIG_B)

    assert scan_region_filter([broken, path_b], "eu") == (True, True)
    assert scan_region_filter([broken], "eu") == (False, True)


# --- cli.main end-to-end -------------------------------------------------


@pytest.fixture()
def _runnable_cli(monkeypatch, tmp_path):
    """Drive cli.main without network, signal handlers, or real state.

    ``process_config`` is faked to record calls; tests that need the real
    loader (single-config path) restore ``harness.real_process_config``.
    """
    ctx = SimpleNamespace(
        stop_event=threading.Event(),
        pending_lock=threading.Lock(),
        pending_notifications=[],
        drain_lock=threading.Lock(),
        stop=lambda: None,
    )
    monkeypatch.setattr(cli, "create_run_context", lambda *a, **k: ctx)
    monkeypatch.setattr(
        cli, "install_interrupt_handler", lambda context: signal.SIG_DFL
    )
    monkeypatch.setattr(cli, "start_watchdog", lambda context, timeout: None)
    monkeypatch.setattr(cli.signal, "signal", lambda *args: None)
    processed: list[Path] = []
    real_process_config = cli.process_config

    def fake_process_config(config_path, args):
        processed.append(config_path)
        return 0

    monkeypatch.setattr(cli, "process_config", fake_process_config)
    return _Harness(processed, real_process_config)


def _sweep_dir(tmp_path) -> Path:
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    _write(config_dir / "config-X6873.yml", _CONFIG_A)
    _write(config_dir / "config-CN7c.yml", _CONFIG_B)
    return config_dir


def test_sweep_with_partial_region_match_exits_zero(
    monkeypatch, tmp_path, capsys, _runnable_cli
):
    harness = _runnable_cli
    config_dir = _sweep_dir(tmp_path)
    monkeypatch.setattr(
        sys, "argv", ["checkota", "-d", str(config_dir), "--reg", "OP", "--dry-run"]
    )

    assert cli.main() == 0
    # Both configs are visited; only the filter decides what each one runs.
    assert sorted(harness.processed) == sorted(config_dir.iterdir())


def test_sweep_with_no_matching_region_fails_once_without_running(
    monkeypatch, tmp_path, capsys, _runnable_cli
):
    harness = _runnable_cli
    config_dir = _sweep_dir(tmp_path)
    monkeypatch.setattr(
        sys, "argv", ["checkota", "-d", str(config_dir), "--reg", "ZZ", "--dry-run"]
    )

    assert cli.main() == 1
    assert harness.processed == []  # failed before any work, no per-file run
    out = capsys.readouterr().out
    assert out.count("No configuration regions") == 1
    assert "ZZ" in out


def test_single_config_region_miss_exits_one(
    monkeypatch, tmp_path, capsys, _runnable_cli
):
    harness = _runnable_cli
    monkeypatch.setattr(cli, "process_config", harness.real_process_config)
    config = _write(tmp_path / "config-X6873.yml", _CONFIG_A)
    monkeypatch.setattr(
        sys, "argv", ["checkota", "-c", str(config), "--reg", "ZZ", "--dry-run"]
    )

    assert cli.main() == 1
    assert "No configuration regions" in capsys.readouterr().out


def test_empty_selection_with_broken_config_still_runs_to_report_it(
    monkeypatch, tmp_path, capsys, _runnable_cli
):
    """The pre-check must not swallow load errors: when a config fails to
    parse, the sweep still runs so that error is reported, and the empty
    selection is reported after it."""
    harness = _runnable_cli
    monkeypatch.setattr(cli, "process_config", harness.real_process_config)
    config_dir = _sweep_dir(tmp_path)
    _write(config_dir / "config-broken.yml", "oem: [oops\n")
    monkeypatch.setattr(
        sys, "argv", ["checkota", "-d", str(config_dir), "--reg", "ZZ", "--dry-run"]
    )

    assert cli.main() == 1
    out = capsys.readouterr().out
    assert "Config error" in out  # the broken file was reported by the run
    assert out.count("No configuration regions") == 1
