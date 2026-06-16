"""
preprocess.py — Stage 2: Group annotations into sections

Reads annotations.json produced by export.py and clusters annotations
into named sections. Three strategies, applied in priority order:

  1. Instructor tags  — if annotation.instructor_note starts with "#SectionName",
                        that annotation belongs to that section.
  2. Page proximity   — annotations within PAGE_WINDOW pages of each other are
                        grouped together; gaps larger than that start a new section.
  3. Color runs       — a sustained change in dominant color signals a new section.

Output: sections.json, a list of section objects each with their source annotations.

Usage:
    python -m zsg.preprocess --input projects/my_project/annotations.json \
                             --output projects/my_project/sections.json

    python -m zsg.preprocess --input projects/my_project/annotations.json \
                             --output projects/my_project/sections.json \
                             --strategy proximity \
                             --page-window 8
"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Optional

PAGE_WINDOW_DEFAULT = 6   # annotations within this many pages → same section
MIN_SECTION_SIZE   = 2    # sections with fewer annotations get merged into previous


# ---------------------------------------------------------------------------
# Strategy 1: instructor tags
# ---------------------------------------------------------------------------

def _parse_tag(note: str):
    """Return section tag if instructor_note starts with #Tag, else None."""
    note = (note or "").strip()
    m = re.match(r"#([\w\-]+)", note)
    return m.group(1).lower() if m else None


def group_by_tags(annotations: list[dict]):
    """
    Return sections if ANY annotation has a #tag, else None (fall through).
    Untagged annotations inherit the most recent tag.

    Untagged annotations that appear BEFORE the first #tag are collected into
    a named "untagged_preamble" section (instead of the silent "general" bucket)
    so instructors can see them clearly in the section table.

    Returns:
        list[dict] | None — Section list on success, None if no tags found.
        The list may include an "untagged_preamble" section as the first entry
        if pre-tag annotations exist.
    """
    # Find index of first tagged annotation.
    first_tag_idx = None
    for i, a in enumerate(annotations):
        if _parse_tag(a.get("instructor_note", "")):
            first_tag_idx = i
            break

    if first_tag_idx is None:
        return None

    sections: dict[str, list] = {}

    # Collect pre-first-tag annotations into "untagged_preamble" if any exist.
    preamble = annotations[:first_tag_idx]
    if preamble:
        sections["untagged_preamble"] = preamble

    # Process the rest: each annotation inherits the most recent tag.
    current_tag = None
    for ann in annotations[first_tag_idx:]:
        tag = _parse_tag(ann.get("instructor_note", ""))
        if tag:
            current_tag = tag
        sections.setdefault(current_tag, []).append(ann)

    return _build_section_list(sections)


# ---------------------------------------------------------------------------
# Strategy 2: page proximity
# ---------------------------------------------------------------------------

def _page_num(ann: dict, nonnumeric_map: Optional[dict] = None) -> int:
    """Return the annotation's page as an integer sort key.

    For numeric pages (int or numeric string), returns the integer value
    directly (e.g. 42 or "42" → 42).

    For non-numeric page labels (e.g. "vii", "Intro") the function returns
    a synthetic *negative* integer that is unique per distinct label so that:

    * Non-numeric labels sort before page 1 (all synthetic values are < 1).
    * Two *different* non-numeric labels get *different* synthetic values,
      preserving their separation rather than collapsing them all to 0.
    * The synthetic value is stable within a single ``group_by_proximity``
      call because it is read from (and written into) ``nonnumeric_map``,
      a dict keyed by the label string.

    When ``nonnumeric_map`` is ``None`` (e.g. when the function is called
    standalone for a quick lookup) the function falls back to returning 0
    for any non-numeric label, which is the pre-Phase-1 behaviour.

    Returns:
        int — The numeric page value, a synthetic negative value for a
        non-numeric label, or 0 if no map is provided for a non-numeric label.
    """
    p = ann.get("page", 0)
    if isinstance(p, int):
        return p
    try:
        return int(p)
    except (TypeError, ValueError):
        # Non-numeric label (e.g. "vii", "Intro", None)
        if nonnumeric_map is None:
            return 0
        if p not in nonnumeric_map:
            # Assign next available negative slot: -1, -2, -3, …
            nonnumeric_map[p] = -(len(nonnumeric_map) + 1)
        return nonnumeric_map[p]


def _split_by_color(runs: list[list[dict]]) -> list[list[dict]]:
    """Further split runs based on a dominant-color transition.

    For each run, if the dominant color changes mid-run (i.e. there is a
    point where the colour of consecutive annotations switches from one
    majority colour to another), the run is split at the first such
    transition point.  A transition is recognised only when a new colour
    appears in a *sustained* way — at least 2 consecutive annotations of
    the new colour — so that isolated outlier highlights don't fragment a
    section.

    Runs that are already short (≤ 2 annotations) or that have a single
    uniform colour are returned unchanged.
    """
    result: list[list[dict]] = []
    for run in runs:
        if len(run) <= 2:
            result.append(run)
            continue

        colors = [a.get("color", "yellow") for a in run]

        # Find the first sustained transition: index i where colors[i] !=
        # colors[i-1] AND colors[i] == colors[i+1] (sustained for ≥ 2).
        split_idx = None
        for i in range(1, len(colors) - 1):
            if colors[i] != colors[i - 1] and colors[i] == colors[i + 1]:
                split_idx = i
                break

        if split_idx is not None:
            # Recursively split each half in case there are further transitions.
            result.extend(_split_by_color([run[:split_idx], run[split_idx:]]))
        else:
            result.append(run)
    return result


def _split_by_span(runs: list[list[dict]], page_window: int,
                   nonnumeric_map: dict) -> list[list[dict]]:
    """Split runs whose total page span exceeds page_window.

    When a run of annotations spans more pages than page_window (all
    consecutive gaps were ≤ page_window yet the cumulative span is large),
    it is split at the point of the largest internal gap.  If all internal
    gaps are equal the run is halved at the midpoint.  The process repeats
    recursively until every sub-run fits within page_window.

    Only positive (numeric) page values are used for span calculations;
    non-numeric (synthetic-negative) pages are left in place and never
    cause a span-split.
    """
    result: list[list[dict]] = []
    for run in runs:
        pages = [_page_num(a, nonnumeric_map) for a in run]
        positive_pages = [p for p in pages if p > 0]

        if not positive_pages or (max(positive_pages) - min(positive_pages)) <= page_window:
            result.append(run)
            continue

        # Find the largest gap between consecutive elements (by page key).
        best_gap = -1
        split_idx = len(run) // 2  # fallback: midpoint

        for i in range(1, len(run)):
            gap = pages[i] - pages[i - 1]
            if gap > best_gap:
                best_gap = gap
                split_idx = i

        # Recursively split each half.
        result.extend(_split_by_span(
            [run[:split_idx], run[split_idx:]], page_window, nonnumeric_map
        ))
    return result


def _group_by_proximity_with_stats(
    annotations: list[dict], page_window: int
) -> tuple:
    """Internal: run proximity grouping and return (sections, stats).

    Stats dict contains:
        merges_performed  — number of tiny runs merged into a prior section
        nonnumeric_count  — number of annotations with non-numeric page labels
        fallback_labels   — number of sections that got a positional label
                            (section_NN) because they had no real page numbers
    """
    if not annotations:
        return [], {"merges_performed": 0, "nonnumeric_count": 0, "fallback_labels": 0}

    # Count non-numeric page annotations BEFORE building the map, so each
    # annotation is counted once regardless of how many share the same label.
    nonnumeric_count = 0
    nonnumeric_map: dict = {}
    for a in annotations:
        p = a.get("page")
        if p is not None and not isinstance(p, int):
            try:
                int(p)
            except (TypeError, ValueError):
                nonnumeric_count += 1
                if p not in nonnumeric_map:
                    nonnumeric_map[p] = -(len(nonnumeric_map) + 1)

    def _key(a):
        return _page_num(a, nonnumeric_map)

    sorted_anns = sorted(annotations, key=_key)
    runs: list[list[dict]] = [[sorted_anns[0]]]

    for ann in sorted_anns[1:]:
        gap = _key(ann) - _key(runs[-1][-1])
        if gap <= page_window:
            runs[-1].append(ann)
        else:
            runs.append([ann])

    # Further split runs with sustained color transitions (cheap topical signal).
    runs = _split_by_color(runs)

    # Further split any run whose total page span exceeds page_window.
    # This prevents dense exports (no large consecutive gaps) from collapsing
    # into a single giant section.
    runs = _split_by_span(runs, page_window, nonnumeric_map)

    # Merge tiny sections into the previous one, but record the merge so it
    # is VISIBLE to callers (non-silent).  Each run that gets force-merged
    # increments a counter on the absorbing run's metadata.
    merged: list[list[dict]] = []
    merge_counts: list[int] = []  # parallel list: how many tiny runs merged in

    for run in runs:
        if merged and len(run) < MIN_SECTION_SIZE:
            merged[-1].extend(run)
            merge_counts[-1] += 1
        else:
            merged.append(run)
            merge_counts.append(0)

    merges_performed = sum(merge_counts)
    sections: dict[str, list] = {}
    section_meta: dict[str, dict] = {}
    fallback_labels = 0

    for i, (run, n_merged) in enumerate(zip(merged, merge_counts)):
        # Only use real (positive) page numbers for the label; synthetic
        # negative values from non-numeric labels are excluded here.
        pages = [_page_num(a) for a in run if _page_num(a) and _page_num(a) > 0]
        if pages:
            label = f"section_p{min(pages)}_p{max(pages)}"
        else:
            label = f"section_{i+1:02d}"
            fallback_labels += 1
        sections[label] = run
        if n_merged > 0:
            section_meta[label] = {
                "merge_reason": "min_section_size",
                "merged_runs_count": n_merged,
            }

    stats = {
        "merges_performed": merges_performed,
        "nonnumeric_count": nonnumeric_count,
        "fallback_labels": fallback_labels,
    }
    return _build_section_list(sections, section_meta), stats


def group_by_proximity(annotations: list[dict], page_window: int) -> list[dict]:
    """Split annotations into runs where consecutive gaps ≤ page_window.

    Public API — returns section list only.  Heuristic stats are available
    via the internal ``_group_by_proximity_with_stats`` helper.
    """
    sections, _stats = _group_by_proximity_with_stats(annotations, page_window)
    return sections


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _build_section_list(
    sections: dict[str, list],
    section_meta: Optional[dict] = None,
) -> list[dict]:
    """Build the canonical section list from a {section_id: [annotations]} dict.

    Args:
        sections: Ordered mapping of section_id → annotation list.
        section_meta: Optional mapping of section_id → ``_metadata`` dict.
            When provided, any section whose id appears in this mapping
            receives an additional ``_metadata`` key in its output object.
            This key is **optional** — downstream consumers (generate.py,
            verify.py, build_guide.py) MUST ignore it gracefully.
    """
    result = []
    for section_id, anns in sections.items():
        entry: dict = {
            "section_id": section_id,
            "page_range": _page_range(anns),
            "annotation_count": len(anns),
            "source_annotations": anns,
        }
        if section_meta and section_id in section_meta:
            entry["_metadata"] = section_meta[section_id]
        result.append(entry)
    return result


def _page_range(annotations: list[dict]) -> dict:
    pages = [_page_num(a) for a in annotations if _page_num(a)]
    if not pages:
        return {"start": None, "end": None}
    return {"start": min(pages), "end": max(pages)}


# ---------------------------------------------------------------------------
# Heuristic summary
# ---------------------------------------------------------------------------

def _print_heuristic_summary(
    strategy_used: str,
    sections: list[dict],
    *,
    preamble_count: int = 0,
    proximity_stats: Optional[dict] = None,
) -> None:
    """Print a human-readable summary of debatable boundary decisions.

    Args:
        strategy_used:   "tags" or "proximity".
        sections:        Final section list (used to count total + heuristic sections).
        preamble_count:  Number of annotations in the "untagged_preamble" section (tags path).
        proximity_stats: Dict with keys merges_performed, nonnumeric_count, fallback_labels
                         (proximity path only; None on tags path).
    """
    print("\n========== Grouping Decisions ==========")

    if strategy_used == "tags":
        print("Strategy: instructor tags")
        print("  Path: tags found → tag-based grouping")
        if preamble_count > 0:
            print(f"  Untagged preamble: {preamble_count} annotation(s) before first #tag")
            print('    → created new section "untagged_preamble"')
        else:
            print("  Untagged preamble: none (first annotation already has a #tag)")
        has_general = any(s["section_id"] == "general" for s in sections)
        if has_general:
            general_count = next(
                s["annotation_count"] for s in sections if s["section_id"] == "general"
            )
            print(f"  General bucket: created ({general_count} annotation(s) — untagged mid-document)")
        else:
            print("  General bucket: NOT created (all untagged annotations follow a #tag)")
        print("\nProximity heuristics:")
        print("  (Not used — tags strategy succeeded)")

    else:  # proximity
        ps = proximity_stats or {}
        merges = ps.get("merges_performed", 0)
        nonnumeric = ps.get("nonnumeric_count", 0)
        fallbacks = ps.get("fallback_labels", 0)
        print("Strategy: page proximity (no #tags found)")
        print(f"  Merges performed: {merges} tiny section(s) merged (MIN_SECTION_SIZE={MIN_SECTION_SIZE})")
        print(f"  Non-numeric pages bucketed: {nonnumeric} annotation(s) with non-numeric page labels")
        print(f"  Fallback labels (no real page bounds): {fallbacks} section(s)")

    # Count sections with heuristic metadata (force-merged via _metadata).
    heuristic_count = sum(1 for s in sections if "_metadata" in s)
    print(f"\n========== Summary ==========")
    print(f"{len(sections)} section(s) total, {heuristic_count} with heuristic boundaries")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def preprocess(
    annotations: list[dict],
    strategy: str,
    page_window: int,
    *,
    print_summary: bool = True,
) -> list[dict]:
    """Group annotations into sections using the given strategy.

    Args:
        annotations:   List of annotation dicts from annotations.json.
        strategy:      One of "auto", "tags", "proximity".
        page_window:   Max page gap within a section (proximity strategy).
        print_summary: When True (default), print the heuristic-decision
                       summary to stdout after grouping.  Set to False in
                       unit tests that capture output and don't want noise.

    Returns:
        List of section dicts satisfying the sections.json contract.
    """
    sections: list[dict]
    strategy_used: str
    preamble_count: int = 0
    proximity_stats: Optional[dict] = None

    if strategy == "auto":
        tag_sections = group_by_tags(annotations)
        if tag_sections:
            print("Strategy: instructor tags (#Tag in notes)")
            sections = tag_sections
            strategy_used = "tags"
        else:
            print("Strategy: page proximity (no #tags found)")
            sections, proximity_stats = _group_by_proximity_with_stats(annotations, page_window)
            strategy_used = "proximity"

    elif strategy == "tags":
        tag_sections = group_by_tags(annotations)
        if not tag_sections:
            print("Warning: no #tags found, falling back to proximity.", file=sys.stderr)
            sections, proximity_stats = _group_by_proximity_with_stats(annotations, page_window)
            strategy_used = "proximity"
        else:
            sections = tag_sections
            strategy_used = "tags"

    elif strategy == "proximity":
        sections, proximity_stats = _group_by_proximity_with_stats(annotations, page_window)
        strategy_used = "proximity"

    else:
        raise ValueError(f"Unknown strategy: {strategy}")

    # Determine preamble count from the sections list if we used tags.
    if strategy_used == "tags":
        preamble_secs = [s for s in sections if s["section_id"] == "untagged_preamble"]
        preamble_count = preamble_secs[0]["annotation_count"] if preamble_secs else 0

    if print_summary:
        _print_heuristic_summary(
            strategy_used,
            sections,
            preamble_count=preamble_count,
            proximity_stats=proximity_stats,
        )

    return sections


def main():
    parser = argparse.ArgumentParser(description="Group annotations into sections.")
    parser.add_argument("--input",  "-i", required=True, help="Path to annotations.json")
    parser.add_argument("--output", "-o", required=True, help="Output path for sections.json")
    parser.add_argument(
        "--strategy", choices=["auto", "tags", "proximity"], default="auto",
        help="Grouping strategy (default: auto — tags if present, else proximity)"
    )
    parser.add_argument(
        "--page-window", type=int, default=PAGE_WINDOW_DEFAULT,
        help=f"Max page gap within a section for proximity strategy (default: {PAGE_WINDOW_DEFAULT})"
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Error: {input_path} not found.", file=sys.stderr)
        sys.exit(1)

    with open(input_path, encoding="utf-8") as f:
        data = json.load(f)

    annotations = data.get("annotations", data) if isinstance(data, dict) else data
    color_config = data.get("color_config", {}) if isinstance(data, dict) else {}

    print(f"Loaded {len(annotations)} annotations.")
    sections = preprocess(annotations, args.strategy, args.page_window)

    output = {
        "color_config": color_config,
        "sections": sections,
    }

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\nSections written to {output_path}")
    print(f"{'Section ID':<35} {'Annotations':>11}  {'Pages':>12}")
    print("-" * 62)
    for s in sections:
        pr = s["page_range"]
        page_str = f"p.{pr['start']}–{pr['end']}" if pr["start"] else "—"
        print(f"  {s['section_id']:<33} {s['annotation_count']:>11}  {page_str:>12}")


if __name__ == "__main__":
    main()
