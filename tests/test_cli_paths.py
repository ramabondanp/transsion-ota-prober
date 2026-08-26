"""Tests for lazy CLI config path resolution."""

from __future__ import annotations

import importlib
import signal
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import checkota
from checkota import cli, paths


def _fail_seed() -> Path:
    raise AssertionError("config defaults must not be seeded")


def test_package_import_does_not_seed_configs(monkeypatch):
    monkeypatch.setattr(paths, "ensure_config_resources", _fail_seed)

    importlib.reload(checkota)


def test_help_does_not_seed_configs(monkeypatch):
    monkeypatch.setattr(cli, "active_config_dir", _fail_seed)
    monkeypatch.setattr(sys, "argv", ["checkota", "--help"])

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 0


def test_direct_fingerprint_does_not_seed_configs(monkeypatch):
    ctx = SimpleNamespace(stop_event=threading.Event(), stop=lambda: None)
    monkeypatch.setattr(cli, "active_config_dir", _fail_seed)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "checkota",
            "--fp",
            "OEM/product/device:14/build/incremental:user/release-keys",
        ],
    )
    monkeypatch.setattr(cli, "create_run_context", lambda *args, **kwargs: ctx)
    monkeypatch.setattr(
        cli, "install_interrupt_handler", lambda context: signal.SIG_DFL
    )
    monkeypatch.setattr(cli, "start_watchdog", lambda context, timeout: None)
    monkeypatch.setattr(cli.signal, "signal", lambda *args: None)
    monkeypatch.setattr(cli, "config_from_fingerprint", lambda fingerprint: object())
    monkeypatch.setattr(cli, "process_config_variant", lambda *args, **kwargs: 0)

    assert cli.main() == 0


def test_nonfinite_timeout_is_rejected():
    parser = cli.build_parser()
    for value in ("nan", "inf"):
        args = parser.parse_args([
            "--fp",
            "OEM/product/device:14/build/incremental:user/release-keys",
            "--timeout",
            value,
        ])
        with pytest.raises(SystemExit):
            cli._validate_args(parser, args)

    args = parser.parse_args([
        "--fp",
        "OEM/product/device:14/build/incremental:user/release-keys",
        "--timeout=-inf",
    ])
    with pytest.raises(SystemExit):
        cli._validate_args(parser, args)


def test_existing_config_file_wins_without_seeding(monkeypatch, tmp_path):
    config = tmp_path / "custom.yml"
    config.touch()
    monkeypatch.setattr(cli, "active_config_dir", _fail_seed)

    assert cli.resolve_config_path(config) == config


def test_bare_codename_seeds_and_resolves_active_config(monkeypatch, tmp_path):
    config_dir = tmp_path / "xdg" / "checkota" / "configs"
    config_dir.mkdir(parents=True)
    config = config_dir / "config-X6873.yml"
    config.touch()
    monkeypatch.setattr(cli, "active_config_dir", lambda: config_dir)

    assert cli.resolve_config_path(Path("X6873")) == config


def test_wheel_configs_alias_resolves_to_seeded_directory(monkeypatch, tmp_path):
    config_dir = tmp_path / "xdg" / "checkota" / "configs"
    config = config_dir / "config-X6873.yml"
    launch_dir = tmp_path / "launch"
    launch_dir.mkdir()
    monkeypatch.chdir(launch_dir)
    monkeypatch.setattr(cli, "IS_SOURCE_CHECKOUT", False)

    def seed() -> Path:
        config_dir.mkdir(parents=True, exist_ok=True)
        config.touch()
        return config_dir

    monkeypatch.setattr(cli, "active_config_dir", seed)
    args = SimpleNamespace(config=None, config_dir=Path("configs/"))

    assert cli.resolve_config_dir(Path("configs/")) == config_dir
    assert cli._collect_config_paths(cli.build_parser(), args) == [config]
    assert args.config_dir == config_dir


def test_existing_config_directory_wins_in_wheel(monkeypatch, tmp_path):
    explicit = tmp_path / "configs"
    explicit.mkdir()
    monkeypatch.setattr(cli, "IS_SOURCE_CHECKOUT", False)
    monkeypatch.setattr(cli, "active_config_dir", _fail_seed)

    assert cli.resolve_config_dir(explicit) == explicit


def test_missing_source_configs_directory_is_not_remapped(monkeypatch):
    monkeypatch.setattr(cli, "IS_SOURCE_CHECKOUT", True)
    monkeypatch.setattr(cli, "active_config_dir", _fail_seed)

    assert cli.resolve_config_dir(Path("configs/")) == Path("configs")
