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
    paths.py           ← Path anchors + install-mode detection (_is_source_checkout):
                         source → repo-local configs/state/vendor; wheel → XDG dirs
                         ($XDG_CONFIG_HOME/checkota/configs seeded lazily from bundled
                         defaults, $XDG_STATE_HOME/checkota for state). VENDOR_DIR honors
                         CHECKOTA_VENDOR_DIR override; ensure_vendor_on_path() fails loud
                         if missing. processed_updates_path() lives here.
    cli.py             ← argparse, arg validation, config-path resolution; orchestration:
                         _run_sequential (--jobs 1), _run_global_pool ((config,region) pool)
    runtime.py         ← RunContext (per-thread sessions w/ tuned HTTPAdapter pool, locks,
                         stop_event), create_run_context, install_interrupt_handler,
                         start_watchdog (--timeout)
    processor.py       ← Pipeline: collect_update_info, apply_update_actions,
                         process_config (per region), load_config_regions,
                         config_from_fingerprint, OTA metadata cache
    models.py          ← RegionUpdate dataclass (processor + notifier)
    description.py     ← TerminalParser (HTML→ANSI) + format_update_description
    notifier.py        ← create_notifier + build_notification_message
    constants.py       ← URLs, region codes, SDK versions, regex patterns
    manager.py         ← Config dataclass, YAML parsing (_UniqueKeyLoader rejects duplicate
                         keys), fingerprint handling, identity matching, safe in-place
                         YAML rewrites (temp file → fsync → round-trip verify → os.replace)
    update_checker.py  ← Builds/sends protobuf check-in request (streamed, size-capped,
                         redirect-rejecting, transient-status retries), parses response
    metadata.py        ← Parses OTA ZIP metadata; transient retry loop + failure cache
    zip_metadata.py    ← Direct HTTP Range fetch of one ZIP member (replaces remotezip);
                         ZIP64-aware, absolute ranges only (Google rejects suffix ranges);
                         strict 206/Content-Range validation, gvt1-only redirect
                         allowlist, per-member size caps, bounded inflate, CRC checks
    fingerprints.py    ← Persistence: processed update titles (dedup, trimmed at 2000);
                         per-title claim locks pruned at startup (committed or >7d stale)
    logging.py         ← Thread-safe logging with ANSI colors
    validation.py      ← Untrusted-input predicates: control chars + Google HTTPS URL
                         allowlist checks shared by update_checker and zip_metadata
    message_text.py    ← Pure Telegram text pipeline: sanitize_html, canonicalization,
                         rendered-UTF-16 length fitting, plain-text fallback (no I/O)
    telegram.py        ← Telegram notify + Telegraph fallback; bot token redacted from
                         error logs; delegates text work to message_text.py
configs/               ← YAML device configs (one per codename, 114 files); bundled into
                         wheels as checkota.bundled_configs and seeded to XDG on first use
tests/                 ← pytest suite
scripts/               ← Ad-hoc tooling (proxy-based checks: fetch_spys, check_update_proxy)
processed_updates.txt  ← Append-only log of seen update titles (trimmed at 2000)
pyproject.toml         ← Package metadata + deps (requests, PyYAML, protobuf)

vendor/google-ota-prober/   ← Vendored (pinned commit in VERSION; ATTRIBUTION = scope/license)
  checkin/             ← Compiled protobuf modules (checkin_generator_pb2)
  proto/               ← .proto sources
  utils/functions.py   ← IMEI/digest/serial/MAC generators

Wheel packaging maps the vendor tree to checkota._vendor.* (package-dir →
vendor/) so regular wheels are self-contained; runtime imports still go through
the sys.path bootstrap (vendored code uses top-level `checkin`/`utils` imports).
pyproject [tool.pyright] extraPaths teaches static analysis the same trick.
```

## Data Flow

1. **Read config** — YAML defines shared identity/default fields (`oem`, `product_base`,
   `model`, `android_version`) and a `regions` mapping. Product, device, and canonical
   build tags are derived for each region; expanded regions contain only overrides.
2. **Build request** — `UpdateChecker` builds protobuf `AndroidCheckinRequest` (fingerprint
   + generated IMEI/serial/MAC/digest), gzips, POSTs to `https://android.googleapis.com/checkin`.
3. **Parse response** — `AndroidCheckinResponse` protobuf; scan `setting` entries for
   `update_url`, `update_title`, `update_description`, `update_size`.
4. **Fetch OTA metadata** — `get_ota_metadata()` reads `META-INF/com/android/metadata`
   from the remote ZIP via `zip_metadata.fetch_zip_member()` (HTTP Range, no full download)
   for target `post-build` fingerprint, incremental, patch level, SDK level. Results are
   deduplicated per URL across workers (in-flight event + cache + negative-failure TTL).
5. **Validate identity** — target fingerprint's oem/product/device must match the config
   (`fingerprint_identity_matches_config`); mismatches skip config updates, notifications,
   and title processing.
6. **Update config** — YAML rewritten in-place with new `android_version`, `build_tag`,
   `incremental`: identity-checked, duplicate-key-safe, atomic (temp → fsync → reparse
   round-trip verification → `os.replace`), preserving comments/quoting/newline style.
7. **Notify** — Telegram message sent. If over the rendered UTF-16 limit, description is
   truncated and a Telegraph page created as fallback; final payload always fitted locally.

## Key Design Decisions & Conventions

### Product → Region Code

Convention `{device_code}-{REGION}`; region can be multi-part (`CN7c-OP-M1` → `OP-M1`).
`region_code_from_product()` in `manager.py` takes everything after the first `-`.

**Always use `product.split("-", 1)[1]`** — never `split("-")[-1]` (breaks `OP-M1`).

Examples: `KL8-OP`→`OP`, `X6852-IN`→`IN`, `CN7c-OP-M1`→`OP-M1`.

### Config file format

Every config uses the compact `regions` schema. Shared values are top level; each region
is either a quoted incremental or an expanded mapping:

```yaml
oem: "Infinix"
product_base: "X6873"
model: "Infinix GT 30 Pro"
android_version: "16"
regions:
  OP: "201500011"
  EU:
    android_version: "15"
    incremental: "131015"
```

Product is `{effective_product_base}-{region}`. Device is
`{device_prefix}-{effective_product_base}`, where `device_prefix` is normally the exact
`oem`; the exact OEM value `"Itel"` is the exception and maps to lowercase `itel`. A
region may override `product_base` (the `IN` region in `config-X6857.yml` uses `X6857B`)
or provide a noncanonical `build_tag` (currently X1301 and T1102). Canonical Android
build tags come from `BUILD_TAG_BY_ANDROID` and must not be written in YAML.

Fingerprint: `{oem}/{product}/{device}:{android_version}/{build_tag}/{incremental}:user/release-keys`

The runtime accepts only this compact schema. A config containing `variants` fails with
`uses the legacy 'variants' schema; migrate it to a 'regions' mapping`; a legacy
single-region config containing `product` or `device` similarly directs the user to
`product_base` and `regions`. From a repository checkout, migrate a legacy directory with
`python scripts/migrate_compact_configs.py --write <config-dir>`. Bundled repository
defaults are migrated, but existing wheel/XDG configs are never overwritten automatically
and require this manual migration.

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

### Install modes & path resolution (`paths.py`)

+ `_is_source_checkout()` (import-time): pyproject.toml + repo configs + vendor dir (or a
  valid `CHECKOTA_VENDOR_DIR` override) ⇒ source mode. Everything else is wheel mode.
+ Source mode: configs/state stay repository-local; legacy CWD fallback for an existing
  `processed_updates.txt` is preserved.
+ Wheel mode: bundled defaults are seeded lazily to `$XDG_CONFIG_HOME/checkota/configs`
  (relative XDG values ignored → home fallback) via copy-once publication
  (`_publish_if_missing`: mkstemp → hardlink, rename fallback on FS without hardlinks;
  never overwrites). State goes to `$XDG_STATE_HOME/checkota`; no CWD migration.
+ Importing the package never seeds anything; seeding happens only when config lookup
  needs it (`active_config_dir()`).

### Fingerprint identity validation (`manager.py`, `processor.py`)

A target fingerprint's immutable identity (`oem`, `product`, `device`) must match the
config before any action: dry-run printing, config update, notification, and title
processing all gate on `fingerprint_identity_matches_config`. Region updates resolve the
exact region code and fail closed if the in-memory identity disagrees with the latest YAML.
This prevents a mismatched OTA response from poisoning a device's config.

### Network hardening

+ Check-in: POST with `allow_redirects=False`, streamed body capped at 4 MiB, redirects
  rejected outright, transient transport errors/HTTP statuses retried 1s→2s→4s
  (`RETRYABLE_HTTP_STATUSES`, shared with zip_metadata), protobuf `DecodeError` retried.
+ Response fields are untrusted: `update_url` must be HTTPS on exactly
  `android.googleapis.com` under `/packages/ota(/api)/` (no port/userinfo/control chars);
  titles containing control characters are dropped (protects the line-oriented dedup file).
+ ZIP fetch: strict 206 + exact Content-Range/Content-Length matching, redirects followed
  ≤5 hops only within `android.googleapis.com`/`*.gvt1.com` under `/packages/`, member
  caps (1 MiB compressed/decompressed, 16 MiB central directory), bounded inflate,
  CRC-32 verified. Central-directory framing is validated for every entry but extra-field
  parsing/ZIP64 fixup only for the requested one.

### Telegram canonicalization & fitting (`telegram.py`)

+ After the 5-step sanitization above, text is tokenized into balanced supported tags +
  per-character escaped fragments (`_tokenize_telegram_html`). Entities are decoded once
  and re-escaped canonically; entity-encoded control characters fail canonicalization.
+ Length limits are enforced on RENDERED UTF-16 units (`_rendered_length`), not raw HTML
  length; `_fit_telegram_html` truncates at token granularity, closes open tags, appends
  `...`, and never splits entities or tags.
+ Fail-safe ladder: uncanonicalizable markup degrades to escaped plain text
  (`_fallback_plain_text`) rather than dropping the notification; whitespace-only content
  fails closed.

## Known Bug Fixes (do not regress)

> Refactor note: `checkota.py` was sliced into the `checkota/` package (entry →
> `checkota.cli.main`, `python -m checkota` via `__main__.py`).
> Historical `checkota.py` rows map to: CLI/orchestration/watchdog → `cli.py`+`runtime.py`;
> pipeline → `processor.py`; `TerminalParser` → `description.py`; notify → `notifier.py`;
> `RunContext` → `runtime.py`; `RegionUpdate` → `models.py`; vendor bootstrap →
> `paths.py`+`checkota/__init__.py`.

| Issue | File | Fix |
| ------- | ------ | ----- |
| `DESC_SECTION_RE` mismatch with OS line | `constants.py` | Trailing `\n` → optional `\n?` |
| `OP-M1` region parsed incorrectly | `manager.py` | `split("-",1)[1]` not `split("-")[-1]` |
| OTA fetch hung whole run | `metadata.py`, `checkota.py` | `RemoteZip` timeout 60→15; `--timeout` watchdog (`threading.Timer` sets `stop_event`, flushes stdio, `os._exit(124)`) since stuck socket reads ignore `stop_event` |
| Dead Python version guards | `checkota.py` | Removed `<(3,7)` / `>=(3,9)` branches (`requires-python>=3.10`); `cancel_futures=True` unconditional |
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
| Multi-region configs serial | `cli.py`, `processor.py`, `runtime.py` | `_run_global_pool` flattens (config,region) pairs into one `--jobs` pool (in-flight ≤ `--jobs`); output buffered per region, regrouped per config. `-c X6873 --jobs 5`: ~15s→4.7s |
| Per-thread session pool too small | `runtime.py` | `HTTPAdapter` `pool_maxsize = max(10, --jobs)` |
| Flat 5s retry backoff | `update_checker.py`, `metadata.py` | Exponential 1s→2s→4s instead of flat 5s×3 |
| Watchdog deadlocked on session close | `runtime.py` | Timer callback must NOT call `ctx.stop()` (races workers); set `stop_event` → flush → `os._exit(124)` only |
| Buffered notifications lost on interrupt/failure | `cli.py` | Drain runs whenever buffering was possible (any exit code); executor owned by `main()` and shut down once with `cancel_futures=True` |
| Metadata waiters timed out spuriously at 15s | `processor.py` | Waiters poll the owner's completion Event (instant wake) instead of abandoning a still-valid fetch |
| Cross-device fingerprint poisoned config/notifications | `manager.py`, `processor.py` | Identity validation gates before update/notify/title processing; exact region resolution fails closed |
| YAML duplicate keys silently last-wins | `manager.py` | `_UniqueKeyLoader` raises `ConstructorError` on duplicate mapping keys |
| Config corruption if rewrite crashes mid-write | `manager.py` | Temp file → write → fsync → chmod → reparse round-trip verification → `os.replace`; original untouched on any failure |
| SSRF via check-in URL / OTA redirect | `update_checker.py`, `zip_metadata.py` | Exact-host HTTPS allowlists; check-in rejects redirects; ZIP fetch follows only Google delivery hosts ≤5 hops |
| Unbounded response/decompression memory | `update_checker.py`, `zip_metadata.py` | Streamed reads capped (4 MiB check-in / 1 MiB member); inflate bounded by declared size + `unconsumed_tail` check |
| Telegram API 400 on oversized/control-char payloads | `telegram.py` | Canonicalize entities, enforce rendered UTF-16 limit locally, plain-text fallback for uncanonicalizable markup |
| Vendor dir missing on plain wheel install | `paths.py`, `pyproject.toml` | Wheels bundle vendor as `checkota._vendor.*` + configs as `checkota.bundled_configs`; XDG seeding keeps them self-contained |
| Bot token leaked into logs on send failure | `telegram.py` | requests includes the full URL (which embeds `bot<TOKEN>`) in HTTPError/ConnectionError messages; `_redact()` scrubs the token before logging |
| Per-title lock files accumulated forever | `fingerprints.py`, `runtime.py` | `prune_title_locks()` at startup removes locks for committed titles or files >7d old, only when no process holds them (LOCK_EX\|LOCK_NB probe); skipped in dry-run |
| Config lock files left beside configs | `manager.py` | `_prune_config_lock()` unlinks the released lock when no other process holds it (residual unlink race documented; rewrites are atomic + idempotent) |
| `assert` used for control flow (stripped by `-O`) | `processor.py`, `update_checker.py` | Explicit `raise RuntimeError`/`UpdateCheckError` on the unreachable branches |

## Running

After `pip install -e .`, use `checkota` directly. Run from repo root
(`-d` paths are relative to it).

```bash
checkota -c X6873                                  # single config (codename → configs/config-X6873.yml)
checkota -d configs/ --jobs 4                      # directory, parallel
checkota -d configs/ --jobs 4 --timeout 600        # cap runtime (exits 124)
checkota --fp "Infinix/X6873-OP/Infinix-X6873:16/BP2A..."   # direct fingerprint
checkota -c X6873 --dry-run                        # dry run
checkota -c X6873 --fetch-zip-proxy                # use proxy env for OTA ZIP fetch
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
