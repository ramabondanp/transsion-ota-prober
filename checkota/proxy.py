"""Optional country-specific proxies for check-in requests only."""

import os
from pathlib import Path
from urllib.parse import unquote, urlsplit

import requests
from requests.utils import get_environ_proxies, select_proxy, should_bypass_proxies

from checkota.constants import CHECKIN_URL
from checkota.manager import region_code_from_product
from checkota.paths import IS_SOURCE_CHECKOUT, PROJECT_ROOT

PROXMINT_PROXY_TEMPLATE_ENV = "PROXMINT_PROXY_TEMPLATE"
ENV_FILE = PROJECT_ROOT / "scripts" / ".env" if IS_SOURCE_CHECKOUT else None
_AUTO_PROXY_REGIONS = frozenset({"IN", "RU"})


def load_proxy_template(env_file: Path | None) -> str:
    """Read just the proxy setting, without exporting any dotenv values."""
    value = os.environ.get(PROXMINT_PROXY_TEMPLATE_ENV, "").strip()
    if value or env_file is None:
        return value
    try:
        lines = env_file.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return ""
    except (OSError, UnicodeError):
        # Decoder exceptions can include fragments of the private file.
        raise ValueError(f"could not read proxy settings from {env_file}") from None

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, raw_value = stripped.split("=", 1)
        if name.strip() == PROXMINT_PROXY_TEMPLATE_ENV:
            return raw_value.strip().strip("'\"")
    return ""


def _proxy_url(country: str, template: str) -> str:
    """Render and validate an HTTP(S) endpoint without exposing its credentials."""
    message = (
        f"{PROXMINT_PROXY_TEMPLATE_ENV} must look like "
        "http://user__cr.{country}:password@host:port (http:// is optional)"
    )
    remainder = template.replace("{country}", "")
    if "{country}" not in template or "{" in remainder or "}" in remainder:
        raise ValueError(message)
    address = template.replace("{country}", country.lower())
    url = address if "://" in address else f"http://{address}"
    try:
        parsed = urlsplit(url)
        port = parsed.port
        valid = (
            parsed.scheme in {"http", "https"}
            and parsed.hostname
            and parsed.username
            and parsed.password
            and port is not None
            and port > 0
            and not parsed.path
            and not parsed.query
            and not parsed.fragment
            and not any(c.isspace() or ord(c) < 32 or 127 <= ord(c) <= 159 for c in url)
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError(message)
    return url


def checkin_proxy_url(product: str, session: requests.Session) -> str | None:
    """Select an IN/RU fallback when opted in, preserving explicit routing.

    Called by UpdateChecker only when regional proxies are enabled.
    Never mutate the environment or session: a worker may reuse its session for
    another region or for ZIP metadata later.
    """
    region = region_code_from_product(product)
    if region is None or region not in _AUTO_PROXY_REGIONS or not session.trust_env:
        return None
    if select_proxy(CHECKIN_URL, session.proxies):
        return None
    no_proxy = os.environ.get("no_proxy") or os.environ.get("NO_PROXY")
    if should_bypass_proxies(CHECKIN_URL, no_proxy=no_proxy):
        return None
    if select_proxy(CHECKIN_URL, get_environ_proxies(CHECKIN_URL)):
        return None
    template = load_proxy_template(ENV_FILE)
    return _proxy_url(region, template) if template else None


def redact_proxy_error(detail: str, proxy_url: str | None) -> str:
    """Scrub automatically selected proxy credentials from request failures."""
    if not proxy_url:
        return detail
    parsed = urlsplit(proxy_url)
    secrets = {
        proxy_url,
        parsed.netloc,
        parsed.username or "",
        parsed.password or "",
        unquote(parsed.username or ""),
        unquote(parsed.password or ""),
    }
    for secret in sorted(secrets - {""}, key=len, reverse=True):
        detail = detail.replace(secret, "***")
    return detail
