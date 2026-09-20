#!/usr/bin/env python3
"""
Check OTA updates through paid Proxmint or free spys.one geo proxies.

By default, use one paid Proxmint proxy per country from
``PROXMINT_PROXY_TEMPLATE`` in ``scripts/.env`` (an exported environment value
takes precedence). Pass ``--verify`` to verify paid proxy exit countries before
running checkota, or ``--free`` to use only proxies fetched from spys.one. Free
proxies are not country-verified.

Usage:
  # scripts/.env: PROXMINT_PROXY_TEMPLATE=user__cr.{country}:password@host:port
  C=KE,NG,KH,PH,CO,SA,CM,MW; python scripts/check_update_proxy.py $C -c CL8 --reg op
  python scripts/check_update_proxy.py PH -c LJ8 --reg op
  python scripts/check_update_proxy.py PH -c LJ8 --reg op --verify
  python scripts/check_update_proxy.py PH -c LJ8 --reg op --free
  python scripts/check_update_proxy.py PK -c X6871 --reg op --free --limit 50

Requires: requests (and requests[socks] when testing SOCKS proxies)
"""

import argparse
import concurrent.futures
import contextlib
import json
import math
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict
from collections.abc import Sequence
from pathlib import Path
from statistics import median
from subprocess import TimeoutExpired
from typing import Final, NamedTuple

import requests
from fetch_spys import fetch_spys

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
ENV_FILE = SCRIPT_DIR / ".env"
DEFAULT_PROCESS_TIMEOUT = 35.0
DEFAULT_MAX_WORKERS = 32
COUNTRY_CHECK_URL: Final = "https://api.country.is/"
COUNTRY_CHECK_TIMEOUT = 12.0
PROXMINT_PROXY_TEMPLATE_ENV: Final = "PROXMINT_PROXY_TEMPLATE"
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
COUNTRY_CODE_RE: Final = re.compile(r"[A-Z]{2}")
# Hex digits, dots, and colons only -- accepts IPv4/IPv6 while rejecting the
# control characters and newlines that would forge log lines.
COUNTRY_IP_RE: Final = re.compile(r"[0-9A-Fa-f:.]{2,45}")
# api.country.is replies with a tiny JSON object; a hostile exit node must not
# be able to force an unbounded body into memory.
MAX_COUNTRY_RESPONSE_BYTES: Final = 64 * 1024
COUNTRY_RESPONSE_CHUNK: Final = 8192

OUTCOME_UPDATE = "update"
OUTCOME_NO_UPDATE = "no_update"
OUTCOME_FAILED = "failed"
OUTCOME_CANCELLED = "cancelled"

_ACTIVE_PROCESSES: set[subprocess.Popen[bytes]] = set()
_ACTIVE_LOCK = threading.Lock()
_STOP_EVENT = threading.Event()


class CheckResult(NamedTuple):
    address: str
    proxy_type: str
    duration: float
    outcome: str
    hit: bool
    title: str
    detail: str


class CountryCheck(NamedTuple):
    matches: bool
    actual_country: str
    ip: str
    detail: str


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def _positive_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a finite number greater than zero")
    return number


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    """Terminate a checkota process group, escalating if it does not stop promptly."""
    if process.poll() is not None:
        return

    with contextlib.suppress(ProcessLookupError, OSError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=2)
    except TimeoutExpired:
        with contextlib.suppress(ProcessLookupError, OSError):
            os.killpg(process.pid, signal.SIGKILL)
        with contextlib.suppress(TimeoutExpired):
            process.wait(timeout=2)


def stop_active_processes() -> None:
    """Prevent new work from starting and terminate every registered process."""
    with _ACTIVE_LOCK:
        _STOP_EVENT.set()
        processes = tuple(_ACTIVE_PROCESSES)
    for process in processes:
        _terminate_process(process)


def register_process(process: subprocess.Popen[bytes]) -> bool:
    """Register a process unless shutdown has started."""
    with _ACTIVE_LOCK:
        if _STOP_EVENT.is_set():
            return False
        _ACTIVE_PROCESSES.add(process)
        return True


def unregister_process(process: subprocess.Popen[bytes]) -> None:
    with _ACTIVE_LOCK:
        _ACTIVE_PROCESSES.discard(process)


def _clean_output_line(line: str) -> str:
    return ANSI_ESCAPE_RE.sub("", line).strip()


def _load_proxy_template() -> str:
    """Read the proxy template from the process environment or scripts/.env."""
    value = os.environ.get(PROXMINT_PROXY_TEMPLATE_ENV, "").strip()
    if value or not ENV_FILE.is_file():
        return value

    try:
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError(f"could not read {ENV_FILE}: {exc}") from exc

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, raw_value = stripped.split("=", 1)
        if name.strip() == PROXMINT_PROXY_TEMPLATE_ENV:
            return raw_value.strip().strip("'\"")
    return ""


def _proxmint_proxy(country: str, template: str) -> str:
    """Build the authenticated Proxmint endpoint for a country code."""
    try:
        address = template.format(country=country.lower())
    except (IndexError, KeyError, ValueError) as exc:
        raise ValueError(
            f"{PROXMINT_PROXY_TEMPLATE_ENV} must contain only a {{country}} placeholder"
        ) from exc
    if "{country}" not in template or "@" not in address:
        raise ValueError(
            f"{PROXMINT_PROXY_TEMPLATE_ENV} must look like "
            "user__cr.{country}:password@host:port"
        )
    return address


def _redact_proxy_address(address: str) -> str:
    """Hide credentials while retaining the Proxmint country identifier."""
    if "@" not in address:
        return address

    credentials, host = address.rsplit("@", 1)
    username, separator, _password = credentials.partition(":")
    country_marker = username.rfind("__cr.")
    if separator and country_marker >= 0:
        return f"*{username[country_marker:]}:***@{host}"
    return f"***:***@{host}"


def _proxy_url(address: str, proxy_type: str) -> str:
    scheme = "socks5" if "SOCKS" in proxy_type.upper() else "http"
    return f"{scheme}://{address}"


def _redact_error_detail(detail: str, address: str) -> str:
    """Scrub proxy credentials that requests may embed in an exception message.

    ``requests`` quotes the full proxy URL in ``InvalidURL``/``ProxyError``
    messages, so an unredacted detail would print the paid proxy password.
    """
    if "@" not in address:
        return detail

    userinfo, _separator, _host = address.rpartition("@")
    detail = detail.replace(address, _redact_proxy_address(address))
    _username, _separator, password = userinfo.partition(":")
    if password:
        # Backstop for messages that quote only the credentials portion.
        detail = detail.replace(password, "***")
    return detail


def verify_proxy_country(
    expected_country: str,
    address: str,
    proxy_type: str,
    timeout: float = COUNTRY_CHECK_TIMEOUT,
) -> CountryCheck:
    """Verify the proxy's apparent country through a public IP lookup.

    Never raises: any failure is reported as a non-matching ``CountryCheck``
    with the proxy credentials scrubbed from ``detail``.
    """
    expected = expected_country.upper()
    proxy_url = _proxy_url(address, proxy_type)
    proxies = {"http": proxy_url, "https": proxy_url}
    session = requests.Session()
    session.trust_env = False
    try:
        response = session.get(
            COUNTRY_CHECK_URL,
            proxies=proxies,
            timeout=timeout,
            stream=True,
        )
        try:
            response.raise_for_status()
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_content(chunk_size=COUNTRY_RESPONSE_CHUNK):
                total += len(chunk)
                if total > MAX_COUNTRY_RESPONSE_BYTES:
                    raise ValueError("country lookup response too large")
                chunks.append(chunk)
        finally:
            response.close()

        payload = json.loads(b"".join(chunks))
        if not isinstance(payload, dict):
            raise TypeError("invalid country lookup response")

        actual = str(payload.get("country", "")).upper()
        if COUNTRY_CODE_RE.fullmatch(actual) is None:
            raise ValueError("country lookup returned no country code")
        ip = str(payload.get("ip", ""))
        if COUNTRY_IP_RE.fullmatch(ip) is None:
            ip = ""
        return CountryCheck(actual == expected, actual, ip, "")
    except (OSError, requests.RequestException, TypeError, ValueError) as exc:
        return CountryCheck(False, "", "", _redact_error_detail(str(exc), address))
    finally:
        session.close()


def _verify_paid_proxies(
    proxies: list[tuple[str, str, str]], max_workers: int
) -> list[CountryCheck]:
    """Verify paid proxies concurrently and return results in input order."""
    if not proxies:
        return []
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=max(1, max_workers)
    ) as executor:
        futures = [
            executor.submit(verify_proxy_country, country, address, proxy_type)
            for country, address, proxy_type in proxies
        ]
        return [future.result() for future in futures]


def _network_error(output: str) -> str:
    terminal_section = output.rpartition("Update check failed")[2] or output
    markers = (
        ("Missing dependencies for SOCKS support", "SOCKS dependency missing"),
        ("ConnectTimeout", "ConnectTimeout"),
        ("Read timed out", "ReadTimeout"),
        ("No route", "No route"),
        ("ProxyError", "ProxyError"),
        ("Tunnel", "TunnelError"),
        ("Connection refused", "Connection refused"),
        ("NameResolutionError", "DNS error"),
    )
    return next(
        (label for marker, label in markers if marker in terminal_section),
        "Network error",
    )


def _summarize_output(
    output: str, returncode: int, target: str
) -> tuple[str, bool, str, str]:
    """Classify final checkota state, ignoring transient errors after recovery."""
    clean_lines = [_clean_output_line(line) for line in output.splitlines()]
    update_line = next(
        (line for line in clean_lines if "New OTA update found:" in line), ""
    )
    title = update_line.partition("New OTA update found:")[2].strip()
    if title:
        return OUTCOME_UPDATE, target in title, title, ""

    terminal_network_failure = any(
        marker in output
        for marker in (
            "Update check failed after multiple retries",
            "Update check failed:",
            "SOCKS support",
        )
    )
    if terminal_network_failure:
        return OUTCOME_FAILED, False, "", _network_error(output)
    if returncode:
        detail = next(
            (line.lstrip("✗!=> ") for line in reversed(clean_lines) if line),
            f"exit {returncode}",
        )
        return OUTCOME_FAILED, False, "", detail
    if any("No updates found" in line for line in clean_lines):
        return OUTCOME_NO_UPDATE, False, "", ""
    return OUTCOME_FAILED, False, "", "No conclusive result"


def _result_label(result: CheckResult, target: str) -> str:
    if result.outcome == OUTCOME_UPDATE:
        marker = f"HIT {target}" if result.hit else "UPDATE"
        return f"{marker:<10} {result.title}"
    if result.outcome == OUTCOME_NO_UPDATE:
        return "NO UPDATE"
    if result.outcome == OUTCOME_CANCELLED:
        return f"CANCELLED  {result.detail}"
    return f"FAILED     {result.detail}"


def _progress_line(
    country: str, result: CheckResult, target: str, show_proxy: bool
) -> str:
    """Compact per-check line; proxy identity only when it varies (free mode)."""
    prefix = f"[{result.duration:5.1f}s] {country}"
    if show_proxy:
        prefix += f" {result.address} [{result.proxy_type}]"
    return f"{prefix}  {_result_label(result, target)}"


def _print_round_report(
    round_number: int,
    results: list[tuple[str, CheckResult]],
    proxy_count: int,
    target: str,
    wall_time: float,
) -> None:
    counts = Counter(result.outcome for _, result in results)
    hits = sum(result.hit for _, result in results)

    print(
        f"\nRound {round_number}: checked {len(results)}/{proxy_count} in "
        f"{wall_time:.1f}s | updates {counts[OUTCOME_UPDATE]} | "
        f"no update {counts[OUTCOME_NO_UPDATE]} | failed {counts[OUTCOME_FAILED]} | "
        f"hits {target!r}: {hits}"
    )

    # Per-country breakdown only pays off with several proxies per country.
    by_country: dict[str, list[CheckResult]] = defaultdict(list)
    for country, result in results:
        by_country[country].append(result)
    if any(len(entries) > 1 for entries in by_country.values()):
        print("Country  Checked  Update  None  Failed  Hit  Success  Median")
        for country in sorted(by_country):
            country_results = by_country[country]
            country_counts = Counter(result.outcome for result in country_results)
            country_conclusive = (
                country_counts[OUTCOME_UPDATE] + country_counts[OUTCOME_NO_UPDATE]
            )
            country_hits = sum(result.hit for result in country_results)
            country_rate = 100 * country_conclusive / len(country_results)
            country_median = median(result.duration for result in country_results)
            print(
                f"{country:7s} {len(country_results):7d} {country_counts[OUTCOME_UPDATE]:7d} "
                f"{country_counts[OUTCOME_NO_UPDATE]:5d} {country_counts[OUTCOME_FAILED]:7d} "
                f"{country_hits:4d} {country_rate:7.1f}% {country_median:6.1f}s"
            )

    update_locations: dict[str, Counter[str]] = defaultdict(Counter)
    for country, result in results:
        if result.outcome == OUTCOME_UPDATE:
            update_locations[result.title][country] += 1
    # Already visible on the progress lines when every check saw the same title.
    if hits or len(update_locations) > 1:
        print("Updates observed:")
        for title, locations in sorted(
            update_locations.items(), key=lambda item: (-sum(item[1].values()), item[0])
        ):
            location_text = ", ".join(
                f"{country}×{count}" for country, count in sorted(locations.items())
            )
            hit_marker = f" [HIT {target}]" if target in title else ""
            print(
                f"  {sum(locations.values()):3d}× {title}{hit_marker} ({location_text})"
            )

    failures = Counter(
        result.detail
        for _, result in results
        if result.outcome == OUTCOME_FAILED and result.detail
    )
    if failures:
        failure_text = ", ".join(
            f"{name}={count}" for name, count in failures.most_common()
        )
        print(f"Failures: {failure_text}")


def _print_target_verdict(
    target: str,
    results: list[tuple[str, CheckResult]],
    baselines: list[CheckResult],
) -> None:
    """End with an explicit answer to the script's target-search question."""
    matches = [(country, result) for country, result in results if result.hit]
    update_count = sum(result.outcome == OUTCOME_UPDATE for _, result in results)
    failure_count = sum(result.outcome == OUTCOME_FAILED for _, result in results)

    print(f"\n--- TARGET SEARCH: {target!r} ---")
    if matches:
        print(
            f"FOUND — {len(matches)} matching OTA response(s) across "
            f"{len(results)} proxy attempt(s)."
        )
        countries = Counter(country for country, _ in matches)
        print(
            "Countries: "
            + ", ".join(
                f"{country}×{count}" for country, count in sorted(countries.items())
            )
        )
        titles = Counter(result.title for _, result in matches)
        print("Matching OTA titles:")
        for title, count in titles.most_common():
            print(f"  {count:3d}× {title}")
        print("Matching proxies:")
        seen_matches: set[tuple[str, str, str, str]] = set()
        for country, result in sorted(
            matches,
            key=lambda item: (item[0], item[1].address, item[1].title),
        ):
            match_key = (country, result.address, result.proxy_type, result.title)
            if match_key in seen_matches:
                continue
            seen_matches.add(match_key)
            print(
                f"  [{country}] [{result.proxy_type:7s}] {result.address:22s} "
                f"{result.title}"
            )
    else:
        print(
            f"NOT FOUND across {len(results)} attempt(s) "
            f"({update_count} updates, {failure_count} failed)."
        )

    baseline_hits = sum(baseline.hit for baseline in baselines)
    baseline_titles = list(
        dict.fromkeys(baseline.title for baseline in baselines if baseline.title)
    )
    baseline_state = "MATCH" if baseline_hits else "NO MATCH"
    baseline_detail = f" — {', '.join(baseline_titles)}" if baseline_titles else ""
    print(f"Direct baseline: {baseline_state}{baseline_detail}")


def run_one(
    address: str,
    proxy_type: str,
    target: str,
    cmd_base: Sequence[str],
    process_timeout: float,
) -> CheckResult:
    """Run one isolated checkota process through a proxy."""
    started = time.monotonic()
    normalized_type = proxy_type or "?"
    display_address = _redact_proxy_address(address)
    if _STOP_EVENT.is_set():
        return CheckResult(
            display_address,
            normalized_type,
            0.0,
            OUTCOME_CANCELLED,
            False,
            "",
            "Interrupted",
        )

    proxy_url = _proxy_url(address, normalized_type)
    env = os.environ.copy()
    env.update(
        {
            "https_proxy": proxy_url,
            "http_proxy": proxy_url,
            "HTTPS_PROXY": proxy_url,
            "HTTP_PROXY": proxy_url,
        }
    )
    env.pop("no_proxy", None)
    env.pop("NO_PROXY", None)

    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            cmd_base,
            cwd=PROJECT_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        if not register_process(process):
            _terminate_process(process)
            return CheckResult(
                display_address,
                normalized_type,
                time.monotonic() - started,
                OUTCOME_CANCELLED,
                False,
                "",
                "Interrupted",
            )

        try:
            raw_output, _ = process.communicate(timeout=process_timeout)
            output = raw_output.decode("utf-8", errors="replace")
        except TimeoutExpired:
            _terminate_process(process)
            return CheckResult(
                display_address,
                normalized_type,
                time.monotonic() - started,
                OUTCOME_FAILED,
                False,
                "",
                "Process timeout",
            )

        duration = time.monotonic() - started
        outcome, hit, title, detail = _summarize_output(
            output, process.returncode or 0, target
        )
        return CheckResult(
            display_address,
            normalized_type,
            duration,
            outcome,
            hit,
            title,
            detail,
        )
    except (OSError, ValueError) as exc:
        return CheckResult(
            display_address,
            normalized_type,
            time.monotonic() - started,
            OUTCOME_FAILED,
            False,
            "",
            str(exc),
        )
    finally:
        if process is not None:
            unregister_process(process)


def _run_baseline(
    cmd_base: Sequence[str], target: str, timeout: float
) -> tuple[CheckResult, str]:
    try:
        result = subprocess.run(
            cmd_base,
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except TimeoutExpired:
        return CheckResult(
            "direct", "DIRECT", timeout, OUTCOME_FAILED, False, "", "Timeout"
        ), ""
    except OSError as exc:
        return CheckResult(
            "direct", "DIRECT", 0.0, OUTCOME_FAILED, False, "", str(exc)
        ), ""

    output = (result.stdout or "") + (result.stderr or "")
    outcome, hit, title, detail = _summarize_output(output, result.returncode, target)
    return CheckResult("direct", "DIRECT", 0.0, outcome, hit, title, detail), output


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Use paid Proxmint proxies by country, or free spys.one proxies with "
            "--free, and batch-check OTA output."
        )
    )
    parser.add_argument(
        "country", help="Comma-separated two-letter country codes, e.g. PH or KE,NG"
    )
    parser.add_argument(
        "-c", "--config", required=True, help="Config codename passed to checkota -c"
    )
    parser.add_argument(
        "--reg", "--region", dest="region", help="Region filter passed to --reg"
    )
    parser.add_argument("-i", "--incremental", help="Incremental override passed to -i")
    parser.add_argument(
        "--free",
        action="store_true",
        help="Use only free spys.one proxies instead of the paid Proxmint proxy",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Verify paid proxy exit countries before checking OTA updates",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=30,
        choices=[30, 50, 100, 200, 300, 500],
        help="Number of free proxies to fetch per country (with --free)",
    )
    parser.add_argument(
        "--rounds", type=_positive_int, default=3, help="Number of rounds (default: 3)"
    )
    parser.add_argument(
        "--target", default="16.3", help="Text to find in checkota output"
    )
    parser.add_argument(
        "--workers",
        type=_positive_int,
        help=(
            "Parallel country verifications and OTA checks "
            f"(default: min(proxy count, {DEFAULT_MAX_WORKERS}))"
        ),
    )
    parser.add_argument(
        "--fetch-zip-proxy",
        "--zip-proxy",
        action="store_true",
        dest="fetch_zip_proxy",
        help="Fetch OTA ZIP metadata using proxy environment variables",
    )
    parser.add_argument(
        "--process-timeout",
        type=_positive_float,
        default=DEFAULT_PROCESS_TIMEOUT,
        help=f"Per-proxy and baseline timeout in seconds (default: {DEFAULT_PROCESS_TIMEOUT:g})",
    )
    args = parser.parse_args()

    countries = list(
        dict.fromkeys(
            cc.strip().upper() for cc in args.country.split(",") if cc.strip()
        )
    )
    invalid_countries = [
        cc for cc in countries if re.fullmatch(r"[A-Z]{2}", cc) is None
    ]
    if not countries:
        parser.error("country must contain at least one country code")
    if invalid_countries:
        parser.error(
            f"invalid two-letter country code(s): {', '.join(invalid_countries)}"
        )
    if not args.target:
        parser.error("target must not be empty")

    try:
        paid_proxy_template = _load_proxy_template()
    except ValueError as exc:
        parser.error(str(exc))
    if not args.free and not paid_proxy_template:
        parser.error(
            f"{PROXMINT_PROXY_TEMPLATE_ENV} is required in {ENV_FILE} or the environment "
            "unless --free is used"
        )

    proxies: list[tuple[str, str, str]] = []
    seen_addresses: set[str] = set()
    if not args.free:
        paid_proxies: list[tuple[str, str, str]] = []
        for country in countries:
            try:
                address = _proxmint_proxy(country, paid_proxy_template)
            except ValueError as exc:
                parser.error(str(exc))
            paid_proxies.append((country, address, "PAID"))

        if args.verify:
            verify_workers = min(args.workers or DEFAULT_MAX_WORKERS, len(paid_proxies))
            country_checks = _verify_paid_proxies(paid_proxies, verify_workers)
            for (country, address, proxy_type), country_check in zip(
                paid_proxies, country_checks, strict=True
            ):
                if country_check.matches:
                    proxies.append((country, address, proxy_type))
                elif country_check.actual_country:
                    print(
                        f"# [{country}] VERIFY MISMATCH expected={country} "
                        f"actual={country_check.actual_country} "
                        f"ip={country_check.ip or '?'}; skipping",
                        file=sys.stderr,
                    )
                else:
                    print(
                        f"# [{country}] VERIFY FAILED {country_check.detail}; skipping",
                        file=sys.stderr,
                    )
            print(
                f"# Verified {len(proxies)}/{len(paid_proxies)} paid proxies",
                file=sys.stderr,
            )
        else:
            proxies = paid_proxies
    else:
        for country in countries:
            print(
                f"# Fetching {args.limit} free proxies for {country} -> "
                f"https://spys.one/free-proxy-list/{country}/",
                file=sys.stderr,
            )
            try:
                country_proxies = fetch_spys(country, limit=args.limit)
            except (
                OSError,
                requests.RequestException,
                RuntimeError,
                ValueError,
            ) as exc:
                # One failed country must not discard successful countries.
                print(f"Error fetching {country}: {exc}", file=sys.stderr)
                continue

            added = 0
            for ip, port, proxy_type, anonymity in country_proxies:
                address = f"{ip}:{port}"
                if address in seen_addresses:
                    continue
                seen_addresses.add(address)
                proxies.append((country, address, proxy_type))
                added += 1
                print(
                    f"#  [{country}] {address:22s} {proxy_type:7s} {anonymity}",
                    file=sys.stderr,
                )
            print(f"# Got {added} unique free proxies for {country}", file=sys.stderr)

    if not proxies:
        source = "free proxies" if args.free else "verified paid proxies"
        print(f"No {source} found for {','.join(countries)}", file=sys.stderr)
        return 1

    cmd_base = [sys.executable, "-m", "checkota", "--dry-run", "-c", args.config]
    if args.region:
        cmd_base.extend(("--reg", args.region))
    if args.incremental:
        cmd_base.extend(("-i", args.incremental))
    if args.fetch_zip_proxy:
        cmd_base.append("--fetch-zip-proxy")

    workers = min(args.workers or DEFAULT_MAX_WORKERS, len(proxies))
    source = "spys.one free" if args.free else "Proxmint paid"
    print(
        f"# Source: {source} | command: {' '.join(cmd_base)} | "
        f"target: {args.target!r} | rounds: {args.rounds} | workers: {workers}",
        file=sys.stderr,
    )

    all_results: list[tuple[str, CheckResult]] = []
    baselines: list[CheckResult] = []
    # Paid mode has exactly one proxy per country, so the address adds nothing.
    show_proxy = args.free
    for round_number in range(1, args.rounds + 1):
        print(
            f"\n--- ROUND {round_number}/{args.rounds} | {len(proxies)} proxies, "
            f"{workers} workers | find {args.target!r} ---",
            flush=True,
        )
        started = time.monotonic()
        results: list[tuple[str, CheckResult]] = []
        futures: dict[concurrent.futures.Future[CheckResult], str] = {}
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
        try:
            futures = {
                executor.submit(
                    run_one,
                    address,
                    proxy_type,
                    args.target,
                    cmd_base,
                    args.process_timeout,
                ): country
                for country, address, proxy_type in proxies
            }
            for future in concurrent.futures.as_completed(futures):
                result = future.result()
                country = futures[future]
                results.append((country, result))
                print(
                    _progress_line(country, result, args.target, show_proxy),
                    flush=True,
                )
        except KeyboardInterrupt:
            stop_active_processes()
            for future in futures:
                future.cancel()
            executor.shutdown(wait=True, cancel_futures=True)
            print("\nInterrupted; stopped proxy checks.", file=sys.stderr)
            return 130
        else:
            executor.shutdown(wait=True)

        _print_round_report(
            round_number,
            results,
            len(proxies),
            args.target,
            time.monotonic() - started,
        )

        all_results.extend(results)
        baseline, baseline_output = _run_baseline(
            cmd_base, args.target, args.process_timeout
        )
        baselines.append(baseline)
        # Baseline outcome is reported once in the final verdict; surface full
        # output immediately only on a direct hit.
        if baseline.hit:
            print(f"\nBaseline hit {args.target!r}:")
            print(baseline_output)
        if round_number < args.rounds:
            # Event.wait is interruptible and avoids a fixed-sleep shutdown delay.
            _STOP_EVENT.wait(0.5)

    _print_target_verdict(args.target, all_results, baselines)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
