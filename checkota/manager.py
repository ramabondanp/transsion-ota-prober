import contextlib
import os
import re
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode

from checkota.constants import (
    BUILD_TAG_BY_ANDROID,
    DEVICE_PREFIX_BY_OEM,
    REGION_CODE_MAP,
)
from checkota.logging import Log

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    _fcntl = None  # type: ignore[assignment]


def _prune_config_lock(lock_path: Path) -> None:
    """Best-effort removal of a released config lock file.

    Config lock files would otherwise accumulate next to every config ever
    rewritten. The lock is re-acquired non-blocking and the file is unlinked
    only when no other process holds it. Residual race (documented, accepted):
    a process that opened the file just before the unlink proceeds on an
    unlinked inode, so two whole-file rewrites could interleave; both are
    atomic and round-trip verified, and the loser's update is reapplied by
    its next OTA run.
    """
    if _fcntl is None:
        return
    try:
        handle = lock_path.open("a+", encoding="utf-8")
    except OSError:
        return
    try:
        try:
            _fcntl.flock(handle.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        except OSError:
            return  # another process holds the lock; keep the file
        with contextlib.suppress(OSError):
            lock_path.unlink()
    finally:
        with contextlib.suppress(OSError):
            _fcntl.flock(handle.fileno(), _fcntl.LOCK_UN)
        handle.close()


@contextmanager
def _config_lock(config_path: Path):
    lock_path = config_path.with_name(config_path.name + ".lock")
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        if _fcntl is not None:
            _fcntl.flock(handle.fileno(), _fcntl.LOCK_EX)
        yield
    finally:
        if _fcntl is not None:
            _fcntl.flock(handle.fileno(), _fcntl.LOCK_UN)
        handle.close()
        _prune_config_lock(lock_path)


_COMPACT_REQUIRED_KEYS = (
    "oem",
    "product_base",
    "model",
    "android_version",
    "regions",
)
_COMPACT_TOP_LEVEL_KEYS = frozenset(_COMPACT_REQUIRED_KEYS)
_COMPACT_REGION_KEYS = frozenset(
    {"incremental", "product_base", "android_version", "build_tag"}
)
_LEGACY_SINGLE_REGION_KEYS = frozenset(
    {"product", "device", "build_tag", "incremental"}
)
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")


def _region_context(file: Path, region_code: Any) -> str:
    return f"Config {file} region {region_code!r}"


def _validated_config_string(
    value: Any,
    field: str,
    file: Path,
    region_code: Any = None,
    forbidden_chars: str = "",
) -> str:
    context = (
        _region_context(file, region_code)
        if region_code is not None
        else f"Config {file}"
    )
    if not isinstance(value, str):
        raise TypeError(f"{context} {field} must be a string.")
    if not value:
        raise ValueError(f"{context} {field} must not be empty.")
    if value != value.strip():
        raise ValueError(
            f"{context} {field} must not have leading or trailing whitespace."
        )
    if _CONTROL_CHAR_RE.search(value):
        raise ValueError(f"{context} {field} must not contain control characters.")
    if any(char in value for char in forbidden_chars):
        raise ValueError(
            f"{context} {field} must not contain any of {forbidden_chars!r}."
        )
    return value


def _validate_region_code(region_code: Any, context: str) -> None:
    if not isinstance(region_code, str):
        raise TypeError(f"{context} key must be a string.")
    if not region_code:
        raise ValueError(f"{context} key must not be empty.")
    if region_code != region_code.strip():
        raise ValueError(
            f"{context} key must not have leading or trailing whitespace."
        )
    if region_code != region_code.upper():
        raise ValueError(f"{context} key must use uppercase characters.")
    if (
        "/" in region_code
        or ":" in region_code
        or _CONTROL_CHAR_RE.search(region_code)
    ):
        raise ValueError(f"{context} key contains an invalid character.")


@dataclass
class Config:
    build_tag: str
    incremental: str
    android_version: str
    model: str
    device: str
    oem: str
    product: str
    region: str | None = None

    @classmethod
    def from_yaml(cls, file: Path) -> list["Config"]:
        if not file.is_file():
            raise FileNotFoundError(f"Config file not found: {file}")

        try:
            with open(file, encoding="utf-8") as handle:
                data = _load_yaml(handle)
        except (OSError, yaml.YAMLError) as exc:
            raise ValueError(f"Could not read or parse config {file}: {exc}") from exc

        return cls._from_compact_data(data, file)

    @classmethod
    def _from_compact_data(cls, data: Any, file: Path) -> list["Config"]:
        context = f"Config {file}"
        if not isinstance(data, dict):
            raise TypeError(f"{context} must contain a top-level mapping.")

        if "variants" in data:
            raise ValueError(
                f"{context} uses the legacy 'variants' schema; migrate it to a "
                "'regions' mapping."
            )
        legacy_keys = [key for key in data if key in _LEGACY_SINGLE_REGION_KEYS]
        if legacy_keys:
            names = ", ".join(repr(key) for key in legacy_keys)
            raise ValueError(
                f"{context} uses the legacy single-region schema ({names}); "
                "migrate it to 'product_base' and a 'regions' mapping."
            )

        unknown_keys = [key for key in data if key not in _COMPACT_TOP_LEVEL_KEYS]
        if unknown_keys:
            names = ", ".join(repr(key) for key in unknown_keys)
            raise ValueError(f"{context} has unknown top-level key(s): {names}.")

        missing_keys = [key for key in _COMPACT_REQUIRED_KEYS if key not in data]
        if missing_keys:
            names = ", ".join(missing_keys)
            raise ValueError(f"{context} is missing required key(s): {names}.")

        oem = _validated_config_string(data["oem"], "oem", file, forbidden_chars="/:")
        product_base = _validated_config_string(
            data["product_base"], "product_base", file, forbidden_chars="/:-"
        )
        model = _validated_config_string(data["model"], "model", file)
        android_version = _validated_config_string(
            data["android_version"],
            "android_version",
            file,
            forbidden_chars="/:",
        )

        regions = data["regions"]
        if not isinstance(regions, dict):
            raise TypeError(f"{context} 'regions' must be a non-empty mapping.")
        if not regions:
            raise ValueError(f"{context} 'regions' must be a non-empty mapping.")

        configs: list[Config] = []
        for region_code, region_data in regions.items():
            region_context = _region_context(file, region_code)
            _validate_region_code(region_code, region_context)

            if isinstance(region_data, str):
                overrides: dict[str, Any] = {"incremental": region_data}
            elif isinstance(region_data, dict):
                unknown_region_keys = [
                    key for key in region_data if key not in _COMPACT_REGION_KEYS
                ]
                if unknown_region_keys:
                    names = ", ".join(repr(key) for key in unknown_region_keys)
                    raise ValueError(
                        f"{region_context} has unknown key(s): {names}."
                    )
                if "incremental" not in region_data:
                    raise ValueError(
                        f"{region_context} expanded mapping requires 'incremental'."
                    )
                overrides = region_data
            else:
                raise TypeError(
                    f"{region_context} must be a string incremental or a mapping."
                )

            effective_product_base = _validated_config_string(
                overrides.get("product_base", product_base),
                "product_base",
                file,
                region_code,
                forbidden_chars="/:-",
            )
            effective_android_version = _validated_config_string(
                overrides.get("android_version", android_version),
                "android_version",
                file,
                region_code,
                forbidden_chars="/:",
            )
            explicit_build_tag = (
                _validated_config_string(
                    overrides["build_tag"],
                    "build_tag",
                    file,
                    region_code,
                    forbidden_chars="/:",
                )
                if "build_tag" in overrides
                else None
            )
            canonical_build_tag = BUILD_TAG_BY_ANDROID.get(effective_android_version)
            if (
                explicit_build_tag is not None
                and explicit_build_tag == canonical_build_tag
            ):
                raise ValueError(
                    f"{region_context} build_tag is canonical for Android "
                    f"{effective_android_version!r}; omit the override."
                )
            try:
                effective_build_tag = resolve_build_tag(
                    effective_android_version, explicit_build_tag
                )
            except ValueError as exc:
                raise ValueError(f"{region_context}: {exc}") from exc
            incremental = _validated_config_string(
                overrides["incremental"],
                "incremental",
                file,
                region_code,
                forbidden_chars="/:",
            )
            product, device = derive_product_and_device(
                oem, effective_product_base, region_code
            )
            configs.append(
                cls(
                    build_tag=effective_build_tag,
                    incremental=incremental,
                    android_version=effective_android_version,
                    model=model,
                    device=device,
                    oem=oem,
                    product=product,
                    region=region_code,
                )
            )

        return configs

    def fingerprint(self) -> str:
        return (
            f"{self.oem}/{self.product}/{self.device}:"
            f"{self.android_version}/{self.build_tag}/"
            f"{self.incremental}:user/release-keys"
        )


def region_code_from_product(product: str) -> str | None:
    """Extract region code from product name (everything after the first '-')."""
    if not product or "-" not in product:
        return None
    return product.split("-", 1)[1].strip().upper()


def region_from_product(product: str) -> str | None:
    """Get human-readable region name from product name."""
    code = region_code_from_product(product)
    return REGION_CODE_MAP.get(code) if code else None


def resolve_build_tag(
    android_version: str, explicit_build_tag: str | None = None
) -> str:
    """Resolve a canonical build tag, or use an explicit override."""
    if explicit_build_tag is not None:
        return explicit_build_tag

    try:
        return BUILD_TAG_BY_ANDROID[android_version]
    except KeyError as exc:
        raise ValueError(
            f"No canonical build tag is known for Android {android_version!r}; "
            "provide an explicit build_tag."
        ) from exc


def derive_product_and_device(
    oem: str, product_base: str, region_code: str
) -> tuple[str, str]:
    """Derive the product and device identity for one region."""
    product = f"{product_base}-{region_code}"
    device_prefix = DEVICE_PREFIX_BY_OEM.get(oem, oem)
    device = f"{device_prefix}-{product_base}"
    return product, device


_FINGERPRINT_RE = re.compile(
    r"^(?P<oem>[^/]+)/(?P<product>[^/]+)/(?P<device>[^:]+):"
    r"(?P<android_version>[^/]+)/(?P<build_tag>[^/]+)/(?P<incremental>[^:]+):.+$"
)

_IMMUTABLE_IDENTITY_KEYS = ("oem", "product", "device")
_UPDATED_KEYS = ("android_version", "build_tag", "incremental")
_DIRECT_KEY_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?P<key>"
    r"[A-Za-z_][A-Za-z0-9_-]*"
    r"|'(?:[^']|'')*'"
    r'|"(?:[^"\\]|\\.)*"'
    r")[ \t]*:"
)


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _UniqueKeyLoader, node: MappingNode, deep: bool = False
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
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def _load_yaml(stream: Any) -> Any:
    # Identical to yaml.load(stream, Loader=_UniqueKeyLoader); spelled out so
    # the SafeLoader subclass is visible at the call site (no unsafe loader).
    loader = _UniqueKeyLoader(stream)
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()


def parse_fingerprint(fingerprint: str) -> dict[str, str] | None:
    match = _FINGERPRINT_RE.match((fingerprint or "").strip())
    return match.groupdict() if match else None


def _identity_matches(values: dict[str, Any], cfg: Config) -> bool:
    return all(
        key in values
        and values[key] is not None
        and str(values[key]) == str(getattr(cfg, key))
        for key in _IMMUTABLE_IDENTITY_KEYS
    )


def fingerprint_identity_matches_config(cfg: Config, fingerprint: str) -> bool:
    """Return whether a target fingerprint belongs to the current config."""
    parsed = parse_fingerprint(fingerprint)
    return bool(parsed and _identity_matches(parsed, cfg))


def _config_values(config: Config) -> dict[str, str]:
    return {
        key: str(getattr(config, key))
        for key in (*_IMMUTABLE_IDENTITY_KEYS, *_UPDATED_KEYS)
    }


def _effective_region_values(
    data: dict[str, Any], region_code: str, config_path: Path
) -> dict[str, str] | None:
    """Resolve one exact compact region from the latest on-disk YAML."""
    regions = data.get("regions")
    if not isinstance(regions, dict) or region_code not in regions:
        return None

    try:
        configs = Config._from_compact_data(data, config_path)
    except (TypeError, ValueError):
        return None

    matches = [config for config in configs if config.region == region_code]
    if len(matches) != 1:
        return None
    return _config_values(matches[0])


def _line_body_and_ending(line: str) -> tuple[str, str]:
    if line.endswith("\r\n"):
        return line[:-2], "\r\n"
    if line.endswith("\n"):
        return line[:-1], "\n"
    if line.endswith("\r"):
        return line[:-1], "\r"
    return line, ""


def _decoded_direct_key(match: re.Match[str]) -> str | None:
    try:
        key = yaml.safe_load(match.group("key"))
    except yaml.YAMLError:
        return None
    return key if isinstance(key, str) else None


def _direct_key_line(line: str, key: str, indent: int | None = None) -> bool:
    body, _ = _line_body_and_ending(line)
    match = _DIRECT_KEY_RE.match(body)
    return bool(
        match
        and _decoded_direct_key(match) == key
        and (indent is None or len(match.group("indent")) == indent)
    )


def _comment_start(text: str) -> int | None:
    quote: str | None = None
    index = 0
    while index < len(text):
        char = text[index]
        if quote == "'":
            if text.startswith("''", index):
                index += 2
                continue
            if char == "'":
                quote = None
            index += 1
            continue
        if quote == '"':
            if char == "\\":
                index += 2
                continue
            if char == '"':
                quote = None
            index += 1
            continue

        if char in "'\"":
            quote = char
        elif char == "#" and (index == 0 or text[index - 1].isspace()):
            return index
        index += 1
    return None


def _quote_yaml_string(value: str) -> str:
    return yaml.safe_dump(
        str(value), default_style='"', default_flow_style=True, allow_unicode=True
    ).rstrip("\r\n")


def _rewrite_yaml_line(line: str, key: str, value: str) -> str:
    body, newline = _line_body_and_ending(line)
    match = _DIRECT_KEY_RE.match(body)
    if not match or _decoded_direct_key(match) != key:
        return line

    comment_index = _comment_start(body)
    if comment_index is None:
        before_comment, comment = body, ""
    else:
        before_comment, comment = body[:comment_index], body[comment_index:]

    value_part = before_comment[match.end() :]
    leading_length = len(value_part) - len(value_part.lstrip(" \t"))
    leading = value_part[:leading_length]
    value_and_suffix = value_part[leading_length:]
    suffix_length = len(value_and_suffix) - len(value_and_suffix.rstrip(" \t"))
    suffix = (
        value_and_suffix[len(value_and_suffix) - suffix_length :]
        if suffix_length
        else ""
    )

    return (
        f"{body[: match.end()]}{leading}{_quote_yaml_string(value)}"
        f"{suffix}{comment}{newline}"
    )


def update_config_from_fingerprint(
    config_path: Path, cfg: Config, fingerprint: str
) -> bool:
    try:
        with _config_lock(config_path):
            return _update_config_from_fingerprint(config_path, cfg, fingerprint)
    except (OSError, ValueError) as exc:
        Log.w(f"Failed to lock config file {config_path}: {exc}")
        return False


def _update_config_from_fingerprint(
    config_path: Path, cfg: Config, fingerprint: str
) -> bool:
    """Apply a target fingerprint to a config file (lock must be held).

    Pipeline: validate target -> read/parse -> resolve the matching region ->
    rewrite lines -> atomic persist with round-trip verification.
    """
    parsed = parse_fingerprint(fingerprint)
    if not parsed:
        Log.w("No valid target fingerprint available to update configuration.")
        return False
    if not _identity_matches(parsed, cfg):
        Log.w(
            "Target fingerprint identity does not match the current config; "
            "configuration was not updated."
        )
        return False

    updates = {
        "android_version": parsed["android_version"],
        "build_tag": parsed["build_tag"],
        "incremental": parsed["incremental"],
    }

    try:
        with config_path.open("r", encoding="utf-8", newline="") as handle:
            raw_text = handle.read()
    except OSError as exc:
        Log.w(f"Failed to read config file {config_path}: {exc}")
        return False

    try:
        data = _load_yaml(raw_text)
    except yaml.YAMLError as exc:
        Log.w(f"Could not parse config {config_path} before updating: {exc}")
        return False
    if not isinstance(data, dict):
        Log.w(f"Config {config_path} did not parse as a dictionary.")
        return False

    proceed, region_code = _resolve_update_target(data, cfg, config_path)
    if not proceed:
        return False

    if region_code is None:
        Log.w(f"Could not resolve update region in {config_path}.")
        return False

    effective = _effective_region_values(data, region_code, config_path)
    if effective is None or not _identity_matches(effective, cfg):
        Log.w(
            f"Effective config identity does not match {config_path}; "
            "configuration was not updated."
        )
        return False
    try:
        before_configs = Config._from_compact_data(data, config_path)
    except (TypeError, ValueError) as exc:
        Log.w(f"Could not snapshot config {config_path} before updating: {exc}")
        return False
    before_by_region = {
        config.region: config
        for config in before_configs
        if config.region is not None
    }
    if len(before_by_region) != len(before_configs):
        Log.w(f"Could not uniquely identify regions in {config_path}.")
        return False

    already_matches = all(
        key in effective
        and effective[key] is not None
        and str(effective[key]) == str(value)
        for key, value in updates.items()
    )

    lines = raw_text.splitlines(keepends=True)
    newline = _detect_newline(raw_text)
    if not already_matches and not _rewrite_compact_region(
        lines, data, region_code, updates, newline, config_path
    ):
        return False
    if not _converge_android_default(lines, config_path, newline):
        return False

    return _write_updated_config(
        config_path,
        lines,
        raw_text,
        cfg,
        region_code,
        updates,
        before_by_region,
    )


def _detect_newline(raw_text: str) -> str:
    if "\r\n" in raw_text:
        return "\r\n"
    if "\r" in raw_text:
        return "\r"
    return "\n"


def _resolve_update_target(
    data: dict[str, Any], cfg: Config, config_path: Path
) -> tuple[bool, str | None]:
    """Locate the exact compact region targeted by a current Config."""
    region_code = region_code_from_product(cfg.product)
    if region_code is None:
        Log.w(
            f"Could not derive a region code from {cfg.product!r} for {config_path}."
        )
        return False, None
    if cfg.region != region_code:
        Log.w(
            f"Config region identity {cfg.region!r} does not match product region "
            f"{region_code!r} for {config_path}; configuration was not updated."
        )
        return False, None

    regions = data.get("regions")
    if not isinstance(regions, dict):
        Log.w(f"Config {config_path} has no valid 'regions' mapping.")
        return False, None
    if region_code not in regions:
        Log.w(
            f"Could not locate region {region_code!r} in {config_path}; "
            "configuration was not updated."
        )
        return False, None

    equivalent_region_keys = [
        key
        for key in regions
        if isinstance(key, str) and key.upper() == region_code
    ]
    if len(equivalent_region_keys) != 1 or equivalent_region_keys[0] != region_code:
        Log.w(
            f"Region {region_code!r} is duplicated or not an exact mapping key in "
            f"{config_path}; configuration was not updated."
        )
        return False, None

    try:
        configs = Config._from_compact_data(data, config_path)
    except (TypeError, ValueError) as exc:
        Log.w(f"Could not resolve region {region_code!r} in {config_path}: {exc}")
        return False, None

    matches = [config for config in configs if config.region == region_code]
    if len(matches) != 1:
        Log.w(
            f"Could not uniquely resolve region {region_code!r} in {config_path}; "
            "configuration was not updated."
        )
        return False, None
    if not _identity_matches(_config_values(matches[0]), cfg):
        Log.w(
            f"Effective config identity changed before updating {config_path}; "
            "configuration was not updated."
        )
        return False, None
    return True, region_code


def _region_block_span(
    lines: list[str], region_code: str, config_path: Path
) -> tuple[int, int, int, int] | None:
    """Locate a compact region block as (start, end, key indent, child indent)."""
    regions_line_idx = next(
        (
            index
            for index, line in enumerate(lines)
            if _direct_key_line(line, "regions", indent=0)
        ),
        None,
    )
    if regions_line_idx is None:
        Log.w(f"Could not find 'regions' section in {config_path}.")
        return None

    regions_match = _DIRECT_KEY_RE.match(
        _line_body_and_ending(lines[regions_line_idx])[0]
    )
    if regions_match is None:  # pragma: no cover - guarded by _direct_key_line above
        Log.w(f"Could not parse 'regions' section in {config_path}.")
        return None
    regions_indent = len(regions_match.group("indent"))
    region_indent: int | None = None
    region_lines: dict[str, int] = {}
    regions_end = len(lines)

    for index in range(regions_line_idx + 1, len(lines)):
        body, _ = _line_body_and_ending(lines[index])
        if not body.strip() or body.lstrip().startswith("#"):
            continue
        match = _DIRECT_KEY_RE.match(body)
        if match is None:
            continue
        indent = len(match.group("indent"))
        if indent <= regions_indent:
            regions_end = index
            break
        if region_indent is None:
            region_indent = indent
        if indent == region_indent:
            key = _decoded_direct_key(match)
            if key is not None:
                region_lines[key] = index

    start = region_lines.get(region_code)
    if region_indent is None or start is None:
        Log.w(f"Could not map region {region_code!r} in {config_path}.")
        return None

    end = regions_end
    for index in region_lines.values():
        if start < index < end:
            end = index

    child_indent = region_indent + 2
    for index in range(start + 1, end):
        body, _ = _line_body_and_ending(lines[index])
        match = _DIRECT_KEY_RE.match(body)
        if match is not None and len(match.group("indent")) > region_indent:
            child_indent = len(match.group("indent"))
            break

    return start, end, region_indent, child_indent


def _line_value_is_scalar(line: str) -> bool:
    body, _ = _line_body_and_ending(line)
    match = _DIRECT_KEY_RE.match(body)
    if match is None:
        return False
    value = body[match.end() :]
    comment_index = _comment_start(value)
    if comment_index is not None:
        value = value[:comment_index]
    return bool(value.strip())


def _inline_comment(line: str) -> str | None:
    body, _ = _line_body_and_ending(line)
    index = _comment_start(body)
    return body[index:] if index is not None else None


def _comment_line(line: str, indent: int) -> str | None:
    comment = _inline_comment(line)
    if comment is None:
        return None
    _, newline = _line_body_and_ending(line)
    return " " * indent + comment + newline


def _desired_region_values(
    data: dict[str, Any], region_code: str, updates: dict[str, str]
) -> dict[str, str] | None:
    regions = data.get("regions")
    if not isinstance(regions, dict) or region_code not in regions:
        return None

    region_data = regions[region_code]
    if isinstance(region_data, str):
        product_base = data["product_base"]
    elif isinstance(region_data, dict):
        product_base = region_data.get("product_base", data["product_base"])
    else:
        return None

    desired: dict[str, str] = {}
    if product_base != data["product_base"]:
        desired["product_base"] = str(product_base)
    if updates["android_version"] != data["android_version"]:
        desired["android_version"] = updates["android_version"]

    try:
        canonical_build_tag = resolve_build_tag(updates["android_version"])
    except ValueError:
        canonical_build_tag = None
    if canonical_build_tag is None or updates["build_tag"] != canonical_build_tag:
        desired["build_tag"] = updates["build_tag"]
    desired["incremental"] = updates["incremental"]
    return {
        key: desired[key]
        for key in ("product_base", "android_version", "build_tag", "incremental")
        if key in desired
    }


def _rewrite_region_mapping(
    block: list[str],
    region_indent: int,
    child_indent: int,
    desired: dict[str, str],
    newline: str,
) -> list[str]:
    """Update an expanded region while retaining surrounding comments/blanks."""
    had_final_newline = bool(_line_body_and_ending(block[-1])[1])
    present: set[str] = set()
    rewritten: list[str] = [block[0]]
    for line in block[1:]:
        body, _ = _line_body_and_ending(line)
        match = _DIRECT_KEY_RE.match(body)
        if match is None or len(match.group("indent")) != child_indent:
            rewritten.append(line)
            continue

        key = _decoded_direct_key(match)
        if key is not None and key in desired:
            rewritten.append(_rewrite_yaml_line(line, key, desired[key]))
            present.add(key)
            continue

        comment_line = _comment_line(line, child_indent)
        if comment_line is not None:
            rewritten.append(comment_line)

    missing = [key for key in desired if key not in present]
    if missing:
        insert_at = 1
        inserted = [
            " " * child_indent
            + f"{key}: {_quote_yaml_string(desired[key])}{newline}"
            for key in missing
        ]
        rewritten[insert_at:insert_at] = inserted
    if not had_final_newline:
        final_body, _ = _line_body_and_ending(rewritten[-1])
        rewritten[-1] = final_body
    return rewritten


def _collapse_region_mapping(
    block: list[str], region_code: str, region_indent: int, incremental: str
) -> list[str]:
    """Collapse an expanded region to scalar incremental form."""
    had_final_newline = bool(_line_body_and_ending(block[-1])[1])
    region_line = block[0]
    body, line_ending = _line_body_and_ending(region_line)
    comment = _inline_comment(region_line)
    match = _DIRECT_KEY_RE.match(body)
    if match is None:  # pragma: no cover - mapped by _region_block_span
        return block
    scalar_line = _rewrite_yaml_line(
        f"{body[: match.end()]} {line_ending}", region_code, incremental
    )
    scalar_body, _ = _line_body_and_ending(scalar_line)
    if comment is not None:
        scalar_body = f"{scalar_body.rstrip()} {comment}"
    scalar_line = scalar_body + line_ending

    collapsed = [scalar_line]
    for line in block[1:]:
        body, _ = _line_body_and_ending(line)
        if not body.strip() or body.lstrip().startswith("#"):
            collapsed.append(line)
            continue
        match = _DIRECT_KEY_RE.match(body)
        if match is None or len(match.group("indent")) <= region_indent:
            collapsed.append(line)
            continue
        comment_line = _comment_line(line, len(match.group("indent")))
        if comment_line is not None:
            collapsed.append(comment_line)
    if not had_final_newline:
        final_body, _ = _line_body_and_ending(collapsed[-1])
        collapsed[-1] = final_body
    return collapsed


def _rewrite_compact_region(
    lines: list[str],
    data: dict[str, Any],
    region_code: str,
    updates: dict[str, str],
    newline: str,
    config_path: Path,
) -> bool:
    """Rewrite one compact region without reserializing unrelated YAML."""
    span = _region_block_span(lines, region_code, config_path)
    desired = _desired_region_values(data, region_code, updates)
    if span is None or desired is None:
        return False

    start, end, region_indent, child_indent = span
    block = lines[start:end]
    if _line_value_is_scalar(block[0]):
        if tuple(desired) == ("incremental",):
            lines[start] = _rewrite_yaml_line(
                block[0], region_code, desired["incremental"]
            )
            return True

        body, line_ending = _line_body_and_ending(block[0])
        comment = _inline_comment(block[0])
        region_match = _DIRECT_KEY_RE.match(body)
        if region_match is None:
            Log.w(f"Could not parse region {region_code!r} in {config_path}.")
            return False
        region_line = body[: region_match.end()].rstrip()
        if comment:
            region_line += f" {comment}"
        region_line += line_ending or newline
        inserted = [
            " " * child_indent
            + f"{key}: {_quote_yaml_string(desired[key])}{newline}"
            for key in desired
        ]
        if line_ending == "":
            inserted[-1] = inserted[-1][:-len(newline)]
        lines[start:end] = [region_line, *inserted, *block[1:]]
        return True

    if tuple(desired) == ("incremental",):
        lines[start:end] = _collapse_region_mapping(
            block, region_code, region_indent, desired["incremental"]
        )
    else:
        lines[start:end] = _rewrite_region_mapping(
            block, region_indent, child_indent, desired, newline
        )
    return True


def _converge_android_default(
    lines: list[str], config_path: Path, newline: str
) -> bool:
    """Promote one Android version only after all regions converge on it."""
    try:
        projected = _load_yaml("".join(lines))
        configs = Config._from_compact_data(projected, config_path)
    except (TypeError, ValueError, yaml.YAMLError) as exc:
        Log.w(f"Could not resolve rewritten config {config_path}: {exc}")
        return False

    versions = {config.android_version for config in configs}
    if len(versions) != 1:
        return True
    converged_version = next(iter(versions))
    if projected["android_version"] == converged_version:
        return True

    fingerprints_before = {
        config.region: config.fingerprint()
        for config in configs
        if config.region is not None
    }
    if len(fingerprints_before) != len(configs):
        Log.w(f"Could not uniquely identify all regions in {config_path}.")
        return False

    version_line = next(
        (
            index
            for index, line in enumerate(lines)
            if _direct_key_line(line, "android_version", indent=0)
        ),
        None,
    )
    if version_line is None:
        Log.w(f"Could not find top-level android_version in {config_path}.")
        return False
    lines[version_line] = _rewrite_yaml_line(
        lines[version_line], "android_version", converged_version
    )

    projected["android_version"] = converged_version
    for config in configs:
        if config.region is None:
            Log.w(f"Resolved region has no stable identity in {config_path}.")
            return False
        if not _rewrite_compact_region(
            lines,
            projected,
            config.region,
            _config_values(config),
            newline,
            config_path,
        ):
            return False

    try:
        normalized = Config._from_compact_data(
            _load_yaml("".join(lines)), config_path
        )
    except (TypeError, ValueError, yaml.YAMLError) as exc:
        Log.w(f"Could not resolve normalized config {config_path}: {exc}")
        return False
    fingerprints_after = {
        config.region: config.fingerprint()
        for config in normalized
        if config.region is not None
    }
    if fingerprints_after != fingerprints_before:
        Log.w(
            "Android default promotion changed an effective region fingerprint in "
            f"{config_path}."
        )
        return False
    return True


def _write_updated_config(
    config_path: Path,
    lines: list[str],
    raw_text: str,
    cfg: Config,
    region_code: str | None,
    updates: dict[str, str],
    before_by_region: dict[str, Config],
) -> bool:
    """Persist rewritten lines atomically after a round-trip verification."""
    new_text = "".join(lines)
    if new_text == raw_text:
        Log.i(f"{config_path} already matches target fingerprint values.")
        return True

    # Write to a temporary file in the same directory, validate it, then
    # atomically replace the original. A failure at any point leaves the
    # original config untouched.
    tmp_path: Path | None = None
    try:
        # lstat, not stat: if an external tamperer swaps config_path for a
        # symlink mid-write we must not read the symlink target's mode.
        # os.replace() below swaps the directory entry itself, so the final
        # write cannot be redirected through that link either. This narrows
        # the local-tamperer window to a documented threat model; the advisory
        # flock only coordinates cooperative checkota processes.
        original_mode = stat.S_IMODE(config_path.lstat().st_mode)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{config_path.name}.", suffix=".tmp", dir=config_path.parent
        )
        tmp_path = Path(tmp_name)
        try:
            handle = os.fdopen(fd, "w", encoding="utf-8", newline="")
        except BaseException:
            # fdopen takes ownership only when it succeeds. Avoid leaking the
            # mkstemp descriptor on setup failures, including cancellation.
            os.close(fd)
            raise
        with handle:
            handle.write(new_text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, original_mode)
        reparse = _load_yaml(tmp_path.read_text(encoding="utf-8"))
        if not isinstance(reparse, dict):
            raise TypeError(f"Round-trip parse yielded {type(reparse).__name__}")

        reparsed_configs = Config._from_compact_data(reparse, config_path)
        after_by_region = {
            config.region: config
            for config in reparsed_configs
            if config.region is not None
        }
        if len(after_by_region) != len(reparsed_configs):
            raise ValueError("Round-trip parse changed region identities")
        if set(after_by_region) != set(before_by_region):
            raise ValueError("Round-trip parse changed the region set")

        for current_region, before in before_by_region.items():
            after = after_by_region[current_region]
            if (
                after.oem,
                after.product,
                after.device,
            ) != (before.oem, before.product, before.device):
                raise ValueError(
                    f"Round-trip parse changed immutable identity for region "
                    f"{current_region!r}"
                )

        if region_code is None or region_code not in after_by_region:
            raise ValueError("Round-trip parse lost the target region")
        target = after_by_region[region_code]
        if not _identity_matches(_config_values(target), cfg):
            raise ValueError("Round-trip parse changed the effective config identity")
        if not all(
            str(getattr(target, key)) == str(value)
            for key, value in updates.items()
        ):
            raise ValueError(
                "Round-trip parse did not preserve target fingerprint values"
            )
        for current_region, before in before_by_region.items():
            if current_region != region_code:
                after = after_by_region[current_region]
                if after.fingerprint() != before.fingerprint():
                    raise ValueError(
                        f"Round-trip parse changed non-target region "
                        f"{current_region!r}"
                    )

        os.replace(tmp_path, config_path)
        tmp_path = None
    except (OSError, TypeError, ValueError, yaml.YAMLError) as exc:
        Log.w(f"Failed to write updated config {config_path}: {exc}")
        return False
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError as exc:
                Log.w(f"Could not remove temporary file {tmp_path}: {exc}")

    Log.s(
        f"Updated {config_path} -> Android {updates['android_version']}, "
        f"build {updates['build_tag']}, incremental {updates['incremental']}"
    )
    return True
