# AGENTS.md — Project Context for AI Agents

## Overview

**checkota** checks OTA firmware updates for Transsion devices (TECNO, Infinix, itel).
Queries Google's Android check-in servers via protobuf requests, parses the response for
available updates, and optionally sends Telegram notifications.

## Architecture

```
checkota/              ← Package (import: from checkota.cli import main)
    __init__.py        ← Bootstraps vendored google-ota-prober onto sys.path on import
    __main__.py        ← `python -m checkota` entry → checkota.cli.main
    paths.py           ← PROJECT_ROOT/APP_CONFIGS_DIR/VENDOR_DIR anchors + ensure_vendor_on_path()
                         (CHECKOTA_VENDOR_DIR override; fails loud if vendor missing)
    cli.py             ← argparse, arg validation, config-path resolution; orchestration:
                         _run_sequential (--jobs 1), _run_global_pool ((config,variant) pool)
    runtime.py         ← RunContext (per-thread sessions w/ tuned HTTPAdapter pool, locks,
                         stop_event), create_run_context, install_interrupt_handler,
                         start_watchdog (--timeout)
    processor.py       ← Pipeline: collect_update_info, apply_update_actions,
                         process_config(_variant), load_config_variants,
                         config_from_fingerprint, OTA metadata cache
    models.py          ← VariantUpdate dataclass (processor + notifier)
    description.py     ← TerminalParser (HTML→ANSI) + format_update_description
    notifier.py        ← create_notifier + build_notification_message
    constants.py       ← URLs, region codes, SDK versions, regex patterns
    manager.py         ← Config dataclass, YAML parsing, fingerprint handling
    update_checker.py  ← Builds/sends protobuf check-in request, parses response
    metadata.py        ← Parses OTA ZIP metadata; processed_updates_path() anchored to repo root
    zip_metadata.py    ← Direct HTTP Range fetch of one ZIP member (replaces remotezip);
                         ZIP64-aware, absolute ranges only (Google rejects suffix ranges)
    fingerprints.py    ← Persistence: processed update titles (dedup, trimmed at 2000)
    logging.py         ← Thread-safe logging with ANSI colors
    telegram.py        ← Telegram notify + Telegraph fallback + HTML sanitization
configs/               ← YAML device configs (one per codename, 108 files)
tests/                 ← pytest suite
processed_updates.txt  ← Append-only log of seen update titles (trimmed at 2000)
pyproject.toml         ← Package metadata + deps (requests, PyYAML, protobuf)

vendor/google-ota-prober/   ← Vendored (pinned commit in VERSION; ATTRIBUTION = scope/license)
  checkin/             ← Compiled protobuf modules (checkin_generator_pb2)
  proto/               ← .proto sources
  utils/functions.py   ← IMEI/digest/serial/MAC generators
```

## Data Flow

1. **Read config** — YAML defines fingerprint fields (`oem`, `product`, `device`,
   `android_version`, `build_tag`, `incremental`). Multiple variants via `variants:` list.
2. **Build request** — `UpdateChecker` builds protobuf `AndroidCheckinRequest` (fingerprint
   + generated IMEI/serial/MAC/digest), gzips, POSTs to `https://android.googleapis.com/checkin`.
3. **Parse response** — `AndroidCheckinResponse` protobuf; scan `setting` entries for
   `update_url`, `update_title`, `update_description`, `update_size`.
4. **Fetch OTA metadata** — `get_ota_metadata()` reads `META-INF/com/android/metadata`
   from the remote ZIP via `zip_metadata.fetch_zip_member()` (HTTP Range, no full download)
   for target `post-build` fingerprint, incremental, patch level, SDK level.
5. **Update config** — YAML rewritten in-place with new `android_version`, `build_tag`,
   `incremental` from the target fingerprint.
6. **Notify** — Telegram message sent. If > 4090 chars, description truncated and a
   Telegraph page created as fallback.

## Key Design Decisions & Conventions

### Product → Region Code

Convention `{device_code}-{REGION}`; region can be multi-part (`CN7c-OP-M1` → `OP-M1`).
`region_code_from_product()` in `manager.py` takes everything after the first `-`.

**Always use `product.split("-", 1)[1]`** — never `split("-")[-1]` (breaks `OP-M1`).

Examples: `KL8-OP`→`OP`, `X6852-IN`→`IN`, `CN7c-OP-M1`→`OP-M1`.

### Config file format

Single variant — all fields top level:

```yaml
oem: "TECNO"
product: "KL8-OP"
device: "TECNO-KL8"
android_version: "14"
build_tag: "UP1A.231005.007"
incremental: "260412V1712"
model: "TECNO SPARK 30 5G"
```

Multiple variants — shared fields top level, list overrides:

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

Fingerprint: `{oem}/{product}/{device}:{android_version}/{build_tag}/{incremental}:user/release-keys`

### Telegram HTML sanitization (`_sanitize_html`, 5 ordered steps)

1. **Bold headers** — lines like `Android Version<br>` NOT wrapped in `<small>/<font>`
   (headers, not content) get wrapped in `<b>`. Must run first while structure intact.
2. **`<br>` → `\n`** — regex `r"<\s*br\s*/?\s*>[^\S\n]*\n?"` consumes inline whitespace +
   at most ONE trailing `\n` (preserves intentional blank lines).
3. **Strip tags** — remove `<small>`, `<font>`, `<a>` (keep text).
4. **Bullets** — Unicode bullets → `"- "`.
5. **Whitespace** — collapse blank lines, clean URL-in-parens, trim trailing spaces.

### Terminal output (`TerminalParser` in `description.py`)

Same two-stage approach as Telegram:

1. **Bold headers** — same pre-parse regex as `_sanitize_html`.
2. **`<br>`** — `_empty_br_count` tracks consecutive `<br>`: reset on content `<br>`,
   incremented on empty `<br>`; blank line pushed only when `== 1` (single break vs section).
3. **`</b>` flush** — `handle_endtag` flushes BEFORE clearing `self.bold`, so `\033[1m…\033[0m`
   wraps text correctly (else `<br>` flushes after bold is off).

### Notifications & Truncation

+ Telegram limit 4096; code uses `MAX_LEN = 4090`.
+ Over limit: description section found via `DESC_SECTION_RE`, truncated at sentence/para
  boundary, "Read full changelogs" Telegraph link appended.
+ `DESC_SECTION_RE` captures `<b>Title:</b>` → description → `\n\n?<b>Size:</b>`. Second
  newline optional (`\n\n?`) since sanitization may collapse the blank line.

## Known Bug Fixes (do not regress)

> Refactor note: `checkota.py` was sliced into the `checkota/` package (entry →
> `checkota.cli.main`, `python -m checkota` via `__main__.py`).
> Historical `checkota.py` rows map to: CLI/orchestration/watchdog → `cli.py`+`runtime.py`;
> pipeline → `processor.py`; `TerminalParser` → `description.py`; notify → `notifier.py`;
> `RunContext` → `runtime.py`; `VariantUpdate` → `models.py`; vendor bootstrap →
> `paths.py`+`checkota/__init__.py`.

| Issue | File | Fix |
|-------|------|-----|
| `DESC_SECTION_RE` mismatch with OS line | `constants.py` | Trailing `\n` → optional `\n?` |
| `OP-M1` region mis-parsed | `manager.py` | `split("-",1)[1]` not `split("-")[-1]` |
| OTA fetch hung whole run | `metadata.py`, `checkota.py` | `RemoteZip` timeout 60→15; `--timeout` watchdog (`threading.Timer` sets `stop_event`, closes sessions, `os._exit(124)`) since stuck socket reads ignore `stop_event` |
| Dead Python version guards | `checkota.py` | Removed `<(3,7)` / `>=(3,9)` branches (`requires-python>=3.9`); `cancel_futures=True` unconditional |
| Vendor dir missing on non-editable install | `checkota.py` | Fail loud if absent; `CHECKOTA_VENDOR_DIR` env override |
| `--update-incremental` skipped known titles | `checkota.py` | Removed early return for non-force |
| Shutdown race (sessions closed mid-run) | `checkota.py` | Set `stop_event` → `shutdown(wait=True)` → close sessions |
| `processed_updates.txt` unbounded growth | `fingerprints.py` | Trim to 2000 newest |
| `<br>\n` double newlines; greedy `\s*` ate template `\n\n` | `telegram.py` | `[^\S\n]*\n?` not `\s*` |
| Flat description, no hierarchy | `telegram.py` | Bold headers via pre-strip regex |
| Terminal: extra blank lines | `checkota.py` | `_empty_br_count`; blank only on first empty `<br>` |
| Terminal: `<b>` headers not bolded | `checkota.py` | `flush()` on `</b>` before clearing bold |
| Worker exceptions crashed parallel run | `checkota.py` | try-except in `run_config_buffered` |
| E402 import warnings | `checkota.py` | `# ruff: noqa: E402` (no longer needed post-refactor) |
| `DESC_SECTION_RE` mismatch when blank line collapsed | `constants.py` | `\n\n?` not `\n\n` |
| Locale-dependent Unicode I/O errors | `manager.py`, `fingerprints.py`, `update_checker.py` | Explicit `encoding="utf-8"` everywhere |
| `processed_updates.txt` path CWD-relative | `checkota/metadata.py` | Anchored to repo root via `Path(__file__).resolve().parent.parent` |
| 989-line monolith | `checkota/*` | Sliced into focused modules; entry → `checkota.cli.main`. Behavior-preserving |
| `remotezip` dep for OTA metadata | `zip_metadata.py`, `metadata.py`, `pyproject.toml` | Vendored ZIP64-aware `fetch_zip_member()` w/ absolute Range requests (Google rejects suffix `bytes=-N`); probes size via `bytes=0-0`, reads EOCD→central-dir→entry. Byte-identical, one less dep |
| Multi-variant configs serial | `cli.py`, `processor.py`, `runtime.py` | `_run_global_pool` flattens (config,variant) pairs into one `--jobs` pool (in-flight ≤ `--jobs`); output buffered per variant, regrouped per config. `-c X6873 --jobs 5`: ~15s→4.7s |
| Per-thread session pool too small | `runtime.py` | `HTTPAdapter` `pool_maxsize = max(10, --jobs)` |
| Flat 5s retry backoff | `update_checker.py`, `metadata.py` | Exponential 1s→2s→4s instead of flat 5s×3 |

## Running

After `pip install -e .`, use `checkota` directly. Run from repo root
(`-d` paths are relative to it).

```bash
checkota -c X6873                                  # single config (codename → configs/config-X6873.yml)
checkota -d configs/ --jobs 4                      # directory, parallel
checkota -d configs/ --jobs 4 --timeout 600        # cap runtime (exits 124)
checkota --fp "Infinix/X6873-OP/Infinix-X6873:16/BP2A..."   # direct fingerprint
checkota -c X6873 --dry-run                        # dry run
checkota -c X6873 --update-incremental             # update config, no notify
checkota -d configs/ --reg OP-M1                   # filter by region
checkota -c X6873 --debug                          # save check-in response
```

> Equivalent without install: `python3 -m checkota <args>`.

Env vars:

+ `bot_token`, `chat_id` — Telegram bot token + target chat
+ `telegraph_token` — Telegraph API token (long descriptions)
+ `CHECKOTA_VENDOR_DIR` — override vendored `google-ota-prober` path
  (default `<repo>/vendor/google-ota-prober`; needed for relocated/wheel installs)
