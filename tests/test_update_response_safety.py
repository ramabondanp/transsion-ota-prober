"""Regression tests for untrusted check-in response fields."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from checkota.manager import Config
from checkota.update_checker import UpdateChecker


def _checker() -> UpdateChecker:
    return UpdateChecker(
        Config(
            oem="Infinix",
            product="X6873-OP",
            device="Infinix-X6873",
            android_version="14",
            build_tag="B",
            incremental="I",
            model="Infinix GT 30 Pro",
        ),
        session=MagicMock(),
    )


def test_update_url_requires_trusted_https_ota_origin():
    checker = _checker()

    assert checker._is_allowed_ota_url(
        "https://android.googleapis.com/packages/ota/x.zip"
    )
    assert checker._is_allowed_ota_url(
        "https://android.googleapis.com/packages/ota-api/package/"
        "3746a289a46815c7cd869c3b0d3f10b04dd40be5.zip"
    )
    assert not checker._is_allowed_ota_url("http://127.0.0.1/private")
    assert not checker._is_allowed_ota_url(
        "https://android.googleapis.com.evil.example/packages/ota/x.zip"
    )
    assert not checker._is_allowed_ota_url(
        "https://android.googleapis.com:8443/packages/ota/x.zip"
    )


def test_update_title_rejects_control_characters():
    checker = _checker()

    assert checker._safe_title("OTA 1") == "OTA 1"
    assert checker._safe_title("OTA 1\nOTA 2") is None
    assert checker._safe_title("OTA\x1b[31m") is None


def test_parse_drops_untrusted_url_and_unsafe_title():
    checker = _checker()
    response = SimpleNamespace(
        setting=[
            SimpleNamespace(name=b"update_url", value=b"http://127.0.0.1/private"),
            SimpleNamespace(name=b"update_title", value=b"OTA-A\nOTA-B"),
        ]
    )

    parsed = checker._parse(response)

    assert parsed["found"] is False
    assert parsed["url"] is None
    assert parsed["title"] is None
