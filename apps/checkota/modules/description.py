"""Terminal rendering of OTA update description HTML.

``TerminalParser`` converts the raw description HTML to ANSI-colored terminal
text. ``format_update_description`` is the public entry point used by the
processing pipeline.
"""

import html
import re
from html.parser import HTMLParser


from modules.constants import SECTION_HEADER_RE


class TerminalParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.indent = 0
        self.bold = False
        self.list_stack = []
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
            self.indent += 2
        elif tag == "ul":
            self.flush()
            self.list_stack.append("ul")
            self.indent += 2
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
                self.indent = max(0, self.indent - 2)

    def handle_data(self, data):
        self.buffer += html.unescape(data)

    def flush(self, style=None):
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
    bolded = SECTION_HEADER_RE.sub(
        lambda m: (
            ("\n" if m.group(0).startswith("\n") else "")
            + "<b>"
            + m.group(1)
            + "</b><br>"
        ),
        description,
    )

    # Normalize explicit section breaks only; do not invent gaps before headers.
    normalized = re.sub(
        r"<br>([ \t]*\n){2,}", "<br><br>\n", bolded, flags=re.IGNORECASE
    )

    parser = TerminalParser()
    return parser.render(normalized or "")
