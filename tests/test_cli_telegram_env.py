"""Default runs must fail fast when the Telegram env vars are missing.

Telegram notifications are the default. Only explicit non-notifying flags
(--dry-run, --skip-telegram, --register-update, --update-incremental, --gen-fp)
may run without bot_token/chat_id, and the check must happen before any work
(run context creation, config seeding, lock pruning, network requests).
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from checkota import cli

_FP = "OEM/product/device:14/build/incremental:user/release-keys"
_BYPASS_FLAGS = [
    "--dry-run",
    "--skip-telegram",
    "--register-update",
    "--update-incremental",
    "--gen-fp",
]


@pytest.fixture(autouse=True)
def _clear_telegram_env(monkeypatch):
    for name in ("bot_token", "chat_id", "telegraph_token"):
        monkeypatch.delenv(name, raising=False)


def _validate(argv: list[str]) -> None:
    parser = cli.build_parser()
    cli._validate_args(parser, parser.parse_args(argv))


def test_default_run_requires_telegram_env(capsys):
    with pytest.raises(SystemExit) as excinfo:
        _validate(["--fp", _FP])

    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "bot_token" in err
    assert "chat_id" in err
    # The message must name the escape hatches so the failure is actionable.
    for flag in _BYPASS_FLAGS:
        assert flag in err


def test_partial_env_names_only_the_missing_var(capsys, monkeypatch):
    monkeypatch.setenv("bot_token", "token")

    with pytest.raises(SystemExit) as excinfo:
        _validate(["--fp", _FP])

    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "chat_id" in err
    assert "bot_token" not in err


@pytest.mark.parametrize("env", [{"bot_token": "token"}, {"chat_id": "chat"}])
def test_either_variable_alone_is_insufficient(capsys, monkeypatch, env):
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(SystemExit) as excinfo:
        _validate(["--fp", _FP])

    assert excinfo.value.code == 2
    assert "not set" in capsys.readouterr().err


@pytest.mark.parametrize("empty", ["bot_token", "chat_id"])
def test_empty_variable_counts_as_unset(capsys, monkeypatch, empty):
    """An exported-but-empty var is a misconfiguration, not a credential."""
    monkeypatch.setenv("bot_token", "token")
    monkeypatch.setenv("chat_id", "chat")
    monkeypatch.setenv(empty, "")

    with pytest.raises(SystemExit) as excinfo:
        _validate(["--fp", _FP])

    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert empty in err
    assert "not set" in err


def test_notifying_run_passes_when_env_is_set(monkeypatch):
    monkeypatch.setenv("bot_token", "token")
    monkeypatch.setenv("chat_id", "chat")

    _validate(["--fp", _FP])  # must not raise


@pytest.mark.parametrize("flag", _BYPASS_FLAGS)
def test_non_telegram_flags_skip_the_env_check(flag):
    _validate(["--fp", _FP, flag])  # must not raise


def test_incremental_and_gen_fp_disable_notifications_without_skip_flag():
    """The predicate is explicit about the folded flags so it stays correct
    even when _validate_args' skip_telegram assignment is stubbed out."""
    args = argparse.Namespace(
        dry_run=False,
        skip_telegram=False,
        register_update=False,
        update_incremental=True,
        gen_fp=False,
    )
    assert cli._telegram_notifications_enabled(args) is False

    args.update_incremental = False
    args.gen_fp = True
    assert cli._telegram_notifications_enabled(args) is False

    args.gen_fp = False
    assert cli._telegram_notifications_enabled(args) is True


def test_main_exits_before_any_work_when_env_is_missing(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["checkota", "--fp", _FP])
    monkeypatch.setattr(
        cli,
        "create_run_context",
        lambda *a, **k: pytest.fail("run context must not be created"),
    )

    with pytest.raises(SystemExit) as excinfo:
        cli.main()

    assert excinfo.value.code == 2


@pytest.mark.parametrize("argv", [["-c", "X6873"], ["-d", "configs/"]])
def test_config_selection_never_reached_without_env(monkeypatch, argv):
    """Bare -c/-d are the paths that trigger XDG seeding and title-lock
    pruning; the env check must short-circuit both."""
    monkeypatch.setattr(sys, "argv", ["checkota", *argv])
    monkeypatch.setattr(
        cli,
        "active_config_dir",
        lambda: pytest.fail("config dir must not be resolved (no seeding)"),
    )
    monkeypatch.setattr(
        cli,
        "create_run_context",
        lambda *a, **k: pytest.fail("run context must not be created"),
    )
    monkeypatch.setattr(
        cli,
        "install_interrupt_handler",
        lambda ctx: pytest.fail("signal handler must not be installed"),
    )

    with pytest.raises(SystemExit) as excinfo:
        cli.main()

    assert excinfo.value.code == 2


def test_update_incremental_runs_without_env(monkeypatch, tmp_path):
    """--update-incremental is a non-notifying path and must reach the work."""
    config = tmp_path / "config-X6873.yml"
    config.write_text("oem: x\n", encoding="utf-8")
    monkeypatch.setattr(
        sys, "argv", ["checkota", "-c", str(config), "--update-incremental"]
    )
    ctx = SimpleNamespace(
        stop_event=threading.Event(),
        stop=lambda: None,
        pending_lock=threading.Lock(),
        pending_notifications=[],
        drain_lock=threading.Lock(),
    )
    processed: list[Path] = []
    monkeypatch.setattr(cli, "create_run_context", lambda *a, **k: ctx)
    monkeypatch.setattr(
        cli, "install_interrupt_handler", lambda context: signal.SIG_DFL
    )
    monkeypatch.setattr(cli, "start_watchdog", lambda context, timeout: None)
    monkeypatch.setattr(cli.signal, "signal", lambda *args: None)

    def fake_process_config(config_path, args):
        processed.append(config_path)
        return 0

    monkeypatch.setattr(cli, "process_config", fake_process_config)

    assert cli.main() == 0
    # Reaching process_config proves the env check did not abort the run.
    assert processed == [config]
