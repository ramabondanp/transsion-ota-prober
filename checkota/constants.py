import re

CHECKIN_URL = "https://android.googleapis.com/checkin"
USER_AGENT_TPL = "Dalvik/2.1.0 (Linux; U; Android {0}; {1} Build/{2})"
PROTO_TYPE = "application/x-protobuffer"
DEBUG_FILE = "debug_checkin_response.txt"
PROCESSED_UPDATES_FILE = "processed_updates.txt"
OTA_URL_PREFIX = b"https://android.googleapis.com/packages/ota"

# Host/path allowlists for server-controlled URLs. The check-in response may
# only point at Google's OTA API endpoint; ZIP range-fetch redirects may hop
# within Google's delivery network.
CHECKIN_API_HOST = "android.googleapis.com"
ZIP_REDIRECT_ALLOWED_HOSTS = ("android.googleapis.com", ".gvt1.com")
OTA_URL_PATH_PREFIXES = ("/packages/ota/", "/packages/ota-api/")
ZIP_REDIRECT_PATH_PREFIXES = ("/packages/",)

TELEGRAPH_API_URL = "https://api.telegra.ph/createPage"

# HTTP statuses that are transient for Google check-in and OTA fetches.
# Shared by update_checker.py and zip_metadata.py.
RETRYABLE_HTTP_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})

# Shared exponential backoff policy for transient failures (check-in request,
# OTA metadata fetch, ZIP range reads).
RETRY_BASE_DELAY_SECONDS = 1
RETRY_BACKOFF_MULTIPLIER = 2

# Per-request HTTP timeouts (seconds).
ZIP_MEMBER_READ_TIMEOUT_SECONDS = 15.0
TELEGRAM_API_TIMEOUT_SECONDS = 15
TELEGRAPH_API_TIMEOUT_SECONDS = 10

# CLI progress heartbeat interval while waiting on parallel region workers.
HEARTBEAT_INTERVAL_SECONDS = 5

# Wall-clock budget granted exclusively to the end-of-run notification drain,
# armed after the main run watchdog is cancelled. Keeps a nearly-expired run
# budget from killing the process mid-drain (which would discard exactly the
# notifications being flushed).
DRAIN_WATCHDOG_SECONDS = 300.0

# Upper bound on sends performed by the watchdog thread's emergency drain when
# the run budget expires mid-sweep.
EMERGENCY_DRAIN_MAX_SENDS = 30

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

# Canonical Android build tags used when a compact config omits a tag.
BUILD_TAG_BY_ANDROID = {
    "13": "TP1A.220624.014",
    "14": "UP1A.231005.007",
    "15": "AP3A.240905.015.A2",
    "16": "BP2A.250605.031.A3",
}

# Some OEMs use a device prefix whose casing differs from the OEM field.
DEVICE_PREFIX_BY_OEM = {
    "Itel": "itel",
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
