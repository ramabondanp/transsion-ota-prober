"""Regression tests for the deep-audit fixes.

Covers:
  - check-in failures are errors, not "no updates found"
  - concurrent duplicate notification prevention
  - Telegram sendMessage ok:false is a failure
  - config update failure propagation
  - Telegraph token is optional
  - drain reports send failures
  - Telegraph nodes unescape HTML entities
  - metadata failure TTL caching
  - inter-process-safe processed-updates save
  - per-config debug filenames
"""

import argparse
import multiprocessing
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from checkota.manager import Config
from checkota.models import PendingNotification, VariantUpdate
from checkota.processor import (
    _claim_new_update,
    _release_claimed_update,
    apply_update_actions,
    drain_pending_notifications,
    get_cached_ota_metadata,
)
from checkota.runtime import RunContext
from checkota.telegram import TgNotify
from checkota.update_checker import UpdateChecker, UpdateCheckError


def _cfg() -> Config:
    return Config(
        oem="Infinix",
        product="X6873-OP",
        device="Infinix-X6873",
        android_version="14",
        build_tag="B",
        incremental="I",
        model="Infinix GT 30 Pro",
    )


def _update(**overrides) -> VariantUpdate:
    base = {
        "cfg": _cfg(),
        "config_path": Path("/tmp/config-X6873.yml"),
        "variant_label": "Global",
        "region_name": None,
        "title": "TECNO-X123-15.0.1.2-OP001",
        "url": "https://example.com/ota.zip",
        "size": "2 GB",
        "desc": "desc",
        "is_new_update": True,
        "target_fp": "Infinix/X6873-OP/Infinix-X6873:16/B2/I2:user/release-keys",
        "target_incremental": "I2",
        "sdk_message": "Android 16",
        "data": {},
    }
    base.update(overrides)
    return VariantUpdate(**base)


def _args(**overrides) -> argparse.Namespace:
    base = {
        "incremental": False,
        "dry_run": False,
        "no_config": False,
        "skip_telegram": False,
        "register_update": False,
        "update_incremental": False,
        "force_notify": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _ctx(tmp_path: Path) -> RunContext:
    return RunContext(
        env={},
        processed_path=tmp_path / "processed_updates.txt",
        processed_titles=set(),
        dry_run=False,
    )


def _claim_worker(path: str, barrier, results) -> None:
    from checkota.processor import _claim_new_update, _commit_claimed_update

    ctx = RunContext(
        env={},
        processed_path=Path(path),
        processed_titles=set(),
        dry_run=False,
    )
    barrier.wait()
    claimed = _claim_new_update(ctx, "PROCESS-TITLE")
    committed = _commit_claimed_update(ctx, "PROCESS-TITLE") if claimed else False
    results.put((claimed, committed))


# --- H1: network failures are errors ------------------------------------------


def test_update_checker_raises_on_network_failure():
    checker = UpdateChecker(_cfg(), session=MagicMock())
    checker.session.post.side_effect = __import__(
        "requests"
    ).exceptions.ConnectionError("boom")
    with (
        patch("checkota.update_checker.time.sleep", lambda _s: None),
        pytest.raises(UpdateCheckError),
    ):
        checker.check()


def test_update_checker_raises_on_http_error():
    checker = UpdateChecker(_cfg(), session=MagicMock())
    response = MagicMock()
    response.raise_for_status.side_effect = __import__("requests").exceptions.HTTPError(
        "bad"
    )
    checker.session.post.return_value = response
    with pytest.raises(UpdateCheckError):
        checker.check()


# --- H2: duplicate notification prevention -------------------------------------


def test_claim_new_update_blocks_duplicates(tmp_path):
    ctx = _ctx(tmp_path)
    assert _claim_new_update(ctx, "TITLE") is True
    assert _claim_new_update(ctx, "TITLE") is False
    ctx.processed_titles.add("TITLE")
    assert _claim_new_update(ctx, "OTHER") is True
    _release_claimed_update(ctx, "OTHER")
    assert _claim_new_update(ctx, "OTHER") is True


def test_claim_new_update_is_process_safe(tmp_path):
    path = tmp_path / "processed_updates.txt"
    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(2)
    results = ctx.Queue()
    processes = [
        ctx.Process(target=_claim_worker, args=(str(path), barrier, results))
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    try:
        values = [results.get(timeout=10) for _ in processes]
    finally:
        for process in processes:
            process.join(timeout=10)
            if process.is_alive():
                process.terminate()
                process.join()
    assert sum(claimed for claimed, _ in values) == 1
    assert sum(committed for _, committed in values) == 1
    assert path.read_text(encoding="utf-8").splitlines() == ["PROCESS-TITLE"]


def test_apply_update_actions_skips_duplicate_notification(tmp_path):
    ctx = _ctx(tmp_path)
    ctx.claimed_titles.add("TECNO-X123-15.0.1.2-OP001")

    sent: list[str] = []

    class _StubNotifier:
        def send(self, msg, truncate_desc=True, device_title=None):
            sent.append(device_title)
            return True

    with patch("checkota.processor.create_notifier", return_value=_StubNotifier()):
        rc = apply_update_actions(ctx, _update(), _args(no_config=True))
    assert rc == 0
    assert sent == []


def test_apply_update_actions_reports_claim_failure(tmp_path):
    ctx = _ctx(tmp_path)

    class _StubNotifier:
        def send(self, msg, truncate_desc=True, device_title=None):
            raise AssertionError("notification must not be sent")

    with (
        patch("checkota.processor._claim_new_update", return_value=None),
        patch("checkota.processor.create_notifier", return_value=_StubNotifier()),
    ):
        assert apply_update_actions(ctx, _update(), _args(no_config=True)) == 1


# --- H3: Telegram ok:false is a failure ----------------------------------------


def test_telegram_send_fails_when_ok_false():
    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"ok": False, "error_code": 400, "description": "Bad Request"}

    class _Session:
        def post(self, *a, **k):
            return _Resp()

    notifier = TgNotify("token", "chat", "telegraph", session=_Session())
    assert notifier.send("hello", truncate_desc=False) is False


# --- H4: config update failure propagates --------------------------------------


def test_apply_update_actions_returns_1_when_config_update_fails(tmp_path):
    ctx = _ctx(tmp_path)
    sent: list[str] = []

    class _StubNotifier:
        def send(self, msg, truncate_desc=True, device_title=None):
            sent.append(device_title)
            return True

    with (
        patch("checkota.processor.update_config_from_fingerprint", return_value=False),
        patch("checkota.processor.create_notifier", return_value=_StubNotifier()),
    ):
        rc = apply_update_actions(ctx, _update(), _args())
    assert rc == 1
    assert sent == []


# --- M1: Telegraph token optional ----------------------------------------------


def test_telegraph_token_optional():
    class _Session:
        def post(self, *a, **k):
            return MagicMock()

    notifier = TgNotify("token", "chat", "", session=_Session())
    assert notifier.telegraph_token == ""


# --- M2: drain reports failures ------------------------------------------------


def test_drain_returns_1_on_send_failure(tmp_path):
    ctx = _ctx(tmp_path)
    ctx.pending_notifications.append(
        PendingNotification(
            msg="<b>x</b>", device_title="D", title="T", is_new_update=True
        )
    )

    class _FailingNotifier:
        def send(self, msg, truncate_desc=True, device_title=None):
            return False

    with (
        patch("checkota.processor.create_notifier", return_value=_FailingNotifier()),
        patch("checkota.processor.SWEEP_TELEGRAM_DELAY", 0),
    ):
        rc = drain_pending_notifications(ctx, _args())
    assert rc == 1


# --- M3: Telegraph nodes unescape entities -------------------------------------


def test_telegraph_nodes_unescape_entities():
    nodes = TgNotify._html_to_telegraph_nodes(
        "5 &lt; 7 &amp; x &gt; y\n\n<b>&lt;b&gt;</b>"
    )
    assert nodes[0]["children"] == ["5 < 7 & x > y"]
    assert nodes[1]["children"] == [{"tag": "b", "children": ["<b>"]}]


# --- M4: metadata failure TTL ---------------------------------------------------


def test_metadata_failure_cached_then_expired(tmp_path):
    ctx = _ctx(tmp_path)
    ctx.metadata_failures["https://x/y.zip"] = time.monotonic() - 1
    calls = {"n": 0}

    def fake_fetch(url, session=None, stop_event=None):
        calls["n"] += 1

    with patch("checkota.processor.get_ota_metadata", fake_fetch):
        assert get_cached_ota_metadata(ctx, "https://x/y.zip") is None
    assert calls["n"] == 0

    ctx.metadata_failures["https://x/y.zip"] = time.monotonic() - 10000
    with patch("checkota.processor.get_ota_metadata", fake_fetch):
        assert get_cached_ota_metadata(ctx, "https://x/y.zip") is None
    assert calls["n"] == 1


# --- M5: processed-updates save is process-safe --------------------------------


def test_save_processed_title_dedupes_existing(tmp_path):
    path = tmp_path / "processed_updates.txt"
    path.write_text("OLD-TITLE\n", encoding="utf-8")

    from checkota.fingerprints import save_processed_title

    assert save_processed_title(path, "OLD-TITLE") is True
    assert path.read_text(encoding="utf-8") == "OLD-TITLE\n"
    assert save_processed_title(path, "NEW-TITLE") is True
    assert "NEW-TITLE" in path.read_text(encoding="utf-8")


def test_sweep_failure_retains_and_releases_claim_for_retry(tmp_path):
    ctx = _ctx(tmp_path)
    first = PendingNotification(
        msg="first", device_title="D1", title="T1", is_new_update=True
    )
    second = PendingNotification(
        msg="second", device_title="D2", title="T2", is_new_update=True
    )
    assert _claim_new_update(ctx, "T1")
    assert _claim_new_update(ctx, "T2")
    ctx.pending_notifications.extend([first, second])

    class _Notifier:
        def __init__(self):
            self.calls = 0

        def send(self, msg, truncate_desc=True, device_title=None):
            self.calls += 1
            return self.calls != 2

    notifier = _Notifier()
    with patch(
        "checkota.processor.create_notifier", return_value=notifier
    ), patch("checkota.processor.SWEEP_TELEGRAM_DELAY", 0):
        assert drain_pending_notifications(ctx, _args()) == 1

    assert ctx.pending_notifications == [second]
    assert "T1" not in ctx.claimed_titles
    assert "T2" not in ctx.claimed_titles
    assert "T1" in ctx.processed_titles
    assert "T2" not in ctx.processed_titles

    with patch(
        "checkota.processor.create_notifier", return_value=notifier
    ), patch("checkota.processor.SWEEP_TELEGRAM_DELAY", 0):
        assert drain_pending_notifications(ctx, _args()) == 0

    assert ctx.pending_notifications == []
    assert "T2" in ctx.processed_titles


# --- M6: per-config debug filenames ---------------------------------------------


def test_update_checker_debug_filenames():
    checker = UpdateChecker(_cfg(), debug_label="X6873-Global")
    assert checker.debug_file == "debug_checkin_response_X6873-Global.txt"
    assert checker.debug_error_file == "debug_checkin_response_X6873-Global_error.bin"
