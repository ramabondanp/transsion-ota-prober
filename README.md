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
- itel A300+ (A681L)
- itel P65 (P671L)
- itel P70 (P673L)
- itel S25 (S685LN)
- itel S25 Ultra (S686LN)
- itel Super 26 Ultra (S688LN)
- itel CITY 200 (C681L)

## XPAD

- Infinix XPAD (X1101)
- Infinix XPAD 20 Pro (X1201)
- Infinix XPAD 30 Pro (X1103)

## HOT SERIES

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

Requires Python 3.10+. From the repository root:

```bash
uv venv
uv pip install -e .
source .venv/bin/activate
```

Source installs keep configs and `processed_updates.txt` in the repository.
Wheels bundle configs and vendor dependencies; configs are seeded without overwriting
existing files to `$XDG_CONFIG_HOME/checkota/configs` (default
`~/.config/checkota/configs`), with state in `$XDG_STATE_HOME/checkota` (default
`~/.local/state/checkota`). XDG paths must be absolute.

## Telegram

Set these environment variables before a notifying run:

```bash
export bot_token="YOUR_BOT_TOKEN"
export chat_id="YOUR_CHAT_ID"
```

- `telegraph_token` is optional for publishing long changelogs to Telegra.ph.
- Notifications are enabled by default; missing `bot_token` or `chat_id` causes exit 2.
  `--dry-run`, `--skip-telegram`, `--register-update`, `--update-incremental`, and
  `--gen-fp` do not require them.
- Config updates without Telegram still record processed titles; dry runs write nothing.
- File-backed notifying runs keep pending messages in `processed_updates.txt.outbox/`
  beside the state file. Later notifying runs retry recoverable entries. Delivery is
  at least once, so a crash can cause duplicates. Dry runs do not recover or send them.

## Usage

```bash
checkota -c X6873 --dry-run                       # Preview without changes
checkota -c X6873                                 # Check and notify
checkota -d configs/ --jobs 4 --timeout 600        # Check all configs
checkota -d configs/ --reg OP-M1                   # Filter by region
checkota -c X6873 --update-incremental             # Update config without Telegram
checkota -c X6873 --fetch-zip-proxy                # Use proxy env for ZIP metadata
checkota --help                                   # All options
```

`-c X6873` selects `config-X6873.yml`. For wheel installs, `-d configs/` uses the
seeded config directory unless a local `configs/` directory exists.

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

Each region supplies an incremental or a mapping of overrides. Product, device, and
canonical build tags are derived automatically. Region codes use uppercase letters,
digits, and hyphens, with no leading hyphen (for example, `OP-M1`). Use block-style
mappings and single-line values; flow collections, block scalars, anchors, and aliases
are unsupported.

Updates preserve comments and formatting; checks with no new build leave files untouched.

<details>
<summary>Migrating legacy configs</summary>

Legacy `variants` and single-region `product`/`device` configs require migration.
Back up custom configs, then run from a checkout:

```bash
python scripts/migrate_compact_configs.py --write ~/.config/checkota/configs
```

Replace the path if your configs are elsewhere, including a custom `XDG_CONFIG_HOME`.

</details>

## Proxy scripts

`scripts/fetch_spys.py` and `scripts/check_update_proxy.py` provide proxy validation
and update checks. They read credentials from the git-ignored `scripts/.env`:
keep it private (`chmod 600 scripts/.env`) and never commit it.

## Credits and license

Built on [google-ota-prober](https://github.com/tangalbert919/google-ota-prober)
by tangalbert919. The trimmed vendor copy's pinned commit and attribution are in
[`VERSION`](vendor/google-ota-prober/VERSION) and
[`ATTRIBUTION`](vendor/google-ota-prober/ATTRIBUTION).

This repository currently grants no public redistribution rights; see
[`LICENSE`](LICENSE) and [`NOTICE`](NOTICE). Public wheel publication remains gated
on confirming the vendored code's redistribution terms.
