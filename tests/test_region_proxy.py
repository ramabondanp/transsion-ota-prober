"""Opt-in country routing is per check-in, never global (no live network)."""

import gzip
import os
import signal
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from unittest.mock import MagicMock

import pytest
import requests
from requests.adapters import HTTPAdapter
from requests.utils import select_proxy

from checkota import cli, proxy
from checkota.constants import CHECKIN_URL
from checkota.manager import Config
from checkota.processor import config_from_fingerprint
from checkota.runtime import RunContext
from checkota.update_checker import UpdateChecker, UpdateCheckError

TEMPLATE = "example-account__cr.{country}:example-password@gw.example:823"


@pytest.fixture(autouse=True)
def clean_proxy_environment(monkeypatch):
    for name in ("http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.upper(), raising=False)


@pytest.fixture
def env_file():
    path = proxy.ENV_FILE
    assert path is not None
    path.write_text(f'PROXMINT_PROXY_TEMPLATE="{TEMPLATE}"\n', encoding="utf-8")
    return path


def _cfg(region="IN"):
    return Config(
        oem="Infinix",
        product=f"X6873-{region}",
        device="Infinix-X6873",
        android_version="14",
        build_tag="B",
        incremental="I",
        model="Infinix GT 30 Pro",
    )


class _RecordingAdapter(HTTPAdapter):
    def __init__(self, barrier=None):
        super().__init__()
        self.calls = []
        self.lock = threading.Lock()
        self.barrier = barrier

    def send(
        self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None
    ):
        if self.barrier is not None:
            self.barrier.wait(timeout=5)
        with self.lock:
            self.calls.append((request, {"proxies": proxies}))
        response = requests.Response()
        response.status_code = 200
        response._content = b""  # Empty protobuf response: a successful no-update.
        response._content_consumed = True
        response.request = request
        return response


@pytest.fixture
def recorded_session():
    with requests.Session() as session:
        adapter = _RecordingAdapter()
        session.mount("https://", adapter)
        yield session, adapter


@pytest.mark.parametrize("region", ["IN", "RU"])
@pytest.mark.parametrize("source", ["file", "environment"])
@pytest.mark.parametrize("template", [TEMPLATE, "invalid"])
def test_default_check_does_not_read_proxy_settings(
    env_file, recorded_session, monkeypatch, region, source, template
):
    if source == "file":
        env_file.write_text(f"PROXMINT_PROXY_TEMPLATE={template}\n", encoding="utf-8")
    else:
        monkeypatch.setenv("PROXMINT_PROXY_TEMPLATE", template)
    loader = MagicMock(side_effect=AssertionError("proxy settings must be opt-in"))
    monkeypatch.setattr(proxy, "load_proxy_template", loader)
    session, adapter = recorded_session

    UpdateChecker(_cfg(region), session=session).check()

    loader.assert_not_called()
    assert not select_proxy(CHECKIN_URL, adapter.calls[0][1]["proxies"])


@pytest.mark.parametrize("opt_in", [False, True])
@pytest.mark.parametrize("mode", ["config", "directory", "fingerprint"])
def test_cli_region_proxy_flag_reaches_all_checkin_modes(
    env_file, recorded_session, monkeypatch, tmp_path, opt_in, mode
):
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    config_path = config_dir / "device.yml"
    config_path.write_text(
        'oem: "Infinix"\nproduct_base: "X6873"\nmodel: "GT 30 Pro"\n'
        'android_version: "14"\nregions:\n  IN: "1"\n  RU: "1"\n  OP: "1"\n',
        encoding="utf-8",
    )
    arguments = {
        "config": ["-c", str(config_path)],
        "directory": ["-d", str(config_dir), "--jobs", "3"],
        "fingerprint": ["--fp", _cfg("RU").fingerprint()],
    }[mode]
    if opt_in:
        arguments.append("--region-proxy")
    monkeypatch.setattr(sys, "argv", ["checkota", "--dry-run", *arguments])
    session, adapter = recorded_session
    ctx = RunContext(
        env={},
        processed_path=tmp_path / "processed.txt",
        processed_titles=set(),
        dry_run=True,
    )
    monkeypatch.setattr(ctx, "session", lambda: session)
    monkeypatch.setattr(cli, "create_run_context", lambda *args, **kwargs: ctx)
    monkeypatch.setattr(
        cli, "install_interrupt_handler", lambda ctx: signal.getsignal(signal.SIGINT)
    )

    assert cli.main() == 0

    regions = ["RU"] if mode == "fingerprint" else ["IN", "RU", "OP"]
    observed = {}
    for request, kwargs in adapter.calls:
        fingerprint = gzip.decompress(request.body)
        region = next(
            code for code in regions if f"X6873-{code}/".encode() in fingerprint
        )
        observed[region] = select_proxy(CHECKIN_URL, kwargs["proxies"])
    assert observed == {
        region: (
            "http://" + TEMPLATE.replace("{country}", region.lower())
            if opt_in and region in {"IN", "RU"}
            else None
        )
        for region in regions
    }
    assert not ctx.processed_path.exists()


@pytest.mark.parametrize("region", ["IN", "RU"])
@pytest.mark.parametrize("scheme", ["", "http://", "https://"])
def test_region_checkin_uses_matching_country(
    env_file, recorded_session, region, scheme
):
    env_file.write_text(
        f"PROXMINT_PROXY_TEMPLATE={scheme}{TEMPLATE}\n", encoding="utf-8"
    )
    session, adapter = recorded_session
    before = dict(os.environ)

    checker = UpdateChecker(_cfg(region), session=session, region_proxy=True)
    assert checker.check()[0] is False

    request, kwargs = adapter.calls[0]
    assert request.url == CHECKIN_URL
    assert kwargs["proxies"]["https"] == (
        f"{scheme or 'http://'}" + TEMPLATE.replace("{country}", region.lower())
    )
    assert session.proxies == {}
    assert dict(os.environ) == before


@pytest.mark.parametrize("region", ["OP", "OP-M1", "EU", "OP-IN", "RU-M1", ""])
def test_other_regions_do_not_load_template(env_file, recorded_session, region):
    env_file.write_text("PROXMINT_PROXY_TEMPLATE=invalid\n", encoding="utf-8")
    session, adapter = recorded_session
    UpdateChecker(_cfg(region), session=session, region_proxy=True).check()
    assert not select_proxy(CHECKIN_URL, adapter.calls[0][1]["proxies"])


@pytest.mark.parametrize("region", ["IN", "RU"])
def test_unconfigured_region_keeps_default_routing(recorded_session, region):
    session, adapter = recorded_session
    UpdateChecker(_cfg(region), session=session, region_proxy=True).check()
    assert not select_proxy(CHECKIN_URL, adapter.calls[0][1]["proxies"])
    assert proxy.ENV_FILE is not None
    assert not proxy.ENV_FILE.exists()


@pytest.mark.parametrize(
    "variable", ["HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"]
)
def test_explicit_environment_proxy_wins(
    env_file, recorded_session, monkeypatch, variable
):
    env_file.write_text("PROXMINT_PROXY_TEMPLATE=invalid\n", encoding="utf-8")
    monkeypatch.setenv(variable, "http://explicit.example:8080")
    session, adapter = recorded_session
    UpdateChecker(_cfg(), session=session, region_proxy=True).check()
    assert select_proxy(CHECKIN_URL, adapter.calls[0][1]["proxies"]) == (
        "http://explicit.example:8080"
    )


@pytest.mark.parametrize("key", ["https", "all", "https://android.googleapis.com"])
def test_explicit_session_proxy_wins(env_file, recorded_session, key):
    env_file.write_text("PROXMINT_PROXY_TEMPLATE=invalid\n", encoding="utf-8")
    session, adapter = recorded_session
    session.proxies[key] = "http://explicit.example:8080"
    UpdateChecker(_cfg(), session=session, region_proxy=True).check()
    assert select_proxy(CHECKIN_URL, adapter.calls[0][1]["proxies"]) == (
        "http://explicit.example:8080"
    )


@pytest.mark.parametrize("variable", ["NO_PROXY", "no_proxy"])
def test_no_proxy_exemption_is_preserved(
    env_file, recorded_session, monkeypatch, variable
):
    monkeypatch.setenv(variable, "android.googleapis.com")
    session, adapter = recorded_session
    UpdateChecker(_cfg(), session=session, region_proxy=True).check()
    assert not select_proxy(CHECKIN_URL, adapter.calls[0][1]["proxies"])


def test_direct_session_does_not_load_template(env_file, recorded_session):
    env_file.write_text("PROXMINT_PROXY_TEMPLATE=invalid\n", encoding="utf-8")
    session, adapter = recorded_session
    session.trust_env = False
    UpdateChecker(_cfg(), session=session, region_proxy=True).check()
    assert not select_proxy(CHECKIN_URL, adapter.calls[0][1]["proxies"])


def test_exported_template_takes_precedence(env_file, recorded_session, monkeypatch):
    monkeypatch.setenv(
        "PROXMINT_PROXY_TEMPLATE", TEMPLATE.replace("gw.example", "export.example")
    )
    session, adapter = recorded_session
    UpdateChecker(_cfg("RU"), session=session, region_proxy=True).check()
    assert adapter.calls[0][1]["proxies"]["https"].endswith("@export.example:823")


def test_dotenv_reads_only_proxy_setting(env_file, monkeypatch):
    env_file.write_text(
        f"# comment\n\nbot_token=not-loaded\nPROXMINT_PROXY_TEMPLATE='{TEMPLATE}'\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("bot_token", raising=False)
    assert proxy.load_proxy_template(env_file) == TEMPLATE
    assert "bot_token" not in os.environ


def test_wheel_can_use_exported_template_without_dotenv(recorded_session, monkeypatch):
    monkeypatch.setattr(proxy, "ENV_FILE", None)
    monkeypatch.setenv("PROXMINT_PROXY_TEMPLATE", TEMPLATE)
    session, adapter = recorded_session
    UpdateChecker(_cfg(), session=session, region_proxy=True).check()
    assert adapter.calls[0][1]["proxies"]["https"] == (
        "http://" + TEMPLATE.replace("{country}", "in")
    )


def test_parallel_and_reused_sessions_do_not_mix_regions(env_file, recorded_session):
    session, adapter = recorded_session
    adapter.barrier = threading.Barrier(3)
    configs = [_cfg(region) for region in ("IN", "RU", "OP")]
    with ThreadPoolExecutor(max_workers=3) as executor:
        results = list(
            executor.map(
                lambda cfg: UpdateChecker(
                    cfg, session=session, region_proxy=True
                ).check(),
                configs,
            )
        )
    assert all(result[0] is False for result in results)
    for request, kwargs in adapter.calls:
        fingerprint = gzip.decompress(request.body)
        cfg = next(cfg for cfg in configs if cfg.fingerprint().encode() in fingerprint)
        expected = (
            "http://"
            + TEMPLATE.replace("{country}", cfg.product.split("-", 1)[1].lower())
            if cfg.product != "X6873-OP"
            else None
        )
        assert select_proxy(CHECKIN_URL, kwargs["proxies"]) == expected

    # The same worker session can subsequently fetch ZIP metadata without
    # inheriting the last check-in's country proxy, even with --fetch-zip-proxy.
    adapter.barrier = None
    session.get("https://android.googleapis.com/packages/ota/example.zip").close()
    assert not select_proxy(CHECKIN_URL, adapter.calls[-1][1]["proxies"])
    assert session.proxies == {}


def test_direct_fingerprint_region_is_used(env_file, recorded_session):
    cfg = config_from_fingerprint(replace(_cfg(), product="X6873-RU").fingerprint())
    session, adapter = recorded_session
    UpdateChecker(cfg, session=session, region_proxy=True).check()
    assert adapter.calls[0][1]["proxies"]["https"].endswith(
        "__cr.ru:example-password@gw.example:823"
    )


@pytest.mark.parametrize(
    "template",
    [
        "example-account:example-password@gw.example:823",
        "example-account__cr.{other}:example-password@gw.example:823",
        "example-account__cr.{country}:example-password@gw.example:badport",
        "example-account__cr.{country}:example-password@gw.example:0",
        "example-account__cr.{country}:example-password@:823",
        "example-account__cr.{country}@gw.example:823",
        "socks5://example-account__cr.{country}:example-password@gw.example:823",
        TEMPLATE + "/path",
        TEMPLATE.replace("@", "\t@", 1),
    ],
)
def test_invalid_template_fails_before_network_without_secrets(
    recorded_session, monkeypatch, template
):
    monkeypatch.setenv("PROXMINT_PROXY_TEMPLATE", template)
    session, adapter = recorded_session
    with pytest.raises(UpdateCheckError) as error:
        UpdateChecker(_cfg(), session=session, region_proxy=True).check()
    assert "PROXMINT_PROXY_TEMPLATE" in str(error.value)
    assert "example-account" not in str(error.value)
    assert "example-password" not in str(error.value)
    assert not adapter.calls


def test_unreadable_dotenv_fails_without_exposing_decoder_input(recorded_session):
    assert proxy.ENV_FILE is not None
    proxy.ENV_FILE.write_bytes(b"PROXMINT_PROXY_TEMPLATE=private-credential\xff")
    session, adapter = recorded_session
    with pytest.raises(UpdateCheckError) as error:
        UpdateChecker(_cfg(), session=session, region_proxy=True).check()
    assert "could not read proxy settings" in str(error.value)
    assert "private-credential" not in "".join(traceback.format_exception(error.value))
    assert not adapter.calls


@pytest.mark.parametrize(
    "error_type", [requests.exceptions.ProxyError, requests.exceptions.InvalidURL]
)
def test_proxy_failures_redact_logs_and_tracebacks(
    env_file, recorded_session, monkeypatch, capsys, error_type
):
    session, _adapter = recorded_session
    url = "http://" + TEMPLATE.replace("{country}", "in")
    post = MagicMock(
        side_effect=error_type(f"Failed to connect: {url}; example-password")
    )
    monkeypatch.setattr(session, "post", post)
    monkeypatch.setattr(UpdateChecker, "_wait_for_retry", lambda *_: True)

    with pytest.raises(UpdateCheckError) as error:
        UpdateChecker(_cfg(), session=session, region_proxy=True).check()

    captured = capsys.readouterr()
    detail = (
        captured.out + captured.err + "".join(traceback.format_exception(error.value))
    )
    assert "example-password" not in detail
    assert "example-account" not in detail
    assert post.call_count == (3 if error_type is requests.exceptions.ProxyError else 1)
    assert all(call.kwargs["proxies"]["https"] == url for call in post.call_args_list)


def test_percent_encoded_proxy_credentials_are_redacted():
    url = "http://example%40account:example%3Apassword@gw.example:823"
    detail = proxy.redact_proxy_error(
        f"Failed: {url}; example%3Apassword; example:password; example@account", url
    )
    assert "example%" not in detail
    assert "example:password" not in detail
    assert "example@account" not in detail
