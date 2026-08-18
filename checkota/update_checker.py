import datetime
import gzip
import threading
import time
from pathlib import Path

import requests
from checkin import checkin_generator_pb2
from google.protobuf import text_format
from utils import functions

from checkota.constants import (
    CHECKIN_URL,
    DEBUG_FILE,
    OTA_URL_PREFIX,
    PROTO_TYPE,
    USER_AGENT_TPL,
)
from checkota.logging import Log
from checkota.manager import Config


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
            try:
                response = self.session.post(
                    CHECKIN_URL,
                    data=data,
                    headers=self.headers,
                    timeout=(5.0, 10.0),
                )
                response.raise_for_status()

                resp = checkin_generator_pb2.AndroidCheckinResponse()
                resp.ParseFromString(response.content)

                if debug:
                    Path(self.debug_file).write_text(
                        text_format.MessageToString(resp), encoding="utf-8"
                    )
                    Log.i(f"Debug response saved to {self.debug_file}")

                info = self._parse(resp)
                has_update = info.get("found", False) and "url" in info
                return has_update, info

            except (
                requests.exceptions.Timeout,
                requests.exceptions.ConnectionError,
            ) as exc:
                if self._stopped():
                    Log.w("Update check interrupted.")
                    return False, None
                Log.w(
                    f"Update check network error: {exc}. Retrying in {delay} seconds... ({attempt + 1}/{retries})"
                )
                if attempt < retries - 1:
                    if self.stop_event is not None and self.stop_event.wait(delay):
                        Log.w("Update check interrupted during retry delay.")
                        return False, None
                    if self.stop_event is None:
                        time.sleep(delay)
                    delay *= 2
                else:
                    Log.e(
                        f"Update check failed after multiple retries due to network error: {exc}"
                    )
                    raise UpdateCheckError(
                        f"Update check failed after multiple retries due to network error: {exc}"
                    ) from exc
            except requests.exceptions.RequestException as exc:
                if self._stopped():
                    Log.w("Update check interrupted.")
                    return False, None
                Log.e(f"Update check failed: {exc}")
                if debug and response is not None:
                    Path(self.debug_error_file).write_bytes(response.content)
                    Log.i("Raw error response saved")
                raise UpdateCheckError(f"Update check failed: {exc}") from exc
            except Exception as exc:
                Log.e(f"Update check failed: {exc}")
                if debug and response is not None:
                    Path(self.debug_error_file).write_bytes(response.content)
                    Log.i("Raw error response saved")
                raise UpdateCheckError(f"Update check failed: {exc}") from exc
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
                if url:
                    info["url"] = url
                    info["found"] = True

            try:
                name = name_bytes.decode("utf-8")
            except Exception as exc:
                Log.w(
                    f"Skipping setting with non-UTF-8 name "
                    f"({len(name_bytes)} bytes): {exc}"
                )
                continue

            if name == "update_title":
                info["title"] = value.strip()
            elif name == "update_description":
                info["description"] = value.strip()
            elif name == "update_size":
                info["size"] = value

        return info
