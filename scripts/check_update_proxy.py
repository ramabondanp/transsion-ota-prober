#!/usr/bin/env python3
"""
Check OTA updates through paid Proxmint or free spys.one geo proxies.

By default, use one paid Proxmint proxy per country from the
``PROXMINT_PROXY_TEMPLATE`` environment variable. Pass ``--verify`` to verify
paid proxy exit countries before running checkota, or ``--free`` to use only
proxies fetched from spys.one. Free proxies are not country-verified.

Usage:
  export PROXMINT_PROXY_TEMPLATE='user__cr.{country}:password@gw.proxmint.com:823'
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

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROCESS_TIMEOUT = 35.0
DEFAULT_MAX_WORKERS = 32
COUNTRY_CHECK_URL: Final = "https://api.country.is/"
COUNTRY_CHECK_TIMEOUT = 12.0
PROXMINT_PROXY_TEMPLATE_ENV: Final = "PROXMINT_PROXY_TEMPLATE"
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

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
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
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


def verify_proxy_country(
    expected_country: str,
    address: str,
    proxy_type: str,
    timeout: float = COUNTRY_CHECK_TIMEOUT,
) -> CountryCheck:
    """Verify the proxy's apparent country through a public IP lookup."""
    proxy_url = _proxy_url(address, proxy_type)
    session = requests.Session()
    session.trust_env = False
    try:
        response = session.get(
            COUNTRY_CHECK_URL,
            proxies={"http": proxy_url, "https": proxy_url},
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise TypeError("invalid country lookup response")

        actual = str(payload.get("country", "")).upper()
        ip = str(payload.get("ip", ""))
        if re.fullmatch(r"[A-Z]{2}", actual) is None:
            raise ValueError("country lookup returned no country code")
        return CountryCheck(actual == expected_country.upper(), actual, ip, "")
    except (OSError, requests.RequestException, TypeError, ValueError) as exc:
        return CountryCheck(False, "", "", str(exc))
    finally:
        session.close()


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


def _print_round_report(
    round_number: int,
    results: list[tuple[str, CheckResult]],
    proxy_count: int,
    target: str,
    wall_time: float,
) -> None:
    counts = Counter(result.outcome for _, result in results)
    conclusive = counts[OUTCOME_UPDATE] + counts[OUTCOME_NO_UPDATE]
    hits = sum(result.hit for _, result in results)
    success_rate = 100 * conclusive / proxy_count if proxy_count else 0

    print(f"\n--- ROUND {round_number} REPORT ---")
    print(
        f"Checked {len(results)}/{proxy_count} in {wall_time:.1f}s | "
        f"conclusive {conclusive} ({success_rate:.1f}%) | "
        f"updates {counts[OUTCOME_UPDATE]} | no update {counts[OUTCOME_NO_UPDATE]} | "
        f"failed {counts[OUTCOME_FAILED]} | hits {target!r}: {hits}"
    )

    print("Country  Checked  Update  None  Failed  Hit  Success  Median")
    by_country: dict[str, list[CheckResult]] = defaultdict(list)
    for country, result in results:
        by_country[country].append(result)
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
    if update_locations:
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

    print(f"\n{'=' * 70}")
    print(f" TARGET SEARCH RESULT: {target!r}")
    print(f"{'=' * 70}")
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
            f"NOT FOUND — 0 matching OTA titles across {len(results)} proxy "
            f"attempt(s) and {update_count} update response(s)."
        )

    baseline_hits = sum(baseline.hit for baseline in baselines)
    baseline_titles = list(
        dict.fromkeys(baseline.title for baseline in baselines if baseline.title)
    )
    baseline_state = "MATCH" if baseline_hits else "NO MATCH"
    baseline_detail = f" — {', '.join(baseline_titles)}" if baseline_titles else ""
    print(f"Direct baseline: {baseline_state}{baseline_detail}")
    print(
        f"Search totals: attempts {len(results)} | updates {update_count} | "
        f"matches {len(matches)} | failed {failure_count}"
    )


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
        help=f"Parallel checks (default: min(proxy count, {DEFAULT_MAX_WORKERS}))",
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

    paid_proxy_template = os.environ.get(PROXMINT_PROXY_TEMPLATE_ENV, "").strip()
    if not args.free and not paid_proxy_template:
        parser.error(
            f"{PROXMINT_PROXY_TEMPLATE_ENV} is required unless --free is used"
        )

    proxies: list[tuple[str, str, str]] = []
    seen_addresses: set[str] = set()
    for country in countries:
        if not args.free:
            try:
                address = _proxmint_proxy(country, paid_proxy_template)
            except ValueError as exc:
                parser.error(str(exc))
            display_address = _redact_proxy_address(address)
            if not args.verify:
                seen_addresses.add(address)
                proxies.append((country, address, "PAID"))
                print(
                    f"#  [{country}] {display_address} PAID Proxmint",
                    file=sys.stderr,
                )
                continue

            print(
                f"#  [{country}] {display_address} PAID Proxmint | verifying...",
                file=sys.stderr,
            )
            country_check = verify_proxy_country(country, address, "PAID")
            if country_check.matches:
                seen_addresses.add(address)
                proxies.append((country, address, "PAID"))
                print(
                    f"#  [{country}] VERIFY OK country={country_check.actual_country} "
                    f"ip={country_check.ip or '?'}",
                    file=sys.stderr,
                )
            elif country_check.actual_country:
                print(
                    f"#  [{country}] VERIFY MISMATCH expected={country} "
                    f"actual={country_check.actual_country} "
                    f"ip={country_check.ip or '?'}; skipping",
                    file=sys.stderr,
                )
            else:
                print(
                    f"#  [{country}] VERIFY FAILED {country_check.detail}; skipping",
                    file=sys.stderr,
                )
            continue

        print(
            f"# Fetching {args.limit} free proxies for {country} -> "
            f"https://spys.one/free-proxy-list/{country}/",
            file=sys.stderr,
        )
        try:
            country_proxies = fetch_spys(country, limit=args.limit)
        except (OSError, requests.RequestException, RuntimeError, ValueError) as exc:
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

    workers = min(args.workers or DEFAULT_MAX_WORKERS, len(proxies))
    country_label = ",".join(countries)
    source = "spys.one free" if args.free else "Proxmint paid"
    print(
        f"# Source: {source} | command: {' '.join(cmd_base)} | "
        f"target: {args.target!r} | rounds: {args.rounds} | workers: {workers}",
        file=sys.stderr,
    )

    all_results: list[tuple[str, CheckResult]] = []
    baselines: list[CheckResult] = []
    for round_number in range(1, args.rounds + 1):
        print(
            f"\n{'=' * 70}\n ROUND {round_number}/{args.rounds} {country_label}  "
            f"{' '.join(cmd_base)}  -> find {args.target!r}  "
            f"{len(proxies)} proxies, {workers} workers\n{'=' * 70}",
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
                    f"[{result.duration:5.1f}s] [{country}] [{result.proxy_type:7s}] "
                    f"{result.address:22s}  {_result_label(result, args.target)}",
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
        print(f"Baseline: {_result_label(baseline, args.target)}")
        if baseline.hit:
            print(baseline_output)
        if round_number < args.rounds:
            # Event.wait is interruptible and avoids a fixed-sleep shutdown delay.
            _STOP_EVENT.wait(0.5)

    _print_target_verdict(args.target, all_results, baselines)
    print(f"\n=== DONE {country_label} {args.rounds} rounds ===", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
