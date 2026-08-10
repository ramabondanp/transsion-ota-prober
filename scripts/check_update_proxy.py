#!/usr/bin/env python3
"""
Check OTA updates through spys.one geo proxies.

Workflow: fetch proxies by country -> run checkota through each proxy -> find target text.

Usage:
  python scripts/check_update_proxy.py PH -c LJ8 --reg op
  python scripts/check_update_proxy.py PH -c CL8 -i 185003 --reg op --rounds 3
  python scripts/check_update_proxy.py PK -c X6871 --reg op --limit 50 --target 16.3
  python scripts/check_update_proxy.py BD -c LJ8 --reg op --limit 30 --workers 30

Country is a spys.one code: PH, BD, PK, NG, KE, MA, GH, etc.
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
from collections.abc import Sequence
from pathlib import Path
from subprocess import TimeoutExpired
from typing import NamedTuple

import requests
from fetch_spys import fetch_spys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROCESS_TIMEOUT = 35.0
DEFAULT_MAX_WORKERS = 32

_ACTIVE_PROCESSES: set[subprocess.Popen[bytes]] = set()
_ACTIVE_LOCK = threading.Lock()
_STOP_EVENT = threading.Event()


class CheckResult(NamedTuple):
    address: str
    proxy_type: str
    duration: float
    hit: bool
    title: str
    status: str
    error: str


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


def _proxy_error(output: str) -> str:
    markers = (
        ("ConnectTimeout", "ConnectTimeout"),
        ("No route", "No route"),
        ("ProxyError", "ProxyError"),
        ("Read timed out", "ReadTimeout"),
        ("Tunnel", "TunnelErr"),
        ("Missing dependencies for SOCKS support", "SOCKS dependency missing"),
    )
    return next((label for marker, label in markers if marker in output), "")


def _summarize_output(
    output: str, returncode: int, target: str
) -> tuple[bool, str, str]:
    title = next(
        (line.strip() for line in output.splitlines() if "New OTA" in line), ""
    )
    hit = target in output

    if hit:
        status = f"HIT {target} -> {(title or 'target found')[:80]}"
    elif title:
        status = title[:90]
    elif "No updates" in output:
        status = "No updates found"
    elif returncode:
        status = f"FAILED (exit {returncode})"
    else:
        status = "No update result"
    return hit, title, status


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
    if _STOP_EVENT.is_set():
        return CheckResult(
            address, normalized_type, 0.0, False, "", "CANCELLED", "Interrupted"
        )

    scheme = "socks5" if "SOCKS" in normalized_type.upper() else "http"
    proxy_url = f"{scheme}://{address}"
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
                address,
                normalized_type,
                time.monotonic() - started,
                False,
                "",
                "CANCELLED",
                "Interrupted",
            )

        try:
            raw_output, _ = process.communicate(timeout=process_timeout)
            output = raw_output.decode("utf-8", errors="replace")
        except TimeoutExpired:
            _terminate_process(process)
            return CheckResult(
                address,
                normalized_type,
                time.monotonic() - started,
                False,
                "",
                "TIMEOUT",
                "Timeout",
            )

        duration = time.monotonic() - started
        hit, title, status = _summarize_output(output, process.returncode or 0, target)
        return CheckResult(
            address,
            normalized_type,
            duration,
            hit,
            title,
            status,
            _proxy_error(output),
        )
    except (OSError, ValueError) as exc:
        return CheckResult(
            address,
            normalized_type,
            time.monotonic() - started,
            False,
            "",
            f"EXC {exc}",
            str(exc),
        )
    finally:
        if process is not None:
            unregister_process(process)


def _run_baseline(
    cmd_base: Sequence[str], target: str, timeout: float
) -> tuple[str, bool, str]:
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
        return "TIMEOUT", False, ""
    except OSError as exc:
        return f"FAILED: {exc}", False, ""

    output = (result.stdout or "") + (result.stderr or "")
    title = next(
        (line.strip() for line in output.splitlines() if "New OTA" in line), ""
    )
    if not title:
        title = (
            "No updates found"
            if "No updates" in output
            else f"exit {result.returncode}"
        )
    return title, target in output, output


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch spys.one proxies by country and batch-check OTA output."
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
        "--limit",
        type=int,
        default=30,
        choices=[30, 50, 100, 200, 300, 500],
        help="Number of proxies to fetch per country",
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

    proxies: list[tuple[str, str, str]] = []
    seen_addresses: set[str] = set()
    for country in countries:
        print(
            f"# Fetching {args.limit} proxies for {country} -> "
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
        print(f"# Got {added} unique proxies for {country}", file=sys.stderr)

    if not proxies:
        print(f"No proxies found for {','.join(countries)}", file=sys.stderr)
        return 1

    cmd_base = [sys.executable, "-m", "checkota", "--dry-run", "-c", args.config]
    if args.region:
        cmd_base.extend(("--reg", args.region))
    if args.incremental:
        cmd_base.extend(("-i", args.incremental))

    workers = min(args.workers or DEFAULT_MAX_WORKERS, len(proxies))
    country_label = ",".join(countries)
    print(
        f"# Command: {' '.join(cmd_base)} | target: {args.target!r} | "
        f"rounds: {args.rounds} | workers: {workers}",
        file=sys.stderr,
    )

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
                flag = " *** HIT ***" if result.hit else ""
                print(
                    f"[{result.duration:4.1f}s] [{country}] [{result.proxy_type:7s}] "
                    f"{result.address:22s} -> {result.status[:65]:65s} "
                    f"{result.error}{flag}",
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

        hits = sum(result.hit for _, result in results)
        reachable = sum(bool(result.title) for _, result in results)
        print(
            f"-> R{round_number} wall {time.monotonic() - started:.1f}s "
            f"reachable {reachable}/{len(proxies)} hits {args.target!r}: {hits}"
        )

        baseline, baseline_hit, baseline_output = _run_baseline(
            cmd_base, args.target, args.process_timeout
        )
        print(
            f"Baseline R{round_number}: {baseline}  {args.target}? "
            f"{'yes' if baseline_hit else 'no'}"
        )
        if baseline_hit:
            print(baseline_output)
        if round_number < args.rounds:
            # Event.wait is interruptible and avoids a fixed-sleep shutdown delay.
            _STOP_EVENT.wait(0.5)

    print(f"\n=== DONE {country_label} {args.rounds} rounds ===", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
