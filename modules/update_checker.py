import datetime
import gzip
from pathlib import Path
from typing import Dict, Optional, Tuple

import requests
from google.protobuf import text_format

from checkin import checkin_generator_pb2
from utils import functions

from modules.manager import Config
from modules.constants import (
    BR_TAG_RE,
    CHECKIN_URL,
    DEBUG_FILE,
    HTML_TAG_RE,
    OTA_URL_PREFIX,
    PROTO_TYPE,
    URL_PAREN_RE,
    USER_AGENT_TPL,
)
from modules.logging import Log


class UpdateChecker:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.ua = USER_AGENT_TPL.format(cfg.android_version, cfg.model, cfg.build_tag)
        self.headers = {
            "accept-encoding": "gzip, deflate",
            "content-encoding": "gzip",
            "content-type": PROTO_TYPE,
            "user-agent": self.ua,
        }

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

        payload.imei = functions.generateImei()
        payload.id = 0
        payload.digest = functions.generateDigest()
        payload.checkin.CopyFrom(checkin)
        payload.locale = "en-US"
        payload.timeZone = "America/New_York"
        payload.version = 3
        payload.serialNumber = functions.generateSerial()
        payload.macAddr.append(functions.generateMac())
        payload.macAddrType.extend(["wifi"])
        payload.fragment = 0
        payload.userSerialNumber = 0
        payload.fetchSystemUpdates = 1

        return gzip.compress(payload.SerializeToString())

    def check(self, debug: bool = False) -> Tuple[bool, Optional[Dict]]:
        Log.i("Checking for updates...")

        try:
            data = self._build_request()
            response = requests.post(CHECKIN_URL, data=data, headers=self.headers, timeout=10)
            response.raise_for_status()

            resp = checkin_generator_pb2.AndroidCheckinResponse()
            resp.ParseFromString(response.content)

            if debug:
                Path(DEBUG_FILE).write_text(text_format.MessageToString(resp))
                Log.i(f"Debug response saved to {DEBUG_FILE}")

            info = self._parse(resp)
            has_update = info.get("found", False) and "url" in info
            return has_update, info

        except Exception as exc:
            Log.e(f"Update check failed: {exc}")
            if debug and "response" in locals():
                Path(DEBUG_FILE.replace(".txt", "_error.bin")).write_bytes(response.content)
                Log.i("Raw error response saved")
            return False, None

    def _parse(self, resp: checkin_generator_pb2.AndroidCheckinResponse) -> Dict:
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

            if not info["found"] and (name_bytes == b"update_url" or OTA_URL_PREFIX in value_bytes):
                url = value.strip()
                if url:
                    info["url"] = url
                    info["found"] = True

            try:
                name = name_bytes.decode("utf-8")
            except Exception:
                continue

            if name == "update_title":
                info["title"] = value.strip()
            elif name == "update_description":
                info["description"] = value.strip()
            elif name == "update_size":
                info["size"] = value

        return info

    @staticmethod
    def _clean_desc(text: str) -> str:
        text = text.replace("\n", "")
        text = BR_TAG_RE.sub("\n", text)
        text = HTML_TAG_RE.sub("", text)
        text = URL_PAREN_RE.sub("", text)
        return text.strip()
