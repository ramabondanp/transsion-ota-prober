"""Terminal rendering of OTA update description HTML.

``TerminalParser`` converts the raw description HTML to ANSI-colored terminal
text. ``format_update_description`` is the public entry point used by the
processing pipeline.
"""

import re
from html.parser import HTMLParser

from checkota.constants import SECTION_HEADER_RE

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
# Keep newlines and tabs because the parser relies on whitespace; neutralise
# every other C0/C1 control so untrusted OTA text cannot emit ANSI escapes or
# other terminal-control sequences. Carriage returns are deliberately included:
# callers normalise CRLF before parsing, so a surviving \r is stray output, not
# a line break, and must not reach the terminal as a raw control byte.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
# Deeply nested (or unclosed) lists are attacker-controlled input: the indent is
# materialized per flushed line, so an unbounded depth made rendering quadratic.
_MAX_LIST_INDENT = 16


def _sanitize_terminal_text(value: str) -> str:
    text = _ANSI_ESCAPE_RE.sub("", value)
    return _CONTROL_RE.sub(lambda match: f"\\x{ord(match.group(0)):02x}", text)


class TerminalParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.indent = 0
        self.bold = False
        self.list_stack: list[str] = []
        self.ol_counter = []
        self.buffer = ""
        self.lines = []
        # Count of consecutive <br> tags that had no preceding content buffer.
        # Used to create exactly one blank line for section breaks (<br><br>)
        # without adding blanks after every single <br>.
        self._empty_br_count = 0

    def _push(self, line: str = ""):
        self.lines.append(line)

    def handle_starttag(self, tag, attrs):
        if tag == "b":
            self.bold = True
        elif tag in ("h3", "h4"):
            self.flush()
        elif tag == "ol":
            self.flush()
            self.list_stack.append("ol")
            self.ol_counter.append(0)
            self._refresh_indent()
        elif tag == "ul":
            self.flush()
            self.list_stack.append("ul")
            self._refresh_indent()
        elif tag == "li":
            self.flush()
        elif tag == "br":
            had_content = bool(self.buffer.strip())
            self.flush()
            if had_content:
                # Single <br> after content = line break, no blank line
                self._empty_br_count = 0
            else:
                # Empty separator <br> — first one in a row creates a blank line
                self._empty_br_count += 1
                if self._empty_br_count == 1 and (not self.lines or self.lines[-1]):
                    self._push("")

    def handle_endtag(self, tag):
        if tag == "b":
            self.flush()
            self.bold = False
            # After flushing bold content, reset counter to -1 so the next
            # <br> increments to 0 (no blank), and only a second <br> pushes blank
            self._empty_br_count = -1
        elif tag in ("h3", "h4"):
            self.flush(style=tag)
        elif tag in ("ol", "ul"):
            self.flush()
            if self.list_stack and self.list_stack[-1] == tag:
                self.list_stack.pop()
                if tag == "ol" and self.ol_counter:
                    self.ol_counter.pop()
                self._refresh_indent()

    def handle_data(self, data):
        # HTMLParser runs with convert_charrefs=True, so entity references are
        # already decoded exactly once here; calling html.unescape() again would
        # turn literal "&amp;lt;" into "<" and enable tag-injection surprises.
        self.buffer += _sanitize_terminal_text(data)

    def _refresh_indent(self) -> None:
        """Derive the list indent from the open-list depth, capped.

        Nesting is still tracked in full so end tags keep pairing correctly;
        only the rendered indentation is bounded.
        """
        self.indent = min(2 * len(self.list_stack), _MAX_LIST_INDENT)

    def flush(self, style: str | None = None) -> None:
        text = self.buffer.strip()
        self.buffer = ""
        if not text:
            return

        self._empty_br_count = 0

        prefix = " " * self.indent

        if style == "h3":
            self._push("\033[1;36m" + "=" * 60 + "\033[0m")
            self._push("\033[1;36m  " + text.upper() + "\033[0m")
            self._push("\033[1;36m" + "=" * 60 + "\033[0m")
            return
        if style == "h4":
            self._push("\033[1;33m  " + text + "\033[0m")
            return

        if self.list_stack:
            lst_type = self.list_stack[-1]
            if lst_type == "ol":
                self.ol_counter[-1] += 1
                bullet = f"{self.ol_counter[-1]}."
            else:
                bullet = "•"

            line = f"{bullet} {text}"
            if self.bold or text.endswith(":"):
                self._push(prefix + f"\033[1;32m{line}\033[0m")
            else:
                self._push(prefix + line)
            return

        if self.bold:
            self._push("\033[1m" + prefix + text + "\033[0m")
            return

        self._push(prefix + text)

    def render(self, markup: str) -> str:
        self.feed(markup)
        self.flush()
        result = "\n".join(self.lines).rstrip()
        # Collapse 3+ consecutive newlines to 2 (one blank line for section breaks)
        result = re.sub(r"\n{3,}", "\n\n", result)
        return result


def format_update_description(description: str) -> str:
    if not description:
        return ""

    # Normalise CRLF before any parsing. HTMLParser and the section regexes
    # treat only \n as a line boundary, so a lingering \r would survive as a
    # literal control byte (escaped to "\x0d") instead of the intended break.
    # Lone \r is left alone: this is a line-ending normalisation, not a
    # general control-character filter (that is _sanitize_terminal_text).
    description = description.replace("\r\n", "\n")

    # Transsion descriptions put Update Version directly after safety prose;
    # terminal output treats it as its own section without adding gaps before
    # every header-like line.
    description = re.sub(
        r"<br>\n(?=Update Version[^<]{0,80}<br>)",
        "<br><br>\n",
        description,
    )

    # Bold section headers before parsing (same pattern as Telegram sanitization).
    # Lines like "Android Version<br>" that are NOT inside <small>/<font> are headers.
    # Boundary is zero-width (lookbehind), so the leading \n/<br> is preserved
    # in place; the replacement only needs to wrap the header text.
    bolded = SECTION_HEADER_RE.sub(
        lambda m: "<b>" + m.group(1) + "</b><br>",
        description,
    )

    # Normalize explicit section breaks only; do not invent gaps before headers.
    normalized = re.sub(
        r"<br>([ \t]*\n){2,}", "<br><br>\n", bolded, flags=re.IGNORECASE
    )

    parser = TerminalParser()
    return parser.render(normalized or "")
