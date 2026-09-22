# The vendored checkin_generator_pb2 constructs its message classes dynamically
# at import time (protobuf builder -> globals()), so static analyzers cannot see
# attributes such as AndroidCheckinRequest. Suppress attribute-access errors for
# this file only; runtime resolution is guaranteed by ensure_vendor_on_path().
# pyright: reportAttributeAccessIssue=false
import contextlib
import datetime
import gzip
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import requests
from checkin import checkin_generator_pb2
from google.protobuf import text_format
from google.protobuf.message import DecodeError
from utils import functions

from checkota.constants import (
    CHECKIN_API_HOST,
    CHECKIN_URL,
    DEBUG_FILE,
    MAX_OTA_URL_LENGTH,
    MAX_UPDATE_DESCRIPTION_LENGTH,
    MAX_UPDATE_SIZE_LENGTH,
    MAX_UPDATE_TITLE_LENGTH,
    OTA_URL_PATH_PREFIXES,
    OTA_URL_PREFIX,
    PROTO_TYPE,
    RETRY_BACKOFF_MULTIPLIER,
    RETRY_BASE_DELAY_SECONDS,
    RETRYABLE_HTTP_STATUSES,
    USER_AGENT_TPL,
)
from checkota.logging import Log
from checkota.manager import Config
from checkota.validation import has_control_chars, is_google_https_url

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


def _write_debug_file(path: str, data: str | bytes) -> None:
    """Write a debug artifact without following symlinks at the target.

    Debug paths are CWD-relative and predictable, so on a shared host a local
    attacker could pre-plant a symlink there to clobber an arbitrary file with
    server-controlled content. Writing through a same-directory temp file and
    atomically replacing the entry closes that hole: rename() swaps the
    directory entry itself instead of writing through the link.
    """
    target = Path(path)
    payload = data.encode("utf-8") if isinstance(data, str) else bytes(data)
    fd, tmp_name = tempfile.mkstemp(
        dir=target.parent, prefix=f"{target.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "wb") as tmp:
            tmp.write(payload)
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


@dataclass
class _AttemptOutcome:
    """Classified result of one check-in attempt.

    Exactly one of ``result`` / ``interrupted`` / ``retryable`` / plain
    ``error`` drives the caller's next move.
    """

    result: tuple[bool, dict] | None = None  # success branch
    retryable: bool = False  # transient failure; another attempt may help
    interrupted: bool = False  # stop requested; give up quietly
    error: Exception | None = None  # always set on failure branches
    http_status: int | None = None  # set when an HTTP status drove the verdict
    error_content: bytes | None = None  # captured body for --debug dumps


#: ANSI CSI sequences and the remaining C0/C1 controls may not appear in a
#: rendered description. Tab, newline and carriage return are legitimate
#: changelog formatting; escape sequences are removed whole (not left as
#: printable "[31m" debris) so a control-only body degrades to no description.
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_UNPRINTABLE_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")

_CHECK_RETRIES = 3
_CHECK_TIMEOUT = (5.0, 10.0)


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
        if len(value) > MAX_OTA_URL_LENGTH:
            return False
        return is_google_https_url(
            value,
            allowed_hosts=(CHECKIN_API_HOST,),
            path_prefixes=OTA_URL_PATH_PREFIXES,
        )

    @staticmethod
    def _safe_title(value: str) -> str | None:
        """Reject titles that can corrupt the line-oriented dedup file/logs."""
        title = value.strip()
        if not title or len(title) > MAX_UPDATE_TITLE_LENGTH:
            return None
        if has_control_chars(title):
            return None
        return title

    @staticmethod
    def _safe_description(value: str) -> str | None:
        """Keep a bounded, printable changelog body for display.

        The description is the one untrusted field with no other consumer-side
        bound: it feeds the terminal renderer, the notification text and the
        Telegraph fallback. Newlines and tabs are legitimate; every other C0/C1
        control is dropped so a description cannot smuggle escape sequences into
        a consumer that renders it without its own sanitizer.
        """
        description = value.strip()
        if not description:
            return None
        if len(description) > MAX_UPDATE_DESCRIPTION_LENGTH:
            Log.w("Ignoring oversized update description.")
            return None
        cleaned = _ANSI_ESCAPE_RE.sub("", description)
        cleaned = _UNPRINTABLE_CONTROL_RE.sub("", cleaned)
        return cleaned if cleaned.strip() else None

    @staticmethod
    def _safe_size(value: str) -> str | None:
        """Keep only short, control-free update-size strings for display."""
        size = value.strip()
        if not size or len(size) > MAX_UPDATE_SIZE_LENGTH:
            return None
        if has_control_chars(size):
            return None
        return size

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
        delay = RETRY_BASE_DELAY_SECONDS
        data = self._build_request()

        for attempt in range(_CHECK_RETRIES):
            if self._stopped():
                Log.w("Update check interrupted.")
                return False, None
            outcome = self._attempt_once(data, debug)

            if outcome.result is not None:
                return outcome.result
            if outcome.error is None:
                # Unreachable: every failure branch carries an error.
                raise UpdateCheckError("Update check failed without an error")

            if outcome.interrupted or (outcome.retryable and self._stopped()):
                Log.w("Update check interrupted.")
                return False, None

            if outcome.retryable and attempt < _CHECK_RETRIES - 1:
                if outcome.http_status is None:
                    Log.w(
                        f"Update check network error: {outcome.error}. Retrying in "
                        f"{delay} seconds... "
                        f"({attempt + 1}/{_CHECK_RETRIES})"
                    )
                else:
                    Log.w(
                        f"Update check HTTP {outcome.http_status}: {outcome.error}. "
                        f"Retrying in {delay} seconds... "
                        f"({attempt + 1}/{_CHECK_RETRIES})"
                    )
                if not self._wait_for_retry(delay):
                    Log.w("Update check interrupted during retry delay.")
                    return False, None
                delay *= RETRY_BACKOFF_MULTIPLIER
                continue

            # Giving up: the final attempt failed, or the failure is permanent.
            if debug and isinstance(outcome.error_content, (bytes, bytearray)):
                _write_debug_file(self.debug_error_file, outcome.error_content)
                Log.i("Raw error response saved")
            if outcome.retryable and outcome.http_status is None:
                Log.e(
                    "Update check failed after multiple retries due to network "
                    f"error: {outcome.error}"
                )
                raise UpdateCheckError(
                    "Update check failed after multiple retries due to network "
                    f"error: {outcome.error}"
                ) from outcome.error
            if outcome.retryable:
                Log.e(
                    f"Update check failed after multiple retries due to HTTP "
                    f"{outcome.http_status}: {outcome.error}"
                )
            else:
                Log.e(f"Update check failed: {outcome.error}")
            raise UpdateCheckError(
                f"Update check failed: {outcome.error}"
            ) from outcome.error
        raise UpdateCheckError("Update check failed: request loop exhausted")

    def _attempt_once(self, data: bytes, debug: bool) -> _AttemptOutcome:
        """Perform a single POST + parse and classify the outcome."""
        response = None
        try:
            try:
                response = self.session.post(
                    CHECKIN_URL,
                    data=data,
                    headers=self.headers,
                    timeout=_CHECK_TIMEOUT,
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
                    _write_debug_file(
                        self.debug_file, text_format.MessageToString(resp)
                    )
                    Log.i(f"Debug response saved to {self.debug_file}")

                info = self._parse(resp)
                has_update = info.get("found", False) and "url" in info
                return _AttemptOutcome(result=(has_update, info))
            except Exception as exc:  # noqa: BLE001 -- classified below
                error_content = self._debug_error_content(response, exc, debug)
                return self._classify_failure(exc, response, error_content)
        finally:
            if response is not None:
                close = getattr(response, "close", None)
                if callable(close):
                    close()

    def _debug_error_content(
        self, response: requests.Response | None, exc: Exception, debug: bool
    ) -> bytes | None:
        """Capture a failed response's body for --debug dumps when possible."""
        if not debug or response is None:
            return None
        if not isinstance(exc, requests.exceptions.HTTPError):
            return None
        try:
            return self._read_response_body(response)
        except (requests.exceptions.RequestException, UpdateCheckError):
            return None

    def _classify_failure(
        self,
        exc: Exception,
        response: requests.Response | None,
        error_content: bytes | None,
    ) -> _AttemptOutcome:
        """Sort a failed attempt into retryable / interrupted / fatal."""
        if isinstance(exc, (*_RETRYABLE_TRANSPORT_ERRORS, DecodeError)):
            return _AttemptOutcome(retryable=True, error=exc)

        if isinstance(exc, requests.exceptions.HTTPError):
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if not isinstance(status, int) and response is not None:
                status = getattr(response, "status_code", None)
            if isinstance(status, int) and status in RETRYABLE_HTTP_STATUSES:
                return _AttemptOutcome(retryable=True, error=exc, http_status=status)
            return _AttemptOutcome(error=exc, error_content=error_content)

        # A transport error noticed after stop was requested is teardown, not
        # a failure worth reporting.
        if self._stopped() and isinstance(exc, requests.exceptions.RequestException):
            return _AttemptOutcome(interrupted=True, error=exc)
        return _AttemptOutcome(error=exc, error_content=error_content)

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

        # A check-in response is one update, not a list of update records.
        # Mixing the first URL with the last title/size would label the wrong
        # ZIP and potentially record the wrong title. Identical repeated values
        # are harmless, but conflicting values are ambiguous and fail closed.
        for entry in resp.setting:
            name_bytes = entry.name or b""
            value_bytes = entry.value or b""

            value = value_bytes.decode("utf-8", errors="ignore")

            if name_bytes == b"update_url" or OTA_URL_PREFIX in value_bytes:
                url = value.strip()
                if url and self._is_allowed_ota_url(url):
                    if info["url"] is not None and info["url"] != url:
                        raise UpdateCheckError(
                            "Conflicting update URLs in check-in response"
                        )
                    info["url"] = url
                    info["found"] = True
                elif url:
                    # repr() -- the URL was rejected partly because it may carry
                    # control characters; interpolating it raw would let a
                    # hostile response forge log lines.
                    Log.w(
                        f"Ignoring update URL outside the trusted OTA origin: {url!r}"
                    )

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
                    if info["title"] is not None and info["title"] != title:
                        raise UpdateCheckError(
                            "Conflicting update titles in check-in response"
                        )
                    info["title"] = title
            elif name == "update_description":
                description = self._safe_description(value)
                if (
                    info["description"] is not None
                    and description is not None
                    and info["description"] != description
                ):
                    raise UpdateCheckError(
                        "Conflicting update descriptions in check-in response"
                    )
                if description is not None:
                    info["description"] = description
            elif name == "update_size":
                size = self._safe_size(value)
                if size is None:
                    Log.w("Ignoring malformed or oversized update size.")
                else:
                    if info["size"] is not None and info["size"] != size:
                        raise UpdateCheckError(
                            "Conflicting update sizes in check-in response"
                        )
                    info["size"] = size

        return info
