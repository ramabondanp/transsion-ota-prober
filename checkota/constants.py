import re

CHECKIN_URL = "https://android.googleapis.com/checkin"
USER_AGENT_TPL = "Dalvik/2.1.0 (Linux; U; Android {0}; {1} Build/{2})"
PROTO_TYPE = "application/x-protobuffer"
DEBUG_FILE = "debug_checkin_response.txt"
PROCESSED_UPDATES_FILE = "processed_updates.txt"
OTA_URL_PREFIX = b"https://android.googleapis.com/packages/ota"

TELEGRAPH_API_URL = "https://api.telegra.ph/createPage"

# HTTP statuses that are transient for Google check-in and OTA fetches.
# Keep this set aligned with zip_metadata.py until that module can import the
# shared constant without widening this change's scope.
RETRYABLE_HTTP_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})

REGION_CODE_MAP = {
    "GL": "Global - GL Market",
    "OP": "Global - OP Market",
    "OP-M1": "Global - OP-M1 Market",
    "RU": "Russia - RU Market",
    "IN": "India - IN Market",
    "EU": "Europe - EU Market",
    "TR": "Turkey - TR Market",
    "OPPJ": "Global - OPPJ Market",
    "COCL": "Columbia - COCL Market",
}

SDK_TO_ANDROID = {
    33: "Android 13",
    34: "Android 14",
    35: "Android 15",
    36: "Android 16",
    37: "Android 17",
    38: "Android 18",
}

# Regex to detect section headers in OTA description HTML.
# Matches lines like "Android Version<br>" that are NOT inside <small>/<font> tags.
# The replacement wraps the header text in <b> tags before HTML tag stripping.
# Zero-width boundary (start / after \n / after <br>) so the preceding char is
# NOT consumed -- this lets back-to-back headers ("A<br>B<br>") both match, since
# the second is preceded only by a <br>, not a newline.
SECTION_HEADER_RE = re.compile(
    r"(?:^|(?<=\n)|(?<=<br>))([A-Z][A-Za-z0-9 \t&:/(),.\-]{1,80})<br>"
)

DESC_SECTION_RE = re.compile(
    r"(<b>Title:</b> .*?\n(?:<b>OS:</b> .*?\n)?\n?)(.*?)(\n\n?<b>Size:</b>)", re.DOTALL
)
SENTENCE_BOUNDARY_RE = re.compile(r"\.\s+")
