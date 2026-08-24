"""Regression tests for retryable check-in HTTP responses."""

import threading
from unittest.mock import MagicMock, patch

import pytest
import requests

from checkota.manager import Config
from checkota.update_checker import (
    _MAX_RESPONSE_BYTES,
    UpdateChecker,
    UpdateCheckError,
    checkin_generator_pb2,
)


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


class _Response:
    def __init__(self, status_code: int, body: bytes = b"", headers=None):
        self.status_code = status_code
        self.body = body
        self.headers = headers or {}
        self.close_calls = 0
        self.iter_calls = 0

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(
                f"HTTP {self.status_code}", response=self
            )

    def close(self):
        self.close_calls += 1

    def iter_content(self, chunk_size):
        self.iter_calls += 1
        for offset in range(0, len(self.body), chunk_size):
            yield self.body[offset : offset + chunk_size]


def _protobuf_body() -> bytes:
    return checkin_generator_pb2.AndroidCheckinResponse().SerializeToString()


@pytest.mark.parametrize("status", [408, 425, 429, 500, 502, 503, 504])
def test_retryable_http_status_retries_and_closes_response(status):
    first = _Response(status)
    second = _Response(200, _protobuf_body())
    session = MagicMock()
    session.post.side_effect = [first, second]
    checker = UpdateChecker(_cfg(), session=session)

    with patch("checkota.update_checker.time.sleep") as sleep:
        result = checker.check()

    assert result[0] is False
    assert session.post.call_count == 2
    sleep.assert_called_once_with(1)
    assert first.close_calls == 1
    assert second.close_calls == 1


def test_retryable_http_status_uses_exponential_backoff():
    responses = [_Response(503), _Response(503), _Response(503)]
    session = MagicMock()
    session.post.side_effect = responses
    checker = UpdateChecker(_cfg(), session=session)

    with (
        patch("checkota.update_checker.time.sleep") as sleep,
        pytest.raises(UpdateCheckError),
    ):
        checker.check()

    assert session.post.call_count == 3
    assert [call.args[0] for call in sleep.call_args_list] == [1, 2]
    assert all(response.close_calls == 1 for response in responses)


def test_nonretryable_http_status_is_single_attempt_and_closes_response():
    response = _Response(400)
    session = MagicMock()
    session.post.return_value = response
    checker = UpdateChecker(_cfg(), session=session)

    with (
        patch("checkota.update_checker.time.sleep") as sleep,
        pytest.raises(UpdateCheckError),
    ):
        checker.check()

    assert session.post.call_count == 1
    sleep.assert_not_called()
    assert response.close_calls == 1


def test_retry_backoff_stops_when_stop_event_is_set():
    response = _Response(503)
    session = MagicMock()
    session.post.return_value = response
    stop_event = MagicMock(spec=threading.Event)
    stop_event.is_set.return_value = False
    stop_event.wait.return_value = True
    checker = UpdateChecker(_cfg(), session=session, stop_event=stop_event)

    assert checker.check() == (False, None)
    assert session.post.call_count == 1
    stop_event.wait.assert_called_once_with(1)
    assert response.close_calls == 1


def test_redirect_is_not_followed_and_response_is_closed():
    response = _Response(307, headers={"Location": "https://example.com/collect"})
    session = MagicMock()
    session.post.return_value = response
    checker = UpdateChecker(_cfg(), session=session)

    with pytest.raises(UpdateCheckError, match="redirect HTTP 307 rejected"):
        checker.check()

    assert session.post.call_count == 1
    assert session.post.call_args.kwargs["allow_redirects"] is False
    assert session.post.call_args.kwargs["stream"] is True
    assert response.iter_calls == 0
    assert response.close_calls == 1


def test_oversized_streamed_response_is_rejected_at_limit():
    class _OversizedResponse(_Response):
        def iter_content(self, chunk_size):
            self.iter_calls += 1
            chunk = b"x" * chunk_size
            for _ in range(_MAX_RESPONSE_BYTES // chunk_size):
                yield chunk
            yield b"x"

    response = _OversizedResponse(200)
    session = MagicMock()
    session.post.return_value = response
    checker = UpdateChecker(_cfg(), session=session)

    with pytest.raises(UpdateCheckError, match="response exceeds"):
        checker.check()

    assert session.post.call_count == 1
    assert response.iter_calls == 1
    assert response.close_calls == 1


def test_success_response_is_parsed_from_streamed_chunks(tmp_path):
    proto = checkin_generator_pb2.AndroidCheckinResponse()
    setting = proto.setting.add()
    setting.name = b"update_title"
    setting.value = b"Update title"
    response = _Response(200, proto.SerializeToString())
    session = MagicMock()
    session.post.return_value = response
    checker = UpdateChecker(_cfg(), session=session)
    checker.debug_file = str(tmp_path / "checkin.txt")

    has_update, info = checker.check(debug=True)

    assert has_update is False
    assert info is not None
    assert info["title"] == "Update title"
    assert 'name: "update_title"' in (tmp_path / "checkin.txt").read_text(
        encoding="utf-8"
    )
    assert response.iter_calls == 1
    assert response.close_calls == 1


def test_truncated_stream_retries_and_closes_each_response():
    class _TruncatedResponse(_Response):
        def iter_content(self, chunk_size):
            self.iter_calls += 1
            yield b"partial"
            raise requests.exceptions.ChunkedEncodingError("truncated response")

    first = _TruncatedResponse(200)
    second = _Response(200, _protobuf_body())
    session = MagicMock()
    session.post.side_effect = [first, second]
    checker = UpdateChecker(_cfg(), session=session)

    with patch("checkota.update_checker.time.sleep") as sleep:
        result = checker.check()

    assert result[0] is False
    assert session.post.call_count == 2
    sleep.assert_called_once_with(1)
    assert first.close_calls == 1
    assert second.close_calls == 1


@pytest.mark.parametrize(
    "error",
    [
        requests.exceptions.ContentDecodingError("invalid gzip body"),
        requests.exceptions.SSLError("TLS connection interrupted"),
    ],
)
def test_transient_transport_errors_retry(error):
    response = _Response(200, _protobuf_body())
    session = MagicMock()
    session.post.side_effect = [error, response]
    checker = UpdateChecker(_cfg(), session=session)

    with patch("checkota.update_checker.time.sleep") as sleep:
        result = checker.check()

    assert result[0] is False
    assert session.post.call_count == 2
    sleep.assert_called_once_with(1)
    assert response.close_calls == 1
