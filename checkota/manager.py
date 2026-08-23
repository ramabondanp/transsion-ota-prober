import os
import re
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

import yaml

from checkota.constants import REGION_CODE_MAP
from checkota.logging import Log

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    _fcntl = None  # type: ignore[assignment]


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

        with open(file, encoding="utf-8") as handle:
            data = yaml.safe_load(handle)

        if not isinstance(data, dict):
            raise ValueError("Config file content is not a valid dictionary.")

        variants = data.get("variants")

        if variants is None:
            return [cls._from_dict(data)]

        if not isinstance(variants, list) or not variants:
            raise ValueError("'variants' must be a non-empty list of dictionaries.")

        base = {k: v for k, v in data.items() if k != "variants"}
        configs = []
        for idx, variant in enumerate(variants, start=1):
            if not isinstance(variant, dict):
                raise ValueError(f"Variant entry #{idx} is not a dictionary.")

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


def parse_fingerprint(fingerprint: str) -> dict[str, str] | None:
    match = _FINGERPRINT_RE.match((fingerprint or "").strip())
    return match.groupdict() if match else None


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
    parsed = parse_fingerprint(fingerprint)
    if not parsed:
        Log.w("No valid target fingerprint available to update configuration.")
        return False

    updates = {
        "android_version": parsed["android_version"],
        "build_tag": parsed["build_tag"],
        "incremental": parsed["incremental"],
    }

    try:
        raw_text = config_path.read_text(encoding="utf-8")
    except Exception as exc:
        Log.w(f"Failed to read config file {config_path}: {exc}")
        return False

    lines = raw_text.splitlines(keepends=True)

    def rewrite_line(line: str, value: str) -> str:
        newline = ""
        if line.endswith("\r\n"):
            newline = "\r\n"
            body = line[:-2]
        elif line.endswith("\n"):
            newline = "\n"
            body = line[:-1]
        else:
            body = line

        before_comment, sep, comment = body.partition("#")
        key_part, _, value_part = before_comment.partition(":")
        if not _:
            return line

        value_prefix = value_part[: len(value_part) - len(value_part.lstrip(" "))]
        value_core = value_part[len(value_prefix) :]
        value_core_stripped = value_core.strip()
        value_suffix = value_core[len(value_core.rstrip(" ")) :] if value_core else ""

        quote_char = ""
        if value_core_stripped.startswith('"') and value_core_stripped.endswith('"'):
            quote_char = '"'
        elif value_core_stripped.startswith("'") and value_core_stripped.endswith("'"):
            quote_char = "'"

        new_value = f"{quote_char}{value}{quote_char}" if quote_char else str(value)
        new_before_comment = f"{key_part}:{value_prefix}{new_value}{value_suffix}"

        if sep:
            return f"{new_before_comment}{sep}{comment}{newline}"
        return f"{new_before_comment}{newline}"

    def find_key_line(key: str, start_idx: int, end_indent: int) -> int | None:
        idx = start_idx
        while idx < len(lines):
            line = lines[idx]
            stripped = line.strip()
            indent = len(line) - len(line.lstrip(" "))

            if indent <= end_indent and stripped.startswith("- "):
                break
            if indent <= end_indent and not stripped:
                idx += 1
                continue
            if (
                indent <= end_indent
                and stripped
                and not stripped.startswith("- ")
                and not stripped.startswith("#")
            ):
                break
            if stripped.startswith(f"{key}:"):
                return idx
            idx += 1
        return None

    def insert_key_line(start_idx: int, indent: int, key: str, value: str) -> None:
        lines.insert(start_idx, " " * indent + f'{key}: "{value}"\n')

    try:
        data = yaml.safe_load(raw_text)
    except Exception:
        data = None

    if isinstance(data, dict) and isinstance(data.get("variants"), list):
        variants: list[dict[str, Any]] = data["variants"]
        match_idx: int | None = None
        if cfg.variant_index is not None and 0 <= cfg.variant_index < len(variants):
            candidate = variants[cfg.variant_index]
            if isinstance(candidate, dict):
                eff_product = candidate.get("product", data.get("product"))
                if eff_product == cfg.product:
                    match_idx = cfg.variant_index
        if match_idx is None:
            for i, variant in enumerate(variants):
                if isinstance(variant, dict):
                    eff_product = variant.get("product", data.get("product"))
                    if eff_product == cfg.product:
                        match_idx = i
                        break
        if match_idx is None:
            Log.w(
                f"Could not locate matching variant in {config_path} when updating incremental."
            )
            return False

        try:
            current_value = {
                key: variants[match_idx].get(key, data.get(key))
                for key in ("android_version", "build_tag", "incremental")
            }
            if all(
                str(current_value.get(key, "")) == str(value)
                for key, value in updates.items()
            ):
                Log.i(f"{config_path} already matches target fingerprint values.")
                return True
        except Exception:
            pass

        variants_line_idx = next(
            (
                i
                for i, line in enumerate(lines)
                if line.lstrip().startswith("variants:")
            ),
            None,
        )
        if variants_line_idx is None:
            Log.w(f"Could not find variants section in {config_path}.")
            return False

        variants_indent = len(lines[variants_line_idx]) - len(
            lines[variants_line_idx].lstrip(" ")
        )

        variant_counter = -1
        target_variant_indent = None
        variant_start_idx = None
        for i in range(variants_line_idx + 1, len(lines)):
            line = lines[i]
            stripped = line.strip()
            indent = len(line) - len(line.lstrip(" "))
            if indent <= variants_indent and stripped:
                break
            if stripped.startswith("- "):
                variant_counter += 1
                if variant_counter == match_idx:
                    target_variant_indent = indent
                    variant_start_idx = i + 1
                    break
        if variant_start_idx is None or target_variant_indent is None:
            Log.w(f"Failed to locate variant block #{match_idx + 1} in {config_path}.")
            return False

        insert_idx = variant_start_idx
        for key in ("android_version", "build_tag", "incremental"):
            line_idx = find_key_line(key, variant_start_idx, target_variant_indent)
            if line_idx is None:
                insert_key_line(
                    insert_idx, target_variant_indent + 2, key, updates[key]
                )
                insert_idx += 1
            else:
                lines[line_idx] = rewrite_line(lines[line_idx], updates[key])
    else:
        top_level_end = next(
            (i for i, line in enumerate(lines) if line.strip().startswith("variants:")),
            len(lines),
        )
        for key in ("android_version", "build_tag", "incremental"):
            line_idx = next(
                (
                    i
                    for i, line in enumerate(lines[:top_level_end])
                    if line.strip().startswith(f"{key}:")
                ),
                None,
            )
            if line_idx is None:
                Log.w(f"Could not find {key} entry in {config_path}.")
                return False
            lines[line_idx] = rewrite_line(lines[line_idx], updates[key])

    new_text = "".join(lines)
    if new_text == raw_text:
        Log.i(f"{config_path} already matches target fingerprint values.")
        return True

    # Write to a temporary file in the same directory, validate it, then
    # atomically replace the original. A failure at any point leaves the
    # original config untouched.
    tmp_path: Path | None = None
    try:
        original_mode = stat.S_IMODE(config_path.stat().st_mode)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{config_path.name}.", suffix=".tmp", dir=config_path.parent
        )
        tmp_path = Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(new_text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, original_mode)
        reparse = yaml.safe_load(tmp_path.read_text(encoding="utf-8"))
        if not isinstance(reparse, dict):
            raise ValueError(f"Round-trip parse yielded {type(reparse).__name__}")
        os.replace(tmp_path, config_path)
        tmp_path = None
    except Exception as exc:
        Log.w(f"Failed to write updated config {config_path}: {exc}")
        return False
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass

    Log.s(
        f"Updated {config_path} -> Android {updates['android_version']}, "
        f"build {updates['build_tag']}, incremental {updates['incremental']}"
    )
    return True
