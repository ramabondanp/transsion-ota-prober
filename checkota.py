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
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple, List, Set

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

    @classmethod
    def from_yaml(cls, file: Path) -> 'Config':
        if not file.is_file():
            raise FileNotFoundError(f"Config file not found: {file}")

        with open(file, 'r') as f:
            data = yaml.safe_load(f)

        if not isinstance(data, dict):
            raise ValueError("Config file content is not a valid dictionary.")

        return cls(**data)

    def fingerprint(self) -> str:
        return (f'{self.oem}/{self.product}/{self.device}:'
                f'{self.android_version}/{self.build_tag}/'
                f'{self.incremental}:user/release-keys')

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
            telegraph_api = "https://api.telegra.ph/createPage"

            clean_content = content.replace('<br>', '\n').replace('<br/>', '\n').replace('<br />', '\n')

            payload = {
                "access_token": self.telegraph_token,
                "title": f"Update Details: {title}",
                "author_name": "TRANSSION Updates Tracker",
                "author_url": "https://t.me/TranssionUpdatesTracker",
                "content": [{"tag": "p", "children": [clean_content]}],
                "return_content": False
            }

            response = requests.post(telegraph_api, json=payload, timeout=10)
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

        sentence_endings = []
        for match in re.finditer(r'\.\s+', truncated):
            sentence_endings.append(match.end() - 1)

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
            import re
            desc_pattern = r'(<b>Title:</b> .*?\n\n)(.*?)(\n\n<b>Size:</b>)'
            match = re.search(desc_pattern, msg, re.DOTALL)

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
            try:
                if entry.name == b'update_url' or b'https://android.googleapis.com/packages/ota' in entry.value:
                    info['url'] = entry.value.decode('utf-8')
                    info['found'] = True
                    break
            except:
                continue

        if info['found']:
            for entry in resp.setting:
                try:
                    name = entry.name.decode('utf-8')
                    value = entry.value.decode('utf-8')

                    if name == 'update_title':
                        info['title'] = value.strip()
                    elif name == 'update_description':
                        info['description'] = self._clean_desc(value)
                    elif name == 'update_size':
                        info['size'] = value
                except:
                    continue

        return info

    @staticmethod
    def _clean_desc(text: str) -> str:
        text = re.sub(r'\n', '', text)
        text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
        text = re.sub(r'<[^>]*>', '', text)
        text = re.sub(r'\s*\(http[s]?://\S+\)?', '', text)
        return text.strip()

def check_cmd(cmd: str) -> bool:
    return shutil.which(cmd) is not None

def check_cmds(cmds: List[str]) -> bool:
    missing = [cmd for cmd in cmds if not check_cmd(cmd)]
    if missing:
        Log.e(f"Missing required command(s): {', '.join(missing)}")
        return False
    return True

def get_fingerprint(url: str) -> Optional[str]:
    Log.i("Fetching target fingerprint...")
    cmds = ['curl', 'bsdtar', 'grep', 'sed']
    if not check_cmds(cmds):
        return None

    curl_cmd = ['curl', '--fail', '-Ls', '--max-time', '60', '--limit-rate', '100K', url]
    bsdtar_cmd = ['bsdtar', '-Oxf', '-', 'META-INF/com/android/metadata']
    grep_cmd = ['grep', '-m1', '^post-build=']
    sed_cmd = ['sed', 's/^post-build=//']

    try:
        curl_proc = subprocess.Popen(curl_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        bsdtar_proc = subprocess.Popen(bsdtar_cmd, stdin=curl_proc.stdout, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        curl_proc.stdout.close()
        grep_proc = subprocess.Popen(grep_cmd, stdin=bsdtar_proc.stdout, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        bsdtar_proc.stdout.close()
        sed_proc = subprocess.Popen(sed_cmd, stdin=grep_proc.stdout, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        grep_proc.stdout.close()

        try:
            stdout_bytes, _ = sed_proc.communicate(timeout=90)
            fp = stdout_bytes.decode('utf-8').strip()
        except subprocess.TimeoutExpired:
            Log.w("Timeout expired while fetching fingerprint.")
            return None

        if not fp:
            Log.w("Could not extract fingerprint (pipeline returned empty).")
            return None

        Log.i(f"Extracted fingerprint: {fp}")
        return fp

    except Exception as e:
        Log.e(f"Error setting up fingerprint pipeline: {e}")
        return None
    finally:
        if 'curl_proc' in locals() and curl_proc and curl_proc.poll() is None:
            Log.i(f"Cleaning up leftover curl process (PID: {curl_proc.pid})...")
            curl_proc.kill()

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

    release_notes = f"""# {device}
## Changelog:
{description}

**Size:** {size}
**Download URL:** {url}
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

def main() -> int:
    if sys.version_info < (3, 7):
        Log.e("Requires Python 3.7+")
        return 1

    parser = argparse.ArgumentParser(description='Android OTA Update Checker')
    parser.add_argument('--debug', action='store_true', help='Enable debugging')
    parser.add_argument('-c', '--config', type=Path, required=True, help='Config file path')
    parser.add_argument('--dry-run', action='store_true', help='Simulate actions without making changes or sending notifications')
    parser.add_argument('--skip-telegram', action='store_true', help='Skip Telegram notifications')
    parser.add_argument('--register-fingerprint', action='store_true', help='Save the update fingerprint without sending a notification')
    parser.add_argument('--force-notify', action='store_true', help='Send notification even if the update has been seen before')
    parser.add_argument('--force-release', action='store_true', help='Create GitHub release even without Telegram token or if fingerprint already exists')
    parser.add_argument('-i', '--incremental', help='Override incremental version')
    args = parser.parse_args()

    try:
        cfg = Config.from_yaml(args.config)
        if args.incremental:
            Log.i(f"Override incremental: {args.incremental}")
            cfg.incremental = args.incremental
    except Exception as e:
        Log.e(f"Config error: {e}")
        return 1

    config_name = args.config.stem

    if args.dry_run:
        Log.i("Dry-run mode enabled: no external side effects will occur.")

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

    checker = UpdateChecker(cfg)
    fp = cfg.fingerprint()
    Log.i(f"Device: {cfg.model} ({cfg.device})")
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

    target_fp = get_fingerprint(url)
    if not target_fp:
        Log.e("Could not determine target fingerprint. Cannot verify if update is new.")
        return 1

    Log.i(f"Target build: {target_fp}")

    processed_fp_path = Path(PROCESSED_FP_FILE)
    processed_fingerprints = load_processed_fingerprints(processed_fp_path)
    is_new_update = target_fp not in processed_fingerprints

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
                Log.s("Update check completed successfully (fingerprint registered).")
        else:
            Log.i("--register-fingerprint flag is set, but fingerprint is already known. No action taken.")
        return 0

    if not is_new_update and args.force_notify:
        Log.w(f"Forcing notification for an already processed update: {target_fp}")

    if not is_new_update and args.force_release:
        Log.w(f"Forcing GitHub release for an already processed update: {target_fp}")

    data['fingerprint'] = target_fp

    if not args.skip_telegram and tg:
        msg = (
            f"<blockquote><b>OTA Update Available</b></blockquote>\n\n"
            f"<b>Device:</b> {cfg.model}\n\n"
            f"<b>Title:</b> {title}\n\n"
            f"{desc}\n\n"
            f"<b>Size:</b> {size}\n"
            f"<b>Fingerprint:</b>\n<code>{target_fp}</code>"
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

    Log.s("Update check completed successfully")
    return 0

if __name__ == "__main__":
    sys.exit(main())
