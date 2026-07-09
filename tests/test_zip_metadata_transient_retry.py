"""H2 fix — transient transport / HTTP statuses are retried; structural failures are not."""

import io
import zipfile
from unittest.mock import MagicMock

import pytest
import requests

from checkota.zip_metadata import (
    _LOCAL_SIG,
    _range_get,
    RemoteZipFetchError,
    RemoteZipTransientError,
    fetch_zip_member,
)
from checkota.metadata import get_ota_metadata


class _RangeSession:
    """Fake requests.Session serving byte ranges from in-memory `data`."""

    def __init__(self, data: bytes):
        self.data = data
        self.ranges = []  # (start, end) requested, in order

    def get(self, url, headers=None, timeout=None, **kwargs):
        rng = (headers or {}).get("Range", "")
        start_s, end_s = rng.split("=", 1)[1].split("-")
        start, end = int(start_s), int(end_s)
        self.ranges.append((start, end))
        chunk = self.data[start : end + 1]
        resp = MagicMock()
        resp.status_code = 206
        resp.content = chunk
        resp.headers = {"Content-Range": f"bytes {start}-{end}/{len(self.data)}"}
        resp.raise_for_status = MagicMock()
        return resp


def _build_zip(member_name: str, content: bytes, compress_type: int) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=compress_type) as zf:
        zf.writestr(member_name, content)
    return buf.getvalue()


def _session_with_error(exc):
    s = MagicMock()
    s.get.side_effect = exc
    return s


def _session_with_status(status_code):
    s = MagicMock()
    resp = MagicMock()
    resp.status_code = status_code
    resp.raise_for_status.side_effect = requests.exceptions.HTTPError(
        response=MagicMock(status_code=status_code)
    )
    s.get.return_value = resp
    return s


def test_request_exception_is_transient():
    sess = _session_with_error(requests.exceptions.ConnectionError("net"))
    with pytest.raises(RemoteZipTransientError):
        _range_get(sess, "https://x/y.zip", 0, 10, 5.0, {})


def test_chunked_encoding_is_transient():
    sess = _session_with_error(requests.exceptions.ChunkedEncodingError("c"))
    with pytest.raises(RemoteZipTransientError):
        _range_get(sess, "https://x/y.zip", 0, 10, 5.0, {})


def test_ssl_is_transient():
    sess = _session_with_error(requests.exceptions.SSLError("ssl"))
    with pytest.raises(RemoteZipTransientError):
        _range_get(sess, "https://x/y.zip", 0, 10, 5.0, {})


@pytest.mark.parametrize("status", [500, 502, 503, 504, 429, 408, 425])
def test_retryable_http_status_is_transient(status):
    sess = _session_with_status(status)
    with pytest.raises(RemoteZipTransientError):
        _range_get(sess, "https://x/y.zip", 0, 10, 5.0, {})


@pytest.mark.parametrize("status", [416, 403, 404])
def test_non_retryable_http_status_is_structural(status):
    sess = _session_with_status(status)
    with pytest.raises(RemoteZipFetchError) as info:
        _range_get(sess, "https://x/y.zip", 0, 10, 5.0, {})
    # Must NOT be a transient subclass.
    assert not isinstance(info.value, RemoteZipTransientError)


def test_get_ota_metadata_retries_on_transient(monkeypatch):
    """A transient error must consume the retry budget, not exit on first hit."""
    calls = {"n": 0}

    def fake_fetch(url, member, **kwargs):
        calls["n"] += 1
        if calls["n"] < 2:
            raise RemoteZipTransientError("simulated transient")
        # 2nd call: return empty bytes; metadata path returns None.
        return b""

    monkeypatch.setattr("checkota.metadata.fetch_zip_member", fake_fetch)
    result = get_ota_metadata("https://x/y.zip", session=MagicMock(), stop_event=None)
    assert calls["n"] >= 2, (
        "Expected at least 2 fetch_zip_member calls on transient error"
    )
    assert result is None


def test_get_ota_metadata_does_not_retry_on_structural(monkeypatch):
    """A structural error must NOT consume retry budget."""
    calls = {"n": 0}

    def fake_fetch(url, member, **kwargs):
        calls["n"] += 1
        raise RemoteZipFetchError("bad EOCD signature")

    monkeypatch.setattr("checkota.metadata.fetch_zip_member", fake_fetch)
    result = get_ota_metadata("https://x/y.zip", session=MagicMock(), stop_event=None)
    assert calls["n"] == 1, "Structural failure must be raised on first attempt only"
    assert result is None


def test_range_get_attempts_2_succeeds_on_second_transient(monkeypatch):
    """attempts=2 must retry once on transient and succeed when 2nd try works."""
    monkeypatch.setattr("checkota.zip_metadata.time.sleep", lambda _s: None)
    calls = {"n": 0}

    def fake_get(url, headers, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            raise requests.exceptions.ConnectionError("transient")
        resp = MagicMock()
        resp.status_code = 206
        resp.content = b"payload"
        resp.raise_for_status = MagicMock()
        return resp

    sess = MagicMock()
    sess.get.side_effect = fake_get
    result = _range_get(sess, "https://x/y.zip", 0, 10, 5.0, {}, attempts=2)
    assert result == b"payload"
    assert calls["n"] == 2, "Expected exactly 2 attempts"


def test_range_get_attempts_2_non_retryable_stays_single_call():
    """Non-retryable 416 must raise immediately even with attempts=2."""
    calls = {"n": 0}

    def fake_get(url, headers, timeout):
        calls["n"] += 1
        resp = MagicMock()
        resp.status_code = 416
        resp.raise_for_status.side_effect = requests.exceptions.HTTPError(
            response=MagicMock(status_code=416)
        )
        return resp

    sess = MagicMock()
    sess.get.side_effect = fake_get
    with pytest.raises(RemoteZipFetchError) as info:
        _range_get(sess, "https://x/y.zip", 0, 10, 5.0, {}, attempts=2)
    assert not isinstance(info.value, RemoteZipTransientError)
    assert calls["n"] == 1, "Non-retryable 416 must not trigger a retry"


def test_fetch_zip_member_merges_local_header_and_payload():
    """The 30-byte local file header and the compressed payload must be fetched
    in ONE Range request (no separate 30-byte header request), and that single
    request keeps attempts=2 retry behaviour. Verified for both stored and
    deflated members."""
    content = b"post-build: X/Y/Z:14/A/B:123:user/release-keys\n"
    member = "META-INF/com/android/metadata"
    for compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
        data = _build_zip(member, content, compress_type)
        sess = _RangeSession(data)
        result = fetch_zip_member("https://x/y.zip", member, session=sess)
        assert result == content

        # The combined request starts exactly at the local header offset.
        local_off = data.find(_LOCAL_SIG)
        combined = [r for r in sess.ranges if r[0] == local_off]
        assert combined, f"no combined header+payload request: {sess.ranges}"

        # Regression guard: there must be no standalone 30-byte header request.
        standalone = [r for r in sess.ranges if r[1] - r[0] + 1 == 30]
        assert not standalone, f"unexpected standalone header request: {standalone}"

        # probe (0-0) + tail + combined header+payload (CD sits in the tiny
        # tail, so no separate central-directory request).
        assert len(sess.ranges) == 3, f"unexpected request count: {sess.ranges}"
