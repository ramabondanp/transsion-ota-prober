"""Regression tests for bounded and structurally validated ZIP reads."""

import io
import struct
import zipfile
from unittest.mock import MagicMock

import pytest

from checkota import zip_metadata
from checkota.zip_metadata import (
    _LOCAL_SIG,
    MAX_COMPRESSED_METADATA_BYTES,
    MAX_DECOMPRESSED_METADATA_BYTES,
    RemoteZipFetchError,
    _decompress_deflate,
    _find_entry,
    _range_get,
    _validate_local_header,
    fetch_zip_member,
)


class _Response:
    def __init__(
        self, body, start, end, total, *, status=206, chunks=None, headers=None
    ):
        self.status_code = status
        self.headers = {
            "Content-Range": f"bytes {start}-{end}/{total}",
            "Content-Length": str(end - start + 1),
        }
        if headers:
            self.headers.update(headers)
        self._chunks = [body] if chunks is None else chunks
        self.closed = False

    def iter_content(self, chunk_size):
        return iter(self._chunks)

    def close(self):
        self.closed = True


class _RangeSession:
    def __init__(self, data):
        self.data = data
        self.trust_env = True
        self.close_count = 0
        self.requests = []

    def get(self, url, headers=None, **kwargs):
        range_header = (headers or {}).get("Range", "")
        start, end = (
            int(value) for value in range_header.removeprefix("bytes=").split("-")
        )
        self.requests.append((start, end, kwargs))
        body = self.data[start : end + 1]
        return _Response(body, start, end, len(self.data))

    def close(self):
        self.close_count += 1


def _build_zip(member, content, compression=zipfile.ZIP_STORED, extra=b""):
    buffer = io.BytesIO()
    info = zipfile.ZipInfo(member)
    info.compress_type = compression
    info.extra = extra
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(info, content)
    return buffer.getvalue()


def _upgrade_to_zip64(archive):
    eocd_offset = archive.rfind(b"PK\x05\x06")
    eocd = bytearray(archive[eocd_offset:])
    entries = struct.unpack_from("<H", eocd, 10)[0]
    cd_size, cd_offset = struct.unpack_from("<II", eocd, 12)
    struct.pack_into("<HHII", eocd, 8, 0xFFFF, 0xFFFF, 0xFFFFFFFF, 0xFFFFFFFF)

    eocd64 = struct.pack(
        "<4sQHHIIQQQQ",
        b"PK\x06\x06",
        44,
        45,
        45,
        0,
        0,
        entries,
        entries,
        cd_size,
        cd_offset,
    )
    locator = struct.pack("<4sIQI", b"PK\x06\x07", 0, eocd_offset, 1)
    return archive[:eocd_offset] + eocd64 + locator + bytes(eocd)


def _central_record(
    name, *, method=0, compressed=1, uncompressed=1, offset=0, extra=b""
):
    record = bytearray(46)
    record[:4] = b"PK\x01\x02"
    struct.pack_into("<H", record, 10, method)
    struct.pack_into("<I", record, 20, compressed)
    struct.pack_into("<I", record, 24, uncompressed)
    struct.pack_into("<HHH", record, 28, len(name), len(extra), 0)
    struct.pack_into("<I", record, 42, offset)
    return bytes(record) + name + extra


@pytest.mark.parametrize(
    "headers",
    [
        {"Content-Range": "bytes 0-3/4", "Content-Length": "3"},
        {"Content-Range": "bytes 1-4/4", "Content-Length": "4"},
        {"Content-Range": "bytes 0-3/*", "Content-Length": "4"},
    ],
)
def test_range_get_rejects_inexact_range_headers(headers):
    response = MagicMock()
    response.status_code = 206
    response.headers = headers
    response.iter_content.return_value = [b"abcd"]
    session = MagicMock()
    session.get.return_value = response

    with pytest.raises(RemoteZipFetchError):
        _range_get(session, "https://example.test/archive.zip", 0, 3, 5.0, {})


def test_range_get_rejects_truncated_and_oversize_streams():
    for chunks in ([b"abc"], [b"abcd", b"e"]):
        response = _Response(b"abcd", 0, 3, 4, chunks=chunks)
        session = MagicMock()
        session.get.return_value = response
        with pytest.raises(RemoteZipFetchError):
            _range_get(session, "https://example.test/archive.zip", 0, 3, 5.0, {})


def test_range_get_streams_with_status_206_and_exact_length():
    response = _Response(b"abcd", 0, 3, 4)
    session = MagicMock()
    session.get.return_value = response

    assert (
        _range_get(session, "https://example.test/archive.zip", 0, 3, 5.0, {})
        == b"abcd"
    )
    assert session.get.call_args.kwargs["stream"] is True
    assert response.closed is True


def test_range_get_follows_google_ota_cdn_redirect_chain():
    first = _Response(
        b"",
        0,
        3,
        4,
        status=302,
        headers={
            "Location": (
                "https://redirector.gvt1.com/packages/data/ota-api/package/x.zip"
            )
        },
    )
    second = _Response(
        b"",
        0,
        3,
        4,
        status=302,
        headers={
            "Location": (
                "https://r2---sn-example.gvt1.com/packages/data/ota-api/"
                "package/x.zip?token=1"
            )
        },
    )
    final = _Response(b"abcd", 0, 3, 4)
    session = MagicMock()
    session.get.side_effect = [first, second, final]

    assert (
        _range_get(
            session,
            "https://android.googleapis.com/packages/ota-api/package/x.zip",
            0,
            3,
            5.0,
            {},
        )
        == b"abcd"
    )
    assert [call.args[0] for call in session.get.call_args_list] == [
        "https://android.googleapis.com/packages/ota-api/package/x.zip",
        "https://redirector.gvt1.com/packages/data/ota-api/package/x.zip",
        (
            "https://r2---sn-example.gvt1.com/packages/data/ota-api/"
            "package/x.zip?token=1"
        ),
    ]
    assert first.closed is True
    assert second.closed is True
    assert final.closed is True


def test_range_get_rejects_redirect_outside_google_ota_cdn():
    response = _Response(
        b"",
        0,
        3,
        4,
        status=302,
        headers={"Location": "https://example.com/packages/ota/x.zip"},
    )
    session = MagicMock()
    session.get.return_value = response

    with pytest.raises(RemoteZipFetchError, match="outside Google's delivery network"):
        _range_get(
            session,
            "https://android.googleapis.com/packages/ota-api/package/x.zip",
            0,
            3,
            5.0,
            {},
        )

    assert session.get.call_count == 1
    assert response.closed is True


def test_find_entry_rejects_member_size_caps_before_reading_payload():
    name = b"META-INF/com/android/metadata"
    with pytest.raises(RemoteZipFetchError):
        _find_entry(
            _central_record(
                name,
                compressed=MAX_COMPRESSED_METADATA_BYTES + 1,
            ),
            name,
        )
    with pytest.raises(RemoteZipFetchError):
        _find_entry(
            _central_record(
                name,
                uncompressed=MAX_DECOMPRESSED_METADATA_BYTES + 1,
            ),
            name,
        )


def test_deflate_output_is_bounded_and_must_match_declaration():
    compressor = zip_metadata.zlib.compressobj(wbits=-zip_metadata.zlib.MAX_WBITS)
    payload = compressor.compress(b"x" * (MAX_DECOMPRESSED_METADATA_BYTES + 1))
    payload += compressor.flush()

    with pytest.raises(RemoteZipFetchError):
        _decompress_deflate(payload, MAX_DECOMPRESSED_METADATA_BYTES)


def test_local_header_name_and_sizes_are_validated():
    name = b"META-INF/com/android/metadata"
    raw = (
        struct.pack(
            "<4s5H3I2H",
            _LOCAL_SIG,
            20,
            0,
            0,
            0,
            0,
            0,
            1,
            1,
            len(name),
            0,
        )
        + name
        + b"x"
    )

    with pytest.raises(RemoteZipFetchError):
        _validate_local_header(raw, b"other", 0, 1, 1, 0)

    bad_size = bytearray(raw)
    struct.pack_into("<I", bad_size, 18, 2)
    with pytest.raises(RemoteZipFetchError):
        _validate_local_header(bytes(bad_size), name, 0, 1, 1, 0)


def test_zip64_uncompressed_size_and_session_ownership(monkeypatch):
    member = "META-INF/com/android/metadata"
    content = (
        b"post-build=vendor/product/device:14/build/incremental:user/release-keys\n"
    )
    zip64_extra = struct.pack("<HHQ", 0x0001, 8, len(content))
    archive = bytearray(_build_zip(member, content, extra=zip64_extra))
    central_offset = archive.find(b"PK\x01\x02")
    struct.pack_into("<I", archive, central_offset + 24, 0xFFFFFFFF)
    archive = bytes(archive)

    caller_session = _RangeSession(archive)
    assert (
        fetch_zip_member(
            "https://example.test/archive.zip", member, session=caller_session
        )
        == content
    )
    assert caller_session.close_count == 0

    owned_session = _RangeSession(archive)
    monkeypatch.setattr(zip_metadata.requests, "Session", lambda: owned_session)
    assert fetch_zip_member("https://example.test/archive.zip", member) == content
    assert owned_session.close_count == 1


def test_fetch_member_from_archive_with_zip64_eocd_and_locator():
    member = "META-INF/com/android/metadata"
    content = (
        b"post-build=vendor/product/device:14/build/incremental:user/release-keys\n"
    )
    archive = _upgrade_to_zip64(_build_zip(member, content))

    assert (
        fetch_zip_member(
            "https://example.test/archive.zip",
            member,
            session=_RangeSession(archive),
        )
        == content
    )


@pytest.mark.parametrize("compression", [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED])
def test_fetch_member_rejects_corrupt_crc_for_stored_and_deflated(compression):
    member = "META-INF/com/android/metadata"
    content = (
        b"post-build=vendor/product/device:14/build/incremental:user/release-keys\n"
    )
    archive = bytearray(_build_zip(member, content, compression))
    wrong_crc = (zip_metadata.zlib.crc32(content) + 1) & 0xFFFFFFFF
    struct.pack_into("<I", archive, archive.find(_LOCAL_SIG) + 14, wrong_crc)
    struct.pack_into("<I", archive, archive.find(b"PK\x01\x02") + 16, wrong_crc)

    with pytest.raises(RemoteZipFetchError, match="CRC"):
        fetch_zip_member(
            "https://example.test/archive.zip",
            member,
            session=_RangeSession(bytes(archive)),
        )
