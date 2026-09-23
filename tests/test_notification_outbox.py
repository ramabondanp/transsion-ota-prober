"""Regression coverage for notification delivery across rewrites and restarts."""

import argparse
import os
import sys
from unittest.mock import patch

import pytest

from checkota import cli, manager, processor
from checkota.manager import Config
from checkota.models import PendingNotification, RegionUpdate
from checkota.outbox import (
    load_pending_notifications,
    remove_pending_notification,
    stage_pending_notification,
)
from checkota.runtime import RunContext

TARGET = "Infinix/X1-OP/Infinix-X1:16/BP2A.250605.031.A3/2:user/release-keys"


def _setup(tmp_path):
    config_path = tmp_path / "config-X1.yml"
    config_path.write_text(
        'oem: "Infinix"\nproduct_base: "X1"\nmodel: "Test"\n'
        'android_version: "15"\nregions:\n  OP: "1"\n',
        encoding="utf-8",
    )
    path = tmp_path / "titles.txt"
    ctx = RunContext(env={}, processed_path=path, processed_titles=set(), dry_run=False)
    update = RegionUpdate(
        cfg=Config.from_yaml(config_path)[0],
        config_path=config_path,
        region_name="Global",
        title="TITLE",
        url="https://android.googleapis.com/packages/ota/a.zip",
        size="100",
        desc="desc",
        is_new_update=True,
        target_fp=TARGET,
        target_incremental="2",
        sdk_message=None,
        data={},
    )
    args = argparse.Namespace(
        skip_telegram=False,
        register_update=False,
        dry_run=False,
        force_notify=False,
        update_incremental=False,
        incremental=None,
        no_config=False,
        config_dir=None,
    )
    return ctx, update, args


class _Notifier:
    def __init__(self, success=True):
        self.success = success
        self.sent = []

    def send(self, msg, **kwargs):
        self.sent.append(msg)
        return self.success


def test_failed_inline_send_survives_restart_and_commits_on_replay(tmp_path):
    ctx, update, args = _setup(tmp_path)
    with patch.object(processor, "create_notifier", return_value=_Notifier(False)):
        assert processor.apply_update_actions(ctx, update, args) == 1
    assert Config.from_yaml(update.config_path)[0].fingerprint() == TARGET
    assert "TITLE" not in ctx.processed_titles
    assert len(load_pending_notifications(ctx.processed_path)) == 1
    ctx.stop()

    restarted = RunContext(
        env={}, processed_path=ctx.processed_path, processed_titles=set(), dry_run=False
    )
    assert processor.load_outbox_into_context(restarted)
    sender = _Notifier()
    with patch.object(processor, "create_notifier", return_value=sender):
        assert processor.drain_pending_notifications(restarted, args, delay=0) == 0
    assert len(sender.sent) == 1
    assert restarted.processed_path.read_text() == "TITLE\n"
    assert load_pending_notifications(ctx.processed_path) == []
    restarted.stop()


def test_known_title_does_not_update_region_or_resend(tmp_path):
    ctx, update, args = _setup(tmp_path)
    ctx.processed_titles.add(update.title)
    update.is_new_update = False
    before = update.config_path.read_bytes()
    sender = _Notifier()
    with patch.object(processor, "create_notifier", return_value=sender):
        assert processor.apply_update_actions(ctx, update, args) == 0
    assert update.config_path.read_bytes() == before
    assert sender.sent == []
    assert load_pending_notifications(ctx.processed_path) == []
    ctx.stop()


@pytest.mark.parametrize("dry_run", [False, True])
def test_processed_tcard_skips_metadata_and_config_even_in_stale_region(
    tmp_path, dry_run
):
    ctx, update, args = _setup(tmp_path)
    update.config_path.write_text(
        'oem: "Infinix"\nproduct_base: "X1"\nmodel: "Test"\n'
        'android_version: "15"\nregions:\n  OP: "1"\n  EU: "1"\n',
        encoding="utf-8",
    )
    op, eu = Config.from_yaml(update.config_path)
    assert processor.update_config_from_fingerprint(update.config_path, op, TARGET)
    ctx.processed_titles.add("Tcard_X1")
    args.fp = None
    args.dry_run = ctx.dry_run = dry_run
    data = {"title": "Tcard_X1", "url": update.url, "size": update.size}
    before = update.config_path.read_bytes()
    with (
        patch.object(processor, "_check_for_updates", return_value=(0, data)),
        patch.object(processor, "_resolve_target_metadata") as metadata,
        patch.object(processor, "apply_update_actions") as actions,
    ):
        assert processor.process_region(ctx, eu, update.config_path, args) == 0
        metadata.assert_not_called()
        actions.assert_not_called()
    assert update.config_path.read_bytes() == before
    assert Config.from_yaml(update.config_path)[1].incremental == "1"
    ctx.stop()


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("override", [None, "update_incremental", "force_notify"])
def test_known_title_skips_metadata_unless_explicitly_requested(
    tmp_path, dry_run, override
):
    ctx, update, args = _setup(tmp_path)
    update.title = "Tcard_X1"
    ctx.processed_titles.add(update.title)
    args.dry_run = ctx.dry_run = dry_run
    args.fp = None
    if override:
        setattr(args, override, True)
    before = update.config_path.read_bytes()
    with (
        patch.object(
            processor,
            "_check_for_updates",
            return_value=(
                0,
                {
                    "title": update.title,
                    "url": update.url,
                    "size": update.size,
                },
            ),
        ),
        patch.object(
            processor, "_resolve_target_metadata", return_value=(0, None)
        ) as metadata,
        patch.object(processor, "_apply_config_update") as rewrite,
    ):
        assert processor.collect_update_info(
            ctx, update.cfg, update.config_path, args
        ) == (0, None)
        if override:
            metadata.assert_called_once()
        else:
            metadata.assert_not_called()
        rewrite.assert_not_called()
    assert update.config_path.read_bytes() == before
    ctx.stop()


@pytest.mark.parametrize("override", ["update_incremental", "force_notify"])
def test_known_title_explicit_overrides_keep_their_behavior(tmp_path, override):
    ctx, update, args = _setup(tmp_path)
    ctx.processed_titles.add(update.title)
    update.is_new_update = False
    setattr(args, override, True)
    before = update.config_path.read_bytes()
    sender = _Notifier()
    notifier = None if override == "update_incremental" else sender
    with patch.object(processor, "create_notifier", return_value=notifier):
        assert processor.apply_update_actions(ctx, update, args) == 0
    if override == "update_incremental":
        assert Config.from_yaml(update.config_path)[0].fingerprint() == TARGET
        assert sender.sent == []
    else:
        assert update.config_path.read_bytes() == before
        assert len(sender.sent) == 1
    ctx.stop()


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("update_incremental", [False, True])
@pytest.mark.parametrize(
    "current,target,should_update",
    [
        ("16", "15", False),
        ("16", "16", False),
        ("15", "16", True),
        ("9", "10", True),
        ("10", "9", False),
        ("preview", "16", False),
        ("16", "preview", False),
    ],
)
def test_tcard_only_rewrites_for_android_upgrade(
    tmp_path, capsys, dry_run, update_incremental, current, target, should_update
):
    ctx, update, args = _setup(tmp_path)
    update.config_path.write_text(
        'oem: "Infinix"\nproduct_base: "X1"\nmodel: "Test"\n'
        f'android_version: "{current}"\nregions:\n'
        '  OP:\n    build_tag: "OLD"\n    incremental: "1"\n',
        encoding="utf-8",
    )
    update.cfg = Config.from_yaml(update.config_path)[0]
    update.title = "Tcard_X1"
    update.target_fp = f"Infinix/X1-OP/Infinix-X1:{target}/NEW/2:user/release-keys"
    args.dry_run = ctx.dry_run = dry_run
    args.update_incremental = update_incremental
    before = update.config_path.read_bytes()
    original_fp = update.cfg.fingerprint()
    sender = _Notifier()
    with (
        patch.object(processor, "create_notifier", return_value=sender),
        patch.object(
            processor, "stage_pending_notification", wraps=stage_pending_notification
        ) as stage,
    ):
        assert processor.apply_update_actions(ctx, update, args) == 0
        assert stage.call_count == (2 if should_update and not dry_run else 0)
    output = capsys.readouterr().out
    if should_update and not dry_run:
        assert Config.from_yaml(update.config_path)[0].fingerprint() == update.target_fp
        assert update.cfg.fingerprint() == update.target_fp
    else:
        assert update.config_path.read_bytes() == before
        assert update.cfg.fingerprint() == original_fp
    if not should_update:
        assert "without a newer Android version" in output
        assert "Dry-run: would update" not in output
    elif dry_run:
        assert "Dry-run: would update" in output
    assert len(sender.sent) == (0 if dry_run else 1)
    assert load_pending_notifications(ctx.processed_path) == []
    ctx.stop()


def test_tcard_upgrade_retains_outbox_after_send_failure(tmp_path):
    ctx, update, args = _setup(tmp_path)
    update.title = "Tcard_X1"
    with patch.object(processor, "create_notifier", return_value=_Notifier(False)):
        assert processor.apply_update_actions(ctx, update, args) == 1
    assert Config.from_yaml(update.config_path)[0].fingerprint() == TARGET
    assert update.title not in ctx.processed_titles
    assert len(load_pending_notifications(ctx.processed_path)) == 1
    ctx.stop()


def test_staging_failure_does_not_advance_yaml(tmp_path):
    ctx, update, args = _setup(tmp_path)
    before = update.config_path.read_bytes()
    with (
        patch.object(processor, "create_notifier", return_value=_Notifier()),
        patch.object(processor, "stage_pending_notification", return_value=False),
    ):
        assert processor.apply_update_actions(ctx, update, args) == 1
    assert update.config_path.read_bytes() == before
    ctx.stop()


def test_failed_rewrite_does_not_replay_unapplied_update(tmp_path):
    ctx, update, args = _setup(tmp_path)
    with (
        patch.object(processor, "create_notifier", return_value=_Notifier()),
        patch.object(processor, "update_config_from_fingerprint", return_value=False),
    ):
        assert processor.apply_update_actions(ctx, update, args) == 1
    assert load_pending_notifications(ctx.processed_path) == []
    ctx.stop()


def test_duplicate_pending_notification_does_not_commit_title_without_send(tmp_path):
    ctx, update, args = _setup(tmp_path)
    existing = PendingNotification(
        msg="earlier", title="TITLE", device_title="Test", is_new_update=True
    )
    ctx.pending_notifications.append(existing)
    with patch.object(processor, "create_notifier", return_value=_Notifier()):
        assert processor.apply_update_actions(ctx, update, args) == 0
    assert Config.from_yaml(update.config_path)[0].fingerprint() == TARGET
    assert "TITLE" not in ctx.processed_titles
    ctx.stop()


def test_crash_after_rewrite_before_ready_is_recovered(tmp_path):
    ctx, update, _ = _setup(tmp_path)
    note = PendingNotification(
        msg="message", title="TITLE", device_title="Test", is_new_update=True
    )
    assert stage_pending_notification(
        ctx.processed_path, note, update.config_path, TARGET
    )
    assert load_pending_notifications(ctx.processed_path) == []
    assert processor.update_config_from_fingerprint(
        update.config_path, update.cfg, TARGET
    )
    assert load_pending_notifications(ctx.processed_path) == [note]
    assert remove_pending_notification(ctx.processed_path, note.title)
    ctx.stop()


def test_ready_outbox_does_not_replay_if_yaml_reverted(tmp_path):
    ctx, update, _ = _setup(tmp_path)
    old_yaml = update.config_path.read_bytes()
    note = PendingNotification(
        msg="message", title="TITLE", device_title="Test", is_new_update=True
    )
    assert processor.update_config_from_fingerprint(
        update.config_path, update.cfg, TARGET
    )
    new_yaml = update.config_path.read_bytes()
    assert stage_pending_notification(
        ctx.processed_path, note, update.config_path, TARGET, ready=True
    )
    update.config_path.write_bytes(old_yaml)
    assert load_pending_notifications(ctx.processed_path) == []
    assert processor.load_outbox_into_context(ctx)
    assert ctx.pending_notifications == []
    assert update.title not in ctx.processed_titles
    update.config_path.write_bytes(new_yaml)
    assert load_pending_notifications(ctx.processed_path) == [note]
    ctx.stop()


@pytest.mark.skipif(os.name == "nt", reason="directory fsync unavailable on Windows")
def test_config_rewrite_fsyncs_config_directory(tmp_path):
    ctx, update, _ = _setup(tmp_path)
    directory_stat = update.config_path.parent.stat()
    synced_directory = []
    real_fsync = os.fsync

    def track_fsync(fd):
        entry = os.fstat(fd)
        if (entry.st_dev, entry.st_ino) == (
            directory_stat.st_dev,
            directory_stat.st_ino,
        ):
            synced_directory.append(True)
        real_fsync(fd)

    with patch.object(manager.os, "fsync", side_effect=track_fsync):
        assert processor.update_config_from_fingerprint(
            update.config_path, update.cfg, TARGET
        )
    assert synced_directory
    ctx.stop()


def test_directory_sync_failure_leaves_recoverable_outbox(tmp_path):
    ctx, update, args = _setup(tmp_path)
    sender = _Notifier()
    with (
        patch.object(processor, "create_notifier", return_value=sender),
        patch.object(
            manager, "_sync_config_directory", side_effect=OSError("sync failed")
        ),
    ):
        assert processor.apply_update_actions(ctx, update, args) == 1
    assert sender.sent == []
    assert Config.from_yaml(update.config_path)[0].fingerprint() == TARGET
    assert load_pending_notifications(ctx.processed_path)
    assert update.title not in ctx.processed_titles
    ctx.stop()

    restarted = RunContext(
        env={}, processed_path=ctx.processed_path, processed_titles=set(), dry_run=False
    )
    assert processor.load_outbox_into_context(restarted)
    with patch.object(processor, "create_notifier", return_value=sender):
        assert processor.drain_pending_notifications(restarted, args, delay=0) == 0
    assert len(sender.sent) == 1
    assert restarted.processed_titles == {update.title}
    restarted.stop()


def test_cli_replays_outbox_even_when_check_finds_no_updates(tmp_path, monkeypatch):
    ctx, update, _ = _setup(tmp_path)
    note = PendingNotification(
        msg="message", title="TITLE", device_title="Test", is_new_update=True
    )
    assert processor.update_config_from_fingerprint(
        update.config_path, update.cfg, TARGET
    )
    assert stage_pending_notification(
        ctx.processed_path, note, update.config_path, TARGET, ready=True
    )
    sender = _Notifier()
    monkeypatch.setenv("bot_token", "token")
    monkeypatch.setenv("chat_id", "chat")
    monkeypatch.setattr(sys, "argv", ["checkota", "-c", str(update.config_path)])
    monkeypatch.setattr(cli, "create_run_context", lambda *a, **kw: ctx)
    monkeypatch.setattr(cli, "start_watchdog", lambda *a, **kw: None)
    monkeypatch.setattr(cli, "process_config", lambda *a, **kw: 0)
    monkeypatch.setattr(processor, "create_notifier", lambda *a, **kw: sender)
    assert cli.main() == 0
    assert sender.sent == ["message"]
    assert ctx.processed_titles == {"TITLE"}
    assert load_pending_notifications(ctx.processed_path) == []


def test_corrupt_outbox_aborts_without_checking_configs(tmp_path, monkeypatch):
    ctx, update, _ = _setup(tmp_path)
    directory = tmp_path / "titles.txt.outbox"
    directory.mkdir()
    (directory / "corrupt.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("bot_token", "token")
    monkeypatch.setenv("chat_id", "chat")
    monkeypatch.setattr(sys, "argv", ["checkota", "-c", str(update.config_path)])
    monkeypatch.setattr(cli, "create_run_context", lambda *a, **kw: ctx)
    monkeypatch.setattr(cli, "start_watchdog", lambda *a, **kw: None)
    monkeypatch.setattr(
        cli, "process_config", lambda *a, **kw: pytest.fail("check should not run")
    )
    assert cli.main() == 1
    assert Config.from_yaml(update.config_path)[0].incremental == "1"
