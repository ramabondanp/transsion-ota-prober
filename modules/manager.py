from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from modules.constants import REGION_CODE_MAP
from modules.logging import Log


@dataclass
class Config:
    build_tag: str
    incremental: str
    android_version: str
    model: str
    device: str
    oem: str
    product: str
    variant: Optional[str] = None
    variant_index: Optional[int] = None

    @classmethod
    def _from_dict(
        cls, data: Dict[str, str], variant_name: Optional[str] = None, variant_index: Optional[int] = None
    ) -> "Config":
        field_names = {field.name for field in fields(cls)}
        required_fields = field_names - {"variant", "variant_index"}

        filtered = {key: value for key, value in data.items() if key in field_names}

        if variant_name:
            filtered["variant"] = variant_name
        if variant_index is not None:
            filtered["variant_index"] = variant_index

        missing = [key for key in required_fields if key not in filtered]
        if missing:
            raise ValueError(f"Config missing required fields: {', '.join(sorted(missing))}")

        return cls(**filtered)

    @classmethod
    def from_yaml(cls, file: Path) -> List["Config"]:
        if not file.is_file():
            raise FileNotFoundError(f"Config file not found: {file}")

        with open(file, "r") as handle:
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


def region_from_product(product: str) -> Optional[str]:
    if not product:
        return None
    try:
        if "-" not in product:
            return None
        code = product.split("-")[-1].strip().upper()
        return REGION_CODE_MAP.get(code)
    except Exception:
        return None


def region_code_from_product(product: str) -> Optional[str]:
    if not product:
        return None
    try:
        if "-" not in product:
            return None
        code = product.rsplit("-", 1)[-1].strip().upper()
        return code or None
    except Exception:
        return None


def update_config_incremental(config_path: Path, cfg: Config, new_incremental: str) -> bool:
    if not new_incremental:
        Log.w("No incremental value available to update configuration.")
        return False

    try:
        raw_text = config_path.read_text()
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

    def find_incremental_line(start_idx: int, end_indent: int) -> Optional[int]:
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
            if indent <= end_indent and stripped and not stripped.startswith("- ") and not stripped.startswith("#"):
                break
            if stripped.startswith("incremental:"):
                return idx
            idx += 1
        return None

    try:
        data = yaml.safe_load(raw_text)
    except Exception:
        data = None

    if isinstance(data, dict) and isinstance(data.get("variants"), list):
        variants: List[Dict[str, Any]] = data["variants"]
        match_idx: Optional[int] = None
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
            Log.w(f"Could not locate matching variant in {config_path} when updating incremental.")
            return False

        try:
            current_value = variants[match_idx].get("incremental")
            if current_value == new_incremental:
                Log.i(f"{config_path} already uses incremental {new_incremental}.")
                return True
        except Exception:
            pass

        variants_line_idx = next((i for i, line in enumerate(lines) if line.lstrip().startswith("variants:")), None)
        if variants_line_idx is None:
            Log.w(f"Could not find variants section in {config_path}.")
            return False

        variants_indent = len(lines[variants_line_idx]) - len(lines[variants_line_idx].lstrip(" "))

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

        inc_idx = find_incremental_line(variant_start_idx, target_variant_indent)
        if inc_idx is None:
            insert_line = " " * (target_variant_indent + 2) + f'incremental: "{new_incremental}"\n'
            lines.insert(variant_start_idx, insert_line)
        else:
            lines[inc_idx] = rewrite_line(lines[inc_idx], new_incremental)
    else:
        inc_idx = None
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("variants:"):
                break
            if stripped.startswith("incremental:"):
                inc_idx = i
                break

        if inc_idx is None:
            Log.w(f"Could not find incremental entry in {config_path}.")
            return False

        lines[inc_idx] = rewrite_line(lines[inc_idx], new_incremental)

    new_text = "".join(lines)
    if new_text == raw_text:
        Log.i(f"{config_path} already uses incremental {new_incremental}.")
        return True

    try:
        config_path.write_text(new_text)
    except Exception as exc:
        Log.w(f"Failed to write updated config {config_path}: {exc}")
        return False

    Log.s(f"Updated {config_path} incremental -> {new_incremental}")
    return True
