import re
from typing import Optional

import requests

from modules.constants import DESC_SECTION_RE, SENTENCE_BOUNDARY_RE, TELEGRAPH_API_URL
from modules.logging import Log


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
            clean_content = content.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")

            payload = {
                "access_token": self.telegraph_token,
                "title": f"Update Details: {title}",
                "author_name": "TRANSSION Updates Tracker",
                "author_url": "https://t.me/TranssionUpdatesTracker",
                "content": [{"tag": "p", "children": [clean_content]}],
                "return_content": False,
            }

            response = requests.post(TELEGRAPH_API_URL, json=payload, timeout=10)
            response.raise_for_status()

            result = response.json()
            if result.get("ok"):
                telegraph_url = result["result"]["url"]
                Log.s(f"Created Telegraph page: {telegraph_url}")
                return telegraph_url
            Log.w(f"Telegraph API error: {result}")
            return None

        except Exception as exc:
            Log.w(f"Failed to create Telegraph page: {exc}")
            return None

    def _truncate_desc(
        self, desc: str, max_len: int = None, telegraph_url: Optional[str] = None
    ) -> str:
        if max_len is None:
            max_len = self.DESC_MAX_LEN

        link_text = f'... <a href="{telegraph_url}">Read full changelogs</a>' if telegraph_url else "..."
        effective_max_len = max_len - len(link_text) if telegraph_url else max_len

        if len(desc) <= max_len:
            return desc

        truncated = desc[:effective_max_len]

        sentence_endings = [match.end() - 1 for match in SENTENCE_BOUNDARY_RE.finditer(truncated)]

        if sentence_endings and sentence_endings[-1] > effective_max_len * 0.6:
            result = truncated[: sentence_endings[-1] + 1]
        else:
            last_paragraph = truncated.rfind("\n\n")
            if last_paragraph > effective_max_len * 0.5:
                result = truncated[:last_paragraph]
            else:
                last_line = truncated.rfind("\n")
                if last_line > effective_max_len * 0.7:
                    result = truncated[:last_line]
                else:
                    last_space = truncated.rfind(" ")
                    if last_space > effective_max_len * 0.8:
                        result = truncated[:last_space]
                    else:
                        result = truncated

        result += link_text
        return result

    def send(
        self,
        msg: str,
        btn_text: Optional[str] = None,
        btn_url: Optional[str] = None,
        truncate_desc: bool = True,
        device_title: Optional[str] = None,
    ) -> bool:
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
                    title_match = re.search(r"<b>Title:</b> (.*?)\n", before_desc)
                    page_title = title_match.group(1) if title_match else (device_title or "Update")
                    telegraph_url = self._create_telegraph_page(page_title, description)

                    truncated_desc = self._truncate_desc(description, telegraph_url=telegraph_url)

                    msg = msg.replace(match.group(0), before_desc + truncated_desc + after_desc)

        try:
            payload = {
                "chat_id": self.chat_id,
                "text": msg,
                "parse_mode": "html",
                "disable_web_page_preview": True,
            }

            if btn_text and btn_url:
                payload["reply_markup"] = {"inline_keyboard": [[{"text": btn_text, "url": btn_url}]]}

            response = requests.post(f"{self.url}/sendMessage", json=payload, timeout=15)
            response.raise_for_status()

            Log.s("Notification sent successfully")
            return True

        except Exception as exc:
            Log.e(f"Failed to send notification: {exc}")
            return False
