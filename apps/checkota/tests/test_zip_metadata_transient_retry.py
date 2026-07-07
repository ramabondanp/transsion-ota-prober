"""H2 fix — transient transport / HTTP statuses are retried; structural failures are not."""

from unittest.mock import MagicMock

import pytest
import requests

from modules.zip_metadata import (
    _range_get,
    RemoteZipFetchError,
    RemoteZipTransientError,
)
from modules.metadata import get_ota_metadata


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

    monkeypatch.setattr("modules.metadata.fetch_zip_member", fake_fetch)
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

    monkeypatch.setattr("modules.metadata.fetch_zip_member", fake_fetch)
    result = get_ota_metadata("https://x/y.zip", session=MagicMock(), stop_event=None)
    assert calls["n"] == 1, "Structural failure must be raised on first attempt only"
    assert result is None
