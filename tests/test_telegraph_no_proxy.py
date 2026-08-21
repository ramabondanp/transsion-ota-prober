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


def test_zip_metadata_uses_proxy_env():
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

    _probe_size(session, "https://example.com/test.zip", 10.0, {}, use_proxy_env=True)  # type: ignore[arg-type]
    assert "proxies" not in session.get_kwargs

    _range_get(session, "https://example.com/test.zip", 0, 10, 10.0, {}, use_proxy_env=True)  # type: ignore[arg-type]
    assert "proxies" not in session.get_kwargs


def test_run_context_zip_session():
    from checkota.runtime import create_run_context

    ctx_direct = create_run_context(dry_run=True, zip_proxy=False)
    assert ctx_direct.zip_session() is ctx_direct.direct_session()
    assert ctx_direct.zip_session().trust_env is False

    ctx_proxy = create_run_context(dry_run=True, zip_proxy=True)
    assert ctx_proxy.zip_session() is ctx_proxy.session()
    assert ctx_proxy.zip_session().trust_env is True


def test_cli_parser_fetch_zip_proxy():
    from checkota.cli import build_parser

    parser = build_parser()
    args1 = parser.parse_args(["-c", "X6873", "--fetch-zip-proxy"])
    assert args1.fetch_zip_proxy is True

    args2 = parser.parse_args(["-c", "X6873", "--zip-proxy"])
    assert args2.fetch_zip_proxy is True

    args3 = parser.parse_args(["-c", "X6873"])
    assert args3.fetch_zip_proxy is False

