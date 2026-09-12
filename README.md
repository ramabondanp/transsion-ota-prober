# transsion-ota-prober

OTA firmware update checker for Transsion Holdings devices (TECNO, Infinix, itel).
Queries Google's Android check-in servers using protobuf-based requests and optionally
sends Telegram notifications.

## List of Tracked Transsion Devices

## PHANTOM SERIES

- TECNO PHANTOM V Fold 5G (AD10)
- TECNO PHANTOM V Flip 5G (AD11)
- TECNO PHANTOM V Fold2 5G (AE10)
- TECNO PHANTOM V Flip2 5G (AE11)

## CAMON SERIES

- TECNO CAMON 30 4G (CL6)
- TECNO CAMON 30 4G (k) (CL6k)
- TECNO CAMON 30 5G (CL7)
- TECNO CAMON 30 Pro 5G (CL8)
- TECNO CAMON 30 Premier 5G (CL9)
- TECNO CAMON 30S (CLA5)
- TECNO CAMON 30S Pro (CLA6)
- TECNO CAMON 40 4G (CM5)
- TECNO CAMON 40 Pro 4G (CM6)
- TECNO CAMON 40 Pro 5G (CM7)
- TECNO CAMON 40 Premier 5G (CM8)
- TECNO CAMON 50 4G (CN5)
- TECNO CAMON 50 Pro 4G (CN5c)
- TECNO CAMON Slim (CN6c)
- TECNO CAMON 50 Ultra 5G / Pro 5G (CN7c)

## SPARK SERIES

- TECNO SPARK Go 1 (KL4)
- TECNO SPARK 20 (KJ5)
- TECNO SPARK 20 Pro (KJ6)
- TECNO SPARK 20 Pro+ (KJ7)
- TECNO SPARK 20 Pro 5G (KJ8)
- TECNO SPARK 30C (KL5)
- TECNO SPARK 30 4G (KL6)
- TECNO SPARK 30 Pro (KL7)
- TECNO SPARK 30 5G (KL8)
- TECNO SPARK 30C 5G (KL8H)
- TECNO SPARK Go 2 (KM4)
- TECNO SPARK 40 / 40S (KM5)
- TECNO SPARK 40 Pro (KM6)
- TECNO SPARK 40 Pro+ (KM7)
- TECNO SPARK Slim (KM7k)
- TECNO SPARK Go 5G (KM8)
- TECNO SPARK 40 5G (KM8n)
- TECNO SPARK Go 3 (KN3)
- TECNO SPARK 50 4G (KN4)
- TECNO SPARK 50 5G (KN8)

## POVA SERIES

- TECNO POVA 6 Neo (LI6)
- TECNO POVA 6 (LI7)
- TECNO POVA 6 Pro (LI9)
- TECNO POVA 7 (LJ6)
- TECNO POVA 7 5G (LJ7)
- TECNO POVA 7 Pro 5G (LJ8)
- TECNO POVA Curve 5G (LJ8k)
- TECNO POVA 7 Ultra 5G (LJ9)
- TECNO POVA Slim 5G (KM9)
- TECNO POVA Curve 2 5G (LK7k)
- TECNO POVA 8 Pro 5G (LK7)

## MEGAPAD

- TECNO MEGAPAD 11 (T1101)
- TECNO MEGAPAD SE (T1102)
- TECNO MegaPad Pro (T1201)
- TECNO MEGAPAD 2 (T1103)

## ITEL

- itel A80 (A671LC)
- itel A95 5G / itel ZENO 5G+ (A671N)
- itel P55 5G (P661N)
- itel P65 (P671L)
- itel P70 (P673L)
- itel RS4 (S666LN)
- itel S25 (S685LN)
- itel S25 Ultra (S686LN)
- itel Super 26 Ultra (S688LN)
- itel VistaTab 30 Pro (P13001L)
- itel CITY 200 (C681L)

## XPAD

- Infinix XPAD (X1101)
- Infinix XPAD 20 Pro (X1201)
- Infinix XPAD GT SD888 (X1301)
- Infinix XPAD 30 Pro (X1103)

## HOT SERIES

- Infinix HOT 40i (X6528) (X6528B)
- Infinix HOT 40 (X6836)
- Infinix HOT 40 Pro (X6837)
- Infinix HOT 50i (X6531) (X6531B)
- Infinix HOT 50 5G (X6720B)
- Infinix HOT 50 Pro+ (X6880)
- Infinix HOT 50 Pro (X6881)
- Infinix HOT 50 (X6882)
- Infinix HOT 60i (X6728)
- Infinix HOT 60i 5G (X6730B)
- Infinix HOT 60 5G (X6726B)
- Infinix HOT 60 Pro (X6885)
- Infinix HOT 60 Pro+ (X6886)
- Infinix HOT 70 (X6895B)
- Infinix HOT 70 Pro 5G (X6896)

## ZERO SERIES

- Infinix ZERO 30 5G (X6731)
- Infinix ZERO 30 4G (X6731B)
- Infinix ZERO 40 4G (X6860)
- Infinix ZERO 40 5G (X6861)
- Infinix ZERO Flip (X6962)

## GT SERIES

- Infinix GT 20 Pro (X6871)
- Infinix GT 30 Pro (X6873)
- Infinix GT 30 (X6876)
- Infinix GT 50 Pro (X6891)

## NOTE SERIES

- Infinix NOTE 40X 5G (X6838)
- Infinix NOTE 40 Pro (X6850)
- Infinix NOTE 40S (X6850B)
- Infinix NOTE 40 Pro 5G (X6851)
- Infinix NOTE 40 Pro+ 5G (X6851B)
- Infinix NOTE 40 5G (X6852)
- Infinix NOTE 40 (X6853)
- Infinix NOTE 50 Pro 4G (X6855)
- Infinix NOTE 50 Pro+ 5G (X6856)
- Infinix NOTE 50X 5G (X6857) (X6857B)
- Infinix NOTE 50 4G (X6858)
- Infinix NOTE 50s 5G (X6870)
- Infinix NOTE Edge (X6887)
- Infinix NOTE 60 Pro 5G SD7sG4 (X6878)
- Infinix NOTE 60 5G (X6879)
- Infinix NOTE 60 Ultra (X6877)

## SMART SERIES

- Infinix SMART 10 (X6725)
- Infinix SMART 10 PLUS (X6725C)
- Infinix SMART 20 (X6840)

## Install

From a source checkout, an editable install keeps configs and processed-update state
in the repository:

```bash
pip install -e .
```

Regular wheels are self-contained and include the pinned protobuf vendor tree and
default YAML configs:

```bash
pip install dist/checkota-*.whl
```

For a wheel install, configs are copied when bundled config lookup is first needed to
`$XDG_CONFIG_HOME/checkota/configs` (or `~/.config/checkota/configs`) and missing
defaults are added without overwriting existing user files. Processed-update state
is stored in `$XDG_STATE_HOME/checkota` (or `~/.local/state/checkota`). Relative XDG
variables are ignored in favor of the home-directory defaults. Source checkouts
continue to use repository-local configs and `processed_updates.txt`. Wheel installs
do not migrate `processed_updates.txt` from the current working directory.

`CHECKOTA_VENDOR_DIR` remains an explicit override for a relocated vendor tree; a
source checkout with a valid override continues to use repository-local configs and
state even when the default `vendor/google-ota-prober/` directory has moved. The
vendored implementation's attribution and version metadata are included in the wheel.
Public wheel publication remains license-gated until redistribution terms are
confirmed.

## Run

After install, use the `checkota` command directly:

```bash
# Single config (bare codename resolves to configs/config-<codename>.yml)
checkota -c X6873

# All configs in parallel (4 jobs)
checkota -d configs/ --jobs 4

# Dry run
checkota -c X6873 --dry-run

# Use proxy environment variables when fetching OTA ZIP metadata
checkota -c X6873 --fetch-zip-proxy

# Direct fingerprint
checkota --fp "Infinix/X6873-OP/Infinix-X6873:16/BP2A..."

# Cap overall runtime (signals in-flight requests to stop, then exits)
checkota -d configs/ --jobs 4 --timeout 600
```

With a wheel install, the documented `-d configs/` path selects the seeded XDG config
directory when no `configs/` directory exists in the current working directory. Any
explicit directory that already exists is used as given.

## Config Format

The runtime supports only the compact `regions` schema. Shared identity and defaults are
stored once; product, device, and canonical build tags are derived at runtime:

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

Updates rewrite the file line by line to preserve comments, quoting, and newline style, so
a config must stay expressible that way. Loading rejects anything the updater could not
rewrite — flow-style collections (`regions: {OP: "1"}`, `OP: {incremental: "1"}`),
multi-line scalars, literal/folded block scalars (`|`, `>`), and YAML anchors or aliases —
rather than accepting a config that reads fine but can never be updated. Region codes are
restricted to `[A-Z0-9-]` (uppercase, no leading hyphen) because the code is concatenated
into `product` and from there into the check-in fingerprint.

A check that finds no new build never touches the file. When an update does land and every
region has converged on the same Android version, the top-level `android_version` default
is promoted and the now-redundant per-region overrides are dropped; comments attached to
removed keys are re-indented to the region key and kept above it.

For example, this former `variants` representation is legacy input and is no longer
accepted:

```yaml
oem: "Infinix"
device: "Infinix-X6873"
model: "Infinix GT 30 Pro"
variants:
  - variant: "Global"
    product: "X6873-OP"
    android_version: "16"
    build_tag: "BP2A.250605.031.A3"
    incremental: "201500011"
  - variant: "Europe"
    product: "X6873-EU"
    android_version: "15"
    build_tag: "AP3A.240905.015.A2"
    incremental: "131015"
```

Its supported compact equivalent is the example above. Loading a config with `variants`
reports that it `uses the legacy 'variants' schema; migrate it to a 'regions' mapping`.
Legacy single-region files containing `product` or `device` are likewise rejected with
instructions to migrate to `product_base` and `regions`.

An existing wheel installation's XDG configs are not overwritten when new bundled defaults
are installed, so XDG configs that still use `variants` require manual migration. From a
repository checkout, run the migration tool against that config directory, for example:

```bash
python scripts/migrate_compact_configs.py --write ~/.config/checkota/configs
```

If `XDG_CONFIG_HOME` is set to an absolute path, use its `checkota/configs` directory
instead. Back up locally customized configs before rewriting them.

Telegram env vars:

- `bot_token`, `chat_id` — required for Telegram notifications
- `telegraph_token` — optional; only needed to create Telegra.ph pages for very long
  changelogs (when the message exceeds 4090 characters)

## Operational scripts

The geo-proxy batch tools (`scripts/fetch_spys.py`, `scripts/check_update_proxy.py`) provide
ad-hoc proxy validation and update verification.
The scripts load a paid proxy template from `scripts/.env`, which is git-ignored and must
not be committed. Keep that file mode `0600` (`chmod 600 scripts/.env`) because it contains
live proxy credentials.

## Credits

checkota builds on
<https://github.com/tangalbert919/google-ota-prober> — full credit for the original
Android check-in protobuf request/response handling.

A trimmed, pinned copy lives in `vendor/google-ota-prober/`:

- Pinned upstream commit: see `vendor/google-ota-prober/VERSION`
- Scope and license notes: see `vendor/google-ota-prober/ATTRIBUTION`

Only the compiled protobuf modules (`checkin/`), the `.proto` sources, and
`utils/functions.py` are vendored — `checkota` does not use `probe.py`, `gui.py`,
or the original `config.yml` format.

## License and third-party notices

The original code in this repository currently carries no public redistribution
grant; see `LICENSE`. The vendored google-ota-prober copy is documented in
`NOTICE` and `vendor/google-ota-prober/ATTRIBUTION`. Public wheel publication
remains gated on confirming the upstream terms for that vendored copy.

The repository ships a GitHub Actions workflow (`.github/workflows/ci.yml`) that
runs Ruff, Pyright, and pytest across supported Python versions. Operational
script parsing helpers are covered by `tests/test_scripts.py`.
