#!/usr/bin/env python3
"""
Check OTA update via spys.one geo proxies, with rounds and target grep.

Workflow: fetch spys proxy by country -> check update using proxy -> grep title for 16.3

Usage:
  python scripts/check_update_proxy.py PH -c LJ8 --reg op
  python scripts/check_update_proxy.py PH -c CL8 -i 185003 --reg op --rounds 3
  python scripts/check_update_proxy.py PK -c X6871 --reg op --limit 50 --target 16.3
  python scripts/check_update_proxy.py BD -c LJ8 --reg op --limit 30 --workers 30

  Country is a spys.one code: PH, BD, PK, NG, KE, MA, GH, etc.
  Page: https://spys.one/free-proxy-list/{CC}/

Requires: requests (for fetch_spys)
"""
import argparse
import concurrent.futures
import os
import signal
import subprocess
import sys
import threading
import time


_ACTIVE_PROCESSES = set()
_ACTIVE_LOCK = threading.Lock()


def stop_active_processes():
    with _ACTIVE_LOCK:
        processes = list(_ACTIVE_PROCESSES)
    for process in processes:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass


def register_process(process):
    with _ACTIVE_LOCK:
        _ACTIVE_PROCESSES.add(process)


def unregister_process(process):
    with _ACTIVE_LOCK:
        _ACTIVE_PROCESSES.discard(process)

# local import
from fetch_spys import fetch_spys


def run_one(args, ptype, cc, target, cmd_base):
    ip, port = args.split(":", 1) if ":" in args else (args, "")
    addr = f"{ip}:{port}"
    # ptype from fetch_spys, else guess
    is_socks = "SOCKS" in (ptype or "")
    proxy_url = f"socks5://{addr}" if is_socks else f"http://{addr}"
    env = os.environ.copy()
    env["https_proxy"] = proxy_url
    env["http_proxy"] = proxy_url
    env["HTTPS_PROXY"] = proxy_url
    env["HTTP_PROXY"] = proxy_url
    env.pop("no_proxy", None)
    env.pop("NO_PROXY", None)
    t0 = time.time()
    try:
        process = subprocess.Popen(
            cmd_base,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        register_process(process)
        try:
            output, _ = process.communicate(timeout=35)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait()
            raise
        finally:
            unregister_process(process)
        dt = time.time() - t0
        r = type("Completed", (), {"stdout": output, "stderr": "", "returncode": process.returncode})()
        out = (r.stdout or "") + (r.stderr or "")
        title = ""
        for line in out.splitlines():
            if "New OTA" in line and ("Tcard" in line or "TCard" in line or "tcard" in line.lower() or "CL8-" in line or "LJ8" in line or "X6871" in line or "LJ6" in line):
                title = line.strip()
                break
        if not title and "No updates" in out:
            title = "No updates found"
        hit = target in out
        if hit:
            status = f"HIT {target} -> {title[:80]}"
        elif title and "New OTA" in title:
            status = title[:90]
        elif "No updates" in out:
            status = "No updates found"
        else:
            status = "No updates found"
        err = ""
        if "ConnectTimeout" in out:
            err = "ConnectTimeout"
        elif "No route" in out:
            err = "No route"
        elif "ProxyError" in out:
            err = "ProxyError"
        elif "Read timed out" in out:
            err = "ReadTimeout"
        elif "Tunnel" in out:
            err = "TunnelErr"
        return (addr, ptype or "?", dt, hit, title, status, err, out)
    except subprocess.TimeoutExpired:
        return (addr, ptype or "?", time.time() - t0, False, "", "TIMEOUT", "Timeout", "")
    except Exception as e:
        return (addr, ptype or "?", time.time() - t0, False, "", f"EXC {e}", str(e), "")


def main():
    ap = argparse.ArgumentParser(description="Fetch spys.one proxies by country and batch-check OTA (grep target in title).")
    ap.add_argument("country", help="Country code(s), comma-separated, e.g. PH or KE,NG")
    ap.add_argument("-c", "--config", required=True, help="Config codename, e.g. LJ8, CL8, X6871, LJ6 (maps to checkota -c)")
    ap.add_argument("--reg", "--region", dest="region", default=None, help="Region filter, e.g. op (maps to --reg)")
    ap.add_argument("-i", "--incremental", default=None, help="Incremental override (maps to -i)")
    ap.add_argument("--limit", type=int, default=30, choices=[30, 50, 100, 200, 300, 500], help="Proxies to fetch")
    ap.add_argument("--rounds", type=int, default=3, help="Rounds to retry (default 3)")
    ap.add_argument("--target", default="16.3", help="Grep string in update title/output (default 16.3)")
    ap.add_argument("--workers", type=int, default=None, help="Parallel workers (default = limit)")
    args = ap.parse_args()

    countries = [cc.strip().upper() for cc in args.country.split(",") if cc.strip()]
    if not countries:
        ap.error("country must contain at least one country code")

    proxies = []
    for cc in countries:
        print(f"# Fetching {args.limit} proxies for {cc} -> https://spys.one/free-proxy-list/{cc}/", file=sys.stderr)
        try:
            country_proxies = fetch_spys(cc, limit=args.limit)
        except Exception as e:
            print(f"Error fetching {cc}: {e}", file=sys.stderr)
            continue
        print(f"# Got {len(country_proxies)} proxies for {cc}", file=sys.stderr)
        for ip, port, ptype, anon in country_proxies:
            proxies.append((cc, ip, port, ptype, anon))
            print(f"#  [{cc}] {ip}:{port:12s}  {ptype:6s} {anon}", file=sys.stderr)

    if not proxies:
        print(f"No proxies found for {','.join(countries)}", file=sys.stderr)
        sys.exit(1)

    country_label = ",".join(countries)

    # Build checkota cmd
    cmd_base = ["python3", "-m", "checkota", "--dry-run", "-c", args.config]
    if args.region:
        cmd_base += ["--reg", args.region]
    if args.incremental:
        cmd_base += ["-i", args.incremental]

    workers = args.workers or len(proxies)
    proxy_entries = [(cc, f"{ip}:{port}", ptype) for cc, ip, port, ptype, _ in proxies]

    print(f"# Command: {' '.join(cmd_base)} | target grep: '{args.target}' | rounds: {args.rounds} workers: {workers}", file=sys.stderr)

    for rnd in range(1, args.rounds + 1):
        title_label = f"{' '.join(cmd_base)}"
        print(f"\n{'='*70}\n ROUND {rnd}/{args.rounds} {country_label}  {title_label}  -> grep '{args.target}'  {len(proxies)} proxies {workers} workers\n{'='*70}", flush=True)
        t0 = time.time()
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
        try:
            futs = {
                executor.submit(run_one, addr, ptype, cc, args.target, cmd_base): (cc, addr)
                for cc, addr, ptype in proxy_entries
            }
            results = []
            for f in concurrent.futures.as_completed(futs):
                addr, ptype, dt, hit, title, status, err, out = f.result()
                country, _ = futs[f]
                results.append((country, addr, ptype, dt, hit, title, status, err))
                flag = " *** HIT ***" if hit else ""
                print(f"[{dt:4.1f}s] [{country}] [{ptype:6s}] {addr:22s} -> {status[:65]:65s} {err}{flag}", flush=True)
        except KeyboardInterrupt:
            stop_active_processes()
            for future in futs:
                future.cancel()
            executor.shutdown(wait=False, cancel_futures=True)
            print("\nInterrupted; stopping proxy checks.", file=sys.stderr)
            return 130
        else:
            executor.shutdown(wait=True)
        hits = sum(1 for r in results if r[4])
        ok = sum(1 for r in results if r[5] and "New OTA" in r[5])
        print(f"-> R{rnd} wall {time.time()-t0:.1f}s reachable {ok}/{len(proxies)} hits '{args.target}': {hits}")

        # baseline
        r = subprocess.run(cmd_base, capture_output=True, text=True, timeout=30)
        out = (r.stdout or "") + (r.stderr or "")
        base = "No updates found" if "No updates" in out else ""
        for line in out.splitlines():
            if "New OTA" in line:
                base = line.strip()
                break
        print(f"Baseline R{rnd}: {base}  {args.target}? {'yes' if args.target in out else 'no'}")
        if args.target in out:
            print(out)
        if rnd < args.rounds:
            time.sleep(0.5)

    print(f"\n=== DONE {country_label} {args.rounds} rounds ===", file=sys.stderr)


if __name__ == "__main__":
    main()
