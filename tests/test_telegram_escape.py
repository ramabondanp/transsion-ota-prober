"""H1 fix — Telegram HTML escape preserves literal <b></b> tags; user-content fields are escaped."""

from pathlib import Path

from checkota.manager import Config
from checkota.models import VariantUpdate
from checkota.notifier import build_notification_message


def _fake_update(
    title: str = "TECNO <hack>",
    desc: str = "a & b",
    sdk: str = "Android 14",
) -> VariantUpdate:
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
        region_name=None,
        title=title,
        url="https://example.com/x.zip",
        size="2 GB",
        desc=desc,
        is_new_update=True,
        target_fp="Infinix/X6873-OP/Infinix-X6873:14/B/I:user/release-keys",
        target_incremental="I",
        sdk_message=sdk,
        data={},
    )


def test_title_angle_brackets_escaped():
    msg = build_notification_message(_fake_update())
    assert "&lt;hack&gt;" in msg, msg


def test_literal_b_tags_preserved():
    msg = build_notification_message(_fake_update())
    assert "<b>Title:</b>" in msg, msg
    assert "<b>Device:</b>" in msg, msg
    assert "<b>Size:</b>" in msg, msg
    assert "<b>Fingerprint:</b>" in msg, msg


def test_os_b_tag_preserved_with_escaped_sdk():
    msg = build_notification_message(_fake_update(sdk='Android 14 "stable"'))
    # The literal <b>OS:</b> must NOT be escaped.
    assert "<b>OS:</b>" in msg, msg
    # SDK content gets escaped where appropriate. quote=False on the sdk
    # field preserves the double quotes as literal '"'.
    assert 'Android 14 "stable"' in msg, msg


def test_ampersand_in_desc_preserved_for_sanitizer():
    msg = build_notification_message(_fake_update(desc="before & after"))
    # Builder keeps OTA description raw so TgNotify._sanitize_html can parse
    # OTA markup (<small>/<font>/<br>) before final Telegram escaping.
    assert "before & after" in msg, msg
    assert "&amp; after" not in msg, msg


def test_url_quote_false_no_quot_entity():
    msg = build_notification_message(_fake_update())
    assert "&quot;" not in msg, msg
    assert "https://example.com/x.zip" in msg, msg


def test_no_escape_when_no_special_chars():
    msg = build_notification_message(
        _fake_update(title="Stable update", desc="plain text", sdk="Android 14")
    )
    # Safe input must NOT be over-escaped.
    assert "&amp;" not in msg
    assert "&lt;" not in msg
    assert "&gt;" not in msg
    assert "Stable update" in msg
    assert "plain text" in msg
