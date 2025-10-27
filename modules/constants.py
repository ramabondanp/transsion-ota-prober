import re

CHECKIN_URL = "https://android.googleapis.com/checkin"
USER_AGENT_TPL = "Dalvik/2.1.0 (Linux; U; Android {0}; {1} Build/{2})"
PROTO_TYPE = "application/x-protobuffer"
DEBUG_FILE = "debug_checkin_response.txt"
PROCESSED_FP_FILE = "processed_fingerprints.txt"
OTA_URL_PREFIX = b"https://android.googleapis.com/packages/ota"

TELEGRAPH_API_URL = "https://api.telegra.ph/createPage"

REGION_CODE_MAP = {
    "GL": "Global",
    "OP": "Global",
    "RU": "Russia",
    "IN": "India",
    "EU": "Europe",
    "TR": "Turkey",
}

SDK_TO_ANDROID = {
    33: "Android 13",
    34: "Android 14",
    35: "Android 15",
    36: "Android 16",
    37: "Android 17",
    38: "Android 18",
}

DESC_SECTION_RE = re.compile(r"(<b>Title:</b> .*?\n\n)(.*?)(\n\n<b>Size:</b>)", re.DOTALL)
SENTENCE_BOUNDARY_RE = re.compile(r"\.\s+")
BR_TAG_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
HTML_TAG_RE = re.compile(r"<[^>]*>")
URL_PAREN_RE = re.compile(r"\s*\(http[s]?://\S+\)?")
