#!/usr/bin/env python3
"""
Fetch free proxies from spys.one by country code.

Usage:
  python scripts/fetch_spys.py PH           # 30 proxies (default)
  python scripts/fetch_spys.py PH --limit 50
  python scripts/fetch_spys.py BD --json
  python scripts/fetch_spys.py GH -o /tmp/gh.txt

Ports are obfuscated with packed JavaScript XOR expressions. This script unpacks
variable definitions, decodes each port, and prints proxy addresses.
"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Final, NamedTuple

import requests

HEADERS: Final = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}
XPP_MAP: Final = {30: 0, 50: 1, 100: 2, 200: 3, 300: 4, 500: 5}

PACKED_RE: Final = re.compile(
    r"\}\('([^']*)',\s*(\d+)\s*,\s*(\d+)\s*,\s*'([^']*)'\.split",
    re.DOTALL,
)
PROXY_RE: Final = re.compile(
    r"<font\s+class\s*=\s*['\"]?spy14['\"]?[^>]*>\s*"
    r"((?:\d{1,3}\.){3}\d{1,3})\s*"
    r"<script[^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)
PORT_EXPR_RE: Final = re.compile(r"\(([^()]+)\)")
TYPE_RE: Final = re.compile(
    r"proxy-list/['\"]?><font[^>]*>\s*(HTTPS?|SOCKS\d?)",
    re.IGNORECASE,
)
ANON_RE: Final = re.compile(
    r"<font\s+class\s*=\s*['\"]?spy5['\"]?[^>]*>\s*(NOA|ANM|HIA)\s*</font>",
    re.IGNORECASE,
)
ASSIGNMENT_NAME_RE: Final = re.compile(r"^[A-Za-z_$][\w$]*$")
TOKEN_RE: Final = re.compile(r"-?\d+|[A-Za-z_$][\w$]*")


class Proxy(NamedTuple):
    ip: str
    port: str
    proxy_type: str
    anonymity: str


def _encode_base(number: int, radix: int) -> str:
    if number >= radix:
        prefix = _encode_base(number // radix, radix)
    else:
        prefix = ""
    remainder = number % radix
    if remainder > 35:
        character = chr(remainder + 29)
    elif remainder < 10:
        character = str(remainder)
    else:
        character = chr(ord("a") + remainder - 10)
    return prefix + character


def unpack_packed(html: str) -> tuple[str | None, int | None, int | None]:
    """Unpack the symbol table in spys.one's packed JavaScript."""
    match = PACKED_RE.search(html)
    if match is None:
        return None, None, None

    packed, radix_text, count_text, replacements_text = match.groups()
    try:
        radix = int(radix_text)
        count = int(count_text)
    except ValueError as exc:
        raise ValueError("invalid packed-script header") from exc
    if not 2 <= radix <= 62:
        raise ValueError(f"unsupported packed-script radix: {radix}")

    replacements = replacements_text.split("^")
    decoded = packed
    for index in range(min(count, len(replacements)) - 1, -1, -1):
        replacement = replacements[index]
        if replacement:
            token = re.escape(_encode_base(index, radix))
            decoded = re.sub(
                rf"\b{token}\b", lambda _match, value=replacement: value, decoded
            )
    return decoded, radix, count


def _xor_expression(expression: str, values: dict[str, int]) -> int | None:
    tokens = [token.strip() for token in expression.split("^")]
    if not tokens or any(not token for token in tokens):
        return None

    result = 0
    for token in tokens:
        if re.fullmatch(r"-?\d+", token):
            try:
                value = int(token)
            except ValueError:
                return None
        elif token in values:
            value = values[token]
        else:
            return None
        result ^= value
    return result


def parse_vars(decoded: str) -> tuple[dict[str, int], dict[str, str]]:
    """Resolve XOR variable assignments without evaluating JavaScript."""
    unresolved: dict[str, str] = {}
    for assignment in decoded.split(";"):
        if "=" not in assignment:
            continue
        name, expression = assignment.split("=", 1)
        name = name.strip().removeprefix("var ").strip()
        expression = expression.strip()
        tokens = [token.strip() for token in expression.split("^")]
        if (
            ASSIGNMENT_NAME_RE.fullmatch(name)
            and tokens
            and all(TOKEN_RE.fullmatch(token) for token in tokens)
        ):
            unresolved[name] = expression

    values: dict[str, int] = {}
    while unresolved:
        resolved_this_pass: list[str] = []
        for name, expression in unresolved.items():
            value = _xor_expression(expression, values)
            if value is not None:
                values[name] = value
                resolved_this_pass.append(name)
        if not resolved_this_pass:
            break
        for name in resolved_this_pass:
            del unresolved[name]
    return values, unresolved


def _decode_port(script: str, values: dict[str, int]) -> str | None:
    digits: list[str] = []
    for expression in PORT_EXPR_RE.findall(script):
        expression = expression.strip()
        if "^" not in expression:
            continue
        value = _xor_expression(expression, values)
        if value is None or not 0 <= value <= 9:
            return None
        digits.append(str(value))

    if not digits:
        return None
    port = "".join(digits)
    try:
        number = int(port)
    except ValueError:
        return None
    return port if 1 <= number <= 65535 else None


def _valid_ipv4(ip: str) -> bool:
    parts = ip.split(".")
    if len(parts) != 4 or not all(part.isdigit() for part in parts):
        return False
    try:
        octets = map(int, parts)
        return all(0 <= octet <= 255 for octet in octets)
    except ValueError:
        return False


def fetch_spys(
    country: str,
    limit: int = 30,
    timeout: float = 25,
    session: requests.Session | None = None,
) -> list[Proxy]:
    """Fetch and decode up to ``limit`` proxies for a two-letter country code."""
    country_code = country.strip().upper()
    if re.fullmatch(r"[A-Z]{2}", country_code) is None:
        raise ValueError(f"invalid two-letter country code: {country!r}")
    if limit not in XPP_MAP:
        raise ValueError(f"unsupported limit {limit}; choose one of {sorted(XPP_MAP)}")
    if timeout <= 0:
        raise ValueError("timeout must be greater than zero")

    url = f"https://spys.one/free-proxy-list/{country_code}/"
    data = None
    if limit != 30:
        data = {
            "xpp": str(XPP_MAP[limit]),
            "xf1": "0",
            "xf2": "0",
            "xf4": "0",
            "xf5": "0",
        }

    client = session or requests
    if data is None:
        response = client.get(url, headers=HEADERS, timeout=timeout)
    else:
        response = client.post(url, data=data, headers=HEADERS, timeout=timeout)
    response.raise_for_status()
    html = response.text

    decoded, _, _ = unpack_packed(html)
    if decoded is None:
        raise RuntimeError(
            "failed to find packed JavaScript; the page format may have changed"
        )
    values, unresolved = parse_vars(decoded)
    if unresolved:
        print(
            f"[!] unresolved variables: {', '.join(sorted(unresolved))}",
            file=sys.stderr,
        )

    proxies: list[Proxy] = []
    seen: set[tuple[str, str]] = set()
    matches = list(PROXY_RE.finditer(html))
    for index, match in enumerate(matches):
        ip, script = match.groups()
        port = _decode_port(script, values)
        if not _valid_ipv4(ip) or port is None or (ip, port) in seen:
            continue

        next_start = (
            matches[index + 1].start() if index + 1 < len(matches) else len(html)
        )
        snippet = html[match.end() : next_start]
        proxy_type_match = TYPE_RE.search(snippet)
        anonymity_match = ANON_RE.search(snippet)
        proxy_type = proxy_type_match.group(1).upper() if proxy_type_match else "?"
        anonymity = anonymity_match.group(1).upper() if anonymity_match else "?"

        seen.add((ip, port))
        proxies.append(Proxy(ip, port, proxy_type, anonymity))
        if len(proxies) >= limit:
            break
    return proxies


def _positive_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch spys.one free proxies by country code"
    )
    parser.add_argument(
        "country", help="Two-letter country code, e.g. PH, BD, PK, NG, KE, MA"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=30,
        choices=sorted(XPP_MAP),
        help="Maximum number of proxies to fetch",
    )
    parser.add_argument(
        "--timeout", type=_positive_float, default=25, help="HTTP timeout in seconds"
    )
    parser.add_argument(
        "--json", action="store_true", help="Output JSON lines instead of addresses"
    )
    parser.add_argument(
        "-o", "--output", type=Path, help="Write output to this UTF-8 file"
    )
    args = parser.parse_args()

    try:
        proxies = fetch_spys(args.country, limit=args.limit, timeout=args.timeout)
    except (requests.RequestException, RuntimeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    country_code = args.country.upper()
    if not proxies:
        print(
            f"No proxies found for {country_code} (the page format may have changed).",
            file=sys.stderr,
        )
        return 1

    if args.json:
        output = "\n".join(
            json.dumps(
                {
                    "ip": proxy.ip,
                    "port": proxy.port,
                    "type": proxy.proxy_type,
                    "anon": proxy.anonymity,
                    "addr": f"{proxy.ip}:{proxy.port}",
                },
                separators=(",", ":"),
            )
            for proxy in proxies
        )
    else:
        print(
            f"# {country_code}  {len(proxies)} proxies  "
            f"https://spys.one/free-proxy-list/{country_code}/",
            file=sys.stderr,
        )
        for proxy in proxies:
            print(
                f"# {proxy.ip}:{proxy.port:10s}  {proxy.proxy_type:7s} {proxy.anonymity}",
                file=sys.stderr,
            )
        output = "\n".join(f"{proxy.ip}:{proxy.port}" for proxy in proxies)

    if args.output:
        try:
            args.output.write_text(output + "\n", encoding="utf-8")
        except OSError as exc:
            print(f"Error writing {args.output}: {exc}", file=sys.stderr)
            return 1
        print(
            f"Wrote {len(proxies)} proxies to {args.output} ({country_code})",
            file=sys.stderr,
        )
    else:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
