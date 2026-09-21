"""Terminal description spacing — format_update_description must preserve
source section breaks and never invent blank lines before header-like content.
Regression coverage for terminal spacing and pre-wrapping regressions.
"""

from checkota.description import format_update_description

# Mirrors raw OTA description shape: Update Version follows safety prose
# directly; later sections use preserved blank source lines. Items are separated
# by <br>; headers are bare uppercase lines.
SAMPLE = (
    "Caution! This update may crash rooted devices.<br>\n"
    "Please plug in charger to OTA upgrade when power is lower than 50%.<br>\n"
    "Update Version:X6885-16.3.0.130(OPPJ001PF001AZ)FANS<br>\n"
    "\n"
    "Communication and Network<br>\n"
    "1.Fixed multiple issues with carrier network display, APN/SPN parameters, "
    "FDN functionality, and STK functionality anomalies <br>\n"
    "\n"
    "Charging and Power Consumption<br>\n"
    "1.Fixed issues with charging protocol recognition and abnormal charging <br>\n"
)

BOLD = "\033[1m"
RESET = "\033[0m"


def _line_index(out, substr):
    for i, line in enumerate(out.split("\n")):
        if substr in line:
            return i
    raise AssertionError(f"{substr!r} not found in output:\n{out}")


def test_header_is_ansi_bold():
    out = format_update_description(SAMPLE)
    assert f"{BOLD}Communication and Network{RESET}" in out
    assert f"{BOLD}Charging and Power Consumption{RESET}" in out


def test_no_blank_between_header_and_its_list():
    out = format_update_description(SAMPLE)
    lines = out.split("\n")
    hdr = _line_index(out, "Communication and Network")
    # Header line, then immediately its first list item (no blank between).
    assert lines[hdr + 1].startswith("1.Fixed multiple issues"), lines[hdr + 1]


def test_blank_line_between_sections():
    out = format_update_description(SAMPLE)
    lines = out.split("\n")
    last_item = _line_index(out, "functionality anomalies")
    # Section content, then one blank line, then the next section header.
    assert lines[last_item + 1] == "", lines[last_item + 1]
    assert "Charging and Power Consumption" in lines[last_item + 2]


def test_long_item_not_pre_wrapped():
    # A single logical line must stay one line; the terminal wraps, not the
    # formatter. This guards against textwrap re-inserting newlines.
    out = format_update_description(SAMPLE)
    assert (
        "1.Fixed multiple issues with carrier network display, APN/SPN "
        "parameters, FDN functionality, and STK functionality anomalies"
        in out.split("\n")
    )


def test_plain_leading_lines_preserved():
    out = format_update_description(SAMPLE)
    charger_line = "Please plug in charger to OTA upgrade when power is lower than 50%."

    assert "Caution! This update may crash rooted devices." in out
    assert charger_line in out
    assert "Update Version:X6885-16.3.0.130(OPPJ001PF001AZ)FANS" in out


def test_blank_line_before_update_version():
    """Update Version should render as its own section after intro prose."""
    out = format_update_description(SAMPLE)
    lines = out.split("\n")
    charger_idx = _line_index(out, "Please plug in charger")
    version_idx = _line_index(out, "Update Version:")
    # There should be exactly one blank line between charger and version
    assert version_idx == charger_idx + 2, (
        f"Expected blank line between lines {charger_idx} and {version_idx}"
    )
    assert lines[charger_idx + 1] == ""


def test_no_synthetic_blank_before_header_like_line():
    out = format_update_description(
        "Please plug in charger to OTA upgrade when power is lower than 50%.<br>\n"
        "Communication and Network<br>\n"
    )
    lines = out.split("\n")
    charger_idx = _line_index(out, "Please plug in charger")
    header_idx = _line_index(out, "Communication and Network")

    assert header_idx == charger_idx + 1
    assert "Communication and Network" in lines[charger_idx + 1]


def test_html_list_item_not_pre_wrapped():
    item = " ".join(f"word{i}" for i in range(30))
    out = format_update_description(f"<ul><li>{item}</li></ul>")

    assert out.split("\n") == [f"  • {item}"]


def test_blank_line_after_update_version():
    out = format_update_description(SAMPLE)
    lines = out.split("\n")
    version_idx = _line_index(out, "Update Version:")
    comm_idx = _line_index(out, "Communication and Network")

    assert comm_idx == version_idx + 2, (
        f"Expected blank line between lines {version_idx} and {comm_idx}"
    )
    assert lines[version_idx + 1] == ""


def test_deeply_nested_lists_have_bounded_indent_and_linear_cost():
    """Unclosed/deep <ul> chains must not blow up the rendered output.

    The indent is materialized for every flushed line, so an unbounded depth
    made rendering quadratic in a server-controlled description.
    """
    depth = 2000
    items = 2000
    desc = "<ul>" * depth + "<li>x</li>" * items

    out = format_update_description(desc)

    lines = out.split("\n")
    assert len(lines) == items
    longest = max(len(line) for line in lines)
    # 16-space cap + "• " + content, not depth * 2.
    assert longest <= 16 + len("• x")
    assert all(line.startswith(" " * 16 + "• x") for line in lines)


def test_list_indent_still_reflects_shallow_nesting():
    out = format_update_description("<ul><li>a</li><ul><li>b</li></ul></ul>")

    assert out.split("\n") == ["  • a", "    • b"]
