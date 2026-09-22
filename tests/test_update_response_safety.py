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


def _parse_with(settings):
    checker = _checker()
    return checker._parse(SimpleNamespace(setting=settings))


def _setting(name, value):
    return SimpleNamespace(name=name, value=value)


def test_description_keeps_formatting_but_drops_other_control_bytes():
    parsed = _parse_with(
        [_setting(b"update_description", b"Fix A\n\tindented\x1b[31m and B\x07")]
    )

    assert parsed["description"] == "Fix A\n\tindented and B"


def test_oversized_description_is_dropped():
    parsed = _parse_with([_setting(b"update_description", b"x" * (65536 + 1))])

    assert parsed["description"] is None


def test_control_only_description_becomes_none():
    parsed = _parse_with([_setting(b"update_description", b"\x1b[2J\x1b[31m")])

    assert parsed["description"] is None


def test_register_update_rejects_mismatched_target(tmp_path, monkeypatch):
    import argparse

    from checkota import processor
    from checkota.runtime import RunContext

    cfg = _checker().cfg
    ctx = RunContext(
        env={},
        processed_path=tmp_path / "titles.txt",
        processed_titles=set(),
        dry_run=False,
    )
    args = argparse.Namespace(
        imei=None,
        debug=False,
        gen_fp=False,
        dry_run=False,
        fp=None,
        register_update=True,
        update_incremental=False,
        force_notify=False,
    )
    monkeypatch.setattr(
        processor,
        "_check_for_updates",
        lambda *a: (
            0,
            {
                "title": "OTHER-DEVICE-OTA",
                "size": "100",
                "url": "https://android.googleapis.com/packages/ota/a.zip",
                "description": "test",
            },
        ),
    )
    monkeypatch.setattr(
        processor,
        "get_cached_ota_metadata",
        lambda *a: {
            "fingerprint": "Infinix/X9999-OP/Infinix-X9999:16/B/2:user/release-keys"
        },
    )
    status, update = processor.collect_update_info(
        ctx, cfg, tmp_path / "config.yml", args
    )
    assert (status, update) == (1, None)
    assert not ctx.processed_path.exists()


def test_url_allowlist_rejects_empty_port_and_dot_segments():
    from checkota.constants import (
        CHECKIN_API_HOST,
        OTA_URL_PATH_PREFIXES,
        ZIP_REDIRECT_ALLOWED_HOSTS,
        ZIP_REDIRECT_PATH_PREFIXES,
    )
    from checkota.validation import is_google_https_url

    ota = {
        "allowed_hosts": (CHECKIN_API_HOST,),
        "path_prefixes": OTA_URL_PATH_PREFIXES,
    }
    redirect = {
        "allowed_hosts": ZIP_REDIRECT_ALLOWED_HOSTS,
        "path_prefixes": ZIP_REDIRECT_PATH_PREFIXES,
    }

    assert is_google_https_url(
        "https://android.googleapis.com/packages/ota/x.zip", **ota
    )
    assert not is_google_https_url(
        "https://android.googleapis.com:/packages/ota/x.zip", **ota
    )
    for escaped in (
        "https://android.googleapis.com/packages/ota/../secret",
        "https://android.googleapis.com/packages/ota/%2e%2e/secret",
        "https://redirector.gvt1.com/packages/../../x.zip",
    ):
        assert not is_google_https_url(escaped, **ota)
        assert not is_google_https_url(escaped, **redirect)
