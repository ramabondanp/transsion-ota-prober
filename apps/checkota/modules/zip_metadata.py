"""Direct HTTP Range fetch of a single file from a remote ZIP.

Replaces the ``remotezip`` dependency. Google's OTA CDN rejects suffix ranges
(``bytes=-N``), so we cannot let a generic remote-zip library locate the central
directory the easy way. Instead we:

  1. probe the total size with ``bytes=0-0`` (reads Content-Range),
  2. read the End-Of-Central-Directory region from the tail (absolute range),
  3. read the central directory and locate the target entry,
  4. read that entry's local header + compressed bytes and inflate them.

ZIP64 is handled because full OTA packages routinely exceed 4 GiB.
"""

import struct
import zlib
from typing import Optional

import requests

# ZIP record signatures
_EOCD_SIG = b"PK\x05\x06"
_EOCD64_LOCATOR_SIG = b"PK\x06\x07"
_EOCD64_SIG = b"PK\x06\x06"
_CD_ENTRY_SIG = b"PK\x01\x02"
_LOCAL_SIG = b"PK\x03\x04"

_EOCD_MIN = 22
# Max bytes to pull from the tail when hunting for the EOCD. The EOCD comment is
# at most 65535 bytes, plus the 22-byte record and a 20-byte ZIP64 locator.
_TAIL_CHUNK = 65536 + _EOCD_MIN + 20


class RemoteZipFetchError(Exception):
    """Raised when the remote ZIP cannot be read or the entry is missing."""


class RemoteZipTransientError(RemoteZipFetchError):
    """Raised on a transient failure (network/transport or retryable HTTP status).

    Transient failure modes:
      - ConnectionError / Timeout / SSL / ChunkedEncodingError / ProtocolError
      - HTTP 408 (request timeout), 425 (too early), 429 (rate-limit),
        500/502/503/504 (server).

    Subclass of RemoteZipFetchError so legacy `except RemoteZipFetchError` arms
    still see these exceptions; place any `except RemoteZipTransientError` arm
    BEFORE plain `except RemoteZipFetchError`.
    """


# HTTP status codes that justify a 1s->2s->4s retry. Other 4xx (notably 416
# Range Not Satisfiable, 403, 404) are treated as structural and surfaced
# immediately via plain RemoteZipFetchError.
_RETRYABLE_HTTP_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


_CONNECT_TIMEOUT = 5.0  # TLS-handshake budget for cold GCP connections.


def _timeout_pair(read_budget: float) -> tuple[float, float]:
    """Convert a single numeric timeout into `requests`' (connect, read) tuple."""
    return (_CONNECT_TIMEOUT, max(read_budget, _CONNECT_TIMEOUT))


def _range_get(
    session: requests.Session,
    url: str,
    start: int,
    end: int,
    timeout: float,
    headers: dict,
) -> bytes:
    """Fetch an inclusive byte range [start, end] via HTTP Range. Returns the body."""
    hdrs = dict(headers)
    hdrs["Range"] = f"bytes={start}-{end}"
    try:
        resp = session.get(url, headers=hdrs, timeout=_timeout_pair(timeout))
        resp.raise_for_status()
    except requests.exceptions.HTTPError as exc:
        status = getattr(exc.response, "status_code", None)
        if status in _RETRYABLE_HTTP_STATUSES:
            raise RemoteZipTransientError(
                f"Retryable HTTP {status} for {url} (bytes={start}-{end}): {exc}"
            ) from exc
        # Non-retryable HTTP error (e.g. 416 Range Not Satisfiable).
        raise RemoteZipFetchError(
            f"Non-retryable HTTP {status} for {url} (bytes={start}-{end}): {exc}"
        ) from exc
    except requests.exceptions.RequestException as exc:
        # ConnectionError / Timeout / SSLError / ChunkedEncodingError / ProtocolError.
        raise RemoteZipTransientError(
            f"Transport failure for {url} (bytes={start}-{end}): {exc}"
        ) from exc
    if resp.status_code != 206:
        # Range ignored AND a 2xx slipped through (rare; CDN quirks).
        raise RemoteZipFetchError(
            f"Server ignored Range request (status {resp.status_code}); "
            "ranged reads are required."
        )
    return resp.content


def _probe_size(
    session: requests.Session, url: str, timeout: float, headers: dict
) -> int:
    """Return the total resource size using a 1-byte ranged probe."""
    hdrs = dict(headers)
    hdrs["Range"] = "bytes=0-0"
    try:
        resp = session.get(url, headers=hdrs, timeout=_timeout_pair(timeout))
        resp.raise_for_status()
    except requests.exceptions.HTTPError as exc:
        status = getattr(exc.response, "status_code", None)
        if status in _RETRYABLE_HTTP_STATUSES:
            raise RemoteZipTransientError(
                f"Retryable HTTP {status} while probing size of {url}: {exc}"
            ) from exc
        raise RemoteZipFetchError(
            f"Non-retryable HTTP {status} while probing size of {url}: {exc}"
        ) from exc
    except requests.exceptions.RequestException as exc:
        raise RemoteZipTransientError(
            f"Transport failure while probing size of {url}: {exc}"
        ) from exc
    content_range = resp.headers.get("Content-Range", "")
    if "/" in content_range:
        total = content_range.rsplit("/", 1)[-1].strip()
        if total.isdigit():
            return int(total)
    # Fallback: a non-ranged Content-Length (only valid if server ignored Range).
    if resp.status_code == 200:
        length = resp.headers.get("Content-Length")
        if length and length.isdigit():
            return int(length)
    raise RemoteZipFetchError("Could not determine remote ZIP size from Content-Range.")


def _locate_cd(tail: bytes, tail_start: int) -> tuple:
    """Find the central directory (offset, size) from an EOCD tail buffer.

    Returns (cd_offset, cd_size). Handles ZIP64.
    """
    eocd_pos = tail.rfind(_EOCD_SIG)
    if eocd_pos < 0:
        raise RemoteZipFetchError("End-of-central-directory record not found.")

    eocd = tail[eocd_pos : eocd_pos + _EOCD_MIN]
    if len(eocd) < _EOCD_MIN:
        raise RemoteZipFetchError("Truncated EOCD record.")

    (cd_size, cd_offset) = struct.unpack("<II", eocd[12:20])

    # ZIP64 sentinel: fall back to the ZIP64 EOCD record.
    if cd_offset == 0xFFFFFFFF or cd_size == 0xFFFFFFFF:
        loc_pos = tail.rfind(_EOCD64_LOCATOR_SIG, 0, eocd_pos)
        if loc_pos < 0:
            raise RemoteZipFetchError("ZIP64 locator not found for large archive.")
        # ZIP64 EOCD locator: relative offset of the ZIP64 EOCD record at byte 8.
        (eocd64_abs_offset,) = struct.unpack("<Q", tail[loc_pos + 8 : loc_pos + 16])
        rel = eocd64_abs_offset - tail_start
        if rel < 0 or rel + 56 > len(tail):
            raise RemoteZipFetchError(
                "ZIP64 EOCD record lies outside the fetched tail."
            )
        eocd64 = tail[rel : rel + 56]
        if eocd64[0:4] != _EOCD64_SIG:
            raise RemoteZipFetchError("ZIP64 EOCD signature mismatch.")
        (cd_size,) = struct.unpack("<Q", eocd64[40:48])
        (cd_offset,) = struct.unpack("<Q", eocd64[48:56])

    return cd_offset, cd_size


def _find_entry(cd: bytes, target_name: bytes) -> tuple:
    """Scan the central directory for target_name.

    Returns (compression_method, compressed_size, local_header_offset).
    """
    pos = 0
    n = len(cd)
    while pos + 46 <= n:
        if cd[pos : pos + 4] != _CD_ENTRY_SIG:
            break
        method = struct.unpack("<H", cd[pos + 10 : pos + 12])[0]
        comp_size = struct.unpack("<I", cd[pos + 20 : pos + 24])[0]
        name_len = struct.unpack("<H", cd[pos + 28 : pos + 30])[0]
        extra_len = struct.unpack("<H", cd[pos + 30 : pos + 32])[0]
        comment_len = struct.unpack("<H", cd[pos + 32 : pos + 34])[0]
        local_offset = struct.unpack("<I", cd[pos + 42 : pos + 46])[0]

        name = cd[pos + 46 : pos + 46 + name_len]
        extra = cd[pos + 46 + name_len : pos + 46 + name_len + extra_len]

        if name == target_name:
            # ZIP64 extra: replace 0xFFFFFFFF sentinels in declared order
            # (uncompressed, compressed, local-header-offset, disk).
            if comp_size == 0xFFFFFFFF or local_offset == 0xFFFFFFFF:
                comp_size, local_offset = _zip64_fixup(
                    extra,
                    uncomp_is_max=(
                        struct.unpack("<I", cd[pos + 24 : pos + 28])[0] == 0xFFFFFFFF
                    ),
                    comp_size=comp_size,
                    local_offset=local_offset,
                )
            return method, comp_size, local_offset

        pos += 46 + name_len + extra_len + comment_len

    raise RemoteZipFetchError("Target entry not found in central directory.")


def _zip64_fixup(
    extra: bytes, uncomp_is_max: bool, comp_size: int, local_offset: int
) -> tuple:
    """Read 8-byte ZIP64 values from a central-dir extra field.

    The 0x0001 extra block stores, in order and only when the corresponding
    32-bit field is 0xFFFFFFFF: uncompressed size, compressed size,
    local-header offset, disk number.
    """
    pos = 0
    while pos + 4 <= len(extra):
        header_id, data_size = struct.unpack("<HH", extra[pos : pos + 4])
        body = extra[pos + 4 : pos + 4 + data_size]
        if header_id == 0x0001:
            bp = 0
            if uncomp_is_max:
                bp += 8  # skip uncompressed size
            if comp_size == 0xFFFFFFFF and bp + 8 <= len(body):
                comp_size = struct.unpack("<Q", body[bp : bp + 8])[0]
                bp += 8
            if local_offset == 0xFFFFFFFF and bp + 8 <= len(body):
                local_offset = struct.unpack("<Q", body[bp : bp + 8])[0]
                bp += 8
            break
        pos += 4 + data_size
    return comp_size, local_offset


def fetch_zip_member(
    url: str,
    member: str,
    session: Optional[requests.Session] = None,
    timeout: float = 15.0,
    headers: Optional[dict] = None,
) -> bytes:
    """Fetch and return the decompressed bytes of a single ZIP member over HTTP.

    Uses only absolute byte ranges (no suffix ranges). Structural problems
    (bad EOCD, missing entry, unsupported compression, non-retryable HTTP
    status) raise RemoteZipFetchError. Transient failures (network errors and
    retryable HTTP statuses) raise RemoteZipTransientError so callers can
    distinguish structural from transient failures.
    """
    sess = session or requests.Session()
    hdrs = headers or {}
    target = member.encode("utf-8")

    size = _probe_size(sess, url, timeout, hdrs)
    if size <= 0:
        raise RemoteZipFetchError("Remote ZIP reported zero size.")

    tail_len = min(_TAIL_CHUNK, size)
    tail_start = size - tail_len
    tail = _range_get(sess, url, tail_start, size - 1, timeout, hdrs)

    cd_offset, cd_size = _locate_cd(tail, tail_start)
    if cd_size <= 0 or cd_offset < 0 or cd_offset + cd_size > size:
        raise RemoteZipFetchError("Central directory bounds are invalid.")

    # Reuse already-fetched tail bytes if the CD falls inside them.
    if cd_offset >= tail_start:
        cd = tail[cd_offset - tail_start : cd_offset - tail_start + cd_size]
    else:
        cd = _range_get(sess, url, cd_offset, cd_offset + cd_size - 1, timeout, hdrs)

    method, comp_size, local_offset = _find_entry(cd, target)

    # Local header has its own (possibly different) extra-field length, so read
    # the fixed 30-byte header first to compute the true data start.
    local_hdr = _range_get(sess, url, local_offset, local_offset + 29, timeout, hdrs)
    if local_hdr[0:4] != _LOCAL_SIG:
        raise RemoteZipFetchError("Local file header signature mismatch.")
    name_len = struct.unpack("<H", local_hdr[26:28])[0]
    extra_len = struct.unpack("<H", local_hdr[28:30])[0]
    data_start = local_offset + 30 + name_len + extra_len

    raw = _range_get(sess, url, data_start, data_start + comp_size - 1, timeout, hdrs)

    if method == 0:  # stored
        return raw
    if method == 8:  # deflate
        return zlib.decompress(raw, -zlib.MAX_WBITS)
    raise RemoteZipFetchError(f"Unsupported ZIP compression method {method}.")
