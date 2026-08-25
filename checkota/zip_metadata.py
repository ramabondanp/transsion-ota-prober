"""Direct HTTP Range fetch of a single file from a remote ZIP.

The OTA CDN rejects suffix ranges, so this module locates the requested member
using absolute ranges only.  Responses are streamed and checked against the
requested range before their bytes are retained in memory.
"""

from __future__ import annotations

import re
import struct
import time
import zlib
from urllib.parse import urljoin, urlsplit

import requests

from checkota.constants import RETRYABLE_HTTP_STATUSES

# ZIP record signatures
_EOCD_SIG = b"PK\x05\x06"
_EOCD64_LOCATOR_SIG = b"PK\x06\x07"
_EOCD64_SIG = b"PK\x06\x06"
_CD_ENTRY_SIG = b"PK\x01\x02"
_LOCAL_SIG = b"PK\x03\x04"

_EOCD_MIN = 22
_EOCD64_MIN = 56
# The tail must include the maximum EOCD comment, the ZIP64 EOCD locator, and
# the fixed portion of the ZIP64 EOCD record.
_TAIL_CHUNK = 65535 + _EOCD_MIN + 20 + _EOCD64_MIN
_STREAM_CHUNK_SIZE = 64 * 1024
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_MAX_REDIRECTS = 5

# Metadata is a small text member.  Keep these caps explicit so archive
# declarations cannot turn range reads or decompression into unbounded work.
MAX_CENTRAL_DIRECTORY_BYTES = 16 * 1024 * 1024
MAX_COMPRESSED_METADATA_BYTES = 1 * 1024 * 1024
MAX_DECOMPRESSED_METADATA_BYTES = 1 * 1024 * 1024

_MAX_LOCAL_VARIABLE_SIZE = 0xFFFF + 0xFFFF
_CONTENT_RANGE_RE = re.compile(r"bytes ([0-9]+)-([0-9]+)/([0-9]+)\Z")


class RemoteZipFetchError(Exception):
    """Raised when the remote ZIP cannot be read or the entry is missing."""


class RemoteZipTransientError(RemoteZipFetchError):
    """Raised on a transient failure (network/transport or retryable HTTP status).

    Transient failure modes:
      - ConnectionError / Timeout / SSL / ChunkedEncodingError / ProtocolError
      - HTTP 408 (request timeout), 425 (too early), 429 (rate-limit),
        500/502/503/504 (server).

    Subclass of RemoteZipFetchError so legacy ``except RemoteZipFetchError``
    arms still see these exceptions; place any ``except RemoteZipTransientError``
    arm before plain ``except RemoteZipFetchError``.
    """


_CONNECT_TIMEOUT = 5.0


def _timeout_pair(read_budget: float) -> tuple[float, float]:
    """Convert a single numeric timeout into requests' (connect, read) tuple."""
    return (_CONNECT_TIMEOUT, max(read_budget, _CONNECT_TIMEOUT))


def _decimal_header(value: str | None, name: str) -> int:
    if not isinstance(value, str) or not value.strip().isdigit():
        raise RemoteZipFetchError(f"Missing or malformed {name} header.")
    return int(value.strip())


def _header_value(headers, name: str):
    value = headers.get(name)
    if value is not None:
        return value
    name_lower = name.lower()
    for key, value in headers.items():
        if isinstance(key, str) and key.lower() == name_lower:
            return value
    return None


def _is_allowed_ota_redirect(value: str) -> bool:
    """Allow only HTTPS redirects within Google's OTA delivery network."""
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname or ""
        trusted_host = hostname == "android.googleapis.com" or (
            hostname == "gvt1.com" or hostname.endswith(".gvt1.com")
        )
        return (
            not any(
                char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value
            )
            and parsed.scheme == "https"
            and trusted_host
            and parsed.port is None
            and not parsed.username
            and not parsed.password
            and parsed.path.startswith("/packages/")
        )
    except ValueError:
        return False


def _validate_range_response(
    response,
    start: int,
    end: int,
    expected_total: int | None,
) -> tuple[int, int]:
    """Validate response metadata and return (archive size, body length)."""
    status = getattr(response, "status_code", None)
    if status != 206:
        if status in RETRYABLE_HTTP_STATUSES:
            raise RemoteZipTransientError(
                f"Retryable HTTP {status} for ranged bytes={start}-{end}."
            )
        raise RemoteZipFetchError(
            f"Ranged request returned unexpected HTTP status {status}; expected 206."
        )

    response_headers = getattr(response, "headers", None) or {}
    content_range = _header_value(response_headers, "Content-Range")
    if not isinstance(content_range, str):
        raise RemoteZipFetchError("Missing Content-Range header on ranged response.")
    match = _CONTENT_RANGE_RE.fullmatch(content_range.strip())
    if match is None:
        raise RemoteZipFetchError(f"Malformed Content-Range header {content_range!r}.")

    response_start, response_end, total = (int(value) for value in match.groups())
    expected_length = end - start + 1
    if response_start != start or response_end != end:
        raise RemoteZipFetchError(
            "Content-Range does not exactly match the requested byte range."
        )
    if response_end >= total:
        raise RemoteZipFetchError("Content-Range extends beyond the remote resource.")
    if expected_total is not None and total != expected_total:
        raise RemoteZipFetchError(
            "Content-Range reports an inconsistent resource size."
        )

    content_length_header = _header_value(response_headers, "Content-Length")
    if content_length_header is not None:
        content_length = _decimal_header(content_length_header, "Content-Length")
        if content_length != expected_length:
            raise RemoteZipFetchError(
                "Content-Length does not exactly match the requested byte range."
            )
    return total, expected_length


def _read_range_response(
    response,
    start: int,
    end: int,
    expected_total: int | None,
) -> tuple[bytes, int]:
    total, expected_length = _validate_range_response(
        response, start, end, expected_total
    )
    chunks: list[bytes] = []
    received = 0
    try:
        iterator = response.iter_content(chunk_size=_STREAM_CHUNK_SIZE)
        for chunk in iterator:
            if not chunk:
                continue
            if not isinstance(chunk, bytes):
                raise RemoteZipFetchError("Ranged response yielded a non-byte chunk.")
            received += len(chunk)
            if received > expected_length:
                raise RemoteZipFetchError(
                    "Ranged response body is larger than declared."
                )
            chunks.append(chunk)
    except AttributeError as exc:
        raise RemoteZipFetchError("Ranged response cannot be streamed.") from exc

    if received != expected_length:
        raise RemoteZipFetchError("Ranged response body is truncated.")
    return b"".join(chunks), total


def _range_get_with_total(
    session: requests.Session,
    url: str,
    start: int,
    end: int,
    timeout: float,
    headers: dict,
    attempts: int = 1,
    use_proxy_env: bool = False,
    expected_total: int | None = None,
) -> tuple[bytes, int]:
    """Fetch an inclusive range and return its bytes plus Content-Range total."""
    if start < 0 or end < start:
        raise RemoteZipFetchError(f"Invalid requested byte range {start}-{end}.")
    if attempts < 1:
        raise ValueError("attempts must be at least one")

    hdrs = dict(headers)
    hdrs["Range"] = f"bytes={start}-{end}"
    hdrs.setdefault("Accept-Encoding", "identity")

    for attempt in range(attempts):
        response = None
        try:
            get_kwargs = {
                "headers": hdrs,
                "timeout": _timeout_pair(timeout),
                "stream": True,
                # OTA URLs are validated at the check-in boundary. Do not
                # follow a compromised redirect to a different origin.
                "allow_redirects": False,
            }
            if not use_proxy_env:
                get_kwargs["proxies"] = {"http": None, "https": None, "all": None}
            request_url = url
            for redirect_count in range(_MAX_REDIRECTS + 1):
                response = session.get(request_url, **get_kwargs)
                status = getattr(response, "status_code", None)
                if status not in _REDIRECT_STATUSES:
                    content, total = _read_range_response(
                        response, start, end, expected_total
                    )
                    return content, total

                location = _header_value(
                    getattr(response, "headers", None) or {}, "Location"
                )
                if not isinstance(location, str) or not location:
                    raise RemoteZipFetchError(
                        f"OTA redirect HTTP {status} did not include a Location header."
                    )
                redirect_url = urljoin(request_url, location)
                if not _is_allowed_ota_redirect(redirect_url):
                    raise RemoteZipFetchError(
                        f"Refusing OTA redirect outside Google's delivery network: "
                        f"{redirect_url}"
                    )
                if redirect_count >= _MAX_REDIRECTS:
                    raise RemoteZipFetchError("Too many OTA download redirects.")

                response.close()
                response = None
                request_url = redirect_url
        except RemoteZipTransientError:
            if attempt < attempts - 1:
                time.sleep(2**attempt)
                continue
            raise
        except requests.exceptions.RequestException as exc:
            transient_err = RemoteZipTransientError(
                f"Transport failure for {url} (bytes={start}-{end}): {exc}"
            )
            if attempt < attempts - 1:
                time.sleep(2**attempt)
                continue
            raise transient_err from exc
        finally:
            if response is not None:
                response.close()

    raise RemoteZipTransientError("Unexpected end of retry loop")


def _range_get(
    session: requests.Session,
    url: str,
    start: int,
    end: int,
    timeout: float,
    headers: dict,
    attempts: int = 1,
    use_proxy_env: bool = False,
    expected_total: int | None = None,
) -> bytes:
    """Fetch an inclusive byte range via HTTP Range and return its body."""
    content, _ = _range_get_with_total(
        session,
        url,
        start,
        end,
        timeout,
        headers,
        attempts=attempts,
        use_proxy_env=use_proxy_env,
        expected_total=expected_total,
    )
    return content


def _probe_size(
    session: requests.Session,
    url: str,
    timeout: float,
    headers: dict,
    use_proxy_env: bool = False,
) -> int:
    """Return the total resource size using a strict one-byte range probe."""
    _, total = _range_get_with_total(
        session,
        url,
        0,
        0,
        timeout,
        headers,
        use_proxy_env=use_proxy_env,
    )
    if total <= 0:
        raise RemoteZipFetchError("Remote ZIP reported zero size.")
    return total


def _locate_cd(tail: bytes, tail_start: int) -> tuple[int, int]:
    """Find the central directory (offset, size) from an EOCD tail buffer."""
    eocd_pos = tail.rfind(_EOCD_SIG)
    while eocd_pos >= 0:
        if eocd_pos + _EOCD_MIN <= len(tail):
            comment_len = struct.unpack_from("<H", tail, eocd_pos + 20)[0]
            if eocd_pos + _EOCD_MIN + comment_len == len(tail):
                break
        eocd_pos = tail.rfind(_EOCD_SIG, 0, eocd_pos)
    if eocd_pos < 0:
        raise RemoteZipFetchError("End-of-central-directory record not found.")

    eocd = tail[eocd_pos : eocd_pos + _EOCD_MIN]
    if len(eocd) < _EOCD_MIN:
        raise RemoteZipFetchError("Truncated EOCD record.")
    (
        disk_number,
        cd_disk_number,
        entries_on_disk,
        entries_total,
        cd_size,
        cd_offset,
        _comment_len,
    ) = struct.unpack("<HHHHIIH", eocd[4:])
    eocd_abs = tail_start + eocd_pos

    zip64_needed = (
        disk_number == 0xFFFF
        or cd_disk_number == 0xFFFF
        or entries_on_disk == 0xFFFF
        or entries_total == 0xFFFF
        or cd_size == 0xFFFFFFFF
        or cd_offset == 0xFFFFFFFF
    )
    if zip64_needed:
        locator_pos = eocd_pos - 20
        if (
            locator_pos < 0
            or tail[locator_pos : locator_pos + 4] != _EOCD64_LOCATOR_SIG
        ):
            raise RemoteZipFetchError("ZIP64 locator not found for large archive.")
        if locator_pos + 20 > len(tail):
            raise RemoteZipFetchError("Truncated ZIP64 locator.")
        locator_disk, eocd64_abs, total_disks = struct.unpack_from(
            "<IQI", tail, locator_pos + 4
        )
        if locator_disk != 0 or total_disks != 1:
            raise RemoteZipFetchError("Multi-disk ZIP64 archives are unsupported.")
        rel = eocd64_abs - tail_start
        if rel < 0 or rel + _EOCD64_MIN > len(tail):
            raise RemoteZipFetchError(
                "ZIP64 EOCD record lies outside the fetched tail."
            )
        eocd64 = tail[rel : rel + _EOCD64_MIN]
        if eocd64[0:4] != _EOCD64_SIG:
            raise RemoteZipFetchError("ZIP64 EOCD signature mismatch.")
        record_size = struct.unpack_from("<Q", eocd64, 4)[0]
        if record_size < 44:
            raise RemoteZipFetchError("Malformed ZIP64 EOCD record size.")
        record_end = rel + 12 + record_size
        if record_end > len(tail) or tail_start + record_end > tail_start + locator_pos:
            raise RemoteZipFetchError("Truncated or misplaced ZIP64 EOCD record.")
        zip64_disk, zip64_cd_disk = struct.unpack_from("<II", eocd64, 16)
        zip64_entries_on_disk, zip64_entries_total = struct.unpack_from(
            "<QQ", eocd64, 24
        )
        if (
            zip64_disk != 0
            or zip64_cd_disk != 0
            or zip64_entries_on_disk != zip64_entries_total
        ):
            raise RemoteZipFetchError("Multi-disk ZIP64 archive metadata is invalid.")
        cd_size, cd_offset = struct.unpack_from("<QQ", eocd64, 40)

    if cd_size <= 0:
        raise RemoteZipFetchError("Central directory is empty.")
    if cd_size > MAX_CENTRAL_DIRECTORY_BYTES:
        raise RemoteZipFetchError("Central directory exceeds its size cap.")
    if cd_offset > eocd_abs or cd_size > eocd_abs - cd_offset:
        raise RemoteZipFetchError("Central directory bounds are invalid.")
    return cd_offset, cd_size


def _extra_fields(extra: bytes):
    """Yield extra-field (header id, body) pairs, rejecting truncation."""
    pos = 0
    while pos < len(extra):
        if len(extra) - pos < 4:
            raise RemoteZipFetchError("Truncated ZIP extra field header.")
        header_id, data_size = struct.unpack_from("<HH", extra, pos)
        body_start = pos + 4
        body_end = body_start + data_size
        if body_end > len(extra):
            raise RemoteZipFetchError("Truncated ZIP extra field body.")
        yield header_id, extra[body_start:body_end]
        pos = body_end


def _zip64_fixup(
    extra: bytes,
    uncompressed_size: int,
    compressed_size: int,
    local_offset: int,
    disk_start: int = 0,
) -> tuple[int, int, int, int]:
    """Resolve ZIP64 sentinel fields in their mandated declaration order."""
    fields = list(_extra_fields(extra))
    needs_uncompressed = uncompressed_size == 0xFFFFFFFF
    needs_compressed = compressed_size == 0xFFFFFFFF
    needs_offset = local_offset == 0xFFFFFFFF
    needs_disk = disk_start == 0xFFFF
    if not (needs_uncompressed or needs_compressed or needs_offset or needs_disk):
        return uncompressed_size, compressed_size, local_offset, disk_start

    zip64_body = next((body for header_id, body in fields if header_id == 0x0001), None)
    if zip64_body is None:
        raise RemoteZipFetchError("ZIP64 extra field is missing.")

    pos = 0

    def take(length: int, description: str) -> bytes:
        nonlocal pos
        if pos + length > len(zip64_body):
            raise RemoteZipFetchError(f"ZIP64 extra field lacks {description}.")
        value = zip64_body[pos : pos + length]
        pos += length
        return value

    if needs_uncompressed:
        uncompressed_size = struct.unpack("<Q", take(8, "uncompressed size"))[0]
    if needs_compressed:
        compressed_size = struct.unpack("<Q", take(8, "compressed size"))[0]
    if needs_offset:
        local_offset = struct.unpack("<Q", take(8, "local-header offset"))[0]
    if needs_disk:
        disk_start = struct.unpack("<I", take(4, "disk number"))[0]
    return uncompressed_size, compressed_size, local_offset, disk_start


def _find_entry(
    cd: bytes, target_name: bytes
) -> tuple[int, int, int, int, int, int, int]:
    """Scan a complete central directory for target_name.

    Returns (compression method, uncompressed size, compressed size,
    local-header offset, name length, extra length, CRC-32).
    """
    if len(cd) > MAX_CENTRAL_DIRECTORY_BYTES:
        raise RemoteZipFetchError("Central directory exceeds its size cap.")
    pos = 0
    n = len(cd)
    while pos < n:
        if n - pos < 46:
            raise RemoteZipFetchError("Truncated central-directory record.")
        if cd[pos : pos + 4] != _CD_ENTRY_SIG:
            raise RemoteZipFetchError("Malformed central-directory record signature.")

        method = struct.unpack_from("<H", cd, pos + 10)[0]
        crc32 = struct.unpack_from("<I", cd, pos + 16)[0]
        uncomp_size = struct.unpack_from("<I", cd, pos + 24)[0]
        comp_size = struct.unpack_from("<I", cd, pos + 20)[0]
        name_len = struct.unpack_from("<H", cd, pos + 28)[0]
        extra_len = struct.unpack_from("<H", cd, pos + 30)[0]
        comment_len = struct.unpack_from("<H", cd, pos + 32)[0]
        disk_start = struct.unpack_from("<H", cd, pos + 34)[0]
        local_offset = struct.unpack_from("<I", cd, pos + 42)[0]

        record_len = 46 + name_len + extra_len + comment_len
        record_end = pos + record_len
        if record_end > n:
            raise RemoteZipFetchError("Truncated central-directory record fields.")

        name_start = pos + 46
        extra_start = name_start + name_len
        name = cd[name_start:extra_start]
        extra = cd[extra_start : extra_start + extra_len]
        # Validate every extra field even when this is not the requested entry.
        list(_extra_fields(extra))
        uncomp_size, comp_size, local_offset, disk_start = _zip64_fixup(
            extra, uncomp_size, comp_size, local_offset, disk_start
        )
        if disk_start != 0:
            raise RemoteZipFetchError(
                "Multi-disk central-directory entries are unsupported."
            )

        if name == target_name:
            if comp_size > MAX_COMPRESSED_METADATA_BYTES:
                raise RemoteZipFetchError("Compressed metadata exceeds its size cap.")
            if uncomp_size > MAX_DECOMPRESSED_METADATA_BYTES:
                raise RemoteZipFetchError("Decompressed metadata exceeds its size cap.")
            return (
                method,
                uncomp_size,
                comp_size,
                local_offset,
                name_len,
                extra_len,
                crc32,
            )
        pos = record_end

    raise RemoteZipFetchError("Target entry not found in central directory.")


def _validate_local_header(
    raw: bytes,
    target_name: bytes,
    central_method: int,
    central_uncomp_size: int,
    central_comp_size: int,
    central_crc32: int,
) -> tuple[int, int]:
    """Validate the local header and return (payload start, payload end)."""
    if len(raw) < 30:
        raise RemoteZipFetchError("Truncated local file header.")
    if raw[0:4] != _LOCAL_SIG:
        raise RemoteZipFetchError("Local file header signature mismatch.")

    flags, method = struct.unpack_from("<HH", raw, 6)
    local_crc32 = struct.unpack_from("<I", raw, 14)[0]
    local_comp_size = struct.unpack_from("<I", raw, 18)[0]
    local_uncomp_size = struct.unpack_from("<I", raw, 22)[0]
    name_len = struct.unpack_from("<H", raw, 26)[0]
    extra_len = struct.unpack_from("<H", raw, 28)[0]
    variable_end = 30 + name_len + extra_len
    if variable_end > len(raw):
        raise RemoteZipFetchError("Truncated local file header fields.")

    local_name = raw[30 : 30 + name_len]
    local_extra = raw[30 + name_len : variable_end]
    list(_extra_fields(local_extra))
    if local_name != target_name:
        raise RemoteZipFetchError("Local header name does not match central directory.")
    if method != central_method:
        raise RemoteZipFetchError("Local and central compression methods differ.")
    if flags & 0x0001:
        raise RemoteZipFetchError("Encrypted ZIP members are unsupported.")

    # Without a data descriptor the local header must carry the same sizes as
    # the central directory. With a descriptor, the central directory is the
    # authoritative size declaration.
    if not flags & 0x0008:
        local_uncomp_size, local_comp_size, _, _ = _zip64_fixup(
            local_extra,
            local_uncomp_size,
            local_comp_size,
            0,
        )
        if (
            local_uncomp_size != central_uncomp_size
            or local_comp_size != central_comp_size
        ):
            raise RemoteZipFetchError("Local and central member sizes differ.")
        if local_crc32 != central_crc32:
            raise RemoteZipFetchError("Local and central member CRC values differ.")

    payload_end = variable_end + central_comp_size
    if payload_end > len(raw):
        raise RemoteZipFetchError("Truncated ZIP member payload.")
    return variable_end, payload_end


def _decompress_deflate(payload: bytes, expected_size: int) -> bytes:
    """Inflate a raw deflate stream without exceeding the metadata cap."""
    if expected_size > MAX_DECOMPRESSED_METADATA_BYTES:
        raise RemoteZipFetchError("Decompressed metadata exceeds its size cap.")

    decoder = zlib.decompressobj(-zlib.MAX_WBITS)
    # A zero-length stream still needs a positive zlib output limit. For every
    # other declaration, never give zlib a limit larger than the declaration.
    output_limit = max(1, expected_size)
    try:
        output = decoder.decompress(payload, output_limit)
        if len(output) > expected_size or len(output) > MAX_DECOMPRESSED_METADATA_BYTES:
            raise RemoteZipFetchError("Deflated metadata exceeds its size cap.")
        if decoder.unconsumed_tail:
            raise RemoteZipFetchError("Deflated metadata exceeds its size cap.")
        if not decoder.eof:
            raise RemoteZipFetchError("Truncated deflated metadata.")
        if decoder.unused_data:
            raise RemoteZipFetchError("Deflated metadata contains trailing bytes.")
        remaining = expected_size - len(output)
        if remaining:
            output += decoder.flush(remaining)
    except zlib.error as exc:
        raise RemoteZipFetchError("Malformed deflated metadata.") from exc

    if len(output) > MAX_DECOMPRESSED_METADATA_BYTES or len(output) != expected_size:
        raise RemoteZipFetchError(
            "Deflated metadata size does not match its declaration."
        )
    return output


def _fetch_zip_member(
    sess: requests.Session,
    url: str,
    member: str,
    timeout: float,
    hdrs: dict,
    use_proxy_env: bool,
) -> bytes:
    target = member.encode("utf-8")
    size = _probe_size(sess, url, timeout, hdrs, use_proxy_env=use_proxy_env)

    tail_len = min(_TAIL_CHUNK, size)
    tail_start = size - tail_len
    tail = _range_get(
        sess,
        url,
        tail_start,
        size - 1,
        timeout,
        hdrs,
        use_proxy_env=use_proxy_env,
        expected_total=size,
    )

    cd_offset, cd_size = _locate_cd(tail, tail_start)
    cd_end = cd_offset + cd_size
    if cd_offset < 0 or cd_end > size:
        raise RemoteZipFetchError("Central directory bounds are invalid.")

    # Reuse already-fetched tail bytes if the CD falls inside it.
    if cd_offset >= tail_start:
        cd = tail[cd_offset - tail_start : cd_end - tail_start]
        if len(cd) != cd_size:
            raise RemoteZipFetchError("Central directory is truncated.")
    else:
        cd = _range_get(
            sess,
            url,
            cd_offset,
            cd_end - 1,
            timeout,
            hdrs,
            use_proxy_env=use_proxy_env,
            expected_total=size,
        )

    method, uncomp_size, comp_size, local_offset, _, _, crc32 = _find_entry(cd, target)
    if method not in (0, 8):
        raise RemoteZipFetchError(f"Unsupported ZIP compression method {method}.")
    if local_offset > size - 30:
        raise RemoteZipFetchError("Local file header lies outside the remote ZIP.")

    # The local name and extra fields are each 16-bit values. Over-fetching
    # their maximum possible prefix preserves the single combined read while
    # still bounding the allocation independently of archive declarations.
    local_read_length = 30 + _MAX_LOCAL_VARIABLE_SIZE + comp_size
    local_end = min(size - 1, local_offset + local_read_length - 1)
    raw = _range_get(
        sess,
        url,
        local_offset,
        local_end,
        timeout,
        hdrs,
        use_proxy_env=use_proxy_env,
        expected_total=size,
    )
    payload_start, payload_end = _validate_local_header(
        raw, target, method, uncomp_size, comp_size, crc32
    )
    absolute_payload_end = local_offset + payload_end
    if absolute_payload_end > size:
        raise RemoteZipFetchError("ZIP member payload lies outside the remote ZIP.")
    payload = raw[payload_start:payload_end]
    if len(payload) != comp_size:
        raise RemoteZipFetchError("ZIP member payload is truncated.")

    if method == 0:
        if len(payload) != uncomp_size:
            raise RemoteZipFetchError(
                "Stored metadata size does not match its declaration."
            )
        result = payload
    else:
        result = _decompress_deflate(payload, uncomp_size)

    if zlib.crc32(result) & 0xFFFFFFFF != crc32:
        raise RemoteZipFetchError("ZIP member CRC does not match its declaration.")
    return result


def fetch_zip_member(
    url: str,
    member: str,
    session: requests.Session | None = None,
    timeout: float = 15.0,
    headers: dict | None = None,
    use_proxy_env: bool = False,
) -> bytes:
    """Fetch and return one decompressed ZIP member over HTTP.

    Structural problems (bad ZIP records, invalid ranges, unsupported
    compression, or non-retryable HTTP statuses) raise
    :class:`RemoteZipFetchError`. Transient failures raise
    :class:`RemoteZipTransientError` so callers can retry the complete fetch.
    Internally-created sessions are closed here; caller-owned sessions remain
    open.
    """
    owned_session = session is None
    if owned_session:
        sess = requests.Session()
        if not use_proxy_env:
            sess.trust_env = False
    else:
        sess = session

    try:
        return _fetch_zip_member(
            sess,
            url,
            member,
            timeout,
            dict(headers or {}),
            use_proxy_env,
        )
    finally:
        if owned_session:
            sess.close()
