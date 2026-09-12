"""Regression tests for the deep-audit hardening pass."""

from __future__ import annotations

import argparse
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from checkota import metadata, processor
from checkota.constants import (
    MAX_FINGERPRINT_LENGTH,
    MAX_UPDATE_SIZE_LENGTH,
    MAX_UPDATE_TITLE_LENGTH,
)
from checkota.description import format_update_description
from checkota.fingerprints import release_processed_claim
from checkota.logging import sanitize_log_text
from checkota.manager import Config, parse_fingerprint
from checkota.message_text import sanitize_html
from checkota.models import PendingNotification, RegionUpdate
from checkota.processor import (
    apply_update_actions,
    drain_pending_notifications,
    get_cached_ota_metadata,
)
from checkota.runtime import RunContext
from checkota.update_checker import UpdateChecker
from checkota.validation import has_control_chars, has_unsafe_url_chars


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


def _update(**overrides) -> RegionUpdate:
    base = {
        "cfg": _cfg(),
        "config_path": Path("/tmp/config-X6873.yml"),
        "region_name": None,
        "title": "TITLE",
        "url": "https://example.com/ota.zip",
        "size": "2 GB",
        "desc": "desc",
        "is_new_update": True,
        "target_fp": "Infinix/X6873-OP/Infinix-X6873:16/B/I:user/release-keys",
        "target_incremental": "I",
        "sdk_message": None,
        "data": {},
    }
    base.update(overrides)
    return RegionUpdate(**base)


def _args(**overrides) -> argparse.Namespace:
    base = {
        "dry_run": False,
        "no_config": True,
        "skip_telegram": True,
        "register_update": False,
        "update_incremental": False,
        "force_notify": False,
        "incremental": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _ctx(tmp_path: Path, *, zip_proxy: bool = False) -> RunContext:
    return RunContext(
        env={},
        processed_path=tmp_path / "processed_updates.txt",
        processed_titles=set(),
        dry_run=False,
        zip_proxy=zip_proxy,
    )


def test_untrusted_checkin_fields_are_capped_and_control_free():
    assert UpdateChecker._safe_title("x" * MAX_UPDATE_TITLE_LENGTH) is not None
    assert UpdateChecker._safe_title("x" * (MAX_UPDATE_TITLE_LENGTH + 1)) is None
    assert UpdateChecker._safe_title("bad\x1b[31mtitle") is None
    assert UpdateChecker._safe_size("2 GB") == "2 GB"
    assert UpdateChecker._safe_size("x" * (MAX_UPDATE_SIZE_LENGTH + 1)) is None
    assert UpdateChecker._safe_size("2\x1b[31m GB") is None


def test_fingerprint_length_and_c1_controls_are_rejected():
    valid = "Infinix/X6873-OP/Infinix-X6873:16/B/I:user/release-keys"
    assert parse_fingerprint(valid) is not None
    assert parse_fingerprint(valid + "x" * (MAX_FINGERPRINT_LENGTH + 1)) is None
    assert has_control_chars("nel\x85end") is True
    assert has_unsafe_url_chars("nel\x85end") is True


def test_terminal_output_decodes_entities_once_and_strips_ansi():
    assert format_update_description("literal &amp;lt;tag&amp;gt; &amp;amp;") == (
        "literal &lt;tag&gt; &amp;"
    )
    out = format_update_description("hello\x1b[31mred\x1b[0m")
    assert out == "hellored"
    assert "\x1b" not in out
    assert format_update_description("bad\x07bell") == "bad\\x07bell"


def test_crlf_line_endings_normalize_before_parsing():
    """CRLF is a line ending, not a stray control byte worth escaping."""
    assert format_update_description("alpha<br>\r\nbeta") == "alpha\nbeta"
    assert format_update_description("alpha\r\nbeta") == "alpha\nbeta"
    assert "x0d" not in format_update_description("alpha<br>\r\nbeta")


def test_log_sanitizer_neutralizes_ansi_and_controls():
    cleaned = sanitize_log_text("a\x1b[31mb\x07c\x85d")
    assert "\x1b" not in cleaned
    assert "\x07" not in cleaned
    assert "\x85" not in cleaned
    assert cleaned == "ab\\x07c\\x85d"


def test_telegram_sanitizer_normalizes_lists_and_blocks():
    assert sanitize_html("<ul><li>One</li><li>Two</li></ul>") == "- One\n- Two"
    assert sanitize_html("<p>Para one</p><p>Para two</p>") == "Para one\n\nPara two"
    assert sanitize_html("<h3>Head</h3><div>body</div>") == "<b>Head</b>\n\nbody"
    assert sanitize_html("<strong>bold</strong>") == "<b>bold</b>"


def test_release_closed_claim_does_not_raise(tmp_path):
    claim = (tmp_path / "claim.lock").open("a+", encoding="utf-8")
    claim.close()
    release_processed_claim(claim)


def test_get_cached_ota_metadata_honors_explicit_proxy_override(tmp_path):
    ctx = _ctx(tmp_path, zip_proxy=False)
    received = {}
    valid = {"fingerprint": "Infinix/X6873-OP/Infinix-X6873:16/B/I:user/release-keys"}

    def fake_fetch(url, session=None, stop_event=None, use_proxy_env=False):
        received["session"] = session
        received["use_proxy_env"] = use_proxy_env
        return valid

    with patch("checkota.processor.get_ota_metadata", fake_fetch):
        assert get_cached_ota_metadata(ctx, "https://x/one.zip", use_proxy_env=True) == valid
    assert received["session"] is ctx.session()
    assert received["use_proxy_env"] is True

    proxy_ctx = _ctx(tmp_path, zip_proxy=True)
    received.clear()
    with patch("checkota.processor.get_ota_metadata", fake_fetch):
        assert (
            get_cached_ota_metadata(proxy_ctx, "https://x/two.zip", use_proxy_env=False)
            == valid
        )
    assert received["session"] is proxy_ctx.direct_session()
    assert received["use_proxy_env"] is False


def test_stopped_metadata_fetch_is_not_cached_even_when_valid(tmp_path):
    ctx = _ctx(tmp_path)
    calls = {"n": 0}
    valid = {"fingerprint": "Infinix/X6873-OP/Infinix-X6873:16/B/I:user/release-keys"}

    def fake_fetch(url, session=None, stop_event=None):
        calls["n"] += 1
        assert stop_event is not None
        stop_event.set()
        return valid

    with patch("checkota.processor.get_ota_metadata", fake_fetch):
        assert get_cached_ota_metadata(ctx, "https://x/stopped.zip") is None
    assert calls["n"] == 1
    assert "https://x/stopped.zip" not in ctx.metadata_cache
    assert "https://x/stopped.zip" not in ctx.metadata_failures

    ctx.stop_event.clear()
    with patch("checkota.processor.get_ota_metadata", return_value=valid) as retry:
        assert get_cached_ota_metadata(ctx, "https://x/stopped.zip") == valid
    assert retry.call_count == 1


def test_emergency_drain_style_options_ignore_stop_and_skip_delay(tmp_path):
    ctx = _ctx(tmp_path)
    ctx.stop_event.set()
    ctx.pending_notifications.append(
        PendingNotification(
            msg="<b>x</b>", device_title="D", title="T", is_new_update=False
        )
    )
    sent = []

    class _Notifier:
        def send(self, msg, truncate_desc=True, device_title=None):
            sent.append(device_title)
            return True

    with patch("checkota.processor.create_notifier", return_value=_Notifier()):
        rc = drain_pending_notifications(
            ctx, _args(dry_run=False), delay=0.0, ignore_stop_event=True
        )
    assert rc == 0
    assert sent == ["D"]


def test_drain_deadline_retains_and_releases_unsent_claims(tmp_path):
    ctx = _ctx(tmp_path)
    ctx.pending_notifications.append(
        PendingNotification(
            msg="<b>x</b>", device_title="D", title="T", is_new_update=True
        )
    )
    ctx.claimed_titles.add("T")
    claim = (tmp_path / "claim.lock").open("a+", encoding="utf-8")
    ctx.claimed_handles["T"] = claim

    with patch("checkota.processor.create_notifier", return_value=object()):
        rc = drain_pending_notifications(
            ctx,
            _args(dry_run=False),
            deadline=time.monotonic() - 1,
        )
    assert rc == 1
    assert "T" not in ctx.claimed_titles
    assert "T" not in ctx.claimed_handles


def test_drain_deadline_stops_between_sends(tmp_path):
    ctx = _ctx(tmp_path)
    for index in range(3):
        ctx.pending_notifications.append(
            PendingNotification(
                msg="<b>x</b>",
                device_title=f"D{index}",
                title=f"T{index}",
                is_new_update=False,
            )
        )
    sent = []

    class _SlowNotifier:
        def send(self, msg, truncate_desc=True, device_title=None):
            sent.append(device_title)
            time.sleep(0.03)
            return True

    with patch("checkota.processor.create_notifier", return_value=_SlowNotifier()):
        rc = drain_pending_notifications(
            ctx,
            _args(dry_run=False),
            delay=0.0,
            deadline=time.monotonic() + 0.01,
        )
    assert rc == 1
    assert sent == ["D0"]
    assert len(ctx.pending_notifications) == 2


def test_apply_update_actions_aborts_when_stop_requested(tmp_path):
    ctx = _ctx(tmp_path)
    ctx.stop_event.set()
    assert apply_update_actions(ctx, _update(), _args()) == 130


def test_stop_after_config_rewrite_still_buffers_notification(tmp_path, monkeypatch):
    """A stop arriving after the config rewrite must not drop the update.

    The config file already carries the new fingerprint, so the next run can no
    longer rediscover this update. Bailing on the stop check here would make the
    notification permanently unretryable; it has to reach the drain buffer.
    """
    config_path = tmp_path / "config-X6873.yml"
    config_path.write_text(
        'oem: "Infinix"\nproduct_base: "X6873"\n'
        'model: "Infinix GT 30 Pro"\nandroid_version: "15"\n'
        'regions:\n  OP: "131015"\n',
        encoding="utf-8",
    )
    cfg = Config.from_yaml(config_path)[0]
    ctx = _ctx(tmp_path)
    update = _update(
        cfg=cfg,
        config_path=config_path,
        target_fp=(
            "Infinix/X6873-OP/Infinix-X6873:16/"
            "BP2A.250605.031.A3/201500011:user/release-keys"
        ),
        target_incremental="201500011",
    )
    real_rewrite = processor.update_config_from_fingerprint

    def rewrite_then_stop(*args):
        result = real_rewrite(*args)
        # The config now holds the new fingerprint; signal shutdown before
        # this workstream reaches the notification step.
        ctx.stop_event.set()
        return result

    class _StubNotifier:
        def send(self, msg, truncate_desc=True, device_title=None):
            raise AssertionError("sweep mode must buffer, not send directly")

    monkeypatch.setattr(processor, "update_config_from_fingerprint", rewrite_then_stop)
    with patch("checkota.processor.create_notifier", return_value=_StubNotifier()):
        rc = apply_update_actions(
            ctx, update, _args(no_config=False, config_dir=tmp_path, skip_telegram=False)
        )

    # The stop check must not fire here: buffering is local and the drain owns
    # stop handling. Returning 0 mirrors the pre-regression behaviour.
    assert rc == 0, f"Expected the buffered notification path to succeed, got {rc}"
    assert [note.title for note in ctx.pending_notifications] == [update.title], (
        "Notification for an already-rewritten config was dropped"
    )


@pytest.mark.parametrize("allow_after_stop", [False, True])
def test_no_config_send_still_aborts_when_stop_requested(tmp_path, allow_after_stop):
    """--fp never rewrites a file, so it remains retryable after a stop."""
    ctx = _ctx(tmp_path)
    ctx.stop_event.set()
    sent: list[str] = []

    class _StubNotifier:
        def send(self, msg, truncate_desc=True, device_title=None):
            sent.append(str(device_title))
            return True

    rc = processor._dispatch_or_buffer_notification(
        ctx, _StubNotifier(), _update(), _args(no_config=True, skip_telegram=False), False,
        allow_after_stop=allow_after_stop,
    )
    assert rc == 130
    assert sent == []
    assert ctx.pending_notifications == []


def test_metadata_rejects_oversized_values(monkeypatch):
    payload = b"post-build=" + b"A" * 600 + b"\n"
    monkeypatch.setattr(metadata, "fetch_zip_member", lambda *a, **k: payload)
    assert metadata.get_ota_metadata("https://example.com/ota.zip") is None


def test_script_timeout_helpers_reject_non_finite(monkeypatch):
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    # Resolve optional scripts dynamically after adding their runtime path.
    check_update_proxy = pytest.importorskip("check_update_proxy")
    fetch_spys = pytest.importorskip("fetch_spys")

    for bad in ("nan", "inf", "-inf"):
        with pytest.raises(argparse.ArgumentTypeError):
            fetch_spys._positive_float(bad)
        with pytest.raises(argparse.ArgumentTypeError):
            check_update_proxy._positive_float(bad)
