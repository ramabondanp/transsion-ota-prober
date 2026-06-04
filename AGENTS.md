# AGENTS.md — Project Context for AI Agents

## Overview

**checkota** is a Python tool that checks for OTA (over-the-air) firmware updates for
Transsion Holdings devices (TECNO, Infinix, itel). It queries Google's Android check-in
servers using protobuf-based requests, parses the response for available updates, and
optionally sends Telegram notifications.

## Architecture

```
checkota.py           ← Entry point: CLI, orchestration, parallel execution
modules/
  constants.py        ← URLs, region codes, SDK versions, regex patterns
  manager.py          ← Config data model (dataclass), YAML parsing, fingerprint handling
  update_checker.py   ← Builds protobuf check-in request, sends to Google, parses response
  metadata.py         ← Fetches OTA ZIP metadata (fingerprint, SDK, patch level) via RemoteZip
  fingerprints.py     ← Persistence: saves/loads processed update titles (dedup)
  logging.py          ← Thread-safe logging with ANSI colors
  telegram.py         ← Telegram notification + Telegraph page fallback + HTML sanitization
google-ota-prober/    ← Git submodule (fork of tangalbert919/google-ota-prober): proto definitions
  checkin/            ← Compiled protobuf Python modules
  utils/functions.py  ← IMEI/digest/serial/MAC generators
  probe.py            ← Original probe script (not used by checkota.py)
configs/              ← YAML device configs (one per device codename, ~100 files)
processed_updates.txt ← Append-only log of seen update titles (trimmed at 2000 entries)
```

## Data Flow

1. **Read config** — YAML file defines device fingerprint fields (`oem`, `product`,
   `device`, `android_version`, `build_tag`, `incremental`). Supports multiple variants
   per file via `variants:` list.

2. **Build check-in request** — `UpdateChecker` constructs a protobuf
   `AndroidCheckinRequest` with the fingerprint, a generated IMEI, serial, MAC, and
   digest. Gzip-compressed and POSTed to `https://android.googleapis.com/checkin`.

3. **Parse response** — The response is an `AndroidCheckinResponse` protobuf. The
   `setting` entries are scanned for `update_url`, `update_title`, `update_description`,
   `update_size`.

4. **Fetch OTA metadata** — If an update is found, `get_ota_metadata()` opens the OTA ZIP
   via `RemoteZip` and reads `META-INF/com/android/metadata` to extract the target
   `post-build` fingerprint, incremental version, security patch level, and SDK level.

5. **Update config** — The config YAML file is rewritten in-place with the new
   `android_version`, `build_tag`, and `incremental` from the target fingerprint.

6. **Notify** — A Telegram message is built and sent. If the message exceeds 4090
   characters, the description section is truncated and a Telegraph page is created as
   a fallback.

## Key Design Decisions & Conventions

### Product → Region Code Mapping
Products follow the convention `{device_code}-{REGION}`. Region codes can be multi-part
(e.g., `CN7c-OP-M1` → region `OP-M1`). The `region_code_from_product()` function in
`manager.py` extracts everything after the first `-`:

| Product        | Region code | Region name                |
|----------------|-------------|----------------------------|
| `KL8-OP`       | `OP`        | Global - OP Market         |
| `X6852-IN`     | `IN`        | India - IN Market          |
| `CN7c-OP-M1`   | `OP-M1`     | Global - OP-M1 Market      |

**Always use `product.split("-", 1)[1]` to extract region code** — never
`split("-")[-1]` (which breaks for multi-part codes like `OP-M1`).

### Config file format
Two styles exist:
- **Single variant** — all fields at top level:
  ```yaml
  oem: "TECNO"
  product: "KL8-OP"
  device: "TECNO-KL8"
  android_version: "14"
  build_tag: "UP1A.231005.007"
  incremental: "260412V1712"
  model: "TECNO SPARK 30 5G"
  ```
- **Multiple variants** — shared fields at top level, variants override in a list:
  ```yaml
  oem: "Infinix"
  device: "Infinix-X6873"
  model: "Infinix GT 30 Pro"
  variants:
    - variant: "Global"
      android_version: "16"
      build_tag: "BP2A.250605.031.A3"
      product: "X6873-OP"
      incremental: "201350016"
    - variant: "India"
      product: "X6873-IN"
      incremental: "201350016"
  ```

### Fingerprint format
```
{oem}/{product}/{device}:{android_version}/{build_tag}/{incremental}:user/release-keys
```

### Telegram HTML sanitization (`_sanitize_html`)
The sanitization pipeline in `telegram.py` runs in 5 ordered steps:

1. **Bold headers** — Detect lines like `Android Version<br>` that are NOT wrapped in
   `<small>/<font>` (i.e., section headers, not content), wrap in `<b>`.
2. **`<br>` → `\n`** — Use regex `r"<\s*br\s*/?\s*>[^\S\n]*\n?"` to consume inline
   whitespace plus at most ONE trailing `\n`, preserving intentional blank lines.
3. **Strip unsupported tags** — Remove `<small>`, `<font>`, `<a>` (keep text content).
4. **Bullet normalization** — Unicode bullets → `"- "`.
5. **Whitespace normalization** — Collapse consecutive blank lines to one, clean up
   URL-in-parens patterns, trim trailing spaces.

**Important:** Step 1 must run BEFORE Step 2–3 so the HTML structure (which distinguishes
headers from `<small>`-wrapped content) is still intact.

### Notifications & Truncation
- Telegram message limit: 4096 chars. Code uses `MAX_LEN = 4090` as safety margin.
- If the message exceeds `MAX_LEN`, the description section is identified via
  `DESC_SECTION_RE` regex, truncated at a sentence/paragraph boundary, and a "Read full
  changelogs" link to a Telegraph page is appended.
- `DESC_SECTION_RE` pattern: captures from `<b:Title:>` through the description to
  `\n\n<b:Size:>`. **The blank line before `<b:Size:>` must be preserved** for the
  regex to match.

## Known Bug Fixes (historical context — do not regress)

| Issue | File | What was fixed |
|-------|------|----------------|
| `DESC_SECTION_RE` mismatch when OS line present | `constants.py` | Trailing `\n` made optional: `\n?` |
| `OP-M1` region code mis-parsed | `manager.py` | `split("-",1)[1]` instead of `split("-")[-1]` |
| `cancel_futures` crashes on Python <3.9 | `checkota.py` | Guarded with `sys.version_info >= (3, 9)` |
| `--update-incremental` skipped known titles | `checkota.py` | Removed early return for non-force case |
| Shutdown race condition (sessions closed while threads running) | `checkota.py` | Set `stop_event` first, `shutdown(wait=True)`, then close sessions |
| `processed_updates.txt` unbounded growth | `fingerprints.py` | Trim to 2000 newest entries after each write |
| `<br>\n` created double newlines; greedy `\s*` ate template `\n\n` | `telegram.py` | `[^\S\n]*\n?` instead of `\s*` |
| Description formatting: flat text, no section hierarchy | `telegram.py` | Bold headers via pre-strip regex pass |

## Running

```bash
# Single config
python3 checkota.py -c configs/config-X6873.yml

# Directory of configs (parallel, 4 jobs)
python3 checkota.py -d configs/ --jobs 4

# Direct fingerprint (no config file)
python3 checkota.py --fp "Infinix/X6873-OP/Infinix-X6873:16/BP2A..."

# Dry run
python3 checkota.py -c configs/config-X6873.yml --dry-run

# Update config incremental without notification
python3 checkota.py -c configs/config-X6873.yml --update-incremental

# Filter by region
python3 checkota.py -d configs/ --reg OP-M1

# Debug (saves check-in response)
python3 checkota.py -c configs/config-X6873.yml --debug
```

Environment variables for Telegram:
- `bot_token` — Telegram bot token
- `chat_id` — Target chat ID
- `telegraph_token` — Telegraph API token (for long descriptions)
