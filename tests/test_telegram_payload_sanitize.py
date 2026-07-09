"""Regression tests for final Telegram payload sanitization."""

from pathlib import Path

from checkota.manager import Config
from checkota.models import VariantUpdate
from checkota.notifier import build_notification_message
from checkota.telegram import TgNotify


class _Response:
    text = "ok"

    def raise_for_status(self):
        return None

    def json(self):
        return {"ok": True, "result": {"url": "https://telegra.ph/full"}}


class _Session:
    def __init__(self):
        self.posts = []

    def post(self, url, json=None, timeout=None):
        self.posts.append((url, json, timeout))
        return _Response()


def _update(desc: str, title: str = "TECNO <hack>") -> VariantUpdate:
    return VariantUpdate(
        cfg=Config(
            oem="Infinix",
            product="X6873-OP",
            device="Infinix-X6873",
            android_version="14",
            build_tag="B",
            incremental="I",
            model="Infinix GT 30 Pro",
        ),
        config_path=Path("/tmp/config-X6873.yml"),
        variant_label="Global",
        region_name="Global - OP Market",
        title=title,
        url="https://example.com/x.zip",
        size="2 GB",
        desc=desc,
        is_new_update=True,
        target_fp="Infinix/X6873-OP/Infinix-X6873:14/B/I:user/release-keys",
        target_incremental="I",
        sdk_message="Android 14",
        data={},
    )


def _sent_text(update: VariantUpdate, *, truncate_desc: bool = True) -> str:
    session = _Session()
    notifier = TgNotify("token", "chat", "telegraph", session=session)  # type: ignore[arg-type]
    assert notifier.send(
        build_notification_message(update), truncate_desc=truncate_desc
    )
    # Last post is sendMessage unless over-limit created Telegraph first.
    return session.posts[-1][1]["text"]


def test_final_payload_strips_ota_markup_and_escapes_text_nodes():
    desc = (
        '<small><font color=""#949494"">5 < 7 & x > y</font></small><br>\n'
        "<br>\nUpdate Version:<br>\n"
        '<small><font color=""#949494"">X6873-16.2</font></small><br>\n'
        "<br>\nUpdate Content:<br>\n"
        "Android Version<br>\n"
        '<small><font color=""#949494"">Safe text</font></small><br>\n'
    )
    text = _sent_text(_update(desc), truncate_desc=False)

    assert "<small>" not in text
    assert "<font" not in text
    assert "<br" not in text
    assert "&lt;small" not in text
    assert "&lt;font" not in text
    assert "&lt;br" not in text
    assert "5 &lt; 7 &amp; x &gt; y" in text
    assert "<b>Update Version:</b>" in text
    assert "<b>Update Content:</b>" in text
    assert "<b>Android Version</b>" in text
    assert "<b>Title:</b> TECNO &lt;hack&gt;" in text
    assert "&amp;lt;hack&amp;gt;" not in text


def test_over_limit_payload_preserves_telegraph_link_and_escaped_desc():
    long_desc = (
        '<small><font color=""#949494"">'
        + ("5 < 7 & x. " * 300)
        + "</font></small><br>\n"
    )
    text = _sent_text(_update(long_desc), truncate_desc=True)

    assert '<a href="https://telegra.ph/full">Read full changelogs</a>' in text
    assert "5 &lt; 7 &amp; x" in text
    assert "<small>" not in text
    assert "<font" not in text
    assert "<br" not in text
