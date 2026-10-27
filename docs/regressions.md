# Regression history

This is the historical maintenance record moved out of [AGENTS.md](../AGENTS.md).
It explains why seemingly small implementation choices matter. For **current
contracts**, use [architecture.md](architecture.md); for test selection, use the
[verification guide](development.md#targeted-regression-tests).

Historical implementation names below are not current instructions. In
particular, `checkota.py` was split into the package, `RemoteZip` was replaced by
`zip_metadata.py`, and notification delivery now also uses the durable outbox.
Do not restore superseded implementations merely because they appear here.

## Historical module mapping

The former monolithic `checkota.py` maps to:

- CLI/orchestration → `checkota/cli.py`
- Runtime/watchdog/session ownership → `checkota/runtime.py`
- Update pipeline/cache → `checkota/processor.py`
- `TerminalParser` → `checkota/description.py`
- Notification construction → `checkota/notifier.py`
- Shared dataclasses → `checkota/models.py`
- Vendor/path bootstrap → `checkota/paths.py` and `checkota/__init__.py`

Pure Telegram text helpers were subsequently extracted to `message_text.py`;
`telegram.py` retains historical helper aliases and owns delivery/Telegraph I/O.
The paths below use current modules where possible.

## Config schema, identity and rewriting

| Failure | Relevant files | Fix / rationale |
| --- | --- | --- |
| `OP-M1` parsed as `M1` | `manager.py` | Split the product at the first hyphen, never the last. |
| Cross-device fingerprint poisoned config/notifications | `manager.py`, `processor.py` | Gate all actions on immutable identity; resolve the exact region and fail closed on disagreement with current YAML. |
| Duplicate YAML keys silently used the last value | `manager.py` | `_UniqueKeyLoader` raises `ConstructorError` on duplicates. |
| A crash mid-write corrupted a config | `manager.py` | Stage, flush/fsync, preserve permissions, verify round-trip, then atomically replace. |
| Loadable configs could never be rewritten | `manager.py` | Reject unsupported source layouts on load and before update: flow collections, multiline/block scalars, anchors and aliases. A rewrite failure releases the title claim and suppresses notification. |
| A no-op check rewrote YAML through convergence | `manager.py` | Return before `_converge_android_default` when values already match; normalization happens only on real updates. |
| Collapsing a region orphaned comments at dead indentation | `manager.py` | Re-indent retained comments to the region key and hoist them above it. |
| Region codes accepted spaces, `#`, dots or non-ASCII | `manager.py` | Enforce `[A-Z0-9][A-Z0-9-]*` because region codes become structural fingerprint values. |
| PyYAML wrapped a rewritten scalar across lines | `manager.py`, `scripts/migrate_compact_configs.py` | Dump quoted strings with `width=2**31`; validate rewritten source layout before publishing so the updater cannot emit an unloadable config. |
| A quote inside a plain scalar swallowed its inline comment | `manager.py` | `_comment_start()` enters quote mode only at a scalar-start position via `_starts_scalar_at`; preserve `# keep` in values such as `OP: OLD's # keep`. |
| UTF-8 BOM caused a misleading source-layout error | `manager.py` | Read with `utf-8-sig`, aligning the validator with PyYAML; a rewrite drops the BOM. |
| Config lock files accumulated beside configs | `manager.py` | Prune released locks after a nonblocking probe. A residual unlink race is documented; rewrites remain atomic and idempotent. |

## Processed titles, notifications and recovery

| Failure | Relevant files | Fix / rationale |
| --- | --- | --- |
| `--update-incremental` skipped known titles | `processor.py` | Permit the explicit update override; normal runs still short-circuit known titles. |
| The processed-title file grew without bound | `fingerprints.py` | Retain the newest 2000 titles. |
| State path depended on the working directory | `paths.py` | Centralize source/wheel path resolution; preserve only the documented source-mode legacy CWD fallback. The original fix anchored a repo path in `metadata.py`, which is no longer the path owner. |
| Per-title claim files accumulated forever | `fingerprints.py`, `runtime.py` | At startup, prune committed or more-than-seven-day-old files only after `LOCK_EX\|LOCK_NB` succeeds; skip dry runs. |
| Closed claim handles raised `ValueError` during cleanup | `fingerprints.py` | Check `claim.closed` and tolerate `ValueError` on release. |
| An unterminated final line merged two processed titles | `fingerprints.py` | Terminate the dangling line before append, retaining separate titles through trimming too. |
| Reading missing dedup state created a lock file | `fingerprints.py` | Return before opening the database lock when the data file is absent. |
| Missing Telegram credentials silently skipped delivery after rewriting YAML | `cli.py` | Validate required env in argument validation, before seeding/pruning/network activity; notifying runs exit 2 when credentials are missing. Only documented non-notifying modes bypass. |
| `--skip-telegram` / `--update-incremental` advanced configs without recording titles | `processor.py` | Record new titles when actions succeed without a notifier; dry runs do not record titles or rewrite device configs. |
| Buffered notifications were lost on failure/interrupt | `cli.py` | Drain whenever buffering was possible, on any exit code; main owns and shuts down the executor once. |
| Stop after a completed rewrite dropped its notification | `processor.py`, `cli.py` | Both `-c` and `-d` can buffer after the config step; check pending work after workers stop and drain. Healthy `-c` stays inline; no-config `--fp` remains stoppable. |

### Current durable-delivery extension

The old in-memory buffering fixes are necessary but not sufficient for a process
crash. `outbox.py` now stages file-backed notifications before rewriting configs,
confirms completed rewrites durably, replays recoverable work on later notifying
runs, and removes entries only after title commit. Ready records remain delivery
obligations even if the config changes or disappears. Startup recovery fails
closed on corrupt state or unresolved claims. Delivery is at least once, not
exactly once; dry runs never recover or send pending work.

See [the outbox contract](architecture.md#durable-outbox) and
[`test_notification_outbox.py`](../tests/test_notification_outbox.py).

## Network, metadata and untrusted data

| Failure | Relevant files | Fix / rationale |
| --- | --- | --- |
| OTA fetch hung the whole run | `metadata.py`, `runtime.py` | Historically reduced RemoteZip timeout from 60s to 15s and added a hard-exit watchdog because stop events cannot interrupt stuck socket reads. RemoteZip is now gone; retain bounded I/O and watchdog behavior. |
| A `remotezip` dependency was needed for one member | `zip_metadata.py`, `metadata.py`, `pyproject.toml` | Replace it with ZIP64-aware absolute HTTP Range reads: size probe, EOCD, central directory, entry; Google rejects suffix ranges. |
| Flat 5s retry delays made failures slow | `update_checker.py`, `metadata.py` | Use exponential backoff; current check-in and metadata loops allow three total attempts with 1s and 2s waits, not a third 4s wait. |
| Metadata waiters abandoned valid fetches at 15s | `processor.py` | Wait on the owner's completion event with polling; wake promptly on completion instead of applying an unrelated timeout. |
| Redirects / check-in URLs enabled SSRF | `update_checker.py`, `zip_metadata.py`, `validation.py` | Exact-host HTTPS allowlists; reject check-in redirects and bound ZIP delivery redirects to allowed Google hosts/paths. |
| Response/decompression memory was unbounded | `update_checker.py`, `zip_metadata.py` | Streamed caps: 4 MiB check-in, 1 MiB member; bound inflate output and check `unconsumed_tail`. |
| Check-in structural fields were unbounded | `update_checker.py`, `metadata.py` | Cap title at 512, size at 64, URL at 8192, fingerprint at 1024 and metadata values at 512; reject C0/C1 controls. |
| OTA descriptions contained unbounded/control-byte content | `update_checker.py`, `processor.py` | `_safe_description()` rejects descriptions longer than 65,536 Python characters after trimming (not a byte cap), and removes ANSI/C0/C1 except TAB/LF/CR. Sanitize the direct-fingerprint dry-run fallback, and use a placeholder when description is absent. |
| URL allowlists accepted empty ports and dot segments | `validation.py` | Reject netlocs ending in `:` and literal or percent-encoded `.` / `..` path segments; a raw prefix check is insufficient. |
| Padded ZIP extra fields made metadata unreadable | `zip_metadata.py` | Accept a 1–3 byte trailing alignment remainder, as mainstream readers do; still reject declared field bodies overrunning the area. |
| Duplicate ZIP member names selected the first record | `zip_metadata.py` | Scan all records and choose the last, matching common readers; deep validation/caps apply to that record, not a shadowed decoy. |
| Proxy script timeout flags accepted `nan` / `inf` | `scripts/fetch_spys.py`, `scripts/check_update_proxy.py` | `_positive_float()` requires finite positive values. |

## Telegram and terminal text

| Failure | Relevant files | Fix / rationale |
| --- | --- | --- |
| `DESC_SECTION_RE` failed around the OS line | `constants.py` | Make the affected trailing newline optional (`\n?`). |
| `DESC_SECTION_RE` failed when sanitization collapsed a blank line | `constants.py` | Match `\n\n?` before Size instead of requiring two newlines. |
| `<br>\n` doubled breaks, while greedy `\s*` ate template newlines | `message_text.py` | Use `[^\S\n]*\n?`, consuming inline whitespace and at most one newline. |
| Telegram descriptions lost header hierarchy | `message_text.py` | Bold bare headers before stripping source presentation tags. |
| Terminal output had extra blank lines | `description.py` | Track `_empty_br_count` and insert a blank only on the first empty break. |
| Terminal headers lost bold styling | `description.py` | Flush on `</b>` before clearing bold. |
| Oversized/control-containing payloads caused Telegram API 400 | `message_text.py`, `telegram.py` | Canonicalize entities, enforce rendered UTF-16 length locally and degrade uncanonicalizable markup to escaped plain text. |
| Bot tokens leaked through requests exceptions | `telegram.py` | Redact the token from errors: request URLs embed `bot<TOKEN>`. |
| Terminal/log text accepted control bytes | `logging.py`, `description.py` | Neutralize C0/C1 and ANSI CSI; decode terminal entities exactly once. |
| List/paragraph tags appeared as literal Telegram text | `message_text.py` | Normalize lists, items, paragraphs, blocks and headings; map `strong` to `b`. |
| CRLF descriptions showed literal `\x0d` | `description.py` | Normalize CRLF before parsing; continue escaping lone carriage returns. |
| Telegraph URLs broke markup and dropped the notification | `telegram.py` | Escape the href, reject whitespace/controls, and fall back to escaped plain text if fitting fails rather than dropping content or leaving only a bare link. |
| Documented sentence-boundary truncation was not implemented | `telegram.py` | `_prefer_sentence_boundary()` uses `SENTENCE_BOUNDARY_RE` when the cut stays outside tags and retains at least half the fitted text; otherwise keep the token cut. |
| Deep/unclosed HTML lists made terminal rendering quadratic | `description.py` | Cap rendered indentation at 16 while tracking full list depth so closing tags still pair correctly. |
| One TAB or CR degraded the whole message to literal markup | `message_text.py` | Normalize CRLF/lone CR to LF and TAB to space before the safety scan; upstream intentionally preserves these controls. |

## Concurrency and shutdown

| Failure | Relevant files | Fix / rationale |
| --- | --- | --- |
| Worker exceptions crashed the parallel run | `cli.py` | Catch worker exceptions in buffered execution. |
| Sessions closed while workers were active | `runtime.py`, `cli.py` | On interrupt, signal stop before waiting for workers. In all cases, wait for executor shutdown and finish any enabled drain before closing sessions. |
| Multi-region configs were unnecessarily serial | `cli.py`, `processor.py`, `runtime.py` | Flatten `(config, region)` jobs into one bounded global pool; buffer per-region output and regroup by config. |
| Per-thread connection pools were too small | `runtime.py` | Size adapters with `pool_maxsize = max(10, jobs)`. |
| Watchdog deadlocked during session close | `runtime.py` | Never call `ctx.stop()` from the timer callback; use bounded emergency delivery, flush and hard exit. |
| Watchdog emergency delivery ran hundreds of seconds beyond timeout | `runtime.py`, `processor.py` | Zero inter-send delay, a 30s deadline and at most 30 sends; never clear the shared stop event while workers may still run. |
| Interrupt let freshly fetched metadata proceed into actions | `processor.py` | Recheck stop after fetch and before actions; do not cache successful metadata once stopped. |
| Directory `--reg` failed every config without the region | `processor.py`, `cli.py` | Treat directory filtering as selection, but retain single-config miss errors. Pre-scan for a wholly empty selection; preserve load-error reporting. |

## Packaging and maintenance

| Failure / change | Relevant files | Fix / rationale |
| --- | --- | --- |
| Vendor directory missing after a non-editable install | `paths.py` | Fail loudly on missing vendor code; allow `CHECKOTA_VENDOR_DIR` for an explicit override. |
| Plain wheels lacked vendor modules/default configs | `paths.py`, `pyproject.toml` | Bundle vendor as `checkota._vendor.*` and defaults as `checkota.bundled_configs`; seed wheel-mode XDG configs lazily. |
| Partial seeding / cleanup races lost user configs | `paths.py` | Stage/fsync complete files, then hardlink or atomically rename without replacement. No destination copy, destination cleanup, tombstones, O_EXCL fallback or cleanup allocation after ENOSPC. |
| Locale-dependent Unicode I/O failed | `manager.py`, `fingerprints.py`, `update_checker.py` | Use explicit UTF-8 encodings; configs additionally accept a BOM via `utf-8-sig`. |
| A 989-line monolith obscured ownership | `checkota/*` | Split into focused modules with `cli.main` as the entry point; preserve behavior. |
| Dead Python-version branches complicated execution | Former `checkota.py` | Remove unsupported pre-3.10 guards; `cancel_futures=True` is unconditional. |
| Monolith import ordering needed E402 suppression | Former `checkota.py` | Historical `# ruff: noqa: E402` workaround is no longer needed after extraction. |
| `assert` controlled runtime behavior and disappeared under `-O` | `processor.py`, `update_checker.py` | Raise explicit `RuntimeError` / `UpdateCheckError` for unreachable branches. |

## Maintaining this record

Keep normative rules in `AGENTS.md` and `architecture.md`, not only in this history.
For a new bug, add a focused regression test first and record its failure mode
here when the rationale is non-obvious. Update current module references after
extraction; explicitly label obsolete implementation details as historical.
