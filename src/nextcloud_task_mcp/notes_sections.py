"""Markdown section addressing for the update_notiz_abschnitt tool.

Pure text transformation, deliberately separate from notes_client.py (HTTP)
and notes_mapping.py (German <-> JSON field translation) so it can be tested
without either.

Only ATX headings are recognized, following CommonMark's shape: up to three
leading spaces, one to six `#`, then whitespace or end of line. Setext
(underlined) headings are not supported. Heading-shaped lines inside fenced
code blocks (``` or ~~~) and inside a leading YAML front matter block are
ignored, both when locating the target heading and when finding where its
section ends.
"""

from __future__ import annotations

import re

from .errors import InvalidNotizDataError

_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})(?:\s|$)")
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
# `abschnitt` itself: 1-6 '#', then nothing or whitespace + the heading's start.
_ABSCHNITT_RE = re.compile(r"(#{1,6})(?:\s.*)?", re.DOTALL)


def _heading_levels(lines: list[str]) -> list[int | None]:
    """Per line: its ATX heading level, or None for non-heading lines.

    Lines inside fenced code blocks and inside a closed YAML front matter
    block at the top of the document are never headings. An unclosed front
    matter opener is treated as a plain thematic break instead, so a note
    that merely starts with "---" doesn't lose all its headings.
    """
    levels: list[int | None] = [None] * len(lines)
    start = 0
    if lines and lines[0].rstrip() == "---":
        for i in range(1, len(lines)):
            if lines[i].rstrip() in ("---", "..."):
                start = i + 1
                break
    fence: str | None = None
    for i in range(start, len(lines)):
        fence_match = _FENCE_RE.match(lines[i])
        if fence is not None:
            # A closing fence must repeat the opening character at least as
            # many times (CommonMark); anything else stays inside the block.
            if (
                fence_match
                and fence_match.group(1)[0] == fence[0]
                and len(fence_match.group(1)) >= len(fence)
            ):
                fence = None
        elif fence_match:
            fence = fence_match.group(1)
        else:
            heading_match = _HEADING_RE.match(lines[i])
            if heading_match:
                levels[i] = len(heading_match.group(1))
    return levels


def _matches_prefix(line: str, prefix: str) -> bool:
    """True if the heading line starts with `prefix` at a word boundary.

    The boundary check keeps "## 7" from selecting "## 75. History": the
    character right after the prefix must not be alphanumeric.
    """
    stripped = line.strip()
    if not stripped.startswith(prefix):
        return False
    rest = stripped[len(prefix) :]
    return not rest[:1].isalnum()


def replace_section(content: str, abschnitt: str, inhalt: str) -> str:
    """Replace one Markdown section - heading line and body - with `inhalt`.

    `abschnitt` must select exactly one heading: a heading line of the same
    level (same number of '#') that starts with it. The section runs from
    that heading up to (not including) the next heading of the same or a
    higher level, or the end of the note. `inhalt` replaces the whole span
    including the heading line; an empty `inhalt` removes the section.
    """
    prefix = abschnitt.strip()
    prefix_match = _ABSCHNITT_RE.fullmatch(prefix)
    if not prefix_match:
        raise InvalidNotizDataError(
            "abschnitt must be a Markdown heading prefix like '## 7.' - "
            "one to six '#' followed by a space and the heading's beginning."
        )
    level = len(prefix_match.group(1))

    lines = content.split("\n")
    levels = _heading_levels(lines)
    matches = [
        i
        for i, line_level in enumerate(levels)
        if line_level == level and _matches_prefix(lines[i], prefix)
    ]
    if not matches:
        raise InvalidNotizDataError(
            "No heading matching abschnitt was found in the note's content "
            "(only ATX headings like '## Title' are recognized, and the "
            "number of '#' must match)."
        )
    if len(matches) > 1:
        raise InvalidNotizDataError(
            f"abschnitt matches {len(matches)} headings in the note's content - "
            "give a longer prefix that matches exactly one."
        )

    start = matches[0]
    end = len(lines)
    for i in range(start + 1, len(lines)):
        line_level = levels[i]
        if line_level is not None and line_level <= level:
            end = i
            break

    new_lines = inhalt.strip("\n").split("\n") if inhalt.strip("\n") else []
    head, tail = lines[:start], lines[end:]
    if new_lines and tail and new_lines[-1].strip():
        # Keep one blank line between the new section and the next heading.
        new_lines.append("")
    result = "\n".join(head + new_lines + tail)
    if content.endswith("\n") and not result.endswith("\n"):
        result += "\n"
    return result
