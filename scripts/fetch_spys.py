#!/usr/bin/env python3
"""
Fetch free proxy list from spys.one by country code.

Usage:
  python scripts/fetch_spys.py PH           # 30 proxies (default)
  python scripts/fetch_spys.py PH --limit 50
  python scripts/fetch_spys.py BD --json
  python scripts/fetch_spys.py GH -o /tmp/gh.txt

Page: https://spys.one/free-proxy-list/{CC}/
Ports are obfuscated via packed JS XOR (eval(function(p,r,o,x,y,s)...)).
This script unpacks the vars, decodes each port, and prints ip:port.

Requires: requests
"""
import argparse
import re
import sys
import json

import requests

HDRS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


def unpack_packed(html):
    # Find eval(function(p,r,o,x,y,s){...}('p_str',r,o,'x_raw'.split('\u005e'),0,{})
    # p_str = XOR var definitions, x_raw = '^' separated names
    m = re.search(
        r"\}\('([^']*)',\s*(\d+)\s*,\s*(\d+)\s*,\s*'([^']*)'\.split",
        html,
        re.DOTALL,
    )
    if not m:
        return None, None, None
    p_str = m.group(1)
    r = int(m.group(2))
    o = int(m.group(3))
    x_raw = m.group(4)
    # x_raw is '^' joined (decoded \u005e is ^, but raw still has literal ^)
    x_arr = x_raw.split("^")

    def y(c):
        if c < r:
            base = ""
        else:
            base = y(c // r)
        c2 = c % r
        if c2 > 35:
            ch = chr(c2 + 29)
        else:
            ch = str(c2) if c2 < 10 else chr(ord("a") + c2 - 10)
        return base + ch

    p = p_str
    for idx in range(o - 1, -1, -1):
        if idx < len(x_arr) and x_arr[idx]:
            pat = r"\b" + re.escape(y(idx)) + r"\b"
            p = re.sub(pat, x_arr[idx], p)
    return p, r, o


def parse_vars(decoded):
    todo = {}
    for part in decoded.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        todo[k.strip()] = v.strip()
    vals = {}
    changed = True
    while todo and changed:
        changed = False
        for k, expr in list(todo.items()):
            tokens = expr.split("^")
            resolved = []
            ok = True
            for t in tokens:
                t = t.strip()
                if not t:
                    continue
                if t.lstrip("-").isdigit():
                    resolved.append(int(t))
                elif t in vals:
                    resolved.append(vals[t])
                else:
                    ok = False
                    break
            if ok:
                cur = 0
                for v in resolved:
                    cur ^= v
                vals[k] = cur
                del todo[k]
                changed = True
    return vals, todo


def fetch_spys(country, limit=30, timeout=25):
    cc = country.strip().upper()
    url = f"https://spys.one/free-proxy-list/{cc}/"
    # POST xpp to control count: 0=30,1=50,2=100,3=200,4=300,5=500
    xpp_map = {30: 0, 50: 1, 100: 2, 200: 3, 300: 4, 500: 5}
    xpp = xpp_map.get(limit, 0)
    data = {"xpp": str(xpp), "xf1": "0", "xf2": "0", "xf4": "0", "xf5": "0"} if limit != 30 else None
    if data:
        r = requests.post(url, data=data, headers=HDRS, timeout=timeout)
    else:
        r = requests.get(url, headers=HDRS, timeout=timeout)
    r.raise_for_status()
    html = r.text

    decoded, _, _ = unpack_packed(html)
    if not decoded:
        raise RuntimeError("Failed to find packed eval (unpack_packed returned None)")
    vals, left = parse_vars(decoded)
    if left:
        print(f"[!] unresolved vars: {left}", file=sys.stderr)

    # Each proxy: <font class=spy14>IP<script>document.write(":"+(A)+(B)+(C)+...)</script>
    proxies = []
    # Match IP + the following script's document.write args
    # The script content is like: document.write(":"+(Three^Seven)+(Four^Six)+...)
    pat = re.compile(
        r"<font class=spy14>([\d\.]+)<script>document\.write\([^<]+?</script>",
        re.DOTALL,
    )
    for m in pat.finditer(html):
        ip = m.group(1)
        block = m.group(0)
        chunks = re.findall(r"\((\w+\^\w+)\)", block)
        if not chunks:
            continue
        port_digits = []
        ok = True
        for ch in chunks:
            parts = ch.split("^")
            vlist = []
            for p in parts:
                p = p.strip()
                if p.lstrip("-").isdigit():
                    vlist.append(int(p))
                elif p in vals:
                    vlist.append(vals[p])
                else:
                    ok = False
                    break
            if not ok:
                break
            cur = 0
            for v in vlist:
                cur ^= v
            port_digits.append(str(cur))
        if not ok or not port_digits:
            continue
        port = "".join(port_digits)
        # proxy type nearby (look ahead 2500 chars)
        snippet = html[m.start() : m.start() + 2500]
        tm = re.search(r"proxy-list/'><font[^>]*>(HTTP[S]?|SOCKS\d?)", snippet)
        ptype = tm.group(1) if tm else "?"
        am = re.search(r"<font class=spy5>(NOA|ANM|HIA)</font>", snippet)
        anon = am.group(1) if am else "?"
        proxies.append((ip, port, ptype, anon))
        if len(proxies) >= limit:
            break

    return proxies


def main():
    ap = argparse.ArgumentParser(description="Fetch spys.one free proxies by country code")
    ap.add_argument("country", help="Country code, e.g. PH, BD, PK, NG, KE, MA, GH, MW, CM")
    ap.add_argument("--limit", type=int, default=30, choices=[30, 50, 100, 200, 300, 500],
                    help="How many to fetch (30/50/100/200/300/500)")
    ap.add_argument("--json", action="store_true", help="Output JSON lines instead of ip:port")
    ap.add_argument("-o", "--output", help="Write to file instead of stdout")
    args = ap.parse_args()

    try:
        proxies = fetch_spys(args.country, limit=args.limit)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if not proxies:
        print(f"No proxies found for {args.country.upper()} (maybe Cloudflare challenge).", file=sys.stderr)
        sys.exit(1)

    if args.json:
        lines = [json.dumps({"ip": ip, "port": port, "type": t, "anon": a, "addr": f"{ip}:{port}"})
                 for ip, port, t, a in proxies]
        out = "\n".join(lines)
    else:
        # ip:port lines to stdout, detail header to stderr
        print(f"# {args.country.upper()}  {len(proxies)} proxies  https://spys.one/free-proxy-list/{args.country.upper()}/",
              file=sys.stderr)
        for ip, port, t, a in proxies:
            print(f"# {ip}:{port:10s}  {t:6s} {a}", file=sys.stderr)
        out = "\n".join(f"{ip}:{port}" for ip, port, _, _ in proxies)

    if args.output:
        open(args.output, "w").write(out + "\n")
        print(f"Wrote {len(proxies)} proxies to {args.output} ({args.country.upper()})", file=sys.stderr)
    else:
        print(out)


if __name__ == "__main__":
    main()
