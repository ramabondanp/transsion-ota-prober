"""Unit tests for the operational scripts (no network access)."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import pytest

check_update_proxy = pytest.importorskip("check_update_proxy")
fetch_spys = pytest.importorskip("fetch_spys")

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"


# --- fetch_spys ----------------------------------------------------------------


def test_encode_base_handles_decimal_and_base62():
    assert fetch_spys._encode_base(0, 10) == "0"
    assert fetch_spys._encode_base(10, 10) == "10"
    assert fetch_spys._encode_base(35, 36) == "z"
    assert fetch_spys._encode_base(36, 62) == "A"
    assert fetch_spys._encode_base(61, 62) == "Z"


def test_unpack_packed_malformed_and_synthetic():
    assert fetch_spys.unpack_packed("not packed") == (None, None, None)

    packed = "}('0;1', 10, 2, 'var _0x1 = 1^var _0x2 = 2'.split('^'))"
    decoded, radix, count = fetch_spys.unpack_packed(packed)
    assert decoded == "var _0x1 = 1;var _0x2 = 2"
    assert (radix, count) == (10, 2)


def test_parse_vars_and_xor_decoding():
    values, unresolved = fetch_spys.parse_vars(
        "var a = 1; var b = a ^ 2; var c = b ^ 4; unknown ^ 1"
    )
    assert values == {"a": 1, "b": 3, "c": 7}
    assert unresolved == {}

    values, unresolved = fetch_spys.parse_vars("var a = b ^ 1")
    assert values == {}
    assert unresolved == {"a": "b ^ 1"}

    assert fetch_spys._xor_expression("1 ^ 2 ^ 4", {}) == 7
    assert fetch_spys._xor_expression("1 ^ x", {}) is None
    assert fetch_spys._xor_expression("1 ^ ^ 2", {}) is None


def test_decode_port_and_ipv4_validation():
    assert fetch_spys._decode_port("(_0x1^3)(_0x2^0)", {"_0x1": 1, "_0x2": 2}) == "22"
    assert fetch_spys._decode_port("(1^2)", {}) == "3"
    assert fetch_spys._decode_port("(1^2^8)", {}) is None
    assert fetch_spys._decode_port("no expressions", {}) is None

    assert fetch_spys._valid_ipv4("1.2.3.4")
    assert not fetch_spys._valid_ipv4("1.2.3")
    assert not fetch_spys._valid_ipv4("1.2.3.999")
    assert not fetch_spys._valid_ipv4("a.b.c.d")


def _proxy_html() -> str:
    return (
        "<html><script>"
        "}('0;1', 10, 2, 'var _0x1 = 1^var _0x2 = 2'.split('^'))"
        "</script>"
        '<font class="spy14">1.2.3.4<script>'
        'document.write(":(_0x1^3)(_0x2^0)")'
        "</script></font>"
        '<a href="proxy-list/"><font class="spy1">HTTPS</font></a>'
        '<font class="spy5">HIA</font>'
        "</html>"
    )


class _FakeResponse:
    def __init__(self, text: str):
        self.text = text

    def raise_for_status(self):
        return None


class _FakeSession:
    def __init__(self, text: str):
        self.text = text
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("get", url, kwargs))
        return _FakeResponse(self.text)

    def post(self, url, data=None, **kwargs):
        self.calls.append(("post", url, data, kwargs))
        return _FakeResponse(self.text)


def test_fetch_spys_parses_fixture_with_get():
    session = _FakeSession(_proxy_html())
    proxies = fetch_spys.fetch_spys("ph", limit=30, session=session)  # type: ignore[arg-type]

    assert proxies == [fetch_spys.Proxy("1.2.3.4", "22", "HTTPS", "HIA")]
    assert session.calls[0][0] == "get"
    assert session.calls[0][1].endswith("/PH/")
    assert "data" not in session.calls[0][2]


def test_fetch_spys_uses_post_for_non_default_limit():
    session = _FakeSession(_proxy_html())
    fetch_spys.fetch_spys("PH", limit=50, session=session)  # type: ignore[arg-type]

    method, _url, data, _kwargs = session.calls[0]
    assert method == "post"
    assert data == {"xpp": "1", "xf1": "0", "xf2": "0", "xf4": "0", "xf5": "0"}


def test_fetch_spys_rejects_invalid_inputs():
    with pytest.raises(ValueError):
        fetch_spys.fetch_spys("PHP")
    with pytest.raises(ValueError):
        fetch_spys.fetch_spys("PH", limit=31)
    with pytest.raises(ValueError):
        fetch_spys.fetch_spys("PH", timeout=0)


# --- check_update_proxy --------------------------------------------------------


def test_proxmint_proxy_builds_and_redacts():
    address = check_update_proxy._proxmint_proxy(
        "KE", "user__cr.{country}:secret@gw.example:8080"
    )
    assert address == "user__cr.ke:secret@gw.example:8080"

    redacted = check_update_proxy._redact_proxy_address(address)
    assert redacted == "*__cr.ke:***@gw.example:8080"
    assert "secret" not in redacted
    assert check_update_proxy._redact_proxy_address("plainhost:80") == "plainhost:80"


def test_proxmint_proxy_rejects_bad_template():
    with pytest.raises(ValueError):
        check_update_proxy._proxmint_proxy("KE", "no-placeholder@host:80")
    with pytest.raises(ValueError):
        check_update_proxy._proxmint_proxy("KE", "user:pass@host:80")


def test_proxy_url_maps_socks_and_http():
    assert check_update_proxy._proxy_url("h:1", "SOCKS5") == "socks5://h:1"
    assert check_update_proxy._proxy_url("h:1", "SOCKS4") == "socks5://h:1"
    assert check_update_proxy._proxy_url("h:1", "HTTPS") == "http://h:1"


def test_clean_output_and_summarize_output():
    assert check_update_proxy._clean_output_line("\x1b[91m✗\x1b[0m boom") == "✗ boom"

    outcome, hit, title, detail = check_update_proxy._summarize_output(
        "=> New OTA update found: Infinix-X6873-16.3.0.130-HIT", 0, "16.3"
    )
    assert (outcome, hit, title, detail) == (
        check_update_proxy.OUTCOME_UPDATE,
        True,
        "Infinix-X6873-16.3.0.130-HIT",
        "",
    )

    outcome, hit, _title, detail = check_update_proxy._summarize_output(
        "=> No updates found", 0, "16.3"
    )
    assert (outcome, hit, detail) == (check_update_proxy.OUTCOME_NO_UPDATE, False, "")

    outcome, hit, _title, detail = check_update_proxy._summarize_output(
        "Update check failed after multiple retries due to network error: Read timed out",
        1,
        "16.3",
    )
    assert outcome == check_update_proxy.OUTCOME_FAILED
    assert hit is False
    assert "ReadTimeout" in detail


def test_load_proxy_template_env_wins_over_file(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("PROXMINT_PROXY_TEMPLATE=fromfile@host:1\n", encoding="utf-8")
    monkeypatch.setattr(check_update_proxy, "ENV_FILE", env_file)
    monkeypatch.setenv("PROXMINT_PROXY_TEMPLATE", "fromenv@host:2")
    assert check_update_proxy._load_proxy_template() == "fromenv@host:2"

    monkeypatch.delenv("PROXMINT_PROXY_TEMPLATE")
    assert check_update_proxy._load_proxy_template() == "fromfile@host:1"


def test_positive_int_and_float_validation():
    assert check_update_proxy._positive_int("3") == 3
    assert check_update_proxy._positive_float("1.5") == 1.5
    for bad in ("0", "-1", "nan", "inf"):
        with pytest.raises(argparse.ArgumentTypeError):
            check_update_proxy._positive_int(bad)
        with pytest.raises(argparse.ArgumentTypeError):
            check_update_proxy._positive_float(bad)

    assert fetch_spys._positive_float("2.5") == 2.5
    for bad in ("0", "-1", "nan", "inf"):
        with pytest.raises(argparse.ArgumentTypeError):
            fetch_spys._positive_float(bad)


def test_env_file_is_not_committed_or_world_readable_if_present():
    env_path = SCRIPT_DIR / ".env"
    if not env_path.exists():
        pytest.skip("optional scripts/.env is not present")
    mode = os.stat(env_path).st_mode & 0o777
    assert mode & 0o077 == 0, f"{env_path} is too permissive: {oct(mode)}"
