#!/usr/bin/env python3
"""Migrate legacy Transsion configs to the compact regions schema."""

from __future__ import annotations

import argparse
import os
import stat
import tempfile
from collections import Counter
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from yaml.constructor import ConstructorError

from checkota.constants import BUILD_TAG_BY_ANDROID, DEVICE_PREFIX_BY_OEM
from checkota.manager import Config, _config_lock


class _UniqueKeyLoader(yaml.SafeLoader):
    """Safe legacy loader that rejects ambiguous duplicate mappings."""


def _construct_unique_mapping(
    loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable key",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


@dataclass(frozen=True)
class LegacyRegion:
    oem: str
    product: str
    product_base: str
    region: str
    device: str
    model: str
    android_version: str
    build_tag: str
    incremental: str


def _quoted(value: str) -> str:
    return yaml.safe_dump(
        value,
        default_style='"',
        default_flow_style=True,
        allow_unicode=True,
    ).rstrip("\r\n")


def _region_key(value: str) -> str:
    if (
        value
        and value.replace("-", "").replace("_", "").isalnum()
        and next(iter(yaml.safe_load(f"{value}: null"))) == value
    ):
        return value
    return _quoted(value)


def _required_string(data: dict[str, Any], key: str, path: Path, index: int) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{path} legacy entry #{index} has invalid {key!r}")
    return value


def load_legacy_regions(path: Path) -> list[LegacyRegion]:
    """Read the old schema without using the new runtime parser."""
    data = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)
    if not isinstance(data, dict):
        raise TypeError(f"{path} must contain a mapping")

    variants = data.get("variants")
    if variants is None:
        entries: list[Any] = [data]
        base: dict[str, Any] = {}
    else:
        if not isinstance(variants, list) or not variants:
            raise ValueError(f"{path} has an invalid variants list")
        base = {key: value for key, value in data.items() if key != "variants"}
        entries = variants

    regions: list[LegacyRegion] = []
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise TypeError(f"{path} legacy entry #{index} must be a mapping")
        effective = {**base, **entry} if variants is not None else dict(entry)
        product = _required_string(effective, "product", path, index)
        if "-" not in product:
            raise ValueError(f"{path} legacy entry #{index} has invalid product")
        product_base, region = product.split("-", 1)
        if not product_base or not region:
            raise ValueError(f"{path} legacy entry #{index} has invalid product")
        regions.append(
            LegacyRegion(
                oem=_required_string(effective, "oem", path, index),
                product=product,
                product_base=product_base,
                region=region,
                device=_required_string(effective, "device", path, index),
                model=_required_string(effective, "model", path, index),
                android_version=_required_string(
                    effective, "android_version", path, index
                ),
                build_tag=_required_string(effective, "build_tag", path, index),
                incremental=_required_string(effective, "incremental", path, index),
            )
        )
    return regions


def _most_common_first(values: list[str]) -> str:
    counts = Counter(values)
    return max(values, key=lambda value: (counts[value], -values.index(value)))


def _uniform(values: list[str], field: str) -> str:
    first = values[0]
    if any(value != first for value in values[1:]):
        raise ValueError(f"legacy {field} differs by region and cannot be represented")
    return first


def migrate_text(regions: list[LegacyRegion]) -> str:
    if not regions:
        raise ValueError("config has no effective regions")

    oem = _uniform([region.oem for region in regions], "oem")
    model = _uniform([region.model for region in regions], "model")
    product_base = _most_common_first([region.product_base for region in regions])
    android_version = _most_common_first(
        [region.android_version for region in regions]
    )
    seen_regions: set[str] = set()
    for region in regions:
        if region.region in seen_regions:
            raise ValueError(f"duplicate legacy region {region.region!r}")
        seen_regions.add(region.region)
        expected_device = (
            f"{DEVICE_PREFIX_BY_OEM.get(region.oem, region.oem)}-"
            f"{region.product_base}"
        )
        if region.device != expected_device:
            raise ValueError(
                f"legacy device {region.device!r} cannot be derived as "
                f"{expected_device!r}"
            )

    lines = [
        f"oem: {_quoted(oem)}",
        f"product_base: {_quoted(product_base)}",
        f"model: {_quoted(model)}",
        f"android_version: {_quoted(android_version)}",
        "regions:",
    ]
    for region in regions:
        canonical = BUILD_TAG_BY_ANDROID.get(region.android_version)
        overrides: list[tuple[str, str]] = []
        if region.product_base != product_base:
            overrides.append(("product_base", region.product_base))
        if region.android_version != android_version:
            overrides.append(("android_version", region.android_version))
        if canonical is None or region.build_tag != canonical:
            overrides.append(("build_tag", region.build_tag))
        overrides.append(("incremental", region.incremental))

        key = _region_key(region.region)
        if len(overrides) == 1:
            lines.append(f"  {key}: {_quoted(region.incremental)}")
            continue
        lines.append(f"  {key}:")
        for name, value in overrides:
            lines.append(f"    {name}: {_quoted(value)}")
    return "\n".join(lines) + "\n"


def _migration_output(path: Path) -> tuple[str, bool]:
    """Return output text and whether a legacy document needs publication."""
    with path.open(encoding="utf-8", newline="") as stream:
        source = stream.read()
    data = yaml.load(source, Loader=_UniqueKeyLoader)
    if isinstance(data, dict) and ({"product_base", "regions"} & data.keys()):
        # A compact-looking document must satisfy the complete runtime schema.
        # In particular, never reinterpret a malformed compact document as a
        # legacy config just because legacy parsing might produce some fields.
        Config.from_yaml(path)
        return source, False
    return migrate_text(load_legacy_regions(path)), True


def migrate_file(path: Path, write: bool = False) -> str:
    if not write:
        output, needs_publication = _migration_output(path)
        if not needs_publication:
            return output
        _validate_output(output)
        return output

    with _config_lock(path):
        output, needs_publication = _migration_output(path)
        if not needs_publication:
            return output
        with _validated_stage(path, output) as staged:
            os.replace(staged, path)
    return output


def _validate_output(output: str) -> None:
    """Validate generated output without writing beside the source config."""
    with tempfile.TemporaryDirectory(prefix="transsion-compact-config-") as directory:
        candidate = Path(directory) / "config.yml"
        candidate.write_bytes(output.encode("utf-8"))
        Config.from_yaml(candidate)


def _stage_validated(path: Path, output: str) -> Path:
    staged = _stage_output(path, output)
    try:
        Config.from_yaml(staged)
    except BaseException:
        staged.unlink(missing_ok=True)
        raise
    return staged


@contextmanager
def _validated_stage(path: Path, output: str):
    """Stage output, validate its exact bytes, and clean it up on exit."""
    staged = _stage_validated(path, output)
    try:
        yield staged
    finally:
        staged.unlink(missing_ok=True)


def _stage_bytes(path: Path, content: bytes) -> Path:
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, stat.S_IMODE(path.stat().st_mode))
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        return temporary
    except BaseException:
        if fd >= 0:
            os.close(fd)
        temporary.unlink(missing_ok=True)
        raise


def _stage_output(path: Path, output: str) -> Path:
    return _stage_bytes(path, output.encode("utf-8"))


def config_paths(config_dir: Path) -> list[Path]:
    return sorted(
        (
            path
            for path in config_dir.iterdir()
            if path.is_file() and path.suffix in (".yml", ".yaml")
        ),
        key=lambda path: (path.name.casefold(), path.name),
    )


def migrate_directory(config_dir: Path, write: bool = False) -> dict[Path, str]:
    """Preflight every config before performing any requested batch writes."""
    paths = config_paths(config_dir)
    if not paths:
        raise ValueError(f"no config files found in {config_dir}")
    outputs = {path: migrate_file(path) for path in paths}
    if write:
        staged: dict[Path, Path] = {}
        backups: dict[Path, Path] = {}
        replaced: list[Path] = []
        preserved_backups: set[Path] = set()
        # Every config lock is held until all staged files have been validated,
        # backups have been captured, and publication (or rollback) completes.
        # Acquiring them in the same order as config_paths prevents deadlocks
        # when another batch migrator is operating on an overlapping directory.
        with ExitStack() as lock_stack:
            for path in paths:
                lock_stack.enter_context(_config_lock(path))
            try:
                # The unlocked pass above is only preflight. Re-read every
                # source after acquiring all locks so a concurrent runtime
                # update is included in the published migration output.
                migration_results = {
                    path: _migration_output(path) for path in paths
                }
                outputs = {
                    path: output
                    for path, (output, _) in migration_results.items()
                }
                for path, (output, needs_publication) in migration_results.items():
                    if not needs_publication:
                        continue
                    temporary = _stage_validated(path, output)
                    staged[path] = temporary
                for path in staged:
                    backups[path] = _stage_bytes(path, path.read_bytes())
                for path, temporary in staged.items():
                    os.replace(temporary, path)
                    replaced.append(path)
            except BaseException as publication_error:
                rollback_errors: list[tuple[Path, Path, BaseException]] = []
                for path in reversed(replaced):
                    backup = backups[path]
                    try:
                        os.replace(backup, path)
                    except BaseException as rollback_error:  # noqa: BLE001 -- a failed restore must never abort the remaining rollbacks
                        preserved_backups.add(backup)
                        rollback_errors.append((path, backup, rollback_error))
                if rollback_errors:
                    details = "; ".join(
                        f"{path}: {error!r}; original retained at {backup}"
                        for path, backup, error in rollback_errors
                    )
                    raise RuntimeError(
                        f"migration publication failed with {publication_error!r}; "
                        f"rollback also failed for {details}"
                    ) from publication_error
                raise
            finally:
                for temporary in staged.values():
                    temporary.unlink(missing_ok=True)
                for backup in backups.values():
                    if backup not in preserved_backups:
                        backup.unlink(missing_ok=True)
    return outputs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config_dir", type=Path)
    parser.add_argument("--write", action="store_true", help="rewrite configs in place")
    args = parser.parse_args()

    outputs = migrate_directory(args.config_dir, write=args.write)
    print(f"migrated {len(outputs)} config files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
