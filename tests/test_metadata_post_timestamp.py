"""M1 fix — bad post-timestamp is logged at warning level; valid path still works."""

from checkota.metadata import get_ota_metadata


def _fake_fetch_returning_content(monkeypatch, content: str) -> None:
    def fake_fetch(url, member, **kwargs):
        return content.encode("utf-8")

    monkeypatch.setattr("checkota.metadata.fetch_zip_member", fake_fetch)


def test_invalid_timestamp_logs_warning(monkeypatch, capsys):
    _fake_fetch_returning_content(monkeypatch, "post-timestamp=not-a-number\n")
    result = get_ota_metadata("https://x/y.zip")
    captured = capsys.readouterr()
    assert result is not None
    assert "post_timestamp" in result
    assert "build_date" not in result  # not parsed
    # Log.w writes to stdout with ANSI yellow prefix; the message text is plain.
    assert "Could not parse post-timestamp" in captured.out


def test_valid_timestamp_builds_date(monkeypatch, capsys):
    _fake_fetch_returning_content(monkeypatch, "post-timestamp=1700000000\n")
    result = get_ota_metadata("https://x/y.zip")
    assert result is not None
    assert "post_timestamp" in result
    assert "build_date" in result
