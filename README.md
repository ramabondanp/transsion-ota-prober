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

```bash
pip install -e apps/checkota/
```

This installs the `checkota` command and its dependencies.

> **Note:** Install editable (`-e`) or run from the source tree. The vendored
> `google-ota-prober` lives at the repo root (`vendor/`), outside the package, so a
> plain wheel install cannot bundle it. To relocate the vendored tree, set
> `CHECKOTA_VENDOR_DIR` to its path.

## Run

After install, use the `checkota` command directly:

```bash
# Single config (bare codename resolves to configs/config-<codename>.yml)
checkota -c X6873

# All configs in parallel (4 jobs)
checkota -d apps/checkota/configs/ --jobs 4

# Dry run
checkota -c X6873 --dry-run

# Direct fingerprint
checkota --fp "Infinix/X6873-OP/Infinix-X6873:16/BP2A..."

# Cap overall runtime (signals in-flight requests to stop, then exits)
checkota -d apps/checkota/configs/ --jobs 4 --timeout 600
```

Telegram env vars: `bot_token`, `chat_id`, `telegraph_token` (for long descriptions).

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
