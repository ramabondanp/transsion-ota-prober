"""Pure Telegram-message text processing: sanitization, canonicalization,
and rendered-length fitting.

No I/O and no Telegram API knowledge -- everything here is a string-to-string
(or string-to-None) transformation, so it can be tested without network or
credentials. TgNotify composes these functions with the actual HTTP dispatch;
the ``TgNotify._*`` staticmethod aliases preserve the historical call surface.
"""

from __future__ import annotations

import html
import re
from html.entities import html5

from checkota.constants import SECTION_HEADER_RE
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


def is_safe_code_point(char: str, *, allow_newline: bool = True) -> bool:
    code_point = ord(char)
    return not (
        code_point < 0x20
        and (char != "\n" or not allow_newline)
        or 0x7F <= code_point <= 0x9F
        or 0xD800 <= code_point <= 0xDFFF
        or 0xFDD0 <= code_point <= 0xFDEF
        or code_point & 0xFFFF in (0xFFFE, 0xFFFF)
    )


def escape_telegram_char(char: str, *, attribute: bool = False) -> str:
    if char == "&":
        return "&amp;"
    if char == "<":
        return "&lt;"
    if char == ">":
        return "&gt;"
    if attribute and char == '"':
        return "&quot;"
    return char


def canonicalize_text(text: str, *, attribute: bool = False) -> list[str] | None:
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
            if not is_safe_code_point(char, allow_newline=not from_entity):
                return None
            fragments.append(escape_telegram_char(char, attribute=attribute))
    return fragments


def tokenize_telegram_html(
    value: str,
) -> list[tuple[str, str, str | None]] | None:
    """Canonicalize and tokenize supported, balanced Telegram HTML."""
    tokens: list[tuple[str, str, str | None]] = []
    stack: list[str] = []
    last_end = 0

    def add_text(text: str) -> bool:
        fragments = canonicalize_text(text)
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
                href = canonicalize_text(anchor_match.group("href"), attribute=True)
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


def rendered_units(value: str) -> int:
    """Count Telegram-visible UTF-16 code units conservatively."""
    return len(value.encode("utf-16-le", errors="surrogatepass")) // 2


def fallback_plain_text(value: str) -> str | None:
    """Last-resort rendering when structured canonicalization fails.

    Decodes entities, drops code points Telegram rejects, and escapes all
    markup literally, so a notification is degraded (markup shown verbatim
    as text) instead of dropped entirely. Returns None when nothing
    sendable remains.
    """
    decoded = html.unescape(value)
    kept = "".join(char for char in decoded if is_safe_code_point(char))
    if not kept.strip():
        return None
    return kept.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def rendered_length(value: str) -> int:
    tokens = tokenize_telegram_html(value)
    if tokens is None:
        return rendered_units(value)
    return sum(
        rendered_units(html.unescape(raw)) for kind, raw, _ in tokens if kind == "text"
    )


def fit_telegram_html(value: str, max_len: int) -> str | None:
    """Canonicalize and fit HTML by Telegram's rendered UTF-16 limit."""
    if max_len <= 0:
        return None

    tokens = tokenize_telegram_html(value)
    if tokens is None:
        return None

    rendered_len = sum(
        rendered_units(html.unescape(raw)) for kind, raw, _ in tokens if kind == "text"
    )
    canonical = "".join(raw for _, raw, _ in tokens)
    if rendered_len <= max_len:
        return canonical

    ellipsis = "." * min(3, max_len)
    ellipsis_len = rendered_units(ellipsis)
    selected: list[tuple[str, str, str | None]] = []
    open_tags: list[tuple[str, str]] = []
    selected_rendered_len = 0

    def closing_markup(tags: list[tuple[str, str]]) -> str:
        return "".join(f"</{name}>" for name, _ in reversed(tags))

    def fits(candidate_rendered_len: int) -> bool:
        return candidate_rendered_len + ellipsis_len <= max_len

    for kind, raw, name in tokens:
        next_tags = open_tags
        next_rendered_len = selected_rendered_len
        if kind == "text":
            next_rendered_len += rendered_units(html.unescape(raw))
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
    if rendered_length(result) > max_len:
        return None
    return result


def escape_text_preserving_telegram_tags(value: str) -> str | None:
    """Canonicalize text and attributes while preserving balanced tags."""
    tokens = tokenize_telegram_html(value)
    if tokens is None:
        return None
    return "".join(raw for _, raw, _ in tokens)


def sanitize_html(value: str) -> str | None:
    if not value:
        return value
    if any(not is_safe_code_point(char) for char in value):
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
        value,
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
    canonical = escape_text_preserving_telegram_tags(sanitized)
    if canonical is not None:
        return canonical
    # Unbalanced/unsupported markup must not silently drop the whole
    # notification; degrade to literal plain text instead.
    Log.w("Telegram HTML structure is invalid; falling back to plain text")
    return fallback_plain_text(sanitized)
