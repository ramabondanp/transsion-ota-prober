#!/usr/bin/python3

import argparse
import datetime
import gzip
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Dict, Optional, Tuple, List, Set, Any

try:
    import requests
    import yaml
    from google.protobuf import text_format
    from checkin import checkin_generator_pb2
    from utils import functions
except ImportError as e:
    print(f"Error: Missing required library. {e}", file=sys.stderr)
    sys.exit(1)

CHECKIN_URL = 'https://android.googleapis.com/checkin'
USER_AGENT_TPL = 'Dalvik/2.1.0 (Linux; U; Android {0}; {1} Build/{2})'
PROTO_TYPE = 'application/x-protobuffer'
DEBUG_FILE = "debug_checkin_response.txt"
PROCESSED_FP_FILE = "processed_fingerprints.txt"
OTA_URL_PREFIX = b'https://android.googleapis.com/packages/ota'

TELEGRAPH_API_URL = "https://api.telegra.ph/createPage"

REGION_CODE_MAP = {
    'GL': 'Global',
    'OP': 'Global',
    'RU': 'Russia',
    'IN': 'India',
    'EU': 'Europe',
    'TR': 'Turkey',
}

SDK_TO_ANDROID = {
    33: 'Android 13',
    34: 'Android 14',
    35: 'Android 15',
    36: 'Android 16',
    37: 'Android 17',
    38: 'Android 18',
}

DESC_SECTION_RE = re.compile(r'(<b>Title:</b> .*?\n\n)(.*?)(\n\n<b>Size:</b>)', re.DOTALL)
SENTENCE_BOUNDARY_RE = re.compile(r'\.\s+')
BR_TAG_RE = re.compile(r'<br\s*/?>', re.IGNORECASE)
HTML_TAG_RE = re.compile(r'<[^>]*>')
URL_PAREN_RE = re.compile(r'\s*\(http[s]?://\S+\)?')

class Log:
    @staticmethod
    def i(m): print(f"\033[94m=>\033[0m {m}")
    @staticmethod
    def s(m): print(f"\033[92m✓\033[0m {m}")
    @staticmethod
    def e(m): print(f"\033[91m✗\033[0m {m}", file=sys.stderr)
    @staticmethod
    def w(m): print(f"\033[93m!\033[0m {m}", file=sys.stderr)

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
    def _from_dict(cls, data: Dict[str, str], variant_name: Optional[str] = None,
                   variant_index: Optional[int] = None) -> 'Config':
        field_names = {field.name for field in fields(cls)}
        required_fields = field_names - {'variant', 'variant_index'}

        filtered = {
            key: value for key, value in data.items()
            if key in field_names
        }

        if variant_name:
            filtered['variant'] = variant_name
        if variant_index is not None:
            filtered['variant_index'] = variant_index

        missing = [key for key in required_fields if key not in filtered]
        if missing:
            raise ValueError(f"Config missing required fields: {', '.join(sorted(missing))}")

        return cls(**filtered)

    @classmethod
    def from_yaml(cls, file: Path) -> List['Config']:
        if not file.is_file():
            raise FileNotFoundError(f"Config file not found: {file}")

        with open(file, 'r') as f:
            data = yaml.safe_load(f)

        if not isinstance(data, dict):
            raise ValueError("Config file content is not a valid dictionary.")

        variants = data.get('variants')

        if variants is None:
            return [cls._from_dict(data)]

        if not isinstance(variants, list) or not variants:
            raise ValueError("'variants' must be a non-empty list of dictionaries.")

        base = {k: v for k, v in data.items() if k != 'variants'}
        configs = []
        for idx, variant in enumerate(variants, start=1):
            if not isinstance(variant, dict):
                raise ValueError(f"Variant entry #{idx} is not a dictionary.")

            merged = {**base, **variant}
            variant_name = (
                variant.get('variant')
                or variant.get('name')
                or variant.get('region')
                or variant.get('label')
                or variant.get('product')
            )
            configs.append(cls._from_dict(merged, variant_name, idx - 1))

        return configs

    def fingerprint(self) -> str:
        return (f'{self.oem}/{self.product}/{self.device}:'
                f'{self.android_version}/{self.build_tag}/'
                f'{self.incremental}:user/release-keys')

def region_from_product(product: str) -> Optional[str]:
    """Map region code in product (e.g., KM9-OP) to human-friendly name.

    Known mappings:
      OP -> Global
      RU -> Russia
      IN -> India
    """
    if not product:
        return None
    try:
        # Expect region after the last hyphen, e.g. "KM9-OP" -> "OP"
        if '-' not in product:
            return None
        code = product.split('-')[-1].strip().upper()
        return REGION_CODE_MAP.get(code)
    except Exception:
        return None

class TgNotify:
    MAX_LEN = 4090
    DESC_MAX_LEN = 1500

    def __init__(self, token: str, chat_id: str, telegraph_token: str):
        if not token or not chat_id:
            raise ValueError("Bot token and chat ID required")
        if not telegraph_token:
            raise ValueError("Telegraph token is required")
        self.token = token
        self.chat_id = chat_id
        self.telegraph_token = telegraph_token
        self.url = f"https://api.telegram.org/bot{token}"

    def _create_telegraph_page(self, title: str, content: str) -> Optional[str]:
        try:
            clean_content = content.replace('<br>', '\n').replace('<br/>', '\n').replace('<br />', '\n')

            payload = {
                "access_token": self.telegraph_token,
                "title": f"Update Details: {title}",
                "author_name": "TRANSSION Updates Tracker",
                "author_url": "https://t.me/TranssionUpdatesTracker",
                "content": [{"tag": "p", "children": [clean_content]}],
                "return_content": False
            }

            response = requests.post(TELEGRAPH_API_URL, json=payload, timeout=10)
            response.raise_for_status()

            result = response.json()
            if result.get("ok"):
                telegraph_url = result["result"]["url"]
                Log.s(f"Created Telegraph page: {telegraph_url}")
                return telegraph_url
            else:
                Log.w(f"Telegraph API error: {result}")
                return None

        except Exception as e:
            Log.w(f"Failed to create Telegraph page: {e}")
            return None

    def _truncate_desc(self, desc: str, max_len: int = None, telegraph_url: Optional[str] = None) -> str:
        if max_len is None:
            max_len = self.DESC_MAX_LEN

        link_text = f'... <a href="{telegraph_url}">Read full changelogs</a>' if telegraph_url else "..."
        effective_max_len = max_len - len(link_text) if telegraph_url else max_len

        if len(desc) <= max_len:
            return desc

        truncated = desc[:effective_max_len]

        sentence_endings = [match.end() - 1 for match in SENTENCE_BOUNDARY_RE.finditer(truncated)]

        if sentence_endings and sentence_endings[-1] > effective_max_len * 0.6:
            result = truncated[:sentence_endings[-1] + 1]
        else:
            last_paragraph = truncated.rfind('\n\n')
            if last_paragraph > effective_max_len * 0.5:
                result = truncated[:last_paragraph]
            else:
                last_line = truncated.rfind('\n')
                if last_line > effective_max_len * 0.7:
                    result = truncated[:last_line]
                else:
                    last_space = truncated.rfind(' ')
                    if last_space > effective_max_len * 0.8:
                        result = truncated[:last_space]
                    else:
                        result = truncated

        result += link_text
        return result

    def send(self, msg: str, btn_text: Optional[str] = None,
             btn_url: Optional[str] = None, truncate_desc: bool = True,
             device_title: Optional[str] = None) -> bool:
        Log.i("Sending Telegram notification...")

        telegraph_url = None

        if truncate_desc and len(msg) > self.MAX_LEN:
            match = DESC_SECTION_RE.search(msg)

            if match:
                before_desc = match.group(1)
                description = match.group(2).strip()
                after_desc = match.group(3)

                excess_chars = len(msg) - self.MAX_LEN

                if excess_chars > 0 and len(description) > self.DESC_MAX_LEN:
                    title_match = re.search(r'<b>Title:</b> (.*?)\n', before_desc)
                    page_title = title_match.group(1) if title_match else (device_title or "Update")

                    telegraph_url = self._create_telegraph_page(page_title, description)

                    truncated_desc = self._truncate_desc(description, telegraph_url=telegraph_url)

                    msg = msg.replace(match.group(0), before_desc + truncated_desc + after_desc)

        try:
            payload = {
                'chat_id': self.chat_id,
                'text': msg,
                'parse_mode': 'html',
                'disable_web_page_preview': True,
            }

            if btn_text and btn_url:
                payload['reply_markup'] = {
                    'inline_keyboard': [[
                        {'text': btn_text, 'url': btn_url}
                    ]]
                }

            r = requests.post(f"{self.url}/sendMessage", json=payload, timeout=15)
            r.raise_for_status()

            Log.s("Notification sent successfully")
            return True

        except Exception as e:
            Log.e(f"Failed to send notification: {e}")
            return False

class UpdateChecker:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.ua = USER_AGENT_TPL.format(cfg.android_version, cfg.model, cfg.build_tag)
        self.headers = {
            'accept-encoding': 'gzip, deflate',
            'content-encoding': 'gzip',
            'content-type': PROTO_TYPE,
            'user-agent': self.ua
        }

    def _build_request(self) -> bytes:
        payload = checkin_generator_pb2.AndroidCheckinRequest()
        build = checkin_generator_pb2.AndroidBuildProto()
        checkin = checkin_generator_pb2.AndroidCheckinProto()

        build.id = self.cfg.fingerprint()
        build.timestamp = 0
        build.device = self.cfg.device

        checkin.build.CopyFrom(build)
        checkin.roaming = "WIFI::"
        checkin.userNumber = 0
        checkin.deviceType = 2
        checkin.voiceCapable = False

        payload.imei = functions.generateImei()
        payload.id = 0
        payload.digest = functions.generateDigest()
        payload.checkin.CopyFrom(checkin)
        payload.locale = 'en-US'
        payload.timeZone = 'America/New_York'
        payload.version = 3
        payload.serialNumber = functions.generateSerial()
        payload.macAddr.append(functions.generateMac())
        payload.macAddrType.extend(['wifi'])
        payload.fragment = 0
        payload.userSerialNumber = 0
        payload.fetchSystemUpdates = 1

        return gzip.compress(payload.SerializeToString())

    def check(self, debug: bool = False) -> Tuple[bool, Optional[Dict]]:
        Log.i("Checking for updates...")

        try:
            data = self._build_request()
            r = requests.post(CHECKIN_URL, data=data, headers=self.headers, timeout=10)
            r.raise_for_status()

            resp = checkin_generator_pb2.AndroidCheckinResponse()
            resp.ParseFromString(r.content)

            if debug:
                Path(DEBUG_FILE).write_text(text_format.MessageToString(resp))
                Log.i(f"Debug response saved to {DEBUG_FILE}")

            info = self._parse(resp)
            has_update = info.get('found', False) and 'url' in info
            return has_update, info

        except Exception as e:
            Log.e(f"Update check failed: {e}")
            if debug and 'r' in locals():
                Path(DEBUG_FILE.replace(".txt", "_error.bin")).write_bytes(r.content)
                Log.i(f"Raw error response saved")
            return False, None

    def _parse(self, resp: checkin_generator_pb2.AndroidCheckinResponse) -> Dict:
        info = {
            'device': self.cfg.model,
            'found': False,
            'timestamp': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'title': None,
            'description': None,
            'size': None,
            'url': None
        }

        for entry in resp.setting:
            name_bytes = entry.name or b''
            value_bytes = entry.value or b''

            value = value_bytes.decode('utf-8', errors='ignore')

            if not info['found'] and (name_bytes == b'update_url' or OTA_URL_PREFIX in value_bytes):
                url = value.strip()
                if url:
                    info['url'] = url
                    info['found'] = True

            try:
                name = name_bytes.decode('utf-8')
            except Exception:
                continue

            if name == 'update_title':
                info['title'] = value.strip()
            elif name == 'update_description':
                info['description'] = self._clean_desc(value)
            elif name == 'update_size':
                info['size'] = value

        return info

    @staticmethod
    def _clean_desc(text: str) -> str:
        text = text.replace('\n', '')
        text = BR_TAG_RE.sub('\n', text)
        text = HTML_TAG_RE.sub('', text)
        text = URL_PAREN_RE.sub('', text)
        return text.strip()

def check_cmd(cmd: str) -> bool:
    return shutil.which(cmd) is not None

def check_cmds(cmds: List[str]) -> bool:
    missing = [cmd for cmd in cmds if not check_cmd(cmd)]
    if missing:
        Log.e(f"Missing required command(s): {', '.join(missing)}")
        return False
    return True

def get_ota_metadata(url: str) -> Optional[Dict[str, str]]:
    """Stream-extract OTA metadata and parse useful post-* fields quickly.

    Uses a pipeline to stop early after matching needed lines to avoid
    downloading the entire OTA zip.

    Returns a dict with keys:
      - fingerprint (post-build)
      - post_build_incremental
      - post_security_patch_level
      - post_timestamp
      - build_date (derived from post_timestamp, CST)
    """
    Log.i("Fetching OTA metadata (fingerprint, patch level, sdk)...")
    cmds = ['curl', 'bsdtar', 'grep']
    if not check_cmds(cmds):
        return None

    curl_cmd = ['curl', '--fail', '-Ls', '--max-time', '60', '--limit-rate', '100K', url]
    bsdtar_cmd = ['bsdtar', '-Oxf', '-', 'META-INF/com/android/metadata']
    # Match relevant keys and stop early after matches
    grep_cmd = ['grep', '-E', '^(post-build=|post-build-incremental=|post-security-patch-level=|post-timestamp=|post-sdk-level=)', '-m', '5']

    try:
        curl_proc = subprocess.Popen(curl_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        bsdtar_proc = subprocess.Popen(bsdtar_cmd, stdin=curl_proc.stdout, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        if curl_proc.stdout:
            curl_proc.stdout.close()
        grep_proc = subprocess.Popen(grep_cmd, stdin=bsdtar_proc.stdout, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        if bsdtar_proc.stdout:
            bsdtar_proc.stdout.close()

        try:
            stdout_bytes, _ = grep_proc.communicate(timeout=90)
            content = stdout_bytes.decode('utf-8', errors='replace')
        except subprocess.TimeoutExpired:
            Log.w("Timeout expired while fetching OTA metadata.")
            return None

        if not content.strip():
            Log.w("Could not extract OTA metadata (empty content).")
            return None

        meta: Dict[str, str] = {}
        for line in content.splitlines():
            if '=' in line:
                k, v = line.strip().split('=', 1)
                meta[k.strip()] = v.strip()

        result: Dict[str, str] = {}
        fp = meta.get('post-build', '')
        if not fp:
            Log.w("post-build not found in metadata.")
        else:
            Log.i(f"Extracted fingerprint: {fp}")
        result['fingerprint'] = fp

        if meta.get('post-build-incremental'):
            result['post_build_incremental'] = meta['post-build-incremental']
        if meta.get('post-security-patch-level'):
            result['post_security_patch_level'] = meta['post-security-patch-level']
        if meta.get('post-timestamp'):
            result['post_timestamp'] = meta['post-timestamp']
            try:
                ts = int(meta['post-timestamp'])
                dt_utc = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
                tz_cst = datetime.timezone(datetime.timedelta(hours=8))
                dt_cst = dt_utc.astimezone(tz_cst)
                result['build_date'] = dt_cst.strftime('%Y-%m-%d %H:%M:%S')
            except Exception:
                pass

        # Capture SDK level (Android 13/14/15+, etc.)
        if meta.get('post-sdk-level'):
            sdk_level = meta['post-sdk-level']
            result['post_sdk_level'] = sdk_level
            try:
                sdk_int = int(sdk_level)
                if sdk_int >= 33:
                    result['android_version'] = SDK_TO_ANDROID.get(sdk_int)
            except Exception:
                pass

        return result

    except Exception as e:
        Log.e(f"Error extracting OTA metadata: {e}")
        return None
    finally:
        # Ensure curl is not left running
        if 'curl_proc' in locals() and curl_proc and curl_proc.poll() is None:
            try:
                curl_proc.kill()
            except Exception:
                pass

def load_processed_fingerprints(path: Path) -> Set[str]:
    if not path.exists():
        return set()
    try:
        with path.open('r') as f:
            return {line.strip() for line in f if line.strip()}
    except Exception as e:
        Log.e(f"Error reading processed fingerprints file {path}: {e}")
        return set()

def save_processed_fingerprint(path: Path, fingerprint: str):
    try:
        with path.open('a') as f:
            f.write(f"{fingerprint}\n")
        Log.s(f"Saved new fingerprint to {path}")
    except Exception as e:
        Log.e(f"Failed to save fingerprint to {path}: {e}")


def extract_incremental_from_fingerprint(fp: str) -> Optional[str]:
    if not fp:
        return None
    try:
        fingerprint_suffix = fp.split(':', 1)[1]
    except IndexError:
        return None

    parts = fingerprint_suffix.split('/')
    if len(parts) < 3:
        return None

    incremental_segment = parts[2]
    return incremental_segment.split(':', 1)[0] if incremental_segment else None


def update_config_incremental(config_path: Path, cfg: Config, new_incremental: str) -> bool:
    if not new_incremental:
        Log.w("No incremental value available to update configuration.")
        return False

    try:
        raw_text = config_path.read_text()
    except Exception as e:
        Log.w(f"Failed to read config file {config_path}: {e}")
        return False

    lines = raw_text.splitlines(keepends=True)

    def rewrite_line(line: str, value: str) -> str:
        newline = ''
        if line.endswith('\r\n'):
            newline = '\r\n'
            body = line[:-2]
        elif line.endswith('\n'):
            newline = '\n'
            body = line[:-1]
        else:
            body = line

        before_comment, sep, comment = body.partition('#')
        key_part, _, value_part = before_comment.partition(':')
        if not _:
            return line

        value_prefix = value_part[:len(value_part) - len(value_part.lstrip(' '))]
        value_core = value_part[len(value_prefix):]
        value_core_stripped = value_core.strip()
        value_suffix = value_core[len(value_core.rstrip(' ')):] if value_core else ''

        quote_char = ''
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
            indent = len(line) - len(line.lstrip(' '))

            if indent <= end_indent and stripped.startswith('- '):
                break
            if indent <= end_indent and not stripped:
                idx += 1
                continue
            if indent <= end_indent and stripped and not stripped.startswith('- ') and not stripped.startswith('#'):
                break
            if stripped.startswith('incremental:'):
                return idx
            idx += 1
        return None

    try:
        data = yaml.safe_load(raw_text)
    except Exception:
        data = None

    if isinstance(data, dict) and isinstance(data.get('variants'), list):
        variants: List[Dict[str, Any]] = data['variants']
        match_idx: Optional[int] = None
        if cfg.variant_index is not None and 0 <= cfg.variant_index < len(variants):
            candidate = variants[cfg.variant_index]
            if isinstance(candidate, dict):
                eff_product = candidate.get('product', data.get('product'))
                if eff_product == cfg.product:
                    match_idx = cfg.variant_index
        if match_idx is None:
            for i, variant in enumerate(variants):
                if isinstance(variant, dict):
                    eff_product = variant.get('product', data.get('product'))
                    if eff_product == cfg.product:
                        match_idx = i
                        break
        if match_idx is None:
            Log.w(f"Could not locate matching variant in {config_path} when updating incremental.")
            return False

        try:
            current_value = variants[match_idx].get('incremental')
            if current_value == new_incremental:
                Log.i(f"{config_path} already uses incremental {new_incremental}.")
                return True
        except Exception:
            pass

        variants_line_idx = next(
            (i for i, line in enumerate(lines) if line.lstrip().startswith('variants:')),
            None
        )
        if variants_line_idx is None:
            Log.w(f"Could not find variants section in {config_path}.")
            return False

        variants_indent = len(lines[variants_line_idx]) - len(lines[variants_line_idx].lstrip(' '))

        variant_counter = -1
        target_variant_indent = None
        variant_start_idx = None
        for i in range(variants_line_idx + 1, len(lines)):
            line = lines[i]
            stripped = line.strip()
            indent = len(line) - len(line.lstrip(' '))
            if indent <= variants_indent and stripped:
                break
            if stripped.startswith('- '):
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
            insert_line = ' ' * (target_variant_indent + 2) + f'incremental: "{new_incremental}"\n'
            lines.insert(variant_start_idx, insert_line)
        else:
            lines[inc_idx] = rewrite_line(lines[inc_idx], new_incremental)
    else:
        # Non-variant config
        inc_idx = None
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith('variants:'):
                break
            if stripped.startswith('incremental:'):
                inc_idx = i
                break

        if inc_idx is None:
            Log.w(f"Could not find incremental entry in {config_path}.")
            return False

        lines[inc_idx] = rewrite_line(lines[inc_idx], new_incremental)

    new_text = ''.join(lines)
    if new_text == raw_text:
        Log.i(f"{config_path} already uses incremental {new_incremental}.")
        return True

    try:
        config_path.write_text(new_text)
    except Exception as e:
        Log.w(f"Failed to write updated config {config_path}: {e}")
        return False

    Log.s(f"Updated {config_path} incremental -> {new_incremental}")
    return True


def commit_incremental_update(config_path: Path, new_incremental: str,
                              variant_label: Optional[str] = None,
                              extra_paths: Optional[List[Path]] = None) -> bool:
    git_path = shutil.which('git')
    if not git_path:
        Log.w("Git executable not found; skipping auto-commit.")
        return False

    try:
        repo_root_result = subprocess.run(
            [git_path, 'rev-parse', '--show-toplevel'],
            capture_output=True,
            text=True,
            check=False,
            cwd=str(config_path.parent)
        )
    except Exception as e:
        Log.w(f"Failed to locate Git repository root: {e}")
        return False

    if repo_root_result.returncode != 0:
        stderr = repo_root_result.stderr.strip() if repo_root_result.stderr else 'Unknown error'
        Log.w(f"Could not determine Git repository root ({stderr}); skipping auto-commit.")
        return False

    repo_root = Path(repo_root_result.stdout.strip() or '.')

    paths: List[Path] = [config_path]
    if extra_paths:
        for p in extra_paths:
            if p and p.exists():
                paths.append(p)

    unique_paths: List[Path] = []
    seen: Set[Path] = set()
    for path in paths:
        try:
            resolved = path.resolve()
        except Exception:
            resolved = path
        if resolved in seen:
            continue
        seen.add(resolved)
        unique_paths.append(resolved)

    if not unique_paths:
        Log.i("No files to add for incremental update commit.")
        return False

    add_args: List[str] = []
    for path in unique_paths:
        try:
            add_args.append(str(path.resolve().relative_to(repo_root)))
        except Exception:
            add_args.append(str(path))

    add_cmd = [git_path, 'add', '--'] + add_args
    add_result = subprocess.run(add_cmd, capture_output=True, text=True, cwd=str(repo_root))
    if add_result.returncode != 0:
        stderr = add_result.stderr.strip() or add_result.stdout.strip()
        Log.w(f"Failed to stage files for commit: {stderr}")
        return False

    diff_result = subprocess.run(
        [git_path, 'diff', '--cached', '--quiet'],
        cwd=str(repo_root)
    )

    if diff_result.returncode == 0:
        Log.i("No staged changes detected; skipping incremental update commit.")
        return False
    if diff_result.returncode not in (0, 1):
        Log.w("Unable to inspect staged changes; skipping incremental update commit.")
        return False

    scope = config_path.stem
    if variant_label:
        scope = f"{scope} ({variant_label})"
    commit_msg = f"{scope}: update incremental to {new_incremental}"

    commit_cmd = [git_path, 'commit', '-m', commit_msg]
    commit_result = subprocess.run(commit_cmd, capture_output=True, text=True, cwd=str(repo_root))
    if commit_result.returncode == 0:
        Log.s(f"Committed incremental update: {commit_msg}")
        return True

    stderr = commit_result.stderr.strip() or commit_result.stdout.strip()
    Log.w(f"Git commit failed: {stderr}")
    return False


def build_sdk_strings(sdk_level: Optional[str], android_version: Optional[str]) -> Tuple[str, str, str]:
    """Return helper strings for Telegram/log output based on SDK level."""
    if sdk_level is None:
        return '', '', ''

    try:
        sdk_int = int(str(sdk_level))
    except (TypeError, ValueError):
        return '', '', ''

    if sdk_int < 33:
        return '', '', ''

    version_label = android_version or SDK_TO_ANDROID.get(sdk_int)
    if version_label:
        message = version_label
        log_line = f"Android: {version_label} (SDK {sdk_level})"
        release_line = f"**Android:** {version_label} (SDK {sdk_level})"
        return message, log_line, release_line

    message = f"SDK: {sdk_level}"
    log_line = f"SDK level: {sdk_level}"
    release_line = f"**SDK:** {sdk_level}"
    return message, log_line, release_line

def create_github_release(config_name: str, update_data: Dict) -> bool:
    if not check_cmd('gh'):
        Log.e("GitHub CLI (gh) not found. Cannot create release.")
        return False

    title = update_data.get('title')
    device = update_data.get('device')
    description = update_data.get('description')
    url = update_data.get('url')
    size = update_data.get('size')

    if not title or title == 'Unknown Update':
        Log.w(f"Skipping GitHub release for {config_name}: Title is missing or unknown")
        return False

    if not device or device == 'Unknown Device':
        Log.w(f"Skipping GitHub release for {config_name}: Device is missing or unknown")
        return False

    if not url:
        Log.w(f"Skipping GitHub release for {config_name}: Download URL is missing")
        return False

    description = description or 'No description available'
    size = size or 'Unknown size'
    fingerprint = update_data.get('fingerprint', 'Unknown fingerprint')
    post_build_incremental = update_data.get('post_build_incremental')
    post_security_patch_level = update_data.get('post_security_patch_level')
    build_date = update_data.get('build_date')
    post_sdk_level = update_data.get('post_sdk_level')
    android_version = update_data.get('android_version')

    extra_lines = []
    if post_build_incremental:
        extra_lines.append(f"**Incremental:** {post_build_incremental}")
    if post_security_patch_level:
        extra_lines.append(f"**Security patch:** {post_security_patch_level}")
    if build_date:
        extra_lines.append(f"**Build date:** {build_date} (CST)")
    if post_sdk_level:
        _, _, release_line = build_sdk_strings(post_sdk_level, android_version)
        if release_line:
            extra_lines.append(release_line)

    extra_block = ("\n" + "\n".join(extra_lines)) if extra_lines else ""

    release_notes = f"""# {device}
## Changelog:
{description}

**Size:** {size}
**Download URL:** {url}{extra_block}
**Fingerprint:** `{fingerprint}`
"""

    try:
        check_release_cmd = ['gh', 'release', 'view', title]
        result = subprocess.run(check_release_cmd, capture_output=True, text=True)

        if result.returncode == 0:
            Log.i(f"Release '{title}' for config {config_name} already exists. Skipping.")
            return True

        create_cmd = [
            'gh', 'release', 'create', title,
            '--title', title,
            '--notes', release_notes
        ]

        result = subprocess.run(create_cmd, capture_output=True, text=True)

        if result.returncode == 0:
            Log.s(f"Created new release: {title} for config {config_name}")
            return True
        else:
            Log.e(f"Failed to create release: {result.stderr}")
            return False

    except Exception as e:
        Log.e(f"Error creating GitHub release: {e}")
        return False

def process_config_variant(cfg: Config, config_name: str, config_path: Path, args: argparse.Namespace,
                           variant_label: Optional[str] = None) -> int:
    tg = None
    if not args.skip_telegram and not args.register_fingerprint:
        token = os.environ.get('bot_token')
        chat = os.environ.get('chat_id')
        telegraph_token = os.environ.get('telegraph_token')

        if not token or not chat or not telegraph_token:
            Log.w("Telegram env vars not set, skipping notifications")
            args.skip_telegram = True
        else:
            try:
                tg = TgNotify(token, chat, telegraph_token)
            except ValueError as e:
                Log.e(f"Telegram setup failed: {e}")
                args.skip_telegram = True

    config_updated = False
    fingerprint_saved = False
    commit_incremental_value: Optional[str] = None

    checker = UpdateChecker(cfg)
    fp = cfg.fingerprint()
    Log.i(f"Device: {cfg.model} ({cfg.device})")
    reg_name = region_from_product(cfg.product)
    if variant_label:
        Log.i(f"Variant: {variant_label}")
    if reg_name:
        Log.i(f"Region: {reg_name}")
    Log.i(f"Build: {fp}")

    found, data = checker.check(args.debug)

    if not found or not data:
        Log.i("No updates found")
        return 0

    title = data.get('title')
    url = data.get('url')
    size = data.get('size')
    desc = data.get('description', 'No description')

    if not all([title, url, size]):
        Log.e("Missing essential update info (title, url, or size)")
        return 1

    Log.s(f"New OTA update found: {title}")
    Log.i(f"Size: {size}")
    Log.i(f"URL: {url}")

    ota_meta = get_ota_metadata(url)
    if not ota_meta or not ota_meta.get('fingerprint'):
        Log.e("Could not determine target fingerprint. Cannot verify if update is new.")
        return 1

    target_fp = ota_meta['fingerprint']
    Log.i(f"Target build: {target_fp}")
    inc = ota_meta.get('post_build_incremental')
    spl = ota_meta.get('post_security_patch_level')
    bdate = ota_meta.get('build_date')
    sdk_level = ota_meta.get('post_sdk_level')
    android_ver = ota_meta.get('android_version')
    sdk_message, sdk_log_line, _ = build_sdk_strings(sdk_level, android_ver)
    if inc:
        Log.i(f"Incremental: {inc}")
    if spl:
        Log.i(f"Security patch: {spl}")
    if bdate:
        Log.i(f"Build date: {bdate} (CST)")
    if sdk_log_line:
        Log.i(sdk_log_line)

    processed_fp_path = Path(PROCESSED_FP_FILE)
    processed_fingerprints = load_processed_fingerprints(processed_fp_path)
    is_new_update = target_fp not in processed_fingerprints
    target_incremental = inc or extract_incremental_from_fingerprint(target_fp)
    commit_incremental_value = target_incremental

    if is_new_update and not args.register_fingerprint:
        if target_incremental:
            if args.dry_run:
                Log.i(f"Dry-run: would update {config_path} incremental to {target_incremental}.")
            else:
                if update_config_incremental(config_path, cfg, target_incremental):
                    cfg.incremental = target_incremental
                    config_updated = True
        else:
            Log.w("Unable to determine new incremental value from OTA metadata; config not updated.")
    elif is_new_update and args.register_fingerprint:
        Log.i("--register-fingerprint set. Skipping config incremental update.")

    if not is_new_update and not args.force_notify:
        Log.i("This update has already been processed. Skipping.")
        return 0

    if args.register_fingerprint:
        if is_new_update:
            if args.dry_run:
                Log.i("--register-fingerprint set. Dry-run: would save new fingerprint without notification.")
            else:
                Log.i("--register-fingerprint flag is set. Saving new fingerprint without notification.")
                save_processed_fingerprint(processed_fp_path, target_fp)
                fingerprint_saved = True
                Log.s("Update check completed successfully (fingerprint registered).")
        else:
            Log.i("--register-fingerprint flag is set, but fingerprint is already known. No action taken.")
        return 0

    if not is_new_update and args.force_notify:
        Log.w(f"Forcing notification for an already processed update: {target_fp}")

    if not is_new_update and args.force_release:
        Log.w(f"Forcing GitHub release for an already processed update: {target_fp}")

    data['fingerprint'] = target_fp
    if inc:
        data['post_build_incremental'] = inc
    if spl:
        data['post_security_patch_level'] = spl
    if bdate:
        data['build_date'] = bdate
    if sdk_level:
        data['post_sdk_level'] = sdk_level
    if android_ver:
        data['android_version'] = android_ver

    if not args.skip_telegram and tg:
        region_line = f" ({reg_name})" if reg_name else ''
        sdk_suffix = f" ({sdk_message})" if sdk_message else ''
        msg = (
            f"<blockquote><b>OTA Update Available</b></blockquote>\n\n"
            f"<b>Device:</b> {cfg.model}{region_line}\n\n"
            f"<b>Title:</b> {title}{sdk_suffix}\n\n"
            f"{desc}\n\n"
            f"<b>Size:</b> {size}\n"
            + (f"<b>Incremental:</b> <code>{inc}</code>\n" if inc else '')
            + (f"<b>Security patch:</b> {spl}\n" if spl else '')
            + f"<b>Fingerprint:</b> <code>{target_fp}</code>"
            + (f"\n<b>Build date:</b> {bdate} (CST)" if bdate else '')
        )

        if args.dry_run:
            Log.i("Dry-run: would send Telegram notification with OTA details.")
            if is_new_update:
                Log.i("Dry-run: would save new fingerprint after successful notification.")
                Log.i("Dry-run: would create GitHub release for new update.")
        else:
            if tg.send(msg, "Google OTA Link", url, truncate_desc=True, device_title=f"{cfg.model} - {title}"):
                if is_new_update:
                    save_processed_fingerprint(processed_fp_path, target_fp)
                    fingerprint_saved = True
                    Log.i("Creating GitHub release for new update...")
                    create_github_release(config_name, data)
            else:
                Log.e("Failed to send notification. Fingerprint will not be saved.")
                return 1

    if args.force_release:
        if args.dry_run:
            Log.i("Dry-run: would create GitHub release due to --force-release.")
        else:
            Log.i("Force release flag detected. Creating GitHub release...")
            if create_github_release(config_name, data):
                if is_new_update and not (not args.skip_telegram and tg):
                    Log.i("Skipping fingerprint save due to force release")

    if (
        is_new_update
        and not args.dry_run
        and config_updated
        and commit_incremental_value
    ):
        extra_paths: List[Path] = []
        if fingerprint_saved:
            extra_paths.append(processed_fp_path)
        commit_incremental_update(
            config_path,
            commit_incremental_value,
            variant_label,
            extra_paths
        )

    Log.s("Update check completed successfully")
    return 0


def process_config(config_path: Path, args: argparse.Namespace) -> int:
    try:
        configs = Config.from_yaml(config_path)
    except Exception as e:
        Log.e(f"Config error for {config_path}: {e}")
        return 1

    if args.incremental and len(configs) != 1:
        Log.e('--incremental can only be used when a single configuration variant is defined')
        return 1

    exit_code = 0
    variants_total = len(configs)

    for idx, cfg in enumerate(configs, start=1):
        variant_label = cfg.variant
        display_label = variant_label or f"variant {idx}"

        if variants_total > 1:
            Log.i(f"Processing variant {idx}/{variants_total}: {display_label}")

        if args.incremental:
            Log.i(f"Override incremental: {args.incremental}")
            cfg.incremental = args.incremental

        slug = None
        if variant_label:
            slug = re.sub(r'[^A-Za-z0-9]+', '-', variant_label).strip('-')
        if not slug and variants_total > 1:
            slug = f"variant{idx}"

        config_name = config_path.stem
        if slug and variants_total > 1:
            config_name = f"{config_name}-{slug}"

        result = process_config_variant(cfg, config_name, config_path, args, variant_label)
        exit_code = max(exit_code, result)

    return exit_code


def main() -> int:
    if sys.version_info < (3, 7):
        Log.e("Requires Python 3.7+")
        return 1

    parser = argparse.ArgumentParser(description='Android OTA Update Checker')
    parser.add_argument('--debug', action='store_true', help='Enable debugging')
    parser.add_argument('-c', '--config', type=Path, help='Config file path')
    parser.add_argument('-d', '--config-dir', type=Path, help='Directory containing config files to process')
    parser.add_argument('--dry-run', action='store_true', help='Simulate actions without making changes or sending notifications')
    parser.add_argument('--skip-telegram', action='store_true', help='Skip Telegram notifications')
    parser.add_argument('--register-fingerprint', action='store_true', help='Save the update fingerprint without sending a notification')
    parser.add_argument('--force-notify', action='store_true', help='Send notification even if the update has been seen before')
    parser.add_argument('--force-release', action='store_true', help='Create GitHub release even without Telegram token or if fingerprint already exists')
    parser.add_argument('-i', '--incremental', help='Override incremental version')
    args = parser.parse_args()

    if args.config and args.config_dir:
        parser.error('Use either --config or --config-dir, not both.')

    if not args.config and not args.config_dir:
        parser.error('Either --config or --config-dir is required.')

    if args.config and args.config.is_dir():
        parser.error('--config expects a file. Use --config-dir for directories.')

    config_paths: List[Path]
    if args.config:
        config_paths = [args.config]
    else:
        if not args.config_dir.exists() or not args.config_dir.is_dir():
            parser.error('--config-dir must be an existing directory.')

        config_paths = sorted(
            (
                path for pattern in ('*.yml', '*.yaml')
                for path in args.config_dir.glob(pattern)
                if path.is_file()
            ),
            key=lambda p: p.name.lower()
        )

        if not config_paths:
            Log.e(f"No config files found in directory: {args.config_dir}")
            return 1

    if args.incremental and len(config_paths) != 1:
        Log.e('--incremental can only be used with a single config file')
        return 1

    if args.dry_run:
        Log.i("Dry-run mode enabled: no external side effects will occur.")

    exit_code = 0
    total = len(config_paths)
    for idx, config_path in enumerate(config_paths, start=1):
        if total > 1 and idx > 1:
            print()
        if total > 1:
            Log.i(f"Processing config {idx}/{total}: {config_path}")
        else:
            Log.i(f"Processing config: {config_path}")
        result = process_config(config_path, args)
        exit_code = max(exit_code, result)

    return exit_code

if __name__ == "__main__":
    sys.exit(main())
