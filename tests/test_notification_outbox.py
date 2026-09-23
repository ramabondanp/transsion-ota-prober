"""Regression coverage for notification delivery across rewrites and restarts."""

import argparse
import sys
from unittest.mock import patch

import pytest

from checkota import cli, processor
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


def test_known_title_updates_stale_region_without_resend(tmp_path):
    ctx, update, args = _setup(tmp_path)
    ctx.processed_titles.add(update.title)
    update.is_new_update = False
    sender = _Notifier()
    with patch.object(processor, "create_notifier", return_value=sender):
        assert processor.apply_update_actions(ctx, update, args) == 0
    assert Config.from_yaml(update.config_path)[0].fingerprint() == TARGET
    assert sender.sent == []
    assert load_pending_notifications(ctx.processed_path) == []
    ctx.stop()


def test_shared_title_catches_up_second_region_and_noop_is_byte_identical(tmp_path):
    ctx, update, args = _setup(tmp_path)
    update.config_path.write_text(
        'oem: "Infinix"\nproduct_base: "X1"\nmodel: "Test"\n'
        'android_version: "15"\nregions:\n  OP: "1"\n  EU: "1"\n',
        encoding="utf-8",
    )
    op, eu = Config.from_yaml(update.config_path)
    assert processor.update_config_from_fingerprint(update.config_path, op, TARGET)
    assert Config.from_yaml(update.config_path)[1].incremental == "1"
    ctx.processed_titles.add(update.title)
    eu_target = TARGET.replace("X1-OP", "X1-EU")
    args.fp = None
    data = {"title": update.title, "url": update.url, "size": update.size}
    sender = _Notifier()
    with (
        patch.object(processor, "_check_for_updates", return_value=(0, data)),
        patch.object(
            processor,
            "get_cached_ota_metadata",
            return_value={"fingerprint": eu_target},
        ),
        patch.object(processor, "create_notifier", return_value=sender),
    ):
        status, found = processor.collect_update_info(ctx, eu, update.config_path, args)
        assert status == 0 and found is not None and not found.is_new_update
        assert processor.apply_update_actions(ctx, found, args) == 0
        op_after, eu_after = Config.from_yaml(update.config_path)
        assert op_after.fingerprint() == TARGET
        assert eu_after.fingerprint() == eu_target
        before = update.config_path.read_bytes()
        status, found = processor.collect_update_info(
            ctx, eu_after, update.config_path, args
        )
        assert status == 0 and found is not None
        assert processor.apply_update_actions(ctx, found, args) == 0
    assert update.config_path.read_bytes() == before
    assert sender.sent == []
    ctx.stop()


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("override", [None, "update_incremental", "force_notify"])
def test_known_title_still_fetches_metadata_to_check_region(
    tmp_path, dry_run, override
):
    ctx, update, args = _setup(tmp_path)
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
        metadata.assert_called_once()
        rewrite.assert_not_called()
    assert update.config_path.read_bytes() == before
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
