# Compact Region Configuration — Full Migration Plan

- **Branch:** `feat/compact-region-configs`
- **Created:** 2026-08-27
- **Status:** Phase 2 complete
- **Decision:** Clean break — the legacy `variants` schema will not be supported by the runtime after migration.

## Purpose

Replace duplicated per-region fingerprint fields with a compact region-oriented schema while preserving every effective OTA check-in fingerprint and all existing safety guarantees.

This document is the implementation checklist and progress log. Check items only after the corresponding code, migration, or validation work is complete.

## Progress summary

- [x] Create and switch to a separate implementation branch: `feat/compact-region-configs`.
- [x] Inventory the current configuration data.
- [x] Agree on a region-oriented schema.
- [x] Decide on a clean break from legacy `variants` configs.
- [x] Confirm that `product` and `device` can be derived for all current effective configs.
- [x] Implement constants and derivation helpers.
- [x] Implement the new parser and validation.
- [x] Replace the legacy variant updater with a region updater.
- [ ] Migrate all repository configs.
- [ ] Update tests and documentation.
- [ ] Complete full validation and migration-equivalence checks.

## Current inventory and verified assumptions

The inventory was generated from all files under `configs/` before migration.

| Item | Current value |
| --- | ---: |
| Config files | 114 |
| Effective config/region entries | 148 |
| Multi-region config files | 18 |
| Single-region config files | 96 |
| Duplicate region codes within a file | 0 |
| Direct `device == oem + "-" + product_base` matches | 139/148 |
| Itel casing exceptions | 9/148 |
| Filename/product-base exception files | 1 |
| Noncanonical Android/build-tag pairs | 2 |

Verified special cases:

1. Itel fingerprints use `oem: "Itel"` but a lowercase device prefix such as `itel-A671N`.
2. `config-X6857.yml` contains `X6857-OP` / `Infinix-X6857` and an India override using `X6857B-IN` / `Infinix-X6857B`.
3. Multi-part region codes such as `OP-M1` must remain intact. Region parsing must continue to use everything after the first `-`.
4. The two current build-tag exceptions are:
   - `config-X1301.yml`: Android 14 with `UKQ1.240826.001`.
   - `config-T1102.yml`: Android 15 with `AQ3A.250226.002`.

## Target schema

### Normal multi-region config

```yaml
oem: "Infinix"
product_base: "X6873"
model: "Infinix GT 30 Pro"
android_version: "16"

regions:
  OP: "201500011"
  IN: "201500011"
  RU: "201500011"
  TR: "201500011"
  EU:
    android_version: "15"
    incremental: "131015"
```

### Single-region config

All configs use the same schema, including single-region devices:

```yaml
oem: "TECNO"
product_base: "KL8"
model: "TECNO SPARK 30 5G"
android_version: "14"

regions:
  OP: "260412V1712"
```

### Region-specific product base

```yaml
oem: "Infinix"
product_base: "X6857"
model: "Infinix NOTE 50X 5G"
android_version: "16"

regions:
  OP: "201500013"
  IN:
    product_base: "X6857B"
    incremental: "201500018"
```

### Noncanonical build-tag override

```yaml
oem: "Itel"
product_base: "T1102"
model: "..."
android_version: "15"

regions:
  OP:
    build_tag: "AQ3A.250226.002"
    incremental: "..."
```

## Schema contract

### Required top-level keys

- `oem`: exact OEM component used in the fingerprint.
- `product_base`: product/device codename before the region suffix.
- `model`: model used in the check-in user agent and notifications.
- `android_version`: default Android version inherited by regions.
- `regions`: non-empty mapping keyed by region code.

### Allowed region forms

Scalar shorthand when only the incremental differs:

```yaml
OP: "201500011"
```

Expanded form when one or more overrides are needed:

```yaml
IN:
  product_base: "X6857B"
  android_version: "16"
  build_tag: "NONSTANDARD.TAG"
  incremental: "201500018"
```

Allowed region mapping keys:

- `incremental` — required in expanded form.
- `product_base` — optional immutable identity override.
- `android_version` — optional default override.
- `build_tag` — optional override for a noncanonical Android/build-tag pair.

### Validation rules

- [x] Reject a missing or empty `regions` mapping.
- [x] Reject the legacy `variants` key with a clear migration error.
- [x] Reject unknown top-level and region-level keys to catch typos.
- [x] Reject non-string identity/build values rather than silently losing leading zeroes.
- [x] Reject empty strings and control characters in fingerprint fields.
- [x] Reject leading/trailing whitespace and fingerprint delimiter characters in resolved fields.
- [x] Reject region keys containing `/`, `:`, control characters, or leading/trailing whitespace.
- [x] Require uppercase region keys and product bases without `-` so region identity round-trips exactly.
- [x] Preserve duplicate-key rejection through `_UniqueKeyLoader`.
- [x] Require an explicit `build_tag` when no canonical mapping exists for an Android version.
- [x] Verify that a region-specific build-tag override is a non-empty string.

## Derivation rules

For each region entry:

```text
effective_product_base = region.product_base or top_level.product_base
product                = effective_product_base + "-" + region_code
device_prefix          = DEVICE_PREFIX_BY_OEM.get(oem, oem)
device                 = device_prefix + "-" + effective_product_base
android_version        = region.android_version or top_level.android_version
build_tag              = region.build_tag or BUILD_TAG_BY_ANDROID[android_version]
incremental            = scalar value or region.incremental
```

Initial constants:

```python
BUILD_TAG_BY_ANDROID = {
    "13": "TP1A.220624.014",
    "14": "UP1A.231005.007",
    "15": "AP3A.240905.015.A2",
    "16": "BP2A.250605.031.A3",
}

DEVICE_PREFIX_BY_OEM = {
    "Itel": "itel",
}
```

Rules:

- `product_base` remains explicit; it is not derived from the filename.
- `device` is never stored in the new schema.
- `product` is never stored in the new schema.
- Canonical `build_tag` values are never stored in configs.
- Region order in YAML defines processing and output order.
- Region codes remain the stable runtime variant identity.

## Runtime model strategy

Keep the existing effective `Config` contract for downstream code. `Config.from_yaml()` resolves compact entries into complete values before they reach the processor or update checker.

Each returned `Config` must still contain:

- `oem`
- derived `product`
- derived `device`
- `model`
- effective `android_version`
- effective `build_tag`
- `incremental`
- a stable region/variant label

Planned compatibility within Python:

- [ ] Keep `Config.fingerprint()` behavior unchanged.
- [ ] Keep direct-fingerprint mode (`--fp`) unchanged.
- [ ] Keep `UpdateChecker` and user-agent construction unchanged.
- [ ] Keep region filtering based on `region_code_from_product()` unchanged.
- [ ] Keep notifications and metadata identity validation unchanged.
- [ ] Use the region code as `Config.variant` for logs and debug artifact names.
- [ ] Stop relying on the old list position for update targeting.

## Implementation phases

### Phase 0 — Establish a migration baseline

- [x] Run the full existing test suite on the branch before code changes.
- [x] Export a temporary pre-migration manifest containing, in original order:
  - source config path,
  - variant/region position,
  - OEM,
  - product,
  - device,
  - Android version,
  - build tag,
  - incremental,
  - model,
  - complete fingerprint.
- [x] Confirm the manifest contains exactly 114 files and 148 effective entries.
- [x] Store the manifest outside tracked config paths so it cannot be accidentally migrated.
- [x] Record the baseline test result and manifest checksum in the progress log.

### Phase 1 — Add constants and pure resolution helpers

Primary files:

- `checkota/constants.py`
- `checkota/manager.py`

Tasks:

- [x] Add `BUILD_TAG_BY_ANDROID`.
- [x] Add `DEVICE_PREFIX_BY_OEM`.
- [x] Add a helper that resolves canonical build tags with an explicit-override fallback.
- [x] Add a helper that derives product and device from OEM, product base, and region.
- [x] Unit-test Android 13–16 mappings.
- [x] Unit-test the Itel lowercase device prefix.
- [x] Unit-test fallback behavior for other OEM names.
- [x] Unit-test unknown Android versions with and without explicit tags.

### Phase 2 — Replace YAML parsing

Primary file:

- `checkota/manager.py`

Tasks:

- [x] Replace `variants` list parsing with `regions` mapping parsing.
- [x] Support scalar incremental shorthand.
- [x] Support expanded region mappings.
- [x] Derive effective product, device, Android version, and build tag.
- [x] Preserve YAML insertion order when producing the `Config` list.
- [x] Attach stable region identity to each resolved `Config`.
- [x] Add strict schema validation and actionable errors that include file and region context.
- [x] Remove legacy variant-name aliases (`name`, `region`, `label`, and list-item `variant`).
- [x] Remove index/label-based ambiguity resolution from update targeting (Phase 3).

Parser test cases:

- [x] Normal scalar region.
- [x] Expanded Android override.
- [x] Product-base override.
- [x] Build-tag override.
- [x] Itel device derivation.
- [x] `OP-M1` region preservation.
- [x] Empty regions mapping.
- [x] Duplicate region key.
- [x] Invalid scalar type.
- [x] Missing expanded `incremental`.
- [x] Unknown key.
- [x] Unknown Android version without a build tag.
- [x] Legacy `variants` rejection.

### Phase 3 — Replace update targeting

Primary file:

- `checkota/manager.py`

The current updater has a high blast radius and participates in config processing, notification gating, and CLI flows. Preserve every existing fail-closed guarantee.

Tasks:

- [x] Derive the target region code from `cfg.product` using `split("-", 1)[1]` behavior.
- [x] Locate the on-disk region by exact mapping key.
- [x] Resolve the current effective region from the latest on-disk YAML.
- [x] Verify the on-disk effective immutable identity still matches `cfg`.
- [x] Verify the target fingerprint identity matches `cfg` before any write.
- [x] Fail closed if the region was removed, renamed, duplicated, or changed incompatibly.
- [x] Remove legacy `_matching_variant_index()` behavior.
- [x] Ensure concurrent region workers continue to serialize through the config lock.

### Phase 4 — Implement compact in-place YAML rewriting

Primary file:

- `checkota/manager.py`

Required behavior:

1. Scalar region, incremental-only update:

```yaml
OP: "OLD"
```

becomes:

```yaml
OP: "NEW"
```

1. Scalar region requiring an Android or tag override is promoted:

```yaml
EU: "OLD"
```

becomes:

```yaml
EU:
  android_version: "15"
  incremental: "NEW"
```

1. Expanded regions are reduced back to scalar form when all overrides become redundant.

Tasks:

- [ ] Map region blocks safely for both scalar and expanded entries.
- [ ] Rewrite scalar incrementals without reserializing the whole file.
- [ ] Promote scalar entries to mappings when required.
- [ ] Rewrite or insert `android_version`, `build_tag`, and `incremental` in expanded entries.
- [ ] Remove `android_version` when it equals the top-level default.
- [ ] Remove `build_tag` when it equals the canonical tag for the effective Android version.
- [ ] Collapse an expanded entry to scalar when no overrides remain.
- [ ] Preserve region-specific `product_base` overrides.
- [ ] Preserve inline comments where possible when promoting or collapsing entries.
- [ ] Preserve quote safety for `#`, `:`, and other YAML-significant characters.
- [ ] Preserve LF/CRLF style and final-newline behavior.
- [ ] Keep temp-file, fsync, chmod, round-trip parse, and atomic `os.replace()` behavior.
- [ ] Keep original files byte-identical on every rejected or failed update.

### Phase 5 — Default Android convergence

To prevent duplication after staggered regional upgrades:

- [ ] After applying an update, compute every region's effective Android version.
- [ ] If all regions now use one Android version, promote that value to top-level `android_version`.
- [ ] Remove now-redundant per-region Android overrides.
- [ ] Recompute canonical tags after promotion.
- [ ] Preserve only genuinely noncanonical per-region build-tag overrides.
- [ ] Do not change the top-level default merely because a temporary majority changed; promote only when all regions converge.
- [ ] Verify that convergence does not alter any effective non-target region fingerprint.

### Phase 6 — Round-trip and concurrency safety

- [ ] Reparse the temporary output with `_UniqueKeyLoader` before publication.
- [ ] Resolve every region before and after the rewrite.
- [ ] Verify the target region matches all target fingerprint values.
- [ ] Verify every non-target region keeps the same complete effective fingerprint.
- [ ] Verify immutable identity for every region remains unchanged.
- [ ] Keep lock cleanup behavior intact.
- [ ] Test two region updates against one config under the existing global pool.
- [ ] Test stale in-memory `Config` objects after another region updates the same file.
- [ ] Test write, parse, chmod, fsync, and replace failures leave the original untouched.

### Phase 7 — Migrate all repository configs

Migration procedure:

- [ ] Write or run a deterministic one-time migration utility that reads the legacy schema independently of the new runtime parser.
- [ ] For each file, preserve original region order.
- [ ] Select the most common effective product base as top-level `product_base`; use original order as the tie-breaker.
- [ ] Select the most common effective Android version as top-level `android_version`; use original order as the tie-breaker.
- [ ] Emit scalar region entries when product base and Android match defaults and the tag is canonical.
- [ ] Emit expanded entries containing only necessary overrides.
- [ ] Omit all canonical build tags.
- [ ] Preserve the X6857B India product-base override.
- [ ] Preserve the X1301 and T1102 build-tag exceptions.
- [ ] Quote every scalar fingerprint value.
- [ ] Use a deterministic key order:
  1. `oem`
  2. `product_base`
  3. `model`
  4. `android_version`
  5. `regions`
- [ ] Use deterministic expanded-region key order:
  1. `product_base`
  2. `android_version`
  3. `build_tag`
  4. `incremental`
- [ ] Ensure all 114 files use the new schema.
- [ ] Ensure no tracked config contains `variants:`, `product:`, `device:`, or a canonical `build_tag:` after migration.

### Phase 8 — Prove migration equivalence

This phase is mandatory before considering the config migration complete.

- [ ] Load every migrated config through the new parser.
- [ ] Generate a post-migration manifest using the same fields and ordering as the baseline.
- [ ] Compare the pre- and post-migration manifests.
- [ ] Require exact equality for all 148 effective entries and complete fingerprints.
- [ ] Investigate every difference; do not approve expected differences without documenting them.
- [ ] Confirm the only structural changes are YAML representation changes.
- [ ] Record the comparison command, result, and final checksum in the progress log.

### Phase 9 — Update tests and fixtures

Primary tests expected to change or gain coverage:

- `tests/test_identity_yaml.py`
- `tests/test_manager_idempotence.py`
- `tests/test_lock_pruning.py`
- `tests/test_cli_main_drain.py`
- any test that writes a legacy config fixture

Tasks:

- [ ] Convert all YAML test fixtures to the new schema unless a test intentionally verifies legacy rejection.
- [ ] Keep direct `Config(...)` tests unchanged unless runtime metadata changes require an update.
- [ ] Add a repository-wide test that loads all bundled configs.
- [ ] Assert there are 114 loadable files and 148 resolved region configs at migration time.
- [ ] Add invariant tests for derived product/device identity.
- [ ] Add invariant tests that canonical build tags are omitted from bundled YAML.
- [ ] Retain all prior identity mismatch, duplicate-key, atomicity, idempotence, permissions, comment, and newline tests.

### Phase 10 — Documentation and user-facing migration notice

Files:

- `AGENTS.md`
- `README.md`
- this plan

Tasks:

- [ ] Replace the legacy config-format section in `AGENTS.md`.
- [ ] Update architecture and data-flow references from variants to regions.
- [ ] Document product, device, and build-tag derivation.
- [ ] Document Itel casing and product-base overrides.
- [ ] Add a concise config-format section to `README.md`.
- [ ] State clearly that existing wheel/XDG configs using `variants` are not automatically overwritten and must be manually migrated.
- [ ] Provide before/after migration examples.
- [ ] Document the error users receive when a legacy config is loaded.

### Phase 11 — Remove obsolete legacy code

- [ ] Remove legacy variant-list block mapping and rewrite helpers.
- [ ] Remove label/index disambiguation used only by the old schema.
- [ ] Remove obsolete tests after equivalent region-schema coverage exists.
- [ ] Search production code, tests, docs, and configs for stale `variants` references.
- [ ] Keep the word `variant` only where it remains a runtime concept or an intentional legacy-error test.
- [ ] Run dead-code and unused-import checks after removal.

### Phase 12 — Final validation

Run diagnostics before builds/tests where applicable.

- [ ] Run LSP diagnostics on all edited Python files.
- [ ] Run `uv run ruff check .`.
- [ ] Run the configured type checker (`uv run pyright` if available in the project environment).
- [ ] Run focused manager/parser/updater tests.
- [ ] Run the full pytest suite.
- [ ] Build the wheel with `uv build`.
- [ ] Inspect wheel contents and confirm all 114 migrated configs are included.
- [ ] Install the wheel into an isolated environment and load representative Infinix, TECNO, and Itel configs.
- [ ] Exercise region filtering, including `OP-M1`.
- [ ] Exercise dry-run and config update paths without making network requests.
- [ ] Run `lens_diagnostics` with `mode=all` and resolve blocking findings.
- [ ] Run GitNexus change detection/impact analysis and review affected flows.
- [ ] Review `git diff --check` and the complete diff.
- [ ] Confirm unrelated untracked files were not modified or staged.

## Acceptance criteria

The migration is complete only when all items below are true:

- [ ] Work remains entirely on `feat/compact-region-configs` until review/merge.
- [ ] All 114 bundled configs use only the new `regions` schema.
- [ ] The new parser resolves exactly 148 effective configs.
- [ ] Every pre-migration effective fingerprint equals its post-migration fingerprint.
- [ ] No runtime path accepts legacy `variants` configs.
- [ ] Legacy configs fail with an actionable migration message.
- [ ] Product and device are always derived rather than stored.
- [ ] Canonical build tags are always derived rather than stored.
- [ ] Itel and X6857B special cases resolve correctly.
- [ ] Region updates preserve non-target fingerprints.
- [ ] Atomicity, identity validation, duplicate-key rejection, comment/newline preservation, and lock safety do not regress.
- [ ] All diagnostics, tests, type checks, lint checks, and packaging checks pass.
- [ ] Documentation reflects only the new supported schema.

## Known risks and mitigations

| Risk | Mitigation |
| --- | --- |
| Existing wheel/XDG configs remain in the old schema | Clean-break behavior is explicit; provide a clear error and manual before/after migration documentation. |
| Derivation changes check-in identity | Compare complete pre/post manifests for every effective config. |
| Itel device casing differs from OEM casing | Central `DEVICE_PREFIX_BY_OEM` mapping plus tests for every Itel config. |
| X6857 India uses another product/device base | Region-level `product_base` override plus migration invariant tests. |
| Build-tag mapping is not universal | Explicit region override and fail clearly for unknown Android versions. |
| Parallel updates touch the same config | Retain per-config locking and verify every non-target region after each rewrite. |
| Default promotion changes lagging regions | Promote only when all effective regions converge; compare all non-target fingerprints before publication. |
| Structural YAML edits damage comments or newline style | Continue line-oriented rewriting and atomic round-trip verification; add focused promotion/collapse tests. |
| GitNexus graph is stale relative to HEAD | Rebuild/reanalyze before final impact review if needed. |

## Progress log

Add dated entries as work proceeds.

### 2026-08-27

- Created branch `feat/compact-region-configs`.
- Inventoried 114 config files and 148 effective entries.
- Confirmed 18 multi-region and 96 single-region files.
- Confirmed no duplicate region codes within a config.
- Confirmed product/device derivation, with an Itel casing map and X6857B region override.
- Identified X1301 and T1102 as the only current noncanonical build-tag cases.
- Selected clean-break migration: the runtime will support only the new `regions` schema.
- Created this migration plan; implementation had not started at creation time.
- Phase 0 baseline complete: `pytest -q` passed (`166 passed`).
- Pre-migration manifest: `/tmp/opencode/pre-migration-manifest.jsonl` (`114` files, `148` effective entries).
- Manifest SHA-256: `50aa62e1eedd9994f1f763a5a9e92d10d7b840717ea3d7af0dab76b391639519`.
- Phase 1 complete: added canonical build-tag and OEM device-prefix constants plus pure resolution/derivation helpers.
- Phase 1 validation: focused tests passed (`26 passed`); full suite passed (`177 passed`).
- Phase 2 complete: replaced runtime YAML loading with strict compact `regions` parsing and stable region identities.
- Phase 2 is intentionally transitional: bundled legacy configs remain unloadable until the repository migration phase and this intermediate commit must not be released independently.
- Phase 2 validation: parser/helper tests passed (`62 passed`); full suite passed (`228 passed`).
- Phase 3 complete: replaced index/label-based update targeting with exact compact region resolution and fail-closed identity checks.
- Phase 3 review: update targeting now also rejects a `Config` whose stable region identity disagrees with its derived product region, and compact no-op behavior has regression coverage.
- Phase 3 validation: compact targeting tests passed (`10 passed`). Compact YAML rewriting remains Phase 4 work.
