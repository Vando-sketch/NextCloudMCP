"""Unit tests for the pure Markdown section replacement behind update_note_section."""

from __future__ import annotations

import pytest

from nextcloud_task_mcp.errors import InvalidNoteDataError
from nextcloud_task_mcp.notes_sections import replace_section

DOC = (
    "# Title\n"
    "\n"
    "Intro.\n"
    "\n"
    "## 7. Deployment\n"
    "\n"
    "Old text.\n"
    "\n"
    "### 7.1 Details\n"
    "\n"
    "Subpoint.\n"
    "\n"
    "## 8. Operations\n"
    "\n"
    "Stays.\n"
)


# --- happy path ---


def test_replaces_section_including_heading_and_subsections():
    result = replace_section(DOC, "## 7.", "## 7. Deployment\n\nNew text.")
    assert result == (
        "# Title\n\nIntro.\n\n## 7. Deployment\n\nNew text.\n\n## 8. Operations\n\nStays.\n"
    )


def test_section_can_be_renamed_via_new_heading_in_content():
    result = replace_section(DOC, "## 8.", "## 8. Maintenance\n\nNew.")
    assert "## 8. Maintenance" in result
    assert "## 8. Operations" not in result


def test_last_section_runs_to_end_of_note():
    result = replace_section(DOC, "## 8.", "## 8. Operations\n\nReplaced.")
    assert result.endswith("## 8. Operations\n\nReplaced.\n")


def test_subsection_stops_at_next_same_or_higher_level_heading():
    result = replace_section(DOC, "### 7.1", "### 7.1 Details\n\nDifferent.")
    assert "Subpoint." not in result
    assert "Old text." in result
    assert "## 8. Operations" in result


def test_full_heading_line_matches_itself():
    result = replace_section(DOC, "## 7. Deployment", "## 7. Deployment\n\nX.")
    assert "Old text." not in result


def test_empty_content_removes_the_section():
    result = replace_section(DOC, "### 7.1", "")
    assert "### 7.1 Details" not in result
    assert "Subpoint." not in result
    assert "## 8. Operations" in result


def test_trailing_newline_is_preserved():
    assert replace_section(DOC, "## 8.", "## 8. New").endswith("\n")


def test_no_trailing_newline_stays_absent():
    doc = "## A\n\nold"
    assert replace_section(doc, "## A", "## A\n\nnew") == "## A\n\nnew"


def test_blank_line_is_inserted_before_the_next_heading():
    result = replace_section(DOC, "## 7.", "## 7. Deployment\nNew.")
    assert "New.\n\n## 8. Operations" in result


# --- matching rules ---


def test_prefix_must_end_at_word_boundary():
    doc = "## 7. One\n\na\n\n## 75. History\n\nb\n"
    result = replace_section(doc, "## 7", "## 7. One\n\nnew")
    assert "## 75. History" in result
    assert "new" in result


def test_prefix_does_not_select_deeper_subnumbering():
    doc = "## 7.1 Overview\n\na\n\n## 7.1.1 Details\n\nb\n"
    result = replace_section(doc, "## 7.1", "## 7.1 Overview\n\nnew")
    assert "## 7.1.1 Details" in result
    assert "b\n" in result
    assert "new" in result


def test_prefix_selecting_only_deeper_subnumbering_finds_nothing():
    doc = "## 7.1.1 Details\n\nb\n"
    with pytest.raises(InvalidNoteDataError, match="No heading matching"):
        replace_section(doc, "## 7.1", "## 7.1 New")


def test_multiline_section_is_rejected_as_invalid_prefix():
    with pytest.raises(InvalidNoteDataError, match="heading prefix"):
        replace_section(DOC, "## 7. Deployment\n\nNew text.", "## 7.")


def test_heading_level_must_match_exactly():
    doc = "# 7. Top\n\na\n\n## 7. Below\n\nb\n"
    result = replace_section(doc, "## 7.", "## 7. Below\n\nnew")
    assert "# 7. Top" in result
    assert "\nnew" in result


def test_heading_inside_fenced_code_block_is_ignored():
    doc = "## Real\n\n```\n## Real\n```\n\nText.\n"
    result = replace_section(doc, "## Real", "## Real\n\nNew.")
    assert result == "## Real\n\nNew.\n"


def test_fence_only_closes_on_matching_marker():
    # The ~~~ inside a ``` block does not close it (CommonMark).
    doc = "## A\n\n```\n~~~\n## B\n```\n\n## C\n\nx\n"
    result = replace_section(doc, "## C", "## C\n\nnew\n")
    assert "## B" in result
    assert "new" in result


def test_fence_with_info_string_does_not_close_a_block():
    # ```python inside a ``` block is content, not a closing fence.
    doc = "## A\n\n```\n```python\n## B\n```\n\n## C\n\nx\n"
    result = replace_section(doc, "## C", "## C\n\nnew")
    assert "## B" in result
    assert "new" in result


def test_section_end_ignores_headings_inside_fences():
    doc = "## A\n\nbefore\n```\n## B\n```\nafter\n\n## C\n\nx\n"
    result = replace_section(doc, "## A", "## A\n\nnew")
    assert "before" not in result
    assert "after" not in result
    assert "## C\n\nx" in result


def test_heading_inside_yaml_front_matter_is_ignored():
    doc = "---\ntitle: x\n# not a heading\n---\n\n# Real\n\nold\n"
    result = replace_section(doc, "# Real", "# Real\n\nnew")
    assert "# not a heading" in result
    assert "old\n" not in result


def test_unclosed_front_matter_is_not_treated_as_front_matter():
    doc = "---\n\n# Real\n\nold\n"
    result = replace_section(doc, "# Real", "# Real\n\nnew")
    assert "new" in result


def test_indented_heading_up_to_three_spaces_matches():
    doc = "   ## A\n\nold\n"
    result = replace_section(doc, "## A", "## A\n\nnew")
    assert "old" not in result


# --- errors ---


def test_no_matching_heading_raises():
    with pytest.raises(InvalidNoteDataError, match="No heading matching"):
        replace_section(DOC, "## 9.", "## 9. New")


def test_ambiguous_prefix_raises_with_count():
    doc = "## Plan A\n\nx\n\n## Plan B\n\ny\n"
    with pytest.raises(InvalidNoteDataError, match="matches 2 headings"):
        replace_section(doc, "## Plan", "## Plan\n\nnew")


def test_section_must_look_like_a_heading():
    with pytest.raises(InvalidNoteDataError, match="heading prefix"):
        replace_section(DOC, "7. Deployment", "x")


def test_section_with_seven_hashes_is_rejected():
    with pytest.raises(InvalidNoteDataError, match="heading prefix"):
        replace_section(DOC, "####### Too deep", "x")


def test_setext_headings_are_not_recognized():
    doc = "Title\n=====\n\nold\n"
    with pytest.raises(InvalidNoteDataError, match="No heading matching"):
        replace_section(doc, "# Title", "# Title\n\nnew")
