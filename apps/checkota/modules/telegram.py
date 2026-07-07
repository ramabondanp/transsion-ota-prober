import re
import requests

from modules.constants import (
    DESC_SECTION_RE,
    SECTION_HEADER_RE,
    SENTENCE_BOUNDARY_RE,
    TELEGRAPH_API_URL,
)
from modules.logging import Log


class TgNotify:
    MAX_LEN = 4090
    DESC_MAX_LEN = 1500

    def __init__(
        self,
        token: str,
        chat_id: str,
        telegraph_token: str,
        session: requests.Session | None = None,
    ):
        if not token or not chat_id:
            raise ValueError("Bot token and chat ID required")
        if not telegraph_token:
            raise ValueError("Telegraph token is required")
        self.token = token
        self.chat_id = chat_id
        self.telegraph_token = telegraph_token
        self.url = f"https://api.telegram.org/bot{token}"
        self.session = session or requests.Session()

    @staticmethod
    def _html_to_telegraph_nodes(html_content: str) -> list:
        """Convert simple HTML (bold tags + newlines) to Telegra.ph NodeElement array.

        Handles:
          - <b>bold</b>  → {"tag": "b", "children": ["bold"]}
          - \n           → {"tag": "br"} (single newline within paragraph)
          - \n\n         → paragraph boundary (new <p> element)
          - Plain text   → string child
          - Strips any leftover <small>, <font>, <a> tags (keeps text)
        """
        # Strip tags that Telegraph doesn't support, keep text content
        cleaned = re.sub(r"<\s*/?\s*small\s*>", "", html_content, flags=re.IGNORECASE)
        cleaned = re.sub(r"<\s*font\b[^>]*>", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"</\s*font\s*>", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"<\s*a\b[^>]*>", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"</\s*a\s*>", "", cleaned, flags=re.IGNORECASE)
        # Normalize any leftover <br> variants to newlines (defensive)
        cleaned = re.sub(r"<\s*br\s*/?\s*>", "\n", cleaned, flags=re.IGNORECASE)

        # Split into paragraphs by double+ newlines
        paragraphs = re.split(r"\n\n+", cleaned)

        nodes = []
        for para in paragraphs:
            para = para.strip()
            if not para:
                continue

            # Split paragraph into lines (single \n = <br> in Telegraph)
            lines = para.split("\n")

            para_children = []
            for idx, line in enumerate(lines):
                line = line.strip()
                if not line:
                    # Empty line within a paragraph — skip
                    continue

                # Parse inline <b>bold</b> tags within this line
                line_children = []
                last_end = 0
                for match in re.finditer(r"<b>(.*?)</b>", line):
                    if match.start() > last_end:
                        text = line[last_end : match.start()]
                        if text:
                            line_children.append(text)
                    line_children.append({"tag": "b", "children": [match.group(1)]})
                    last_end = match.end()

                if last_end < len(line):
                    text = line[last_end:]
                    if text:
                        line_children.append(text)

                # Add line children to paragraph
                para_children.extend(line_children)

                # Add <br> between lines (not after the last line)
                if idx < len(lines) - 1:
                    para_children.append({"tag": "br"})

            if para_children:
                nodes.append({"tag": "p", "children": para_children})

        return nodes if nodes else [{"tag": "p", "children": [html_content]}]

    def _create_telegraph_page(self, title: str, content: str) -> str | None:
        try:
            content_nodes = self._html_to_telegraph_nodes(content)

            payload = {
                "access_token": self.telegraph_token,
                "title": f"Update Details: {title}",
                "author_name": "TRANSSION Updates Tracker",
                "author_url": "https://t.me/TranssionUpdatesTracker",
                "content": content_nodes,
                "return_content": False,
            }

            response = self.session.post(TELEGRAPH_API_URL, json=payload, timeout=10)
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
        self, desc: str, max_len: int | None = None, telegraph_url: str | None = None
    ) -> str:
        if max_len is None:
            max_len = self.DESC_MAX_LEN

        link_text = (
            f'... <a href="{telegraph_url}">Read full changelogs</a>'
            if telegraph_url
            else "..."
        )
        effective_max_len = max_len - len(link_text) if telegraph_url else max_len

        if len(desc) <= effective_max_len:
            return desc

        truncated = desc[:effective_max_len]

        sentence_endings = [
            match.end() - 1 for match in SENTENCE_BOUNDARY_RE.finditer(truncated)
        ]

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

    @staticmethod
    def _escape_text_preserving_telegram_tags(html: str) -> str:
        """Escape text nodes while preserving trusted Telegram HTML tags."""
        tag_re = re.compile(
            r"</?(?:b|code|blockquote)>|<a\s+href=\"[^\"]+\">|</a>",
            flags=re.IGNORECASE,
        )
        pieces: list[str] = []
        last_end = 0
        for match in tag_re.finditer(html):
            if match.start() > last_end:
                text = html[last_end : match.start()]
                text = re.sub(
                    r"&(?!#\d+;|#x[0-9A-Fa-f]+;|[A-Za-z][A-Za-z0-9]+;)",
                    "&amp;",
                    text,
                )
                pieces.append(text.replace("<", "&lt;").replace(">", "&gt;"))
            pieces.append(match.group(0))
            last_end = match.end()
        if last_end < len(html):
            text = html[last_end:]
            text = re.sub(
                r"&(?!#\d+;|#x[0-9A-Fa-f]+;|[A-Za-z][A-Za-z0-9]+;)",
                "&amp;",
                text,
            )
            pieces.append(text.replace("<", "&lt;").replace(">", "&gt;"))
        return "".join(pieces)

    @staticmethod
    def _sanitize_html(html: str) -> str:
        if not html:
            return html

        # --- Step 1: Bold section headers in raw HTML ---
        # Headers are short lines followed by <br> that are NOT wrapped in <small>/<font>.
        # The structure in OTA descriptions is consistently:
        #   <small><font>content</font></small><br>
        #   HEADER<br>
        # Wrap only the un-wrapped header lines in <b>.
        sanitized = SECTION_HEADER_RE.sub(
            lambda m: (
                ("\n" if m.group(0).startswith("\n") else "")
                + "<b>"
                + m.group(1)
                + "</b><br>"
            ),
            html,
        )

        # --- Step 2: Replace <br> with newlines ---
        # Consume inline whitespace after <br> plus at most one \n,
        # so <br>\n becomes \n (not \n\n) but <br>\n\n keeps \n\n.
        sanitized = re.sub(
            r"<\s*br\s*/?\s*>[^\S\n]*\n?", "\n", sanitized, flags=re.IGNORECASE
        )

        # --- Step 3: Strip unsupported HTML tags ---
        sanitized = re.sub(r"<\s*/?\s*small\s*>", "", sanitized, flags=re.IGNORECASE)
        sanitized = re.sub(r"<\s*font\b[^>]*>", "", sanitized, flags=re.IGNORECASE)
        sanitized = re.sub(r"</\s*font\s*>", "", sanitized, flags=re.IGNORECASE)
        # Strip all <a> tags, keeping their text content.
        # Telegram HTML only allows <a href="...">, and arbitrary links from OTA
        # descriptions should not be sent as clickable URLs.
        sanitized = re.sub(r"<\s*a\b[^>]*>", "", sanitized, flags=re.IGNORECASE)
        sanitized = re.sub(r"</\s*a\s*>", "", sanitized, flags=re.IGNORECASE)

        # --- Step 4: Normalize common bullet characters ---
        for bullet in ("\u2022", "\u2023", "\u2043", "\u2219", "\xb7"):
            sanitized = sanitized.replace(bullet, "- ")
        sanitized = sanitized.replace("\u00c2", "")

        # --- Step 5: Normalize whitespace ---
        # Collapse extra spaces on blank lines and limit consecutive blanks.
        lines = []
        prev_blank = False
        for line in sanitized.splitlines():
            stripped = line.strip()
            if stripped:
                lines.append(stripped)
                prev_blank = False
            else:
                if not prev_blank:
                    lines.append("")
                prev_blank = True

        sanitized = "\n".join(lines).strip()
        sanitized = re.sub(r":\n\n", ":\n", sanitized)
        sanitized = re.sub(r"\n\n(-\s+)", r"\n\1", sanitized)
        sanitized = re.sub(r"\n\n(\d+\.)", r"\n\1", sanitized)
        sanitized = re.sub(r"-\s{2,}", "- ", sanitized)
        sanitized = re.sub(r"[ \t]*\(\s*https?://[^\)]*\)", "", sanitized)
        sanitized = re.sub(r"\n[ \t]+", "\n", sanitized)
        sanitized = re.sub(r"[ \t]{2,}", " ", sanitized)
        sanitized = sanitized.replace(" \n", "\n").strip()
        return TgNotify._escape_text_preserving_telegram_tags(sanitized)

    def send(
        self,
        msg: str,
        btn_text: str | None = None,
        btn_url: str | None = None,
        truncate_desc: bool = True,
        device_title: str | None = None,
    ) -> bool:
        Log.i("Sending Telegram notification...")

        msg = self._sanitize_html(msg)

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
                    page_title = (
                        title_match.group(1)
                        if title_match
                        else (device_title or "Update")
                    )
                    telegraph_url = self._create_telegraph_page(page_title, description)

                    truncated_desc = self._truncate_desc(
                        description, telegraph_url=telegraph_url
                    )

                    msg = msg.replace(
                        match.group(0), before_desc + truncated_desc + after_desc
                    )

        try:
            payload = {
                "chat_id": self.chat_id,
                "text": msg,
                "parse_mode": "html",
                "disable_web_page_preview": True,
            }

            if btn_text and btn_url:
                payload["reply_markup"] = {
                    "inline_keyboard": [[{"text": btn_text, "url": btn_url}]]
                }

            response = self.session.post(
                f"{self.url}/sendMessage", json=payload, timeout=15
            )
            response.raise_for_status()

            Log.s("Notification sent successfully")
            return True

        except requests.HTTPError as exc:
            detail = ""
            if exc.response is not None:
                try:
                    detail = exc.response.text
                except Exception:
                    detail = str(exc.response)
            Log.e(f"Failed to send notification: {exc} - {detail}")
            return False
        except Exception as exc:
            Log.e(f"Failed to send notification: {exc}")
            return False
