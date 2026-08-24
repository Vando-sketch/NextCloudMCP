"""Unit tests for the pure Markdown section replacement behind update_notiz_abschnitt."""

from __future__ import annotations

import pytest

from nextcloud_task_mcp.errors import InvalidNotizDataError
from nextcloud_task_mcp.notes_sections import replace_section

DOC = (
    "# Titel\n"
    "\n"
    "Intro.\n"
    "\n"
    "## 7. Deployment\n"
    "\n"
    "Alter Text.\n"
    "\n"
    "### 7.1 Details\n"
    "\n"
    "Unterpunkt.\n"
    "\n"
    "## 8. Betrieb\n"
    "\n"
    "Bleibt.\n"
)


# --- happy path ---


def test_replaces_section_including_heading_and_subsections():
    result = replace_section(DOC, "## 7.", "## 7. Deployment\n\nNeuer Text.")
    assert result == (
        "# Titel\n\nIntro.\n\n## 7. Deployment\n\nNeuer Text.\n\n## 8. Betrieb\n\nBleibt.\n"
    )


def test_section_can_be_renamed_via_new_heading_in_inhalt():
    result = replace_section(DOC, "## 8.", "## 8. Wartung\n\nNeu.")
    assert "## 8. Wartung" in result
    assert "## 8. Betrieb" not in result


def test_last_section_runs_to_end_of_note():
    result = replace_section(DOC, "## 8.", "## 8. Betrieb\n\nErsetzt.")
    assert result.endswith("## 8. Betrieb\n\nErsetzt.\n")


def test_subsection_stops_at_next_same_or_higher_level_heading():
    result = replace_section(DOC, "### 7.1", "### 7.1 Details\n\nAnders.")
    assert "Unterpunkt." not in result
    assert "Alter Text." in result
    assert "## 8. Betrieb" in result


def test_full_heading_line_matches_itself():
    result = replace_section(DOC, "## 7. Deployment", "## 7. Deployment\n\nX.")
    assert "Alter Text." not in result


def test_empty_inhalt_removes_the_section():
    result = replace_section(DOC, "### 7.1", "")
    assert "### 7.1 Details" not in result
    assert "Unterpunkt." not in result
    assert "## 8. Betrieb" in result


def test_trailing_newline_is_preserved():
    assert replace_section(DOC, "## 8.", "## 8. Neu").endswith("\n")


def test_no_trailing_newline_stays_absent():
    doc = "## A\n\nalt"
    assert replace_section(doc, "## A", "## A\n\nneu") == "## A\n\nneu"


def test_blank_line_is_inserted_before_the_next_heading():
    result = replace_section(DOC, "## 7.", "## 7. Deployment\nNeu.")
    assert "Neu.\n\n## 8. Betrieb" in result


# --- matching rules ---


def test_prefix_must_end_at_word_boundary():
    doc = "## 7. Eins\n\na\n\n## 75. Historie\n\nb\n"
    result = replace_section(doc, "## 7", "## 7. Eins\n\nneu")
    assert "## 75. Historie" in result
    assert "neu" in result


def test_heading_level_must_match_exactly():
    doc = "# 7. Oben\n\na\n\n## 7. Unten\n\nb\n"
    result = replace_section(doc, "## 7.", "## 7. Unten\n\nneu")
    assert "# 7. Oben" in result
    assert "\nneu" in result


def test_heading_inside_fenced_code_block_is_ignored():
    doc = "## Echt\n\n```\n## Echt\n```\n\nText.\n"
    result = replace_section(doc, "## Echt", "## Echt\n\nNeu.")
    assert result == "## Echt\n\nNeu.\n"


def test_fence_only_closes_on_matching_marker():
    # The ~~~ inside a ``` block does not close it (CommonMark).
    doc = "## A\n\n```\n~~~\n## B\n```\n\n## C\n\nx\n"
    result = replace_section(doc, "## C", "## C\n\nneu\n")
    assert "## B" in result
    assert "neu" in result


def test_section_end_ignores_headings_inside_fences():
    doc = "## A\n\nvorher\n```\n## B\n```\nnachher\n\n## C\n\nx\n"
    result = replace_section(doc, "## A", "## A\n\nneu")
    assert "vorher" not in result
    assert "nachher" not in result
    assert "## C\n\nx" in result


def test_heading_inside_yaml_front_matter_is_ignored():
    doc = "---\ntitle: x\n# kein Heading\n---\n\n# Echt\n\nalt\n"
    result = replace_section(doc, "# Echt", "# Echt\n\nneu")
    assert "# kein Heading" in result
    assert "alt\n" not in result


def test_unclosed_front_matter_is_not_treated_as_front_matter():
    doc = "---\n\n# Echt\n\nalt\n"
    result = replace_section(doc, "# Echt", "# Echt\n\nneu")
    assert "neu" in result


def test_indented_heading_up_to_three_spaces_matches():
    doc = "   ## A\n\nalt\n"
    result = replace_section(doc, "## A", "## A\n\nneu")
    assert "alt" not in result


# --- errors ---


def test_no_matching_heading_raises():
    with pytest.raises(InvalidNotizDataError, match="No heading matching"):
        replace_section(DOC, "## 9.", "## 9. Neu")


def test_ambiguous_prefix_raises_with_count():
    doc = "## Plan A\n\nx\n\n## Plan B\n\ny\n"
    with pytest.raises(InvalidNotizDataError, match="matches 2 headings"):
        replace_section(doc, "## Plan", "## Plan\n\nneu")


def test_abschnitt_must_look_like_a_heading():
    with pytest.raises(InvalidNotizDataError, match="heading prefix"):
        replace_section(DOC, "7. Deployment", "x")


def test_abschnitt_with_seven_hashes_is_rejected():
    with pytest.raises(InvalidNotizDataError, match="heading prefix"):
        replace_section(DOC, "####### Zu tief", "x")


def test_setext_headings_are_not_recognized():
    doc = "Titel\n=====\n\nalt\n"
    with pytest.raises(InvalidNotizDataError, match="No heading matching"):
        replace_section(doc, "# Titel", "# Titel\n\nneu")
