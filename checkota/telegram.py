from __future__ import annotations

import html
import re
from typing import cast

import requests

from checkota import message_text
from checkota.constants import (
    DESC_SECTION_RE,
    SENTENCE_BOUNDARY_RE,
    TELEGRAM_API_TIMEOUT_SECONDS,
    TELEGRAPH_API_TIMEOUT_SECONDS,
    TELEGRAPH_API_URL,
)
from checkota.logging import Log
from checkota.message_text import (
    fallback_plain_text,
    fit_telegram_html,
    rendered_length,
    sanitize_html,
)
from checkota.validation import has_unsafe_url_chars

# requests treats None proxy values as "disable proxies" at runtime, but its
# typeshed annotation only admits str values; normalize the type once here.
_NO_PROXIES = cast("dict[str, str]", {"http": None, "https": None, "all": None})


class TgNotify:
    MAX_LEN = 4090
    DESC_MAX_LEN = 1500

    # Text canonicalization/sanitization/fitting live in message_text.py as
    # pure functions; these aliases preserve the historical TgNotify._* call
    # surface used across the codebase and tests.
    _sanitize_html = staticmethod(message_text.sanitize_html)
    _rendered_length = staticmethod(message_text.rendered_length)
    _fit_telegram_html = staticmethod(message_text.fit_telegram_html)
    _fallback_plain_text = staticmethod(message_text.fallback_plain_text)
    _tokenize_telegram_html = staticmethod(message_text.tokenize_telegram_html)
    _rendered_units = staticmethod(message_text.rendered_units)
    _canonicalize_text = staticmethod(message_text.canonicalize_text)
    _is_safe_code_point = staticmethod(message_text.is_safe_code_point)
    _escape_telegram_char = staticmethod(message_text.escape_telegram_char)
    _escape_text_preserving_telegram_tags = staticmethod(
        message_text.escape_text_preserving_telegram_tags
    )

    def __init__(
        self,
        token: str,
        chat_id: str,
        telegraph_token: str | None = "",
        session: requests.Session | None = None,
    ):
        if not token or not chat_id:
            raise ValueError("Bot token and chat ID required")
        self.token = token
        self.chat_id = chat_id
        self.telegraph_token = telegraph_token or ""
        self.url = f"https://api.telegram.org/bot{token}"
        if session is None:
            sess = requests.Session()
            sess.trust_env = False
            self.session = sess
        else:
            self.session = session

    def _redact(self, value: object) -> str:
        """Return str(value) with the bot token scrubbed.

        requests includes the full request URL in HTTPError/ConnectionError
        messages, and self.url embeds the bot token; never let it reach logs.
        """
        text = str(value)
        return text.replace(self.token, "***") if self.token else text

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
        # Normalize any leftover <br> alternatives to newlines (defensive)
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
                line_children: list = []
                last_end = 0
                for match in re.finditer(r"<b>(.*?)</b>", line):
                    if match.start() > last_end:
                        text = line[last_end : match.start()]
                        if text:
                            line_children.append(html.unescape(text))
                    line_children.append(
                        {"tag": "b", "children": [html.unescape(match.group(1))]}
                    )
                    last_end = match.end()

                if last_end < len(line):
                    text = line[last_end:]
                    if text:
                        line_children.append(html.unescape(text))

                # Add line children to paragraph
                para_children.extend(line_children)

                # Add <br> between lines (not after the last line)
                if idx < len(lines) - 1:
                    para_children.append({"tag": "br"})

            if para_children:
                nodes.append({"tag": "p", "children": para_children})

        return (
            nodes
            if nodes
            else [{"tag": "p", "children": [html.unescape(html_content)]}]
        )

    def _create_telegraph_page(self, title: str, content: str) -> str | None:
        try:
            content_nodes = self._html_to_telegraph_nodes(content)

            payload = {
                "access_token": self.telegraph_token,
                "title": f"Update Details: {html.unescape(title)}",
                "author_name": "TRANSSION Updates Tracker",
                "author_url": "https://t.me/TranssionUpdatesTracker",
                "content": content_nodes,
                "return_content": False,
            }

            response = self.session.post(
                TELEGRAPH_API_URL,
                json=payload,
                proxies=_NO_PROXIES,
                timeout=TELEGRAPH_API_TIMEOUT_SECONDS,
            )
            response.raise_for_status()

            result = response.json()
            if result.get("ok"):
                telegraph_url = result["result"]["url"]
                Log.s(f"Created Telegraph page: {telegraph_url}")
                return telegraph_url
            Log.w(f"Telegraph API error: {result}")
            return None

        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            Log.w(f"Failed to create Telegraph page: {exc}")
            return None

    def _telegraph_link_suffix(self, telegraph_url: str | None) -> str:
        """Build the "Read full changelogs" suffix for a Telegraph URL.

        The URL is interpolated into an HTML attribute of a message that is
        canonicalized as a whole, so it is escaped and rejected when it carries
        characters no URL may contain. An unescaped quote (or a newline) used to
        unbalance the tag stream, which made the final fit fail and dropped the
        entire notification after the page had already been created.
        """
        if not telegraph_url:
            return ""
        url = str(telegraph_url)
        if has_unsafe_url_chars(url):
            Log.w("Ignoring Telegraph link containing whitespace or control characters.")
            return ""
        return (
            f' <a href="{html.escape(url, quote=True)}">Read full changelogs</a>'
        )

    @staticmethod
    def _prefer_sentence_boundary(text: str, max_len: int) -> str:
        """Trim back to the last sentence break when that keeps most of the text.

        The token-granular fit cuts mid-word. Sentences are the readable unit for
        a changelog, so prefer the last complete sentence as long as at least
        half of the fitted text survives; a cut that lands inside a tag is
        rejected by re-canonicalization and the original text is kept.
        """
        boundaries = list(SENTENCE_BOUNDARY_RE.finditer(text))
        if not boundaries:
            return text
        candidate = text[: boundaries[-1].end()].rstrip()
        if not candidate or rendered_length(candidate) * 2 < rendered_length(text):
            return text
        refitted = fit_telegram_html(candidate, max_len)
        return refitted if refitted is not None else text

    def _truncate_desc(
        self, desc: str, max_len: int | None = None, telegraph_url: str | None = None
    ) -> str:
        if max_len is None:
            max_len = self.DESC_MAX_LEN

        link_suffix = self._telegraph_link_suffix(telegraph_url)

        if rendered_length(desc) <= max_len:
            return desc

        effective_max_len = max_len - rendered_length(link_suffix)
        truncated = fit_telegram_html(desc, effective_max_len)
        if truncated is None:
            # Degrade to escaped plain text rather than discarding the whole
            # description and keeping only the link.
            plain_desc = fallback_plain_text(desc)
            if plain_desc:
                truncated = fit_telegram_html(plain_desc, effective_max_len)
        if truncated is None:
            return link_suffix.lstrip()
        return self._prefer_sentence_boundary(truncated, effective_max_len) + link_suffix

    def send(
        self,
        msg: str,
        btn_text: str | None = None,
        btn_url: str | None = None,
        truncate_desc: bool = True,
        device_title: str | None = None,
    ) -> bool:
        """Canonicalize, length-fit, and dispatch one notification.

        Despite the name this runs the whole message pipeline -- sanitize,
        optional description truncation with a Telegraph fallback, final
        UTF-16 limit enforcement -- before the API call.
        """
        Log.i("Sending Telegram notification...")

        raw_msg = msg
        canonical_msg = sanitize_html(raw_msg)
        if canonical_msg is None:
            # Unsafe code points survived sanitization; degrade to plain text
            # rather than dropping the notification entirely.
            Log.w("Notification HTML could not be canonicalized; sending as plain text")
            canonical_msg = fallback_plain_text(raw_msg)
            if canonical_msg is None:
                Log.e("Notification contained no sendable content after sanitization")
                return False
        msg = canonical_msg

        telegraph_url = None

        if truncate_desc and rendered_length(msg) > self.MAX_LEN:
            match = DESC_SECTION_RE.search(msg)

            if match:
                before_desc = match.group(1)
                description = match.group(2).strip()
                after_desc = match.group(3)

                if rendered_length(description) > self.DESC_MAX_LEN:
                    title_match = re.search(r"<b>Title:</b> (.*?)\n", before_desc)
                    matched_title = title_match.group(1) if title_match else None
                    page_title = matched_title or device_title or "Update"
                    if self.telegraph_token:
                        telegraph_url = self._create_telegraph_page(
                            page_title, description
                        )

                    truncated_desc = self._truncate_desc(
                        description, telegraph_url=telegraph_url
                    )

                    msg = msg.replace(
                        match.group(0), before_desc + truncated_desc + after_desc
                    )

        # The description-specific path is only an optimization. Always apply
        # the final Telegram limit after all sanitization and optional rewriting.
        fitted_msg = fit_telegram_html(msg, self.MAX_LEN)
        if fitted_msg is None:
            # Fail safe instead of dropping the notification: the same escaped
            # plain-text degradation `sanitize_html` uses when markup cannot be
            # canonicalized. Only a message with nothing sendable is refused.
            Log.w("Notification HTML could not be fitted; sending as plain text")
            plain_msg = fallback_plain_text(msg)
            fitted_msg = (
                fit_telegram_html(plain_msg, self.MAX_LEN) if plain_msg else None
            )
        if fitted_msg is None:
            Log.e("Failed to fit Telegram notification within the final length limit")
            return False
        msg = fitted_msg

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
                f"{self.url}/sendMessage",
                json=payload,
                proxies=_NO_PROXIES,
                timeout=TELEGRAM_API_TIMEOUT_SECONDS,
            )
            response.raise_for_status()

            result = response.json()
            if not isinstance(result, dict) or not result.get("ok"):
                Log.e(f"Telegram API error: {result}")
                return False

            Log.s("Notification sent successfully")
            return True

        except requests.HTTPError as exc:
            detail = ""
            if exc.response is not None:
                try:
                    detail = exc.response.text
                except requests.RequestException:
                    detail = str(exc.response)
            Log.e(
                "Failed to send notification: "
                f"{self._redact(exc)} - {self._redact(detail)}"
            )
            return False
        except (
            requests.RequestException,
            ValueError,
            TypeError,
            KeyError,
        ) as exc:
            Log.e(f"Failed to send notification: {self._redact(exc)}")
            return False
