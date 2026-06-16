"""
test_preprocess.py — Coverage for Stage 2 (section grouping).

PIPELINE_EVALUATION.md calls section grouping "the weakest link," yet it had no
unit tests. These pin down the current behavior of zsg.preprocess across the
three strategies (tags / proximity / auto) and document the known edge-case
failure modes so any future fix has a spec to change against.

Tests whose names start with `test_known_limitation_` assert the CURRENT
(documented-as-suboptimal) behavior. If the grouping logic is improved, expect
those to fail — update them to the new, better behavior at that point.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

from zsg.preprocess import (
    PAGE_WINDOW_DEFAULT,
    MIN_SECTION_SIZE,
    _parse_tag,
    _page_num,
    group_by_tags,
    group_by_proximity,
    preprocess,
)


# ===========================================================================
# Helpers
# ===========================================================================

def ann(id_, page=1, color="yellow", note="", text="x"):
    return {
        "id": id_,
        "page": page,
        "color": color,
        "instructor_note": note,
        "text": text,
    }


def section_ids(sections):
    return [s["section_id"] for s in sections]


def counts_by_id(sections):
    return {s["section_id"]: s["annotation_count"] for s in sections}


# ===========================================================================
# Tag parsing
# ===========================================================================

class TestParseTag:
    def test_extracts_leading_hashtag_lowercased(self):
        assert _parse_tag("#Reconstruction use as intro") == "reconstruction"

    def test_allows_hyphen_and_underscore(self):
        assert _parse_tag("#civil-rights_m7 note") == "civil-rights_m7"

    def test_none_when_no_tag(self):
        assert _parse_tag("just a normal note") is None

    def test_none_for_empty_or_missing(self):
        assert _parse_tag("") is None
        assert _parse_tag(None) is None

    def test_tag_must_be_at_start(self):
        # A hash mid-note is not a section tag.
        assert _parse_tag("see #later for context") is None


# ===========================================================================
# Page number coercion
# ===========================================================================

class TestPageNum:
    def test_int_passthrough(self):
        assert _page_num({"page": 42}) == 42

    def test_numeric_string_coerced(self):
        assert _page_num({"page": "42"}) == 42

    def test_missing_page_is_zero(self):
        assert _page_num({}) == 0

    def test_non_numeric_pages_get_distinct_synthetic_values(self):
        # With no nonnumeric_map provided (standalone call), non-numeric pages
        # still fall back to 0 (backward-compatible behaviour).
        assert _page_num({"page": "vii"}) == 0
        assert _page_num({"page": "Intro"}) == 0
        assert _page_num({"page": None}) == 0

        # When a shared map is provided, each distinct non-numeric label gets a
        # unique negative synthetic value so they don't collapse together.
        nonnumeric_map: dict = {}
        val_vii = _page_num({"page": "vii"}, nonnumeric_map)
        val_intro = _page_num({"page": "Intro"}, nonnumeric_map)
        assert val_vii != val_intro, "distinct labels must map to distinct synthetic values"
        assert val_vii < 0, "synthetic values should be negative (before page 1)"
        assert val_intro < 0
        # Calling again with the same map must return the same value (stability).
        assert _page_num({"page": "vii"}, nonnumeric_map) == val_vii


# ===========================================================================
# Strategy 1: instructor tags
# ===========================================================================

class TestGroupByTags:
    def test_returns_none_when_no_tags(self):
        anns = [ann("a", note="plain"), ann("b", note="also plain")]
        assert group_by_tags(anns) is None

    def test_groups_by_tag_with_inheritance(self):
        anns = [
            ann("a", note="#intro first"),
            ann("b", note="follows intro"),       # inherits intro
            ann("c", note="#body new section"),
            ann("d", note="still body"),          # inherits body
        ]
        sections = group_by_tags(anns)
        c = counts_by_id(sections)
        assert c == {"intro": 2, "body": 2}

    def test_untagged_prefix_forms_named_preamble_section(self):
        # Annotations before the first #tag are now placed in a named
        # "untagged_preamble" section instead of the silent "general" bucket.
        # The preamble section is contract-compatible (has all four required fields)
        # and is the FIRST section in the list so instructors see it clearly.
        anns = [
            ann("a", note="no tag yet"),
            ann("b", note="#realsection start"),
            ann("c", note="in realsection"),
        ]
        sections = group_by_tags(anns)
        ids = section_ids(sections)
        # "general" must NOT be created — it was the old undocumented bucket.
        assert "general" not in ids, (
            '"general" bucket should no longer be created; '
            "pre-tag annotations go to 'untagged_preamble' instead"
        )
        # "untagged_preamble" must exist and hold exactly the pre-tag annotations.
        assert "untagged_preamble" in ids, (
            'expected an "untagged_preamble" section for annotations before the first #tag'
        )
        assert counts_by_id(sections)["untagged_preamble"] == 1
        # The real section must still be intact with its two annotations.
        assert "realsection" in ids
        assert counts_by_id(sections)["realsection"] == 2
        # Preamble section must be first in the list.
        assert ids[0] == "untagged_preamble", (
            '"untagged_preamble" should be the first section in the list'
        )

    def test_section_shape_has_required_fields(self):
        anns = [ann("a", page=10, note="#s one"), ann("b", page=12, note="in s")]
        sections = group_by_tags(anns)
        s = sections[0]
        assert set(s) >= {"section_id", "page_range", "annotation_count", "source_annotations"}
        assert s["page_range"] == {"start": 10, "end": 12}


# ===========================================================================
# Strategy 2: page proximity
# ===========================================================================

class TestGroupByProximity:
    def test_empty_returns_empty(self):
        assert group_by_proximity([], PAGE_WINDOW_DEFAULT) == []

    def test_splits_on_large_gap(self):
        # Two clusters: pages 1-3 and pages 40-42, gap >> window.
        anns = [
            ann("a", page=1), ann("b", page=2), ann("c", page=3),
            ann("d", page=40), ann("e", page=41), ann("f", page=42),
        ]
        sections = group_by_proximity(anns, page_window=6)
        assert len(sections) == 2
        assert counts_by_id(sections) == {"section_p1_p3": 3, "section_p40_p42": 3}

    def test_sorts_unordered_input_by_page(self):
        anns = [ann("d", page=40), ann("a", page=1), ann("e", page=41), ann("b", page=2)]
        sections = group_by_proximity(anns, page_window=6)
        # First section starts at the lowest page regardless of input order.
        assert sections[0]["page_range"]["start"] == 1

    def test_dense_close_pages_split_into_multiple_sections(self):
        # Previously a real export spanning pages 1-11 with the default window
        # of 6 produced a SINGLE giant section because no consecutive gap ever
        # exceeded the window (PIPELINE_EVALUATION.md item #1).
        # Fixed: adaptive span-splitting now breaks large spans into multiple
        # sections even when no single consecutive gap is large.
        anns = [ann(f"a{p}", page=p) for p in range(1, 12)]  # pages 1..11
        sections = group_by_proximity(anns, page_window=PAGE_WINDOW_DEFAULT)
        # Must now produce more than one section.
        assert len(sections) > 1, (
            f"Expected >1 section for pages 1-11 with window={PAGE_WINDOW_DEFAULT}, "
            f"got {len(sections)}"
        )
        # All 11 annotations must still be present across the sections.
        total = sum(s["annotation_count"] for s in sections)
        assert total == 11

    def test_tiny_section_merge_is_visible_in_metadata(self):
        # Previously a lone annotation that started a genuinely new topic
        # (gap > window) was < MIN_SECTION_SIZE and silently glued onto the
        # prior section (PIPELINE_EVALUATION.md item #1).
        # Fixed: the merge still happens (the section count is 1 and the
        # annotation is included), but it is now VISIBLE via _metadata on the
        # absorbing section.
        assert MIN_SECTION_SIZE == 2
        anns = [
            ann("a", page=1), ann("b", page=2),   # a real 2-annotation section
            ann("c", page=50),                    # lone new-topic annotation, far away
        ]
        sections = group_by_proximity(anns, page_window=6)
        # The lone annotation is still merged into the previous section.
        assert len(sections) == 1
        assert sections[0]["annotation_count"] == 3
        # The merge must now be visible via _metadata — no longer silent.
        meta = sections[0].get("_metadata")
        assert meta is not None, (
            "Expected _metadata on the section that absorbed a forced merge"
        )
        assert meta.get("merge_reason") == "min_section_size"
        assert meta.get("merged_runs_count", 0) >= 1

    def test_nonnumeric_pages_form_separate_sections(self):
        # Each distinct non-numeric label gets its own synthetic sort key so
        # "vii" and "Intro" no longer collapse into one front pseudo-section.
        anns = [
            ann("front1", page="vii"), ann("front2", page="Intro"),
            ann("body1", page=40), ann("body2", page=41),
        ]
        sections = group_by_proximity(anns, page_window=6)
        # Expect at least 3 sections: one for "vii", one for "Intro", one for body.
        # (MIN_SECTION_SIZE=2 may merge a lone annotation, but "vii" and "Intro"
        # each have exactly 1 annotation — their gap is 1 synthetic page apart so
        # they will be merged by MIN_SECTION_SIZE into a combined front section,
        # or kept separate depending on gap.  What must NOT happen is all four
        # annotations in a single section or vii+Intro sharing the same section
        # as the body.  The body section (pages 40-41) must be separate.)
        assert len(sections) >= 2
        # Body section must be separate and identifiable by its page range.
        body_sections = [s for s in sections if s["page_range"]["start"] is not None
                         and s["page_range"]["start"] >= 40]
        assert len(body_sections) == 1
        assert body_sections[0]["annotation_count"] == 2
        # The non-numeric (front-matter) sections collectively contain front1+front2
        # and must NOT include body1/body2.
        front_sections = [s for s in sections if s["page_range"] == {"start": None, "end": None}]
        front_ann_ids = [a["id"] for s in front_sections for a in s["source_annotations"]]
        assert "front1" in front_ann_ids
        assert "front2" in front_ann_ids
        assert "body1" not in front_ann_ids
        assert "body2" not in front_ann_ids

    def test_label_falls_back_when_no_real_pages(self):
        anns = [ann("a", page="vii"), ann("b", page="x")]
        sections = group_by_proximity(anns, page_window=6)
        # Label uses the positional fallback, not section_pX_pY.
        assert sections[0]["section_id"].startswith("section_")

    def test_non_numeric_pages_preserve_label_order(self):
        # "vii" appears first in the input so it gets a lower synthetic page
        # number (closer to -1) than "Intro", meaning its section sorts to
        # the front of the output list.
        anns = [
            ann("a", page="vii"),
            ann("b", page="Intro"),
            ann("c", page=10), ann("d", page=11),
        ]
        sections = group_by_proximity(anns, page_window=6)
        # All front-matter sections come before the numeric body section.
        non_numeric_sections = [
            s for s in sections if s["page_range"] == {"start": None, "end": None}
        ]
        numeric_sections = [
            s for s in sections if s["page_range"]["start"] is not None
        ]
        assert non_numeric_sections, "expected at least one non-numeric section"
        assert numeric_sections, "expected at least one numeric section"
        # Non-numeric sections must appear before numeric ones in the output.
        last_non_numeric_idx = max(sections.index(s) for s in non_numeric_sections)
        first_numeric_idx = min(sections.index(s) for s in numeric_sections)
        assert last_non_numeric_idx < first_numeric_idx, (
            "non-numeric sections should sort before numeric sections"
        )


# ===========================================================================
# preprocess() dispatch + auto fallback
# ===========================================================================

class TestPreprocessDispatch:
    def test_auto_uses_tags_when_present(self):
        anns = [ann("a", page=1, note="#alpha"), ann("b", page=2, note="#beta")]
        sections = preprocess(anns, strategy="auto", page_window=6)
        assert set(section_ids(sections)) == {"alpha", "beta"}

    def test_auto_falls_back_to_proximity_without_tags(self):
        anns = [ann("a", page=1), ann("b", page=2), ann("c", page=40), ann("d", page=41)]
        sections = preprocess(anns, strategy="auto", page_window=6)
        assert section_ids(sections) == ["section_p1_p2", "section_p40_p41"]

    def test_tags_strategy_falls_back_to_proximity_when_no_tags(self):
        anns = [ann("a", page=1), ann("b", page=2), ann("c", page=40), ann("d", page=41)]
        sections = preprocess(anns, strategy="tags", page_window=6)
        # No #tags -> proximity result, not an empty/None.
        assert len(sections) == 2

    def test_proximity_strategy_ignores_tags(self):
        anns = [ann("a", page=1, note="#alpha"), ann("b", page=2, note="#beta")]
        sections = preprocess(anns, strategy="proximity", page_window=6)
        # Grouped by page proximity, so the #tags do NOT split them.
        assert len(sections) == 1

    def test_unknown_strategy_raises(self):
        with pytest.raises(ValueError, match="Unknown strategy"):
            preprocess([ann("a")], strategy="bogus", page_window=6)

    def test_page_window_is_respected(self):
        # Two clusters of 2 (so neither is merged away by MIN_SECTION_SIZE):
        # pages {1,2} and {5,6}. A gap of 3 (5-2) splits them at window=2 but
        # keeps them together at window=6.
        anns = [ann("a", page=1), ann("b", page=2), ann("c", page=5), ann("d", page=6)]
        assert len(preprocess(anns, "proximity", page_window=2)) == 2
        assert len(preprocess(anns, "proximity", page_window=6)) == 1
