"""M4 fix — cli.main Ctrl-C drain end-to-end test.

A) Sweep mode where workers set stop_event (simulating signal/Ctrl-C) and
   leave a notification buffered. The fix's `ctx.stop_event.clear()` before
   drain must let the buffered notification through.
B) Direct `--fp` mode (no sweep, no buffering). The fix's
   `buffered_notifications_possible` predicate must gate the drain, so direct
   mode never calls drain_pending_notifications.
"""
import argparse
import signal

from checkota.runtime import RunContext
from checkota.models import PendingNotification
from checkota import processor
from checkota import cli


def _make_args(tmp_path, *, config_dir_present):
    args = argparse.Namespace(
        fp=None,
        config=None,
        config_dir=(tmp_path / "configs") if config_dir_present else None,
        no_config=False,
        update_incremental=False,
        gen_fp=False,
        incremental=False,
        dry_run=False,
        skip_telegram=False,
        register_update=False,
        timeout=None,
        jobs=1,
        region=None,
        debug=False,
        imei=None,
    )
    args.run_context = RunContext(
        env={"bot_token": "t", "chat_id": "c", "telegraph_token": "p"},
        processed_path=tmp_path / "processed_updates.txt",
        processed_titles=set(),
        dry_run=False,
    )
    return args


def _patch_parser_to_return(monkeypatch, args):
    parser = argparse.ArgumentParser()
    monkeypatch.setattr(parser, "parse_args", lambda: args)
    monkeypatch.setattr(cli, "build_parser", lambda: parser)


def _patch_create_run_context(monkeypatch, args):
    monkeypatch.setattr(
        cli, "create_run_context", lambda dry_run, pool_size: args.run_context
    )


def test_cli_main_drains_after_interrupt_sweep(monkeypatch, tmp_path):
    args = _make_args(tmp_path, config_dir_present=True)
    args.config_dir.mkdir()
    (args.config_dir / "config-X6873.yml").write_text(
        "oem: Infinix\nproduct: X6873-OP\ndevice: Infinix-X6873\n"
        "android_version: '14'\nbuild_tag: B1\nincremental: I1\n"
        'model: "Infinix GT 30 Pro"\n',
        encoding="utf-8",
    )
    ctx = args.run_context

    sent: list[str] = []

    def fake_process_config(config_path, args):
        # Pre-set stop_event to simulate Ctrl-C, then append a buffered
        # notification that the drain must deliver.
        ctx.stop_event.set()
        ctx.pending_notifications.append(
            PendingNotification(
                msg="<b>fake-ota</b>",
                device_title="Infinix_X6873-title",
                title="Infinix-X6873-title",
                is_new_update=True,
            )
        )
        return 0

    class _StubNotifier:
        def __init__(self, *a, **kw):
            pass

        def send(self, msg, truncate_desc=True, device_title=None):
            sent.append(device_title)
            return True

    _patch_parser_to_return(monkeypatch, args)
    _patch_create_run_context(monkeypatch, args)
    monkeypatch.setattr(cli, "_validate_args", lambda p, a: None)
    monkeypatch.setattr(cli, "install_interrupt_handler", lambda ctx: signal.SIG_DFL)
    monkeypatch.setattr(cli, "start_watchdog", lambda ctx, t: None)
    monkeypatch.setattr(
        cli,
        "_collect_config_paths",
        lambda p, a: list(a.config_dir.glob("*.yml")),
    )
    # Skip the real OTA check entirely; the fake just buffers a notification.
    # `cli.process_config` is the bound reference used by `_run_sequential` --
    # patch that one, not `processor.process_config`.
    monkeypatch.setattr(cli, "process_config", fake_process_config)
    monkeypatch.setattr(processor, "create_notifier", lambda c, a: _StubNotifier())
    monkeypatch.setattr(processor, "SWEEP_TELEGRAM_DELAY", 0)

    rc = cli.main()
    assert rc == 0, f"Expected 0 (successful drain), got {rc}"
    assert sent == ["Infinix_X6873-title"], (
        f"Buffered notification must be delivered; got {sent}"
    )
    assert ctx.pending_notifications == [], (
        f"Buffer not drained: {ctx.pending_notifications}"
    )


def test_cli_main_no_drain_in_direct_fp(monkeypatch, tmp_path):
    args = _make_args(tmp_path, config_dir_present=False)
    args.no_config = False
    args.fp = "OEM/X6873-OP/Device:14/B1/I1:user/release-keys"

    drain_calls: list[int] = []

    def tracker(*a, **kw):
        drain_calls.append(1)
        return 0

    _patch_parser_to_return(monkeypatch, args)
    _patch_create_run_context(monkeypatch, args)
    monkeypatch.setattr(cli, "_validate_args", lambda p, a: None)
    monkeypatch.setattr(cli, "install_interrupt_handler", lambda ctx: signal.SIG_DFL)
    monkeypatch.setattr(cli, "start_watchdog", lambda ctx, t: None)
    monkeypatch.setattr(cli, "process_config_variant", lambda *a, **kw: 0)
    monkeypatch.setattr(cli, "drain_pending_notifications", tracker)
    from checkota.manager import Config

    monkeypatch.setattr(
        cli,
        "config_from_fingerprint",
        lambda fp: Config(
            oem="OEM",
            product="X6873-OP",
            device="Device",
            android_version="14",
            build_tag="B1",
            incremental="I1",
            model="Model",
        ),
    )

    cli.main()
    assert drain_calls == [], (
        f"Direct --fp mode (config_dir is None, sweep predicate false) must "
        f"NOT call drain_pending_notifications; got calls: {drain_calls}"
    )
