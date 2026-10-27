# AGENTS.md — checkota contributor instructions

## Purpose and orientation

**checkota** checks OTA firmware updates for TECNO, Infinix and itel devices using
Google's protobuf Android check-in API. The CLI enables Telegram notifications by
default; explicit non-notifying modes are available.

Entry points: `checkota` / `python -m checkota` → `checkota.cli.main`.

| Area | Start here |
| --- | --- |
| CLI, config selection, worker pool | `checkota/cli.py` |
| Update pipeline, metadata cache, notification draining | `checkota/processor.py` |
| Sessions, interrupts, watchdog | `checkota/runtime.py` |
| Config schema, fingerprint identity, atomic YAML rewrites | `checkota/manager.py` |
| Check-in, metadata and ZIP Range fetching | `checkota/update_checker.py`, `metadata.py`, `zip_metadata.py`, `validation.py` |
| Processed titles, claims and durable notification recovery | `checkota/fingerprints.py`, `outbox.py` |
| Telegram text versus delivery I/O | `checkota/message_text.py` versus `telegram.py`; message construction in `notifier.py` |
| Terminal rendering and logs | `checkota/description.py`, `logging.py` |
| Source/wheel paths, lazy config seeding, vendor bootstrap | `checkota/paths.py`, `pyproject.toml` |
| Defaults, tests and operational tooling | `configs/`, `tests/`, `scripts/` |

Read the relevant reference before changing a subsystem:

- [Architecture and behavioral contracts](docs/architecture.md): schema, data flow,
  delivery, concurrency, install modes, network boundaries and text rendering.
- [Development and verification](docs/development.md): targeted tests, safe workflow
  and an isolated wheel smoke check.
- [Regression history](docs/regressions.md): historical failures and fix rationale;
  superseded implementations are not current instructions.
- [README](README.md): user installation, credentials, usage and migration.

## Non-negotiable invariants

1. **Validate identity before actions.** The target fingerprint's `oem`, `product`
   and `device` must match the config before writes, notifications, title
   processing or dry-run action previews. Direct `--dry-run --fp` can print a
   sanitized description before target validation. Resolve the exact region
   against current YAML; fail closed on disagreement. See
   [identity tests](tests/test_identity_yaml.py).
2. **Known processed titles are terminal.** Normal runs skip before ZIP metadata
   fetch and leave configs alone, even if another region is behind. Do not add
   catch-up fetches. Only `--update-incremental` and `--force-notify` override this;
   the latter resends without rewriting a known update. Dry-run is not an
   override. See [known-title/delivery tests](tests/test_notification_outbox.py).
3. **No-op updates are byte-identical.** Return before Android-default convergence
   when target values already match. On a real update, convergence must preserve
   every region's fingerprint and retained comments. See
   [idempotence](tests/test_manager_idempotence.py) and
   [convergence tests](tests/test_compact_android_convergence.py).
4. **Loadable implies updatable.** Accept only the compact `regions` schema and
   source layouts the rewriter supports. Reject duplicate keys. Validate the
   rewritten layout and semantic round-trip before atomic publication; preserve
   comments and newline style, and emit changed scalar values double-quoted. See
   [rewrite safety tests](tests/test_compact_phase6_safety.py).
5. **Never overwrite user configs while seeding.** Stage/fsync complete files,
   then hardlink or atomically rename without replacement. No destination copy,
   destination cleanup, tombstones or O_EXCL-copy fallback. Imports do not seed
   configs; reading absent dedup data does not create state. See
   [path/seeding tests](tests/test_paths.py).
6. **A completed rewrite retains its delivery obligation.** Stage file-backed
   notifications durably before YAML changes, confirm completed rewrites and
   retain pending work through stops/crashes. Remove outbox records only after
   title commit. Ready records survive later config changes; recovery fails
   closed on corrupt/unresolved state. Delivery is at least once, not exactly
   once. Without a notifier, successful new-update actions still record the title;
   dry runs neither record nor deliver. See
   [outbox tests](tests/test_notification_outbox.py) and
   [interrupt-drain tests](tests/test_drain_after_interrupt.py).
7. **Keep shutdown and emergency delivery bounded.** Stop workers, wait for the
   main-owned executor, then close sessions. The watchdog must not call
   `ctx.stop()` or clear the stop event; its emergency drain has a 30s deadline,
   zero delay and at most 30 sends before hard exit 124. See
   [watchdog tests](tests/test_watchdog_deadline.py).
8. **Treat remote data as untrusted.** Preserve shared HTTPS host/path validation,
   redirect restrictions, response/ZIP size caps, bounded inflate and CRC checks.
   Sanitize logs/terminal controls and redact bot tokens. Fit Telegram text by
   rendered UTF-16 units without splitting entities/tags; degrade invalid markup
   to escaped plain text rather than losing the notification. See the
   [network contracts](docs/architecture.md#network-and-untrusted-input) and
   [text contracts](docs/architecture.md#telegram-text-and-terminal-rendering).

## Working conventions

- Region extraction uses `region_code_from_product()` or
  `product.split("-", 1)[1]`, never `split("-")[-1]`: `CN7c-OP-M1` → `OP-M1`.
- Configs use shared defaults plus compact `regions`; derive product/device and
  canonical build tags rather than duplicating them in YAML. Region codes match
  `[A-Z0-9][A-Z0-9-]*`. See the [schema](docs/architecture.md#config-schema-and-identity).
- Add regression tests for behavioral changes. Do not pin repository config or
  target counts: adding/removing a device must not require numeric test updates.
- Preserve unrelated working-tree changes and generated files. Do not casually
  edit vendored/generated protobuf code or remove existing state/build artifacts.
- Do not run live OTA/Telegram checks for routine verification. Normal commands can
  rewrite configs, persist titles and deliver pending messages. `--skip-telegram`
  and `--update-incremental` still mutate data; `--dry-run` still uses the network,
  can create directories/default configs or dedup locks, and writes response
  artifacts with `--debug`. Prefer mocked tests/temp paths.
- Never commit credentials, `scripts/.env`, real outbox contents or debug dumps.
  Notifying runs require `bot_token` and `chat_id` before any operational work;
  non-notifying exceptions are documented in the [README](README.md#telegram).

## Setup and verification

Use Python 3.10+ and `uv` from the repository root. On first setup:

```bash
uv venv
uv pip install -e . pytest ruff pyright
```

Reuse an existing `.venv` rather than recreating it. There is no development
dependency group; `uv sync` alone does not install the verification tools.

```bash
uv run --no-sync ruff check checkota tests scripts
uv run --no-sync pyright checkota scripts
uv run --no-sync python -m pytest -q
```

These match [CI check scopes](.github/workflows/ci.yml); CI covers Python
3.10–3.14. For packaging/vendor/path changes, also run the
[isolated wheel smoke check](docs/development.md#wheel-smoke-check). Do not publish
artifacts: vendored redistribution rights remain gated by [LICENSE](LICENSE) and
[NOTICE](NOTICE).

Before finishing, review the diff, update affected contracts/tests and report the
checks actually run (including failures or unavailable checks). For docs-only
changes, validate links, commands and Markdown; do not claim runtime coverage
from an empty diagnostics cache. Keep detailed implementation explanations and
historical fixes in the linked docs, not in this file.
