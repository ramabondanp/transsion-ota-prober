"""Regression tests for final Telegram payload sanitization."""

from pathlib import Path

from checkota.manager import Config
from checkota.models import RegionUpdate
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

    def post(self, url, json=None, timeout=None, **kwargs):
        self.posts.append((url, json, timeout))
        return _Response()


def _update(desc: str, title: str = "TECNO <hack>") -> RegionUpdate:
    return RegionUpdate(
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


def _sent_text(update: RegionUpdate, *, truncate_desc: bool = True) -> str:
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
        + ("5 < 7 & x. " * 500)
        + "</font></small><br>\n"
    )
    text = _sent_text(_update(long_desc), truncate_desc=True)

    assert '<a href="https://telegra.ph/full">Read full changelogs</a>' in text
    assert "5 &lt; 7 &amp; x" in text
    assert "<small>" not in text
    assert "<font" not in text
    assert "<br" not in text


def test_final_limit_applies_without_description_match_or_truncation():
    session = _Session()
    notifier = TgNotify("token", "chat", session=session)  # type: ignore[arg-type]

    assert notifier.send("x" * 6000, truncate_desc=False)
    text = session.posts[-1][1]["text"]

    assert TgNotify._tokenize_telegram_html(text) is not None
    assert TgNotify._rendered_length(text) <= TgNotify.MAX_LEN


def test_final_limit_does_not_split_entities_or_supported_tags():
    msg = (
        "<blockquote><b>Header</b> "
        '<a href="https://example.com/?a=1&amp;b=2">'
        + ("&lt;value&gt; " * 1000)
        + "</a></blockquote>"
    )

    fitted = TgNotify._fit_telegram_html(msg, 120)

    assert fitted is not None
    assert TgNotify._tokenize_telegram_html(fitted) is not None
    assert TgNotify._rendered_length(fitted) <= 120
    assert "<a href=" in fitted


def test_unbalanced_supported_tag_degrades_to_escaped_plain_text():
    session = _Session()
    notifier = TgNotify("token", "chat", session=session)  # type: ignore[arg-type]

    # Invalid structure must degrade to literal plain text, not drop the
    # notification entirely.
    assert notifier.send("<b>unterminated", truncate_desc=False) is True
    text = session.posts[-1][1]["text"]
    assert text == "&lt;b&gt;unterminated"
    assert TgNotify._tokenize_telegram_html(text) is not None


def test_common_and_arbitrary_entities_are_canonicalized_before_fitting():
    session = _Session()
    notifier = TgNotify("token", "chat", session=session)  # type: ignore[arg-type]

    assert notifier.send(
        "A&nbsp;B &copy; &apos; &NotEqualTilde; &#169; &#x1F600; &unknown;",
        truncate_desc=False,
    )
    text = session.posts[-1][1]["text"]

    assert text == "A\u00a0B \u00a9 ' \u2242\u0338 \u00a9 \U0001f600 &amp;unknown;"
    assert TgNotify._tokenize_telegram_html(text) is not None


def test_entity_and_tag_heavy_message_is_not_limited_by_raw_markup_length():
    session = _Session()
    notifier = TgNotify("token", "chat", session=session)  # type: ignore[arg-type]
    message = "<b>&lt;</b>" * 700

    assert notifier.send(message, truncate_desc=False)
    text = session.posts[-1][1]["text"]

    assert len(text) > TgNotify.MAX_LEN
    assert "..." not in text
    assert TgNotify._rendered_length(text) == 700
    assert TgNotify._tokenize_telegram_html(text) is not None


def test_numeric_entities_obey_rendered_utf16_limit():
    fitted = TgNotify._fit_telegram_html("&#x1F600;" * 10, 11)

    assert fitted is not None
    assert fitted == "\U0001f600" * 4 + "..."
    assert TgNotify._rendered_length(fitted) == 11


def test_common_named_entities_can_be_fitted_at_the_rendered_limit():
    fitted = TgNotify._fit_telegram_html("&nbsp;&copy;&apos;" * 2000, TgNotify.MAX_LEN)

    assert fitted is not None
    assert TgNotify._rendered_length(fitted) == TgNotify.MAX_LEN
    assert fitted.endswith("...")
    assert "&nbsp;" not in fitted
    assert "&copy;" not in fitted
    assert "&apos;" not in fitted


def test_anchor_entities_are_canonicalized_and_tags_remain_balanced():
    fitted = TgNotify._fit_telegram_html(
        '<blockquote><a href="https://example.com/?x=1&amp;y=&quot;two&quot;">'
        "&copy; &lt;safe&gt;</a></blockquote>",
        100,
    )

    assert fitted == (
        '<blockquote><a href="https://example.com/?x=1&amp;y=&quot;two&quot;">'
        "\u00a9 &lt;safe&gt;</a></blockquote>"
    )
    assert TgNotify._tokenize_telegram_html(fitted) is not None


def test_invalid_and_control_entities_degrade_or_fail_closed():
    """Hostile entities yield safe plain text, or nothing when unsalvageable."""
    cases = (
        ("&#0;", "\ufffd"),
        ("&#x1F;", None),
        ("&#10;", None),  # whitespace-only content is dropped
        ("&#x7F;", None),
        ("&#xD800;", "\ufffd"),
        ("&#x110000;", "\ufffd"),
        ("&#xFDD0;", None),
        ("&#xFFFF;", None),
        ("&NewLine;", None),  # whitespace-only content is dropped
        ("raw\x00control", "rawcontrol"),
        ("raw\x1ccontrol", "rawcontrol"),
    )

    for value, expected in cases:
        session = _Session()
        notifier = TgNotify("token", "chat", session=session)  # type: ignore[arg-type]
        result = notifier.send(value, truncate_desc=False)
        if expected is None:
            assert result is False, value
            assert session.posts == [], value
        else:
            assert result is True, value
            text = session.posts[-1][1]["text"]
            assert text == expected, value
            assert TgNotify._tokenize_telegram_html(text) is not None, value
            assert TgNotify._rendered_length(text) <= TgNotify.MAX_LEN, value


def test_multiline_content_with_encoded_newline_still_delivered():
    session = _Session()
    notifier = TgNotify("token", "chat", session=session)  # type: ignore[arg-type]

    # Entity-encoded newlines cannot pass strict canonicalization, but real
    # content around them must survive via the plain-text fallback.
    assert notifier.send("line1&#10;line2", truncate_desc=False) is True
    assert session.posts[-1][1]["text"] == "line1\nline2"


def test_telegraph_link_url_is_escaped_into_a_balanced_anchor():
    """A quote in the Telegraph URL must not unbalance the tag stream.

    The URL is interpolated into an <a href="..."> attribute of a message that
    is canonicalized as a whole; an unescaped quote made the final fit fail and
    dropped the notification after the page had already been created.
    """
    session = _Session()

    def post(url, json=None, timeout=None, **kwargs):
        session.posts.append((url, json, timeout))
        if "createPage" in url:
            return type(
                "_TelegraphResponse",
                (),
                {
                    "raise_for_status": lambda self: None,
                    "json": lambda self: {
                        "ok": True,
                        "result": {"url": 'https://telegra.ph/a"b'},
                    },
                },
            )()
        return _Response()

    session.post = post  # type: ignore[method-assign]
    notifier = TgNotify("token", "chat", "telegraph", session=session)  # type: ignore[arg-type]

    assert notifier.send(
        build_notification_message(_update("Fixed things. " * 400)),
        truncate_desc=True,
        device_title="D",
    )

    ends = [url.rsplit("/", 1)[-1] for url, _, _ in session.posts]
    assert ends == ["createPage", "sendMessage"]
    text = session.posts[-1][1]["text"]
    assert 'href="https://telegra.ph/a&quot;b"' in text
    assert "Read full changelogs" in text


def test_unsafe_telegraph_link_is_dropped_and_notification_still_sent():
    session = _Session()

    def post(url, json=None, timeout=None, **kwargs):
        session.posts.append((url, json, timeout))
        if "createPage" in url:
            return type(
                "_TelegraphResponse",
                (),
                {
                    "raise_for_status": lambda self: None,
                    "json": lambda self: {
                        "ok": True,
                        "result": {"url": "https://telegra.ph/a\nb"},
                    },
                },
            )()
        return _Response()

    session.post = post  # type: ignore[method-assign]
    notifier = TgNotify("token", "chat", "telegraph", session=session)  # type: ignore[arg-type]

    assert notifier.send(
        build_notification_message(_update("Fixed things. " * 400)),
        truncate_desc=True,
        device_title="D",
    )

    text = session.posts[-1][1]["text"]
    assert "Read full changelogs" not in text
    assert "Fixed things." in text


def test_unfittable_markup_degrades_to_plain_text_instead_of_dropping(monkeypatch):
    """The final fit failure must fall back to escaped plain text."""
    from checkota import telegram

    real_fit = telegram.fit_telegram_html
    calls = {"n": 0}

    def failing_then_real(value, max_len):
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return real_fit(value, max_len)

    monkeypatch.setattr(telegram, "fit_telegram_html", failing_then_real)

    session = _Session()
    notifier = TgNotify("token", "chat", "", session=session)  # type: ignore[arg-type]
    assert notifier.send("<b>Alert</b> body", truncate_desc=False)

    assert calls["n"] == 2
    assert len(session.posts) == 1
    assert "&lt;b&gt;Alert&lt;/b&gt; body" in session.posts[-1][1]["text"]
