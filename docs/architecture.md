# Architecture and behavioral contracts

This is the implementation reference for [AGENTS.md](../AGENTS.md). Read the
sections relevant to a change; use [regressions.md](regressions.md) for historical
failure modes and [development.md](development.md) for verification.

## Module map

| Module / path | Responsibility |
| --- | --- |
| `checkota/__init__.py`, `__main__.py` | Vendor bootstrap on import; `python -m checkota` delegates to `cli.main` |
| `checkota/cli.py` | Argument validation, config lookup, sequential execution and a global `(config, region)` worker pool |
| `checkota/runtime.py` | `RunContext` (including metadata cache state), per-thread sessions, locks, stop events, interrupt handling and watchdog |
| `checkota/processor.py` | Check/update pipeline, region selection, metadata cache, notification dispatch/recovery/draining |
| `checkota/models.py` | Shared update and pending-notification dataclasses |
| `checkota/manager.py` | Config parsing, fingerprint derivation and identity checks, comment-preserving atomic YAML rewrites |
| `checkota/update_checker.py` | Protobuf check-in request construction, bounded HTTP response reading and response validation |
| `checkota/metadata.py` | OTA metadata parsing and transient retries; success/failure caching is coordinated in `processor.py` using `RunContext` state |
| `checkota/zip_metadata.py` | ZIP64-aware HTTP Range access to one ZIP member; no full OTA download |
| `checkota/validation.py` | Shared control-character and Google HTTPS URL validation |
| `checkota/fingerprints.py` | Processed-title persistence, per-title claims and lock pruning |
| `checkota/outbox.py` | Durable, per-title pending notifications for file-backed updates |
| `checkota/notifier.py` | Notifier creation and notification-message construction |
| `checkota/message_text.py` | Pure Telegram sanitization, canonicalization, UTF-16 length fitting and plain-text fallback |
| `checkota/telegram.py` | Telegram/Telegraph I/O, long-description handling and token-redacted errors; historical text-helper aliases delegate to `message_text.py` |
| `checkota/description.py`, `logging.py` | HTML-to-ANSI terminal rendering and thread-safe, sanitized logging |
| `checkota/constants.py` | URLs, region codes, SDK/build-tag mappings and regexes |
| `configs/` | Compact device YAML defaults; packaged as `checkota.bundled_configs` |
| `vendor/google-ota-prober/` | Pinned protobuf modules/sources and identity generators; see `VERSION` and `ATTRIBUTION` |
| `scripts/` | Proxy tooling and compact-config migration |
| `tests/` | Pytest regression suite |
| `pyproject.toml` | Dependencies, entry point, wheel package mapping and static-analysis configuration |

## Data flow

1. Load compact YAML defaults and region overrides; derive product, device and
   fingerprint for each selected region.
2. Build an `AndroidCheckinRequest` with fingerprint and generated identity values,
   gzip it and POST to `https://android.googleapis.com/checkin`.
3. Parse `AndroidCheckinResponse.setting` fields such as `update_url`,
   `update_title`, `update_description` and `update_size`. Validate remote values.
4. Short-circuit known processed titles unless an explicit override applies.
5. Fetch `META-INF/com/android/metadata` via HTTP Range reads. Extract the target
   fingerprint, incremental, patch level and SDK level. Per-URL in-flight events
   and caches deduplicate metadata work across workers, including a negative TTL.
6. Validate the target's immutable identity against the config before actions.
7. For a notifying, file-backed update, durably stage a notification before
   rewriting YAML. Atomically publish and sync the config, then mark the outbox
   record ready.
8. Send or buffer the notification. Commit the processed title on successful
   delivery, then remove the durable pending record. Without a notifier, record a
   new title after successful update actions instead. Dry runs do not persist it.

## Config schema and identity

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

- Product is `{effective_product_base}-{region}`. A region may override
  `product_base`, as `IN` does in `config-X6857.yml` (`X6857B`).
- Device is `{device_prefix}-{effective_product_base}`. The prefix is the exact
  OEM, except the exact value `Itel` maps to lowercase `itel`.
- Region codes match `[A-Z0-9][A-Z0-9-]*`; they become part of the fingerprint.
- Extract a region using `region_code_from_product()` or splitting at the first
  hyphen, never the last: `CN7c-OP-M1` means region `OP-M1`.
- Canonical build tags come from `BUILD_TAG_BY_ANDROID` and must not be stored in
  YAML. Noncanonical overrides are supported (for example, T1102).

Fingerprint format:

```text
{oem}/{product}/{device}:{android_version}/{build_tag}/{incremental}:user/release-keys
```

A target fingerprint's `oem`, `product` and `device` must match the config before
config writes, notifications, title processing and dry-run action previews.
Initial check-in logging and the sanitized description in direct `--dry-run --fp`
mode can appear before target identity validation; not all terminal output is
identity-gated. Region rewrites resolve the exact region and fail closed if the
in-memory identity disagrees with the latest YAML.

### Loadable implies updatable

The runtime accepts only the compact `regions` schema. Legacy `variants` and
single-region `product`/`device` configs fail with migration guidance. See the
[README config and migration instructions](../README.md#configs). Bundled
defaults are migrated; existing user/XDG configs are never migrated automatically.

`_UniqueKeyLoader` rejects duplicate keys. `_validate_compact_source_layout` runs
on load and before updates, rejecting syntax the line-oriented rewriter cannot
express: flow collections, multiline scalars, `|`/`>` block scalars, anchors and
aliases. Empty `{}`/`[]` are left to schema validation for a better error message.
A parsed config that always fails rewriting can suppress its notification, so
these restrictions are a correctness contract, not merely style.

Rewrites preserve comments and surrounding whitespace/newline style, but changed
scalar values are emitted double-quoted rather than retaining their original
plain/single/double-quoted style. Untouched lines retain their source spelling.
Read configs using `utf-8-sig` (accept a BOM; drop it on a real rewrite). Quote
strings with a sufficiently large PyYAML width to avoid introducing wrapped
multiline scalars. Stage the
rewrite in a temporary file, flush/fsync, preserve permissions, reparse and verify
both semantic round-trip and source layout, then atomically replace the original.
The config directory is synced before the corresponding outbox record becomes
ready. Do not publish partially written or unrewritable configs.

### Android default convergence

On a real update, `_converge_android_default` promotes a common effective Android
version to the top level and removes redundant region overrides. Compare every
region's fingerprint before and after; reject promotion that changes one.

A no-op returns **before convergence** and leaves the file byte-identical. Do not
turn normal checks into normalization sweeps. When collapsing mappings, re-indent
retained child comments and hoist them above the region key. When expanding a
scalar, use `_dominant_child_indent` rather than assuming two spaces.

Repository config tests derive identities without pinning config or target counts;
adding a device must not require changing numeric assertions or documentation.

## Processed titles and delivery

### Known titles

For normal runs, a processed title is terminal even when another region's YAML is
behind. `collect_update_info()` returns before ZIP metadata work, and
`apply_update_actions()` leaves configs alone. Do not fetch metadata to catch up
other regions for a known title; that repeats Tcard processing.

Only explicit `--update-incremental` and `--force-notify` bypass this skip:
`--update-incremental` may rewrite YAML; `--force-notify` resends without rewriting
an already-processed update. Dry-run alone is not an override.

The processed-title log retains the newest 2000 titles. Reading a missing log
returns before opening its database lock, so a read does not create state. Append
operations terminate a dangling final line before adding another title. Startup
prunes committed or more-than-seven-day-old title claim files only after a
nonblocking exclusive-lock probe; dry runs skip pruning. Claim cleanup tolerates
already-closed handles.

### Durable outbox

File-backed notifying updates stage JSON records in
`<processed-title-path>.outbox/` **before** advancing YAML. Records contain the
notification, config path, target fingerprint and a `ready` flag.

- If staging fails, do not rewrite the config.
- An unconfirmed record is recoverable only when the on-disk config contains its
  target fingerprint. Startup confirms recovered rewrites under the title claim
  and durably marks them ready before allowing new check-in work.
- A ready record proves a completed rewrite and remains a delivery obligation
  even if the config later advances, rolls back, moves or disappears.
- Corrupt outbox state or an unavailable claim during confirmation fails closed;
  do not advance configs past unresolved recovery work.
- Remove a record only after the title commit. Delivery is **at least once**:
  a crash between Telegram acceptance and title commit can cause a duplicate.
- Later notifying runs recover pending work even if no fresh update is found.
  Dry runs do not recover or send it.

Keep these durable guarantees distinct from in-memory notification buffering.
After a successful config step, both `-c` and `-d` jobs may buffer locally after a
stop request. Main checks pending work after workers stop and drains on success,
failure or interrupt. Healthy single-config runs still send inline; no-config
`--fp` work remains stoppable.

### Credentials and disabled notifications

Notifications are the CLI default. `_require_telegram_env()` validates `bot_token`
and `chat_id` after argument-shape validation, before config seeding, lock pruning
or network work. Missing credentials cause parser exit 2. `--dry-run`,
`--skip-telegram`, `--register-update`, `--update-incremental` and `--gen-fp` bypass
that requirement; `telegraph_token` is optional for long descriptions.

When update actions succeed without a notifier, new titles are still recorded.
`--skip-telegram` and `--update-incremental` are not read-only alternatives to a
notifying run. Dry runs do not rewrite device configs, record titles or deliver
notifications, but they still contact servers. They can create the wheel state
directory, seed missing config defaults or create a database lock when reading
existing dedup data. `--debug` can also write response artifacts during a dry run.

## Concurrency, interrupts and deadlines

`_run_global_pool` flattens `(config, region)` pairs into one `--jobs` pool with
bounded in-flight work. Output is buffered per region and regrouped per config.
Each thread has a session with `HTTPAdapter.pool_maxsize = max(10, jobs)`.

Metadata waiters poll the owner's completion event rather than timing out a
still-valid fetch at an arbitrary 15 seconds. Check stop state after metadata
fetch and before actions; do not cache successful metadata once stop is requested.
Worker exceptions must not bring down the whole pool.

On interrupt, set the stop event before waiting for workers. On normal completion,
main shuts down its executor once (`wait=True`, `cancel_futures=True`), drains
pending notifications when enabled, then calls `ctx.stop()` to close sessions.
After workers stop, main may clear the stop event for this drain; do not do so
while workers remain active. The run watchdog is replaced by a separate
300-second watchdog for the end-of-run drain, so `--timeout` is not a strict
whole-process deadline including delivery.

The watchdog must not call `ctx.stop()`; that can deadlock against workers. Its
emergency drain uses zero delay, a 30-second cooperative deadline and at most 30
sends without clearing the shared stop event. Flushing and emergency delivery run
in a daemon cleanup thread; the watchdog joins it for at most 30 seconds and then
hard-exits 124 even if cleanup blocks. Do not rely on cooperative drain checks
alone for a hard deadline or close worker sessions while they are active.

A directory `--reg` filter is selection: configs without that region are skipped;
a single-config miss is an error. `scan_region_filter()` pre-scans a sweep and
reports an empty selection once. It fails before work only when every config
loaded cleanly; otherwise the sweep also reports load errors.

## Install modes and paths

`paths.py` detects source mode at import using the project metadata, configs and
vendor directory (or a valid `CHECKOTA_VENDOR_DIR` override). Otherwise it uses
wheel mode. Importing the package bootstraps vendor imports but does not seed
configs; lookup via `active_config_dir()` triggers lazy seeding.

- **Source:** configs/state are repository-local, with the legacy CWD fallback for
  an existing `processed_updates.txt`.
- **Wheel:** configs live in `$XDG_CONFIG_HOME/checkota/configs` and state in
  `$XDG_STATE_HOME/checkota`. Relative XDG values use home-directory fallbacks.
  There is no CWD state migration. Explicit `processed_updates_path()` resolution
  creates the wheel state directory; this is separate from `load_processed_titles()`
  reading an absent file without creating its data/lock files.
- **Seeding:** write and fsync a private temporary file, then publish by hardlink
  or atomic no-replace rename (Linux/Windows). Never copy into the destination or
  clean it up: inode-check/unlink and tombstone restoration can lose user edits.
  Fail closed if neither publication primitive is available; no O_EXCL copy
  fallback and no cleanup allocations after ENOSPC.
- **Vendor:** wheels map the pinned tree to `checkota._vendor.*`; runtime bootstrap
  accommodates its top-level `checkin`/`utils` imports. Missing vendor files fail
  loudly. `CHECKOTA_VENDOR_DIR` overrides the location; Pyright's `extraPaths`
  describes the source layout for analysis.

## Network and untrusted input

### Check-in

POST with `allow_redirects=False`; reject redirects. Stream responses with a
4 MiB cap. Check-in and OTA metadata fetching each allow three total attempts
for transient failures, with 1s and 2s waits between attempts; there is no 4s wait
after the final attempt. Check-in also retries protobuf `DecodeError`. Retryable
HTTP statuses are shared with ZIP fetching. Use explicit exceptions for control
flow, not `assert` (removed by `-O`).

Remote values have caps: title 512 characters, size 64, URL 8192, fingerprint 1024
and metadata values 512. Reject C0/C1 controls in structural fields. Descriptions
longer than 65,536 Python characters (after trimming) are discarded, not truncated;
this is a character limit, not a UTF-8 byte cap. Accepted descriptions are stripped
of ANSI sequences and C0/C1 controls except TAB/LF/CR before consumers see them.
A missing or discarded description gets a placeholder in the processing pipeline.

Check-in update URLs must use HTTPS on exactly `android.googleapis.com` under
`/packages/ota/` or `/packages/ota-api/`, with no ports, userinfo or controls.
Shared URL validation rejects explicit empty ports and `.`/`..` path segments,
including percent-encoded segments. Do not replace it with a raw prefix match.

### ZIP metadata

Use absolute HTTP ranges (Google rejects suffix ranges). Probe size with
`bytes=0-0`, read EOCD, central directory and the chosen member; support ZIP64.
Require 206 responses and exact Content-Range/Content-Length agreement. Follow
at most five redirects, restricted to `android.googleapis.com`, `gvt1.com` and
subdomains of `gvt1.com` under `/packages/` using the shared HTTPS checks.

Cap compressed/decompressed members at 1 MiB and the central directory at 16 MiB.
Bound inflate output, check `unconsumed_tail`, and verify CRC-32. Validate framing
for every central-directory record but defer deep extra-field/ZIP64 validation
to the requested member. For duplicate names choose the **last** record and apply
deep validation/caps to it, not a shadowed decoy. Accept 1–3 trailing extra-field
padding bytes; still reject any field whose declared body overruns its area.

## Telegram text and terminal rendering

### Pure text pipeline (`message_text.py`)

First normalize CRLF/lone CR to LF and TAB to space, then scan for unsafe controls.
Keep the five sanitization stages in order:

1. Bold bare section headers while the source HTML structure is still intact.
2. Replace `<br>` using `r"<\s*br\s*/?\s*>[^\S\n]*\n?"`: consume inline whitespace
   and at most one trailing newline, preserving intentional blank lines.
3. Normalize structural tags: headings to bold, blocks/lists to line boundaries,
   list items to bullets, `strong` to `b`; strip presentation tags and original
   anchors while retaining text.
4. Normalize Unicode bullets to `- `.
5. Collapse excess blank lines, clean URL-in-parentheses and trim whitespace.

Canonicalize into balanced supported tags and per-character escaped fragments.
Decode entities exactly once and re-escape canonically; entity-encoded controls
fail canonicalization. Fit by **rendered UTF-16 units**, not raw HTML length.
Truncate only at token boundaries, close open tags and append `...`; never split
tags or entities. Uncanonicalizable content degrades to escaped plain text rather
than dropping the notification; whitespace-only content fails closed.

### Delivery and long descriptions (`telegram.py`)

Telegram's limit is 4096; the implementation uses `MAX_LEN = 4090`.
`DESC_SECTION_RE` identifies the description between `<b>Title:</b>` and
`\n\n?<b>Size:</b>`. The optional second newline handles sanitization collapsing
blank lines. Prefer a sentence boundary when at least half the fitted description
survives and the cut is outside tags; otherwise retain token-granular fitting.

Long descriptions may get a Telegraph page and a “Read full changelogs” link.
Escape its href and reject whitespace/control-containing URLs so they cannot
unbalance markup. If description fitting or final fitting still fails, degrade to
escaped plain text rather than dropping the message or retaining only a bare
link. Redact bot tokens from exception text: requests URLs embed `bot<TOKEN>`.

### Terminal (`description.py`, `logging.py`)

Bold bare headers before HTML parsing. `_empty_br_count` distinguishes content
breaks from empty breaks and inserts a blank only on the first empty `<br>`.
Flush on `</b>` before clearing bold so ANSI formatting covers the intended text.

Normalize CRLF, escape lone CR, and decode entities once. Sanitize terminal and
log controls/ANSI, including the direct-fingerprint dry-run fallback. Derive list
indentation from depth but cap the rendered indent at 16; retain full nesting
state for correct closing tags without quadratic output growth.
