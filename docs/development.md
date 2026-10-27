# Development and verification

Start with [AGENTS.md](../AGENTS.md) for invariants and
[architecture.md](architecture.md) for implementation details. User-facing
installation, credentials and usage belong in [README.md](../README.md).

## Safe local setup

Use Python 3.10+ and run from the repository root. Prefer `uv`; the project has no
configured development dependency group, so install the verification tools
explicitly. Do not assume `uv sync` alone installs pytest, Ruff or Pyright.

```bash
uv venv                         # First setup; reuse an existing .venv
uv pip install -e . pytest ruff pyright
```

`uv run --no-sync` below uses the prepared environment without removing those
explicitly installed tools through a project sync.

## Standard checks

These match the scopes in [CI](../.github/workflows/ci.yml), which tests Python
3.10–3.14. CI uses pip for setup; local commands here use uv.

```bash
uv run --no-sync ruff check checkota tests scripts
uv run --no-sync pyright checkota scripts
uv run --no-sync python -m pytest -q
```

Run the checks relevant to a change during development, then the full suite for
code changes. Report which checks ran, their results, and anything unavailable.
An empty editor diagnostics cache is not verification.

For documentation-only edits, check local links/anchors, referenced commands and
paths, Markdown formatting, and the diff. Runtime tests are optional unless the
edit changes executable examples or makes new behavioral claims.

### Targeted regression tests

Use these as starting points, not as a substitute for the full suite or reading
the affected code. Tests mock network boundaries and use temporary state for
failure injection; do not replace them with live OTA/Telegram runs.

| Change area | Relevant tests |
| --- | --- |
| Fingerprint identity, duplicate YAML keys, safe writes | [`test_identity_yaml.py`](../tests/test_identity_yaml.py), [`test_compact_update_targeting.py`](../tests/test_compact_update_targeting.py), [`test_compact_phase6_safety.py`](../tests/test_compact_phase6_safety.py) |
| Schema/layout, region rewriting and no-op preservation | [`test_compact_config_parser.py`](../tests/test_compact_config_parser.py), [`test_compact_region_rewrite.py`](../tests/test_compact_region_rewrite.py), [`test_compact_android_convergence.py`](../tests/test_compact_android_convergence.py), [`test_manager_idempotence.py`](../tests/test_manager_idempotence.py) |
| Repository defaults and migration | [`test_repository_compact_configs.py`](../tests/test_repository_compact_configs.py), [`test_migrate_compact_configs.py`](../tests/test_migrate_compact_configs.py) |
| Known-title short circuit, durable delivery, crash recovery | [`test_notification_outbox.py`](../tests/test_notification_outbox.py), [`test_title_claim_contention.py`](../tests/test_title_claim_contention.py) |
| Stop handling, buffered delivery and watchdog bounds | [`test_cli_main_drain.py`](../tests/test_cli_main_drain.py), [`test_drain_after_interrupt.py`](../tests/test_drain_after_interrupt.py), [`test_watchdog_deadline.py`](../tests/test_watchdog_deadline.py), [`test_pr4_concurrency.py`](../tests/test_pr4_concurrency.py) |
| Metadata cache and retries | [`test_metadata_cache_dedup.py`](../tests/test_metadata_cache_dedup.py), [`test_update_checker_retry.py`](../tests/test_update_checker_retry.py), [`test_zip_metadata_transient_retry.py`](../tests/test_zip_metadata_transient_retry.py) |
| Response/URL/ZIP validation | [`test_update_response_safety.py`](../tests/test_update_response_safety.py), [`test_zip_metadata_hardening.py`](../tests/test_zip_metadata_hardening.py), [`test_audit_hardening.py`](../tests/test_audit_hardening.py) |
| Telegram text, escaping and fitting | [`test_telegram_payload_sanitize.py`](../tests/test_telegram_payload_sanitize.py), [`test_telegram_escape.py`](../tests/test_telegram_escape.py), [`test_telegraph_no_proxy.py`](../tests/test_telegraph_no_proxy.py) |
| Terminal rendering | [`test_description_terminal_wrap.py`](../tests/test_description_terminal_wrap.py), [`test_audit_fixes.py`](../tests/test_audit_fixes.py) |
| Install modes, config seeding and vendor bootstrap | [`test_paths.py`](../tests/test_paths.py), [`test_cli_paths.py`](../tests/test_cli_paths.py), [`test_bootstrap.py`](../tests/test_bootstrap.py) |
| Credential validation, region filtering and scripts | [`test_cli_telegram_env.py`](../tests/test_cli_telegram_env.py), [`test_region_filter.py`](../tests/test_region_filter.py), [`test_scripts.py`](../tests/test_scripts.py) |
| State persistence and lock cleanup | [`test_lock_pruning.py`](../tests/test_lock_pruning.py), [`test_regression_followups.py`](../tests/test_regression_followups.py), [`test_audit_fixes.py`](../tests/test_audit_fixes.py) |

For example:

```bash
uv run --no-sync python -m pytest -q tests/test_notification_outbox.py tests/test_title_claim_contention.py
```

## Avoid operational side effects

- Ordinary check commands can rewrite repository configs, persist processed
  titles, recover/send pending notifications and contact external services.
  Do not run them as a routine test or to regenerate expected fixtures.
- `--skip-telegram` and `--update-incremental` still mutate configs/state.
  `--register-update` explicitly persists state. None is a read-only check.
- `--dry-run` avoids device-config rewrites, title commits and outbox delivery,
  but still performs network work. It can create the wheel state directory,
  seed missing config defaults or create a database lock when reading existing
  dedup data. With `--debug`, it can also write check-in response artifacts.
  It is not a substitute for isolated tests.
- Never use real Telegram credentials or production dedup/outbox state in tests.
  For an explicitly requested live check, use copied configs and isolated state
  as appropriate, and agree on intended writes/delivery first.
- `scripts/.env` contains proxy credentials. Keep it private and untracked; never
  include it, tokens, real pending-message contents or check-in debug dumps in
  commits or diagnostic output.
- Preserve unrelated working-tree files. Review `git status` and the diff before
  and after the change; do not clean build artifacts or state indiscriminately.

## Wheel smoke check

Run this in addition to the relevant tests when changing packaging, vendoring,
bootstrap, resource lookup or install-mode detection. Editable installs and
monkeypatched path tests cannot prove the built wheel is self-contained.

The following Bash example builds locally and installs into a temporary venv.
The runtime probe runs **outside the checkout**, unsets source/vendor overrides,
uses temporary XDG directories, and does not contact OTA or Telegram servers.
It checks lazy seeding, non-overwrite behavior, bundled configs/vendor imports
and that no state is created merely by importing/reading missing dedup data.
Building may create local setuptools artifacts; inspect them rather than deleting
unrelated pre-existing `build/` or `dist/` directories.

```bash
(
  set -euo pipefail
  smoke=$(mktemp -d)
  trap 'rm -rf -- "$smoke"' EXIT

  uv build --wheel --out-dir "$smoke/dist"
  uv venv "$smoke/venv"
  uv pip install --python "$smoke/venv/bin/python" "$smoke"/dist/*.whl

  cd "$smoke"
  env -u CHECKOTA_VENDOR_DIR -u PYTHONPATH \
    XDG_CONFIG_HOME="$smoke/config" XDG_STATE_HOME="$smoke/state" \
    "$smoke/venv/bin/python" - <<'PY'
from importlib import resources
from pathlib import Path
import os

import checkota
from checkota import paths
from checkota.fingerprints import load_processed_titles
from checkota.manager import Config
from checkin import checkin_generator_pb2

root = Path.cwd()
config_dir = Path(os.environ["XDG_CONFIG_HOME"]) / "checkota" / "configs"
state_dir = Path(os.environ["XDG_STATE_HOME"]) / "checkota"
assert not paths.IS_SOURCE_CHECKOUT
assert Path(checkota.__file__).resolve().is_relative_to(root / "venv")
assert paths.VENDOR_DIR == paths.PACKAGED_VENDOR_DIR
assert paths.VENDOR_DIR.is_dir()
assert not config_dir.exists(), "import must not seed configs"
assert not state_dir.exists(), "import must not create state"
assert resources.files("checkota._vendor").joinpath("VERSION").is_file()
assert checkin_generator_pb2.AndroidCheckinRequest() is not None

assert paths.active_config_dir() == config_dir
configs = sorted(config_dir.glob("config-*.yml")) + sorted(config_dir.glob("config-*.yaml"))
assert configs, "wheel must bundle default configs"
for config in configs:
    assert Config.from_yaml(config)

existing = configs[0]
custom = existing.read_bytes() + b"\n# smoke-test user edit\n"
existing.write_bytes(custom)
paths.active_config_dir()
assert existing.read_bytes() == custom, "seeding must not overwrite user edits"

assert paths.STATE_DIR == state_dir
processed = paths.STATE_DIR / paths.PROCESSED_UPDATES_FILE
assert not load_processed_titles(processed)
assert not state_dir.exists(), "reading absent dedup state must not create it"
# Explicit wheel-mode state-path resolution creates the parent directory.
assert paths.processed_updates_path() == processed
assert state_dir.is_dir()
assert not processed.exists()
assert not processed.with_name(processed.name + ".db.lock").exists()
print("Wheel imports, bundled resources, lazy seeding and isolated state: OK")
PY
)
```

This smoke test does not replace cross-platform failure/race tests in
`test_paths.py`, concurrency tests or notification-recovery tests.

Do **not** publish the wheel as part of verification. Public redistribution is
gated on vendored licensing confirmation; see [LICENSE](../LICENSE),
[NOTICE](../NOTICE) and the [README](../README.md#credits-and-license).

## Keeping the documentation useful

- Root `AGENTS.md`: concise instructions, high-priority invariants and verification.
- `architecture.md`: current implementation contracts and subsystem details.
- `regressions.md`: historical failure modes and the rationale behind fixes.
- `README.md`: user installation, configuration and operational usage.

Update the relevant contract alongside behavior changes and add regression tests
for non-obvious failures. Avoid copying full implementation explanations or
historical tables back into the root agent instructions.
