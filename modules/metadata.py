import datetime
from zipfile import BadZipFile
from pathlib import Path
from typing import Dict, Optional, Tuple

import requests
from remotezip import RemoteIOError, RemoteZip, RemoteZipError

from modules.constants import PROCESSED_UPDATES_FILE, SDK_TO_ANDROID
from modules.logging import Log


METADATA_PATH = "META-INF/com/android/metadata"
METADATA_KEYS = {
    "post-build",
    "post-build-incremental",
    "post-security-patch-level",
    "post-timestamp",
    "post-sdk-level",
}


def get_ota_metadata(url: str, session: Optional[requests.Session] = None) -> Optional[Dict[str, str]]:
    Log.i("Fetching OTA metadata (fingerprint, patch level, sdk)...")
    try:
        with RemoteZip(
            url,
            timeout=60,
            session=session,
            support_suffix_range=False,
            headers={"User-Agent": "transsion-ota-prober/1.0"},
        ) as ota_zip:
            with ota_zip.open(METADATA_PATH) as metadata_file:
                content = metadata_file.read().decode("utf-8", errors="replace")

        if not content.strip():
            Log.w("Could not extract OTA metadata (empty content).")
            return None

        meta: Dict[str, str] = {}
        for line in content.splitlines():
            if "=" in line:
                key, value = line.strip().split("=", 1)
                key = key.strip()
                if key in METADATA_KEYS:
                    meta[key] = value.strip()

        result: Dict[str, str] = {}
        fingerprint = meta.get("post-build", "")
        if not fingerprint:
            Log.w("post-build not found in metadata.")
        else:
            Log.i(f"Extracted fingerprint: {fingerprint}")
        result["fingerprint"] = fingerprint

        if meta.get("post-build-incremental"):
            result["post_build_incremental"] = meta["post-build-incremental"]
        if meta.get("post-security-patch-level"):
            result["post_security_patch_level"] = meta["post-security-patch-level"]
        if meta.get("post-timestamp"):
            result["post_timestamp"] = meta["post-timestamp"]
            try:
                timestamp = int(meta["post-timestamp"])
                dt_utc = datetime.datetime.fromtimestamp(timestamp, datetime.timezone.utc)
                tz_cst = datetime.timezone(datetime.timedelta(hours=8))
                dt_cst = dt_utc.astimezone(tz_cst)
                result["build_date"] = dt_cst.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pass

        if meta.get("post-sdk-level"):
            sdk_level = meta["post-sdk-level"]
            result["post_sdk_level"] = sdk_level
            try:
                sdk_int = int(sdk_level)
                if sdk_int >= 33:
                    result["android_version"] = SDK_TO_ANDROID.get(sdk_int)
            except Exception:
                pass

        return result

    except KeyError:
        Log.w(f"{METADATA_PATH} not found in OTA package.")
        return None
    except (RemoteIOError, RemoteZipError, BadZipFile) as exc:
        Log.e(f"Error extracting OTA metadata: {exc}")
        return None
    except Exception as exc:
        Log.e(f"Error extracting OTA metadata: {exc}")
        return None


def extract_incremental_from_fingerprint(fingerprint: str) -> Optional[str]:
    if not fingerprint:
        return None
    try:
        fingerprint_suffix = fingerprint.split(":", 1)[1]
    except IndexError:
        return None

    parts = fingerprint_suffix.split("/")
    if len(parts) < 3:
        return None

    incremental_segment = parts[2]
    return incremental_segment.split(":", 1)[0] if incremental_segment else None


def build_sdk_strings(sdk_level: Optional[str], android_version: Optional[str]) -> Tuple[str, str, str]:
    if sdk_level is None:
        return "", "", ""

    try:
        sdk_int = int(str(sdk_level))
    except (TypeError, ValueError):
        return "", "", ""

    if sdk_int < 33:
        return "", "", ""

    version_label = android_version or SDK_TO_ANDROID.get(sdk_int)
    if version_label:
        message = version_label
        log_line = f"Android: {version_label} (SDK {sdk_level})"
        release_line = f"**Android:** {version_label} (SDK {sdk_level})"
        return message, log_line, release_line

    message = f"SDK: {sdk_level}"
    log_line = f"SDK level: {sdk_level}"
    release_line = f"**SDK:** {sdk_level}"
    return message, log_line, release_line


def processed_updates_path() -> Path:
    return Path(PROCESSED_UPDATES_FILE)
