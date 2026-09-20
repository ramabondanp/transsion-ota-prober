"""Shutdown delivery and copy-once seeding regression tests (no network I/O)."""

import errno
import signal
import sys
import threading
from pathlib import Path

import pytest

from checkota import cli, paths, processor
from checkota.manager import Config
from checkota.models import RegionUpdate
from checkota.runtime import RunContext


@pytest.mark.parametrize("selection", ["-c", "-d"])
@pytest.mark.parametrize("stop_after_rewrite", [False, True])
def test_cli_delivers_completed_rewrite(
    tmp_path, monkeypatch, selection, stop_after_rewrite
):
    # Default (notifying) runs require the Telegram env vars before any work.
    monkeypatch.setenv("bot_token", "test-token")
    monkeypatch.setenv("chat_id", "test-chat")
    configs = tmp_path / "configs"
    configs.mkdir()
    config_path = configs / "config-X6873.yml"
    config_path.write_text(
        'oem: "Infinix"\nproduct_base: "X6873"\n'
        'model: "Infinix GT 30 Pro"\nandroid_version: "15"\n'
        'regions:\n  OP: "131015"\n',
        encoding="utf-8",
    )
    target = (
        "Infinix/X6873-OP/Infinix-X6873:16/"
        "BP2A.250605.031.A3/201500011:user/release-keys"
    )
    title = "X6873-OTA-TITLE"
    ctx = RunContext(
        env={},
        processed_path=tmp_path / "processed_updates.txt",
        processed_titles=set(),
        dry_run=False,
    )
    sent: list[str] = []

    class Notifier:
        def send(self, msg: str, **kwargs):
            assert not ctx.stop_event.is_set()
            from_drain = threading.current_thread() is threading.main_thread()
            # Healthy -c keeps sending inline; sweeps and stopped workers must
            # leave delivery to main's drain after executor shutdown.
            assert from_drain == (selection == "-d" or stop_after_rewrite)
            sent.append(msg)
            return True

    def collect(context, cfg, file_path, args):
        return 0, RegionUpdate(
            cfg=cfg,
            config_path=file_path,
            region_name="Global",
            title=title,
            url="https://android.googleapis.com/packages/ota/update.zip",
            size="2 GB",
            desc="Update description",
            is_new_update=True,
            target_fp=target,
            target_incremental="201500011",
            sdk_message="Android 16",
            data={},
        )

    real_rewrite = processor.update_config_from_fingerprint

    def rewrite_then_stop(*args):
        result = real_rewrite(*args)
        assert result is True
        if stop_after_rewrite:
            ctx.stop_event.set()
        return result

    selected_path = config_path if selection == "-c" else configs
    monkeypatch.setattr(
        sys, "argv", ["checkota", selection, str(selected_path), "--jobs", "2"]
    )
    monkeypatch.setattr(cli, "create_run_context", lambda *a, **k: ctx)
    monkeypatch.setattr(
        cli, "install_interrupt_handler", lambda _: signal.getsignal(signal.SIGINT)
    )
    monkeypatch.setattr(cli, "start_watchdog", lambda *a, **k: None)
    monkeypatch.setattr(processor, "collect_update_info", collect)
    monkeypatch.setattr(processor, "create_notifier", lambda *a, **k: Notifier())
    monkeypatch.setattr(processor, "update_config_from_fingerprint", rewrite_then_stop)

    assert cli.main() in (0, 130)
    assert Config.from_yaml(config_path)[0].fingerprint() == target
    assert len(sent) == 1
    assert title in ctx.processed_titles
    assert ctx.pending_notifications == []
    assert ctx.claimed_handles == {}


@pytest.mark.parametrize("replace_before_cleanup", [False, True])
def test_failed_seed_cleanup_preserves_latest_user_save(
    tmp_path, monkeypatch, replace_before_cleanup
):
    """Race saves against both the old unlink and the tombstone restoration."""
    source = tmp_path / "bundled.yml"
    source.write_bytes(b"bundled config")
    destination = tmp_path / "configs" / "config-X6873.yml"
    first_edit = tmp_path / "first-edit.yml"
    first_edit.write_bytes(b"first user edit")
    latest_edit = tmp_path / "latest-edit.yml"
    latest_edit.write_bytes(b"latest user edit")
    real_replace = paths.os.replace
    real_unlink = Path.unlink
    failed = False
    latest_saved = False

    def no_hardlinks(*args):
        raise OSError(errno.EOPNOTSUPP, "hardlinks unavailable")

    def fail_sync(fd):
        nonlocal failed
        if replace_before_cleanup:
            real_replace(first_edit, destination)
        failed = True
        raise OSError("simulated sync failure")

    def save_latest():
        nonlocal latest_saved
        real_replace(latest_edit, destination)
        latest_saved = True

    def race_replace(src, dst, *args, **kwargs):
        result = real_replace(src, dst, *args, **kwargs)
        if failed and Path(src) == destination and not latest_saved:
            # If cleanup moved a foreign file aside, a newer atomic save can
            # win the vacant destination before restoration.
            save_latest()
        return result

    def race_unlink(path, *args, **kwargs):
        if failed and not latest_saved:
            # Also cover a save between a path-based inode check and unlink.
            save_latest()
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(paths.os, "link", no_hardlinks)
    monkeypatch.setattr(paths.os, "fsync", fail_sync)
    monkeypatch.setattr(paths.os, "replace", race_replace)
    monkeypatch.setattr(Path, "unlink", race_unlink)

    with pytest.raises(OSError, match="simulated sync failure"):
        paths._publish_if_missing(source, destination, 0o644)

    assert latest_saved
    assert destination.read_bytes() == b"latest user edit"
    assert list(destination.parent.iterdir()) == [destination]


def test_disk_full_seeding_can_retry_without_allocating_cleanup_files(
    tmp_path, monkeypatch
):
    source = tmp_path / "bundled.yml"
    source.write_bytes(b"bundled config")
    destination = tmp_path / "configs" / "config-X6873.yml"
    real_mkstemp = paths.tempfile.mkstemp
    real_fsync = paths.os.fsync
    disk_full = False

    def no_hardlinks(*args):
        raise OSError(errno.EOPNOTSUPP, "hardlinks unavailable")

    def fail_sync(fd):
        nonlocal disk_full
        disk_full = True
        raise OSError(errno.ENOSPC, "disk full")

    def allocate(*args, **kwargs):
        if disk_full:
            raise OSError(errno.ENOSPC, "disk full during cleanup")
        return real_mkstemp(*args, **kwargs)

    monkeypatch.setattr(paths.os, "link", no_hardlinks)
    monkeypatch.setattr(paths.os, "fsync", fail_sync)
    monkeypatch.setattr(paths.tempfile, "mkstemp", allocate)

    with pytest.raises(OSError, match="disk full"):
        paths._publish_if_missing(source, destination, 0o644)

    disk_full = False
    monkeypatch.setattr(paths.os, "fsync", real_fsync)
    assert paths._publish_if_missing(source, destination, 0o644) is True
    assert destination.read_bytes() == b"bundled config"
    assert list(destination.parent.iterdir()) == [destination]
