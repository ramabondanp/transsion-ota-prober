import datetime
import subprocess
from pathlib import Path
from typing import Dict, Optional, Tuple

from modules.constants import PROCESSED_FP_FILE, SDK_TO_ANDROID
from modules.logging import Log
from modules.system import check_cmds


def get_ota_metadata(url: str) -> Optional[Dict[str, str]]:
    Log.i("Fetching OTA metadata (fingerprint, patch level, sdk)...")
    cmds = ["curl", "bsdtar", "grep"]
    if not check_cmds(cmds):
        return None

    curl_cmd = ["curl", "--fail", "-Ls", "--max-time", "60", "--limit-rate", "100K", url]
    bsdtar_cmd = ["bsdtar", "-Oxf", "-", "META-INF/com/android/metadata"]
    grep_cmd = [
        "grep",
        "-E",
        "^(post-build=|post-build-incremental=|post-security-patch-level=|post-timestamp=|post-sdk-level=)",
        "-m",
        "5",
    ]

    try:
        curl_proc = subprocess.Popen(curl_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        bsdtar_proc = subprocess.Popen(
            bsdtar_cmd, stdin=curl_proc.stdout, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
        if curl_proc.stdout:
            curl_proc.stdout.close()
        grep_proc = subprocess.Popen(grep_cmd, stdin=bsdtar_proc.stdout, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        if bsdtar_proc.stdout:
            bsdtar_proc.stdout.close()

        try:
            stdout_bytes, _ = grep_proc.communicate(timeout=90)
            content = stdout_bytes.decode("utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            Log.w("Timeout expired while fetching OTA metadata.")
            return None

        if not content.strip():
            Log.w("Could not extract OTA metadata (empty content).")
            return None

        meta: Dict[str, str] = {}
        for line in content.splitlines():
            if "=" in line:
                key, value = line.strip().split("=", 1)
                meta[key.strip()] = value.strip()

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

    except Exception as exc:
        Log.e(f"Error extracting OTA metadata: {exc}")
        return None
    finally:
        if "curl_proc" in locals() and curl_proc and curl_proc.poll() is None:
            try:
                curl_proc.kill()
            except Exception:
                pass


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


def processed_fp_path() -> Path:
    return Path(PROCESSED_FP_FILE)
