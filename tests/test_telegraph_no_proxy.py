"""Tests for Telegraph page creation proxy bypass."""

from checkota.telegram import TgNotify


class _ProxyCheckSession:
    def __init__(self):
        self.kwargs_passed = {}

    def post(self, url, json=None, timeout=None, **kwargs):
        self.kwargs_passed = kwargs

        class MockResp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"ok": True, "result": {"url": "https://telegra.ph/test"}}

        return MockResp()


def test_create_telegraph_page_bypasses_proxies():
    session = _ProxyCheckSession()
    notifier = TgNotify("token", "chat", "telegraph", session=session)  # type: ignore[arg-type]
    url = notifier._create_telegraph_page("Test Title", "Test Content")
    assert url == "https://telegra.ph/test"
    assert "proxies" in session.kwargs_passed
    assert session.kwargs_passed["proxies"] == {
        "http": None,
        "https": None,
        "all": None,
    }


def test_telegram_send_bypasses_proxies():
    session = _ProxyCheckSession()
    notifier = TgNotify("token", "chat", "telegraph", session=session)  # type: ignore[arg-type]
    res = notifier.send("Test message", truncate_desc=False)
    assert res is True
    assert "proxies" in session.kwargs_passed
    assert session.kwargs_passed["proxies"] == {
        "http": None,
        "https": None,
        "all": None,
    }


def test_zip_metadata_bypasses_proxies():
    class _ZipProxyCheckSession:
        def __init__(self):
            self.get_kwargs = {}

        def get(self, url, headers=None, timeout=None, stream=False, **kwargs):
            self.get_kwargs = kwargs

            class MockResp:
                status_code = 206
                headers = {"Content-Range": "bytes 0-0/100"}
                content = b"x"

                def raise_for_status(self):
                    pass

                def close(self):
                    pass

            return MockResp()

    session = _ZipProxyCheckSession()
    from checkota.zip_metadata import _probe_size, _range_get

    _probe_size(session, "https://example.com/test.zip", 10.0, {})  # type: ignore[arg-type]
    assert session.get_kwargs.get("proxies") == {
        "http": None,
        "https": None,
        "all": None,
    }

    _range_get(session, "https://example.com/test.zip", 0, 10, 10.0, {})  # type: ignore[arg-type]
    assert session.get_kwargs.get("proxies") == {
        "http": None,
        "https": None,
        "all": None,
    }
