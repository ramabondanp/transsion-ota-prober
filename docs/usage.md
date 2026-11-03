# checkota user guide

**checkota** tracks TECNO, Infinix and itel devices and checks their OTA firmware
updates using Google's protobuf-based Android check-in API. Telegram notifications
are enabled by default; checks fetch OTA ZIP metadata using HTTP range requests,
not the full firmware package. See the [device catalogue](../README.md#list-of-tracked-transsion-devices)
for tracked models and codes.

- [Install](#install) · [Telegram](#telegram) · [Usage](#usage) · [Configs](#configs)
- [Development](#development) · [Credits and license](#credits-and-license)

## Install

Requires Python 3.10+ and [uv](https://docs.astral.sh/uv/). From a checkout:

```bash
uv venv                         # First setup only; reuse an existing .venv
uv pip install -e .
source .venv/bin/activate
```

After activation, use `checkota` or `python -m checkota`.

## Install modes and paths

A source install uses repository-local configs and `processed_updates.txt` state,
with a legacy working-directory fallback for existing state.

A non-editable wheel bundles the default configs and vendor dependencies. On
first config lookup, it seeds missing defaults without overwriting existing
files. Wheel configs live in `$XDG_CONFIG_HOME/checkota/configs` (default
`~/.config/checkota/configs`), and state lives in `$XDG_STATE_HOME/checkota`
(default `~/.local/state/checkota`). Relative XDG values are ignored in favor
of the home-directory defaults. See the [path contracts](architecture.md#install-modes-and-paths)
for details.

## Telegram

Notifications are enabled by default. Set these **lowercase** environment
variables for notifying runs:

```bash
export bot_token="YOUR_BOT_TOKEN"
export chat_id="YOUR_CHAT_ID"
```

Notifying runs exit with code 2 before operational work if either is missing.
`telegraph_token` is optional for publishing long changelogs to Telegra.ph. Keep
credentials private; never commit tokens, `scripts/.env`, pending messages or
debug artifacts. `--dry-run`, `--skip-telegram`, `--register-update`,
`--update-incremental` and `--gen-fp` do not require Telegram credentials;
their [side effects](#modes-and-side-effects) differ.

## Usage

> **Before running:** Normal checks can update configs, record titles and send
> or retry Telegram notifications. `--dry-run` avoids those actions but still
> contacts the network and can create local files. `--skip-telegram` and
> `--update-incremental` can still change configs and state.

```bash
checkota -c X6873 --dry-run            # Check without rewriting or notifying
checkota -c X6873                      # Check and notify (the default)
checkota -d configs/ --jobs 4          # Check a directory in parallel
checkota -d configs/ --reg OP-M1       # Select one region across configs
checkota -c X6873 --update-incremental # Update without Telegram
checkota --help                        # Show all options
```

## Selecting configs and regions

`-c X6873` resolves a bare device code to `config-X6873.yml` in the active
config directory; an existing file path can be supplied instead. For wheel
installs, `-d configs/` uses the seeded directory when no local `configs/`
directory exists. `--reg` skips nonmatching configs in a directory sweep, but
is an error when a single config has no matching region. `--jobs` controls the
worker pool; `--timeout 0` (the default) disables the run watchdog. A separate
notification-drain deadline means `--timeout` is not a strict whole-process
limit. To use proxy environment variables for ZIP metadata, pass
`--fetch-zip-proxy`.

## Modes and side effects

All of these modes can contact remote services. For a file-backed invocation:

| Mode | Config and notification behavior |
| --- | --- |
| Default | May rewrite the config, send notifications and record processed titles. A later notifying run also retries recoverable pending notifications. |
| `--dry-run` | Does not rewrite device configs, record titles or deliver notifications. It can still create directories, seed missing wheel defaults or create a dedup lock; `--debug` can write response artifacts. |
| `--skip-telegram` | May update configs and record new titles, but does not send notifications. |
| `--update-incremental` | May update the config without Telegram, including for a title already in the history. New titles are recorded. |
| `--register-update` | Records a new update title without updating the config or sending a notification. |
| `--gen-fp` | Prints the target fingerprint when an update is available, instead of updating a config, recording its title or notifying. |
| `--force-notify` | Allows resending an already-processed update without rewriting the config for that known title. |

Non-dry-run file-backed modes can inspect and confirm existing outbox entries
even when notifications are disabled. They do not send pending entries unless
notifications are enabled. Normal checks skip already-processed titles before
fetching ZIP metadata, including in dry-run mode; `--update-incremental` and
`--force-notify` explicitly override that action-path skip.

For direct fingerprint checks, `--fp` replaces `-c`/`-d` and does not rewrite a
config. It cannot be combined with `--reg` or `--incremental`; notification
credentials are still required unless a non-notifying mode is selected.

File-backed notifying runs store pending messages in
`processed_updates.txt.outbox/` beside the title history. Delivery is **at least
once**, not exactly once: a crash after Telegram accepts a message can lead to a
duplicate on retry. Dry runs do not recover or deliver pending messages. See the
[delivery contract](architecture.md#durable-outbox) for recovery details.

## Configs

Use the compact `regions` format:

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

The top-level `oem`, `product_base`, `model`, `android_version` and non-empty
`regions` mapping are required. Quote version and incremental values so YAML
loads them as strings. Each region is either a string incremental or a mapping
with a required `incremental` and optional `product_base`, `android_version` or
`build_tag` overrides. Product, device and canonical build tags are derived
automatically; use a region-level `build_tag` only for a noncanonical build.

Region codes use uppercase letters, digits and hyphens, with no leading hyphen
(for example, `OP-M1`). Use block-style mappings and single-line values; flow
collections, block scalars, anchors, aliases and duplicate keys are unsupported.
See the [schema and identity contract](architecture.md#config-schema-and-identity).

Normal updates preserve comments and surrounding formatting; rewritten scalar
values use double quotes. Checks with matching target values leave config files
byte-identical. Legacy migration has different formatting behavior, described
below.

### Migrating legacy configs

Legacy `variants` and single-region `product`/`device` configs require migration;
user configs are not migrated automatically. Back up custom configs first:
migration regenerates legacy YAML and does **not** preserve its comments or
formatting. From a checkout with the package installed, validate the directory
without publishing changes, then explicitly opt in to rewriting:

```bash
python scripts/migrate_compact_configs.py ~/.config/checkota/configs
python scripts/migrate_compact_configs.py --write ~/.config/checkota/configs
```

The first command validates the migration and reports a file count; it does not
print a diff. Replace the path if your configs are elsewhere, including under a
custom `XDG_CONFIG_HOME`. The tool preflights the directory before writing;
already-compact files are validated and left unchanged.

## Regional check-in proxies

Regional proxies are **off by default**. Add `--region-proxy` to use the
configured country proxy for the exact `IN` (India) and `RU` (Russia) regions:

```bash
checkota -c X6873 --reg IN --region-proxy --dry-run
checkota -c X6873 --reg RU --region-proxy --dry-run
```

With that flag, source installs read `PROXMINT_PROXY_TEMPLATE` from the private,
git-ignored `scripts/.env`; an exported value takes precedence. Wheel installs
use the exported setting only. Without the flag, normal check-ins do not read
the template and keep their existing routing even when it is configured.

```dotenv
PROXMINT_PROXY_TEMPLATE="user__cr.{country}:password@gw.proxmint.com:823"
```

Replace the example credentials with your own and keep the file private
(`chmod 600 scripts/.env`). The `{country}` placeholder becomes `in` or `ru`;
`http://` is added if no scheme is given. An explicit `http://` or `https://`
proxy URL is also accepted. `--region-proxy` applies to each selected region,
including parallel sweeps and direct `--fp` checks.

An explicit proxy applicable to the check-in URL (for example, `HTTPS_PROXY` or
`ALL_PROXY`, including their lowercase forms) takes precedence. `NO_PROXY`
exemptions are respected. Other regions and unconfigured installs retain their
existing routing. With `--region-proxy`, an invalid configured template fails
the affected check before any request rather than silently checking directly.

Regional proxies apply **only to check-in requests**. ZIP metadata
continues to be fetched directly by default; `--fetch-zip-proxy` uses explicit
proxy environment settings, not the automatic country proxy. Telegram and
Telegraph routing is unchanged. See the [network contract](architecture.md#check-in).

## Proxy scripts

[`scripts/fetch_spys.py`](../scripts/fetch_spys.py) and
[`scripts/check_update_proxy.py`](../scripts/check_update_proxy.py) provide proxy
validation and update checks. They read credentials from the git-ignored
`scripts/.env`: keep it private (`chmod 600 scripts/.env`) and never commit it.

## Development

See [AGENTS.md](../AGENTS.md) for contributor invariants and the
[development guide](development.md) for setup, tests, lint/type checks and an
isolated wheel smoke check. Detailed contracts and historical failure modes live
in [architecture](architecture.md) and [regression history](regressions.md).
Do not run live OTA or Telegram checks as routine tests: use mocked tests and
temporary state instead.

## Credits and license

Built on [google-ota-prober](https://github.com/tangalbert919/google-ota-prober)
by tangalbert919. The trimmed vendor copy's pinned commit and attribution are in
[`VERSION`](../vendor/google-ota-prober/VERSION) and
[`ATTRIBUTION`](../vendor/google-ota-prober/ATTRIBUTION).

This repository currently grants no public redistribution rights; see
[`LICENSE`](../LICENSE) and [`NOTICE`](../NOTICE). Public wheel publication remains
gated on confirming the vendored code's redistribution terms.
