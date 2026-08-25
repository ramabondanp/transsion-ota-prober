from __future__ import annotations

import html
import re
from html.entities import html5
from typing import cast

import requests

from checkota.constants import (
    DESC_SECTION_RE,
    SECTION_HEADER_RE,
    TELEGRAPH_API_URL,
)
from checkota.logging import Log

_SUPPORTED_TAG_RE = re.compile(
    r"</?(?:b|code|blockquote)>|</a>|"
    r"<a\s+href=(?:\"[^\"]*\"|'[^']*')>",
    flags=re.IGNORECASE,
)
_HTML_ENTITY_RE = re.compile(r"&(?:#[xX][0-9A-Fa-f]+|#\d+|[A-Za-z][A-Za-z0-9]+);")
_ANCHOR_TAG_RE = re.compile(
    r"<a\s+href=(?P<quote>\"|')(?P<href>.*)(?P=quote)>",
    flags=re.IGNORECASE,
)

# requests treats None proxy values as "disable proxies" at runtime, but its
# typeshed annotation only admits str values; normalize the type once here.
_NO_PROXIES = cast("dict[str, str]", {"http": None, "https": None, "all": None})


class TgNotify:
    MAX_LEN = 4090
    DESC_MAX_LEN = 1500

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
                timeout=10,
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

    def _truncate_desc(
        self, desc: str, max_len: int | None = None, telegraph_url: str | None = None
    ) -> str:
        if max_len is None:
            max_len = self.DESC_MAX_LEN

        link_suffix = (
            f' <a href="{telegraph_url}">Read full changelogs</a>'
            if telegraph_url
            else ""
        )

        if self._rendered_length(desc) <= max_len:
            return desc

        effective_max_len = max_len - self._rendered_length(link_suffix)
        truncated = self._fit_telegram_html(desc, effective_max_len)
        if truncated is None:
            return link_suffix.lstrip()
        return truncated + link_suffix

    @staticmethod
    def _is_safe_code_point(char: str, *, allow_newline: bool = True) -> bool:
        code_point = ord(char)
        return not (
            code_point < 0x20
            and (char != "\n" or not allow_newline)
            or 0x7F <= code_point <= 0x9F
            or 0xD800 <= code_point <= 0xDFFF
            or 0xFDD0 <= code_point <= 0xFDEF
            or code_point & 0xFFFF in (0xFFFE, 0xFFFF)
        )

    @staticmethod
    def _escape_telegram_char(char: str, *, attribute: bool = False) -> str:
        if char == "&":
            return "&amp;"
        if char == "<":
            return "&lt;"
        if char == ">":
            return "&gt;"
        if attribute and char == '"':
            return "&quot;"
        return char

    @staticmethod
    def _canonicalize_text(text: str, *, attribute: bool = False) -> list[str] | None:
        """Decode HTML entities once and emit Telegram-safe text fragments."""
        fragments: list[str] = []
        position = 0
        while position < len(text):
            match = _HTML_ENTITY_RE.match(text, position)
            from_entity = match is not None
            if match is None:
                decoded = text[position]
                position += 1
            else:
                entity = match.group(0)
                position = match.end()
                if entity.startswith("&#"):
                    number = entity[2:-1]
                    base = 10
                    if number[:1].lower() == "x":
                        number = number[1:]
                        base = 16
                    try:
                        code_point = int(number, base)
                        decoded = chr(code_point)
                    except (ValueError, OverflowError):
                        return None
                else:
                    decoded = html5.get(entity[1:])
                    if decoded is None:
                        decoded = entity

            for char in decoded:
                if not TgNotify._is_safe_code_point(
                    char, allow_newline=not from_entity
                ):
                    return None
                fragments.append(
                    TgNotify._escape_telegram_char(char, attribute=attribute)
                )
        return fragments

    @staticmethod
    def _tokenize_telegram_html(value: str) -> list[tuple[str, str, str | None]] | None:
        """Canonicalize and tokenize supported, balanced Telegram HTML."""
        tokens: list[tuple[str, str, str | None]] = []
        stack: list[str] = []
        last_end = 0

        def add_text(text: str) -> bool:
            fragments = TgNotify._canonicalize_text(text)
            if fragments is None:
                return False
            tokens.extend(("text", fragment, None) for fragment in fragments)
            return True

        for match in _SUPPORTED_TAG_RE.finditer(value):
            if not add_text(value[last_end : match.start()]):
                return None

            matched_tag = match.group(0)
            is_closing = matched_tag.startswith("</")
            name = (
                "a"
                if re.match(r"<\s*/?\s*a\b", matched_tag, re.IGNORECASE)
                else matched_tag[2 if is_closing else 1 : -1].lower()
            )
            if is_closing:
                if not stack or stack[-1] != name:
                    return None
                stack.pop()
                tokens.append(("tag_close", f"</{name}>", name))
            else:
                if name == "a":
                    anchor_match = _ANCHOR_TAG_RE.fullmatch(matched_tag)
                    if anchor_match is None:
                        return None
                    href = TgNotify._canonicalize_text(
                        anchor_match.group("href"), attribute=True
                    )
                    if href is None:
                        return None
                    raw_tag = f'<a href="{"".join(href)}">'
                else:
                    raw_tag = f"<{name}>"
                stack.append(name)
                tokens.append(("tag_open", raw_tag, name))
            last_end = match.end()

        if not add_text(value[last_end:]) or stack:
            return None
        return tokens

    @staticmethod
    def _rendered_units(value: str) -> int:
        """Count Telegram-visible UTF-16 code units conservatively."""
        return len(value.encode("utf-16-le", errors="surrogatepass")) // 2

    @staticmethod
    def _fallback_plain_text(value: str) -> str | None:
        """Last-resort rendering when structured canonicalization fails.

        Decodes entities, drops code points Telegram rejects, and escapes all
        markup literally, so a notification is degraded (markup shown verbatim
        as text) instead of dropped entirely. Returns None when nothing
        sendable remains.
        """
        decoded = html.unescape(value)
        kept = "".join(char for char in decoded if TgNotify._is_safe_code_point(char))
        if not kept.strip():
            return None
        return kept.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    @staticmethod
    def _rendered_length(value: str) -> int:
        tokens = TgNotify._tokenize_telegram_html(value)
        if tokens is None:
            return TgNotify._rendered_units(value)
        return sum(
            TgNotify._rendered_units(html.unescape(raw))
            for kind, raw, _ in tokens
            if kind == "text"
        )

    @staticmethod
    def _fit_telegram_html(value: str, max_len: int) -> str | None:
        """Canonicalize and fit HTML by Telegram's rendered UTF-16 limit."""
        if max_len <= 0:
            return None

        tokens = TgNotify._tokenize_telegram_html(value)
        if tokens is None:
            return None

        rendered_len = sum(
            TgNotify._rendered_units(html.unescape(raw))
            for kind, raw, _ in tokens
            if kind == "text"
        )
        canonical = "".join(raw for _, raw, _ in tokens)
        if rendered_len <= max_len:
            return canonical

        ellipsis = "." * min(3, max_len)
        ellipsis_len = TgNotify._rendered_units(ellipsis)
        selected: list[tuple[str, str, str | None]] = []
        open_tags: list[tuple[str, str]] = []
        selected_rendered_len = 0

        def closing_markup(tags: list[tuple[str, str]]) -> str:
            return "".join(f"</{name}>" for name, _ in reversed(tags))

        def fits(
            candidate_rendered_len: int,
        ) -> bool:
            return candidate_rendered_len + ellipsis_len <= max_len

        for kind, raw, name in tokens:
            next_tags = open_tags
            next_rendered_len = selected_rendered_len
            if kind == "text":
                next_rendered_len += TgNotify._rendered_units(html.unescape(raw))
            elif kind == "tag_open":
                if name is None:
                    return None
                next_tags = [*open_tags, (name, raw)]
            elif kind == "tag_close":
                if not open_tags or name != open_tags[-1][0]:
                    return None
                next_tags = open_tags[:-1]

            if not fits(next_rendered_len):
                break

            selected.append((kind, raw, name))
            selected_rendered_len = next_rendered_len
            open_tags = next_tags

        result = "".join(raw for _, raw, _ in selected)
        result += ellipsis + closing_markup(open_tags)
        if TgNotify._rendered_length(result) > max_len:
            return None
        return result

    @staticmethod
    def _escape_text_preserving_telegram_tags(html: str) -> str | None:
        """Canonicalize text and attributes while preserving balanced tags."""
        tokens = TgNotify._tokenize_telegram_html(html)
        if tokens is None:
            return None
        return "".join(raw for _, raw, _ in tokens)

    @staticmethod
    def _sanitize_html(html: str) -> str | None:
        if not html:
            return html
        if any(not TgNotify._is_safe_code_point(char) for char in html):
            return None

        # --- Step 1: Bold section headers in raw HTML ---
        # Headers are short lines followed by <br> and not wrapped in tags.
        # The structure in OTA descriptions is consistently:
        #   <small><font>content</font></small><br>
        #   HEADER<br>
        # Wrap only the un-wrapped header lines in <b>.
        # Boundary is zero-width (lookbehind), so the leading \n/<br> is
        # preserved in place; the replacement only wraps the header text.
        sanitized = SECTION_HEADER_RE.sub(
            lambda m: "<b>" + m.group(1) + "</b><br>",
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
        canonical = TgNotify._escape_text_preserving_telegram_tags(sanitized)
        if canonical is not None:
            return canonical
        # Unbalanced/unsupported markup must not silently drop the whole
        # notification; degrade to literal plain text instead.
        Log.w("Telegram HTML structure is invalid; falling back to plain text")
        return TgNotify._fallback_plain_text(sanitized)

    def send(
        self,
        msg: str,
        btn_text: str | None = None,
        btn_url: str | None = None,
        truncate_desc: bool = True,
        device_title: str | None = None,
    ) -> bool:
        Log.i("Sending Telegram notification...")

        raw_msg = msg
        canonical_msg = self._sanitize_html(raw_msg)
        if canonical_msg is None:
            # Unsafe code points survived sanitization; degrade to plain text
            # rather than dropping the notification entirely.
            Log.w("Notification HTML could not be canonicalized; sending as plain text")
            canonical_msg = TgNotify._fallback_plain_text(raw_msg)
            if canonical_msg is None:
                Log.e("Notification contained no sendable content after sanitization")
                return False
        msg = canonical_msg

        telegraph_url = None

        if truncate_desc and self._rendered_length(msg) > self.MAX_LEN:
            match = DESC_SECTION_RE.search(msg)

            if match:
                before_desc = match.group(1)
                description = match.group(2).strip()
                after_desc = match.group(3)

                if self._rendered_length(description) > self.DESC_MAX_LEN:
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
        fitted_msg = self._fit_telegram_html(msg, self.MAX_LEN)
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
                timeout=15,
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
            Log.e(f"Failed to send notification: {exc} - {detail}")
            return False
        except (
            requests.RequestException,
            ValueError,
            TypeError,
            KeyError,
        ) as exc:
            Log.e(f"Failed to send notification: {exc}")
            return False
