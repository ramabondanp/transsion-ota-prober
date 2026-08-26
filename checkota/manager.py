import contextlib
import os
import re
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, cast

import yaml
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode

from checkota.constants import REGION_CODE_MAP
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


@dataclass
class Config:
    build_tag: str
    incremental: str
    android_version: str
    model: str
    device: str
    oem: str
    product: str
    variant: str | None = None
    variant_index: int | None = None

    @classmethod
    def _from_dict(
        cls,
        data: dict[str, str],
        variant_name: str | None = None,
        variant_index: int | None = None,
    ) -> "Config":
        field_names = {field.name for field in fields(cls)}
        required_fields = field_names - {"variant", "variant_index"}

        filtered: dict[str, Any] = {
            key: value for key, value in data.items() if key in field_names
        }

        if variant_name:
            filtered["variant"] = variant_name
        if variant_index is not None:
            filtered["variant_index"] = variant_index

        missing = [key for key in required_fields if key not in filtered]
        if missing:
            raise ValueError(
                f"Config missing required fields: {', '.join(sorted(missing))}"
            )

        return cls(**filtered)

    @classmethod
    def from_yaml(cls, file: Path) -> list["Config"]:
        if not file.is_file():
            raise FileNotFoundError(f"Config file not found: {file}")

        try:
            with open(file, encoding="utf-8") as handle:
                data = _load_yaml(handle)
        except (OSError, yaml.YAMLError) as exc:
            raise ValueError(f"Could not read or parse config {file}: {exc}") from exc

        if not isinstance(data, dict):
            raise TypeError("Config file content is not a valid dictionary.")

        variants = data.get("variants")

        if variants is None:
            return [cls._from_dict(data)]

        if not isinstance(variants, list) or not variants:
            raise ValueError("'variants' must be a non-empty list of dictionaries.")

        base = {k: v for k, v in data.items() if k != "variants"}
        configs = []
        for idx, variant in enumerate(variants, start=1):
            if not isinstance(variant, dict):
                raise TypeError(f"Variant entry #{idx} is not a dictionary.")

            merged = {**base, **variant}
            variant_name = (
                variant.get("variant")
                or variant.get("name")
                or variant.get("region")
                or variant.get("label")
                or variant.get("product")
            )
            configs.append(cls._from_dict(merged, variant_name, idx - 1))

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


_FINGERPRINT_RE = re.compile(
    r"^(?P<oem>[^/]+)/(?P<product>[^/]+)/(?P<device>[^:]+):"
    r"(?P<android_version>[^/]+)/(?P<build_tag>[^/]+)/(?P<incremental>[^:]+):.+$"
)

_IMMUTABLE_IDENTITY_KEYS = ("oem", "product", "device")
_UPDATED_KEYS = ("android_version", "build_tag", "incremental")
_DIRECT_KEY_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?P<key>[A-Za-z_][A-Za-z0-9_-]*)[ \t]*:"
)
_SEQUENCE_KEY_RE = re.compile(
    r"^(?P<indent>[ \t]*)-[ \t]+"
    r"(?P<key>[A-Za-z_][A-Za-z0-9_-]*)[ \t]*:"
)
_SEQUENCE_ITEM_RE = re.compile(r"^(?P<indent>[ \t]*)-(?=$|[ \t])")


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


def _effective_variant_values(
    data: dict[str, Any], variant_index: int | None
) -> dict[str, Any] | None:
    if variant_index is None:
        return data

    variants = data.get("variants")
    if not isinstance(variants, list) or not (0 <= variant_index < len(variants)):
        return None
    variant = variants[variant_index]
    if not isinstance(variant, dict):
        return None

    return {
        key: variant[key] if key in variant else data.get(key)
        for key in (*_IMMUTABLE_IDENTITY_KEYS, *_UPDATED_KEYS)
    }


def _matching_variant_index(data: dict[str, Any], cfg: Config) -> int | None:
    variants = data.get("variants")
    if not isinstance(variants, list):
        return None

    identity_matches: list[int] = []
    for index in range(len(variants)):
        effective = _effective_variant_values(data, index)
        if effective is not None and _identity_matches(effective, cfg):
            identity_matches.append(index)

    if len(identity_matches) <= 1:
        return identity_matches[0] if identity_matches else None

    label_matches: list[int] = []
    if cfg.variant is not None:
        for index in identity_matches:
            variant = variants[index]
            if not isinstance(variant, dict):
                continue
            label = (
                variant.get("variant")
                or variant.get("name")
                or variant.get("region")
                or variant.get("label")
                or variant.get("product")
            )
            if label is not None and str(label) == str(cfg.variant):
                label_matches.append(index)

    build_matches = []
    for index in identity_matches:
        effective = _effective_variant_values(data, index)
        if effective is not None and all(
            key in effective
            and effective[key] is not None
            and str(effective[key]) == str(getattr(cfg, key))
            for key in _UPDATED_KEYS
        ):
            build_matches.append(index)

    unique_evidence = {
        matches[0] for matches in (label_matches, build_matches) if len(matches) == 1
    }
    return unique_evidence.pop() if len(unique_evidence) == 1 else None


def _line_body_and_ending(line: str) -> tuple[str, str]:
    if line.endswith("\r\n"):
        return line[:-2], "\r\n"
    if line.endswith("\n"):
        return line[:-1], "\n"
    if line.endswith("\r"):
        return line[:-1], "\r"
    return line, ""


def _direct_key_line(line: str, key: str, indent: int | None = None) -> bool:
    body, _ = _line_body_and_ending(line)
    match = _DIRECT_KEY_RE.match(body)
    return bool(
        match
        and match.group("key") == key
        and (indent is None or len(match.group("indent")) == indent)
    )


def _sequence_item_indent(line: str) -> int | None:
    body, _ = _line_body_and_ending(line)
    match = _SEQUENCE_ITEM_RE.match(body)
    return len(match.group("indent")) if match else None


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
    if not match or match.group("key") != key:
        match = _SEQUENCE_KEY_RE.match(body)
        if not match or match.group("key") != key:
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

    Pipeline: validate target -> read/parse -> resolve the matching variant ->
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

    proceed, match_idx = _resolve_update_target(data, cfg, config_path)
    if not proceed:
        return False

    effective = _effective_variant_values(data, match_idx)
    if effective is None or not _identity_matches(effective, cfg):
        Log.w(
            f"Effective config identity does not match {config_path}; "
            "configuration was not updated."
        )
        return False

    if all(
        key in effective
        and effective[key] is not None
        and str(effective[key]) == str(value)
        for key, value in updates.items()
    ):
        Log.i(f"{config_path} already matches target fingerprint values.")
        return True

    lines = raw_text.splitlines(keepends=True)
    newline = _detect_newline(raw_text)

    variants = data.get("variants")
    if isinstance(variants, list):
        variant_index = cast(int, match_idx)
        if not _rewrite_variant_block(
            lines, len(variants), variant_index, updates, newline, config_path
        ):
            return False
    elif not _rewrite_top_level_keys(lines, updates, config_path):
        return False

    return _write_updated_config(config_path, lines, raw_text, cfg, match_idx, updates)


def _detect_newline(raw_text: str) -> str:
    if "\r\n" in raw_text:
        return "\r\n"
    if "\r" in raw_text:
        return "\r"
    return "\n"


def _resolve_update_target(
    data: dict[str, Any], cfg: Config, config_path: Path
) -> tuple[bool, int | None]:
    """Locate which part of the parsed config the target applies to.

    Returns (proceed, variant_index). variant_index is None for single-variant
    configs; proceed=False means the reason was already logged.
    """
    variants = data.get("variants")
    if isinstance(variants, list):
        match_idx = _matching_variant_index(data, cfg)
        if match_idx is None:
            Log.w(
                f"Could not locate matching variant in {config_path} when updating incremental."
            )
            return False, None
        return True, match_idx
    if "variants" in data:
        Log.w(f"Config {config_path} has an invalid variants section.")
        return False, None
    if not _identity_matches(data, cfg):
        Log.w(
            f"Config identity changed before updating {config_path}; "
            "configuration was not updated."
        )
        return False, None
    return True, None


def _rewrite_variant_block(
    lines: list[str],
    variants_count: int,
    variant_index: int,
    updates: dict[str, str],
    newline: str,
    config_path: Path,
) -> bool:
    """Rewrite (or insert) the target keys inside one variants-list entry."""
    variants_line_idx = next(
        (
            i
            for i, line in enumerate(lines)
            if _direct_key_line(line, "variants", indent=0)
        ),
        None,
    )
    if variants_line_idx is None:
        Log.w(f"Could not find variants section in {config_path}.")
        return False

    variants_indent = len(lines[variants_line_idx]) - len(
        lines[variants_line_idx].lstrip(" ")
    )

    sequence_indent: int | None = None
    variant_lines: list[int] = []
    variants_end_idx = len(lines)
    for i in range(variants_line_idx + 1, len(lines)):
        line = lines[i]
        stripped = line.strip()
        indent = len(line) - len(line.lstrip(" "))
        if not stripped or stripped.startswith("#"):
            continue

        item_indent = _sequence_item_indent(line)
        if sequence_indent is None:
            if item_indent is not None and item_indent >= variants_indent:
                sequence_indent = item_indent
                variant_lines.append(i)
                continue
            if indent <= variants_indent:
                variants_end_idx = i
                break
            continue

        if item_indent == sequence_indent:
            variant_lines.append(i)
            continue
        if indent <= variants_indent:
            variants_end_idx = i
            break

    if sequence_indent is None or len(variant_lines) != variants_count:
        Log.w(f"Failed to map variant blocks in {config_path}.")
        return False

    mapping_indent = _variant_mapping_indent(
        lines, variant_lines, variant_index, variants_end_idx, sequence_indent
    )
    if mapping_indent is None:
        Log.w(f"Failed to locate variant block #{variant_index + 1} in {config_path}.")
        return False

    variant_line_idx = variant_lines[variant_index]
    variant_end_idx = (
        variant_lines[variant_index + 1]
        if variant_index + 1 < len(variant_lines)
        else variants_end_idx
    )

    key_lines: dict[str, int] = {}
    marker_body, _ = _line_body_and_ending(lines[variant_line_idx])
    for key in ("android_version", "build_tag", "incremental"):
        marker_match = _SEQUENCE_KEY_RE.match(marker_body)
        if marker_match is not None and marker_match.group("key") == key:
            key_lines[key] = variant_line_idx
            continue
        line_idx = next(
            (
                i
                for i in range(variant_line_idx + 1, variant_end_idx)
                if _direct_key_line(lines[i], key, indent=mapping_indent)
            ),
            None,
        )
        if line_idx is not None:
            key_lines[key] = line_idx

    for key, line_idx in key_lines.items():
        lines[line_idx] = _rewrite_yaml_line(lines[line_idx], key, updates[key])

    insert_idx = variant_line_idx + 1
    for key in ("android_version", "build_tag", "incremental"):
        if key not in key_lines:
            lines.insert(
                insert_idx,
                " " * mapping_indent
                + f"{key}: {_quote_yaml_string(updates[key])}{newline}",
            )
            insert_idx += 1
    return True


def _variant_mapping_indent(
    lines: list[str],
    variant_lines: list[int],
    variant_index: int,
    variants_end_idx: int,
    sequence_indent: int,
) -> int | None:
    """Find the indentation of the key mappings inside one variant block."""
    variant_line_idx = variant_lines[variant_index]
    variant_end_idx = (
        variant_lines[variant_index + 1]
        if variant_index + 1 < len(variant_lines)
        else variants_end_idx
    )
    marker_body, _ = _line_body_and_ending(lines[variant_line_idx])
    marker_key_match = _SEQUENCE_KEY_RE.match(marker_body)
    if marker_key_match is not None:
        return marker_key_match.start("key")
    return next(
        (
            len(match.group("indent"))
            for line in lines[variant_line_idx + 1 : variant_end_idx]
            if (match := _DIRECT_KEY_RE.match(_line_body_and_ending(line)[0]))
            and len(match.group("indent")) > sequence_indent
        ),
        None,
    )


def _rewrite_top_level_keys(
    lines: list[str], updates: dict[str, str], config_path: Path
) -> bool:
    """Rewrite the target keys at the top level of a single-variant config."""
    top_level_end = next(
        (
            i
            for i, line in enumerate(lines)
            if _direct_key_line(line, "variants", indent=0)
        ),
        len(lines),
    )
    for key in ("android_version", "build_tag", "incremental"):
        line_idx = next(
            (
                i
                for i, line in enumerate(lines[:top_level_end])
                if _direct_key_line(line, key, indent=0)
            ),
            None,
        )
        if line_idx is None:
            Log.w(f"Could not find {key} entry in {config_path}.")
            return False
        lines[line_idx] = _rewrite_yaml_line(lines[line_idx], key, updates[key])
    return True


def _write_updated_config(
    config_path: Path,
    lines: list[str],
    raw_text: str,
    cfg: Config,
    match_idx: int | None,
    updates: dict[str, str],
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
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(new_text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, original_mode)
        reparse = _load_yaml(tmp_path.read_text(encoding="utf-8"))
        if not isinstance(reparse, dict):
            raise TypeError(f"Round-trip parse yielded {type(reparse).__name__}")

        if isinstance(reparse.get("variants"), list):
            reparsed_effective = _effective_variant_values(reparse, match_idx)
        elif "variants" not in reparse and match_idx is None:
            reparsed_effective = reparse
        else:
            reparsed_effective = None
        if reparsed_effective is None or not _identity_matches(reparsed_effective, cfg):
            raise ValueError("Round-trip parse changed the effective config identity")
        if not all(
            key in reparsed_effective
            and reparsed_effective[key] is not None
            and str(reparsed_effective[key]) == str(value)
            for key, value in updates.items()
        ):
            raise ValueError(
                "Round-trip parse did not preserve target fingerprint values"
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
