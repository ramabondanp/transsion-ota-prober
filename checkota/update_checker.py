# The vendored checkin_generator_pb2 constructs its message classes dynamically
# at import time (protobuf builder -> globals()), so static analyzers cannot see
# attributes such as AndroidCheckinRequest. Suppress attribute-access errors for
# this file only; runtime resolution is guaranteed by ensure_vendor_on_path().
# pyright: reportAttributeAccessIssue=false
import datetime
import gzip
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests
from checkin import checkin_generator_pb2
from google.protobuf import text_format
from google.protobuf.message import DecodeError
from utils import functions

from checkota.constants import (
    CHECKIN_URL,
    DEBUG_FILE,
    OTA_URL_PREFIX,
    PROTO_TYPE,
    RETRYABLE_HTTP_STATUSES,
    USER_AGENT_TPL,
)
from checkota.logging import Log
from checkota.manager import Config

_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_STREAM_CHUNK_SIZE = 64 * 1024
_RETRYABLE_TRANSPORT_ERRORS = (
    requests.exceptions.Timeout,
    requests.exceptions.SSLError,
    requests.exceptions.ConnectionError,
    requests.exceptions.ChunkedEncodingError,
    requests.exceptions.ContentDecodingError,
)


class UpdateCheckError(Exception):
    """Raised when the check-in request failed (network/protocol/parse error).

    This deliberately excludes "no update found", which is a successful
    check-in with an empty result.
    """


class UpdateChecker:
    def __init__(
        self,
        cfg: Config,
        session: requests.Session | None = None,
        imei: str | None = None,
        stop_event: threading.Event | None = None,
        debug_label: str | None = None,
    ):
        self.cfg = cfg
        self.session = session or requests.Session()
        self.imei = imei
        self.stop_event = stop_event
        if debug_label:
            safe_label = "".join(
                c if c.isalnum() or c in "-_." else "_" for c in debug_label
            )
            self.debug_file = DEBUG_FILE.replace(".txt", f"_{safe_label}.txt")
            self.debug_error_file = DEBUG_FILE.replace(
                ".txt", f"_{safe_label}_error.bin"
            )
        else:
            self.debug_file = DEBUG_FILE
            self.debug_error_file = DEBUG_FILE.replace(".txt", "_error.bin")
        # Pin identity generators so retries don't re-randomise -- reproducibility
        # and Google-edge fairness. The four functions in vendor/utils/functions.py
        # use unseeded random.*, so calling them per retry would change the
        # identity on each attempt.
        self._imei = imei or functions.generateImei()
        self._digest = functions.generateDigest()
        self._serial = functions.generateSerial()
        self._mac = functions.generateMac()
        self.ua = USER_AGENT_TPL.format(cfg.android_version, cfg.model, cfg.build_tag)
        self.headers = {
            "accept-encoding": "gzip, deflate",
            "content-encoding": "gzip",
            "content-type": PROTO_TYPE,
            "user-agent": self.ua,
        }

    def _stopped(self) -> bool:
        return self.stop_event is not None and self.stop_event.is_set()

    @staticmethod
    def _is_allowed_ota_url(value: str) -> bool:
        """Accept only HTTPS OTA objects served by Google's OTA endpoint."""
        try:
            parsed = urlsplit(value)
            return (
                not any(
                    char.isspace() or ord(char) < 32 or ord(char) == 127
                    for char in value
                )
                and parsed.scheme == "https"
                and parsed.hostname == "android.googleapis.com"
                and parsed.port is None
                and not parsed.username
                and not parsed.password
                and parsed.path.startswith(("/packages/ota/", "/packages/ota-api/"))
            )
        except ValueError:
            return False

    @staticmethod
    def _safe_title(value: str) -> str | None:
        """Reject titles that can corrupt the line-oriented dedup file/logs."""
        title = value.strip()
        if any(ord(char) < 32 or ord(char) == 127 for char in title):
            return None
        return title

    def _wait_for_retry(self, delay: int) -> bool:
        """Wait between attempts, returning False if shutdown was requested."""
        if self.stop_event is not None:
            return not self.stop_event.wait(delay)
        time.sleep(delay)
        return True

    @staticmethod
    def _read_response_body(response: requests.Response) -> bytes:
        """Read a streamed response without allowing an unbounded allocation."""
        content_length = response.headers.get("Content-Length")
        if content_length is not None:
            try:
                if int(content_length) > _MAX_RESPONSE_BYTES:
                    raise UpdateCheckError(
                        f"Check-in response exceeds {_MAX_RESPONSE_BYTES} bytes"
                    )
            except ValueError:
                pass

        body = bytearray()
        for chunk in response.iter_content(chunk_size=_STREAM_CHUNK_SIZE):
            if not chunk:
                continue
            if len(body) + len(chunk) > _MAX_RESPONSE_BYTES:
                raise UpdateCheckError(
                    f"Check-in response exceeds {_MAX_RESPONSE_BYTES} bytes"
                )
            body.extend(chunk)
        return bytes(body)

    def _build_request(self) -> bytes:
        payload = checkin_generator_pb2.AndroidCheckinRequest()
        build = checkin_generator_pb2.AndroidBuildProto()
        checkin = checkin_generator_pb2.AndroidCheckinProto()

        build.id = self.cfg.fingerprint()
        build.timestamp = 0
        build.device = self.cfg.device

        checkin.build.CopyFrom(build)
        checkin.roaming = "WIFI::"
        checkin.userNumber = 0
        checkin.deviceType = 2
        checkin.voiceCapable = False

        payload.imei = self._imei
        payload.id = 0
        payload.digest = self._digest
        payload.checkin.CopyFrom(checkin)
        payload.locale = "en-US"
        payload.timeZone = "America/New_York"
        payload.version = 3
        payload.serialNumber = self._serial
        payload.macAddr.append(self._mac)
        payload.macAddrType.extend(["wifi"])
        payload.fragment = 0
        payload.userSerialNumber = 0
        payload.fetchSystemUpdates = 1

        return gzip.compress(payload.SerializeToString())

    def check(self, debug: bool = False) -> tuple[bool, dict | None]:
        Log.i("Checking for updates...")
        if self.imei:
            Log.i(f"Using custom IMEI: {self.imei}")
        retries = 3
        delay = 1
        data = self._build_request()
        response = None

        for attempt in range(retries):
            if self._stopped():
                Log.w("Update check interrupted.")
                return False, None
            response = None
            error = None
            error_content = None
            try:
                try:
                    response = self.session.post(
                        CHECKIN_URL,
                        data=data,
                        headers=self.headers,
                        timeout=(5.0, 10.0),
                        allow_redirects=False,
                        stream=True,
                    )
                    status = getattr(response, "status_code", None)
                    if isinstance(status, int) and 300 <= status < 400:
                        raise requests.exceptions.HTTPError(
                            f"Check-in redirect HTTP {status} rejected",
                            response=response,
                        )
                    response.raise_for_status()

                    resp = checkin_generator_pb2.AndroidCheckinResponse()
                    resp.ParseFromString(self._read_response_body(response))

                    if debug:
                        Path(self.debug_file).write_text(
                            text_format.MessageToString(resp), encoding="utf-8"
                        )
                        Log.i(f"Debug response saved to {self.debug_file}")

                    info = self._parse(resp)
                except Exception as exc:  # noqa: BLE001 -- classified below
                    error = exc
                    if (
                        debug
                        and response is not None
                        and isinstance(exc, requests.exceptions.HTTPError)
                    ):
                        try:
                            error_content = self._read_response_body(response)
                        except (
                            requests.exceptions.RequestException,
                            UpdateCheckError,
                        ):
                            error_content = None
                else:
                    has_update = info.get("found", False) and "url" in info
                    return has_update, info
            finally:
                if response is not None:
                    close = getattr(response, "close", None)
                    if callable(close):
                        close()

            if error is None:
                raise UpdateCheckError("Update check failed: request loop exhausted")

            if isinstance(
                error,
                (*_RETRYABLE_TRANSPORT_ERRORS, DecodeError),
            ):
                if self._stopped():
                    Log.w("Update check interrupted.")
                    return False, None
                if attempt < retries - 1:
                    Log.w(
                        f"Update check network error: {error}. Retrying in "
                        f"{delay} seconds... "
                        f"({attempt + 1}/{retries})"
                    )
                    if not self._wait_for_retry(delay):
                        Log.w("Update check interrupted during retry delay.")
                        return False, None
                    delay *= 2
                    continue
                Log.e(
                    "Update check failed after multiple retries due to network "
                    f"error: {error}"
                )
                raise UpdateCheckError(
                    "Update check failed after multiple retries due to network "
                    f"error: {error}"
                ) from error

            if isinstance(error, requests.exceptions.HTTPError):
                status = getattr(getattr(error, "response", None), "status_code", None)
                if not isinstance(status, int) and response is not None:
                    status = getattr(response, "status_code", None)

                if status in RETRYABLE_HTTP_STATUSES:
                    if self._stopped():
                        Log.w("Update check interrupted.")
                        return False, None
                    if attempt < retries - 1:
                        Log.w(
                            f"Update check HTTP {status}: {error}. Retrying in "
                            f"{delay} seconds... "
                            f"({attempt + 1}/{retries})"
                        )
                        if not self._wait_for_retry(delay):
                            Log.w("Update check interrupted during retry delay.")
                            return False, None
                        delay *= 2
                        continue
                    Log.e(
                        f"Update check failed after multiple retries due to HTTP "
                        f"{status}: {error}"
                    )
                else:
                    Log.e(f"Update check failed: {error}")

                if debug and isinstance(error_content, (bytes, bytearray)):
                    Path(self.debug_error_file).write_bytes(error_content)
                    Log.i("Raw error response saved")
                raise UpdateCheckError(f"Update check failed: {error}") from error

            if self._stopped() and isinstance(
                error, requests.exceptions.RequestException
            ):
                Log.w("Update check interrupted.")
                return False, None
            Log.e(f"Update check failed: {error}")
            if debug and isinstance(error_content, (bytes, bytearray)):
                Path(self.debug_error_file).write_bytes(error_content)
                Log.i("Raw error response saved")
            raise UpdateCheckError(f"Update check failed: {error}") from error
        raise UpdateCheckError("Update check failed: request loop exhausted")

    def _parse(self, resp: checkin_generator_pb2.AndroidCheckinResponse) -> dict:
        info = {
            "device": self.cfg.model,
            "found": False,
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "title": None,
            "description": None,
            "size": None,
            "url": None,
        }

        for entry in resp.setting:
            name_bytes = entry.name or b""
            value_bytes = entry.value or b""

            value = value_bytes.decode("utf-8", errors="ignore")

            if not info["found"] and (
                name_bytes == b"update_url" or OTA_URL_PREFIX in value_bytes
            ):
                url = value.strip()
                if url and self._is_allowed_ota_url(url):
                    info["url"] = url
                    info["found"] = True
                elif url:
                    Log.w(f"Ignoring update URL outside the trusted OTA origin: {url}")

            try:
                name = name_bytes.decode("utf-8")
            except UnicodeDecodeError as exc:
                Log.w(
                    f"Skipping setting with non-UTF-8 name "
                    f"({len(name_bytes)} bytes): {exc}"
                )
                continue

            if name == "update_title":
                title = self._safe_title(value)
                if title is None:
                    Log.w("Ignoring update title containing control characters.")
                else:
                    info["title"] = title
            elif name == "update_description":
                info["description"] = value.strip()
            elif name == "update_size":
                info["size"] = value

        return info
