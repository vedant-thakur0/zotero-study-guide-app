"""
docx_parser.py — Generic DOCX paragraph extraction and section-detection utilities.

These utilities are exam-agnostic: they operate on a flat list of cleaned paragraph
strings and know nothing about French grammar or specific question types.

Typical usage:
    from docx import Document
    from exam_toolkit.docx_parser import load_paragraphs, find_section, extract_numbered_items

    paras = load_paragraphs("exam.docx")
    start = find_section(paras, "B. Notre monde")
    items = extract_numbered_items(paras, start + 1)
"""

from __future__ import annotations

import re
from pathlib import Path


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_paragraphs(docx_path: str | Path) -> list[str]:
    """
    Load a DOCX and return a flat list of cleaned, non-empty paragraph strings.
    Normalises non-breaking spaces and collapses internal whitespace.
    """
    from docx import Document  # lazy import so the module is usable without python-docx installed

    doc = Document(str(docx_path))
    return [_clean(p.text) for p in doc.paragraphs if _clean(p.text)]


def _clean(text: str) -> str:
    return " ".join(text.replace("\xa0", " ").split())


# ---------------------------------------------------------------------------
# Section boundary detection
# ---------------------------------------------------------------------------

def find_section(paras: list[str], marker: str, *, fuzzy: bool = True) -> int:
    """
    Return the index of the first paragraph whose start matches *marker*.

    With fuzzy=True (default) the comparison is case-insensitive and strips
    leading/trailing punctuation, so "B. Notre monde" matches
    "B. Notre monde — Créez des phrases (9 pts)".

    Raises ValueError if not found.
    """
    needle = _normalise(marker)
    for i, p in enumerate(paras):
        candidate = _normalise(p)
        if fuzzy:
            if candidate.startswith(needle):
                return i
        else:
            if candidate == needle:
                return i
    raise ValueError(f"Section marker not found: {marker!r}")


def find_section_safe(paras: list[str], marker: str, *, fuzzy: bool = True) -> int | None:
    """Like find_section but returns None instead of raising."""
    try:
        return find_section(paras, marker, fuzzy=fuzzy)
    except ValueError:
        return None


def _normalise(s: str) -> str:
    return s.lower().strip(" .:-–—")


# ---------------------------------------------------------------------------
# Structured list extractors
# ---------------------------------------------------------------------------

def extract_numbered_items(
    paras: list[str],
    start: int,
    *,
    pattern: str = r"^\d+\.",
    stop_pattern: str | None = None,
    max_items: int = 50,
) -> list[tuple[int, str]]:
    """
    Starting at paras[start], collect paragraphs that match *pattern* (a regex).
    Stops when a non-matching line is encountered, or when *stop_pattern* matches,
    or after *max_items*.

    Returns list of (original_index, text) tuples.
    """
    results: list[tuple[int, str]] = []
    i = start
    while i < len(paras) and len(results) < max_items:
        p = paras[i]
        if stop_pattern and re.match(stop_pattern, p):
            break
        if re.match(pattern, p):
            results.append((i, p))
            i += 1
        else:
            break
    return results


def extract_lettered_options(
    paras: list[str],
    start: int,
    *,
    letters: str = "abc",
    max_options: int | None = None,
) -> list[str]:
    """
    Collect paragraphs starting at *start* that match "a.", "b.", "c." etc.
    Strips the leading letter+dot prefix from each.
    Returns plain text options in order.
    """
    cap = max_options or len(letters)
    pattern = rf"^[{letters}]\."
    results: list[str] = []
    i = start
    while i < len(paras) and len(results) < cap:
        p = paras[i]
        if re.match(pattern, p):
            results.append(re.sub(rf"^[{letters}]\.\s*", "", p))
            i += 1
        else:
            break
    return results


def extract_blank_sentences(
    paras: list[str],
    start: int,
    *,
    blank_marker: str = "____",
    max_items: int = 20,
) -> list[tuple[int, str]]:
    """
    Collect paragraphs from *start* that contain *blank_marker*.
    Used to find fill-in-the-blank stem sentences.
    Returns list of (original_index, text) tuples.
    """
    results: list[tuple[int, str]] = []
    i = start
    while i < len(paras) and len(results) < max_items:
        p = paras[i]
        if blank_marker in p:
            results.append((i, p))
        elif results:
            # stop at the first non-blank line after we've started collecting
            break
        i += 1
    return results


def extract_range(paras: list[str], start: int, end_marker: str) -> list[str]:
    """
    Return paragraphs from *start* up to (but not including) the paragraph
    that starts with *end_marker*.  Useful for grabbing a reading passage.
    """
    end = find_section_safe(paras, end_marker)
    stop = end if (end is not None and end > start) else len(paras)
    return paras[start:stop]


# ---------------------------------------------------------------------------
# Si-clause block parser (common enough to ship as a utility)
# ---------------------------------------------------------------------------

def parse_si_clause_block(
    paras: list[str],
    start: int,
    *,
    n_options: int = 3,
) -> list[dict]:
    """
    Parse a sequence of numbered Si-clause MCQ blocks of the form:
        1. Si j'avais étudié, je ____
        a. réussirais
        b. aurais réussi
        c. réussis
        2. Si tu veux réussir, tu ____
        ...

    Returns list of dicts:  {"stem": str, "options": [str, str, str]}

    If two options are identical (known authoring error), the last is replaced
    with a placeholder so downstream code can override it.
    """
    results: list[dict] = []
    i = start
    while i < len(paras):
        if not re.match(r"^\d+\.", paras[i]):
            break
        stem = paras[i]
        options = extract_lettered_options(paras, i + 1, max_options=n_options)
        if len(options) != n_options:
            break
        # Fix duplicate options (e.g. Q6 authoring error in FR201 exam)
        if len(set(options)) < len(options):
            seen: set[str] = set()
            fixed: list[str] = []
            for opt in options:
                if opt in seen:
                    fixed.append(f"{opt} (variante)")
                else:
                    seen.add(opt)
                    fixed.append(opt)
            options = fixed
        results.append({"stem": stem, "options": options})
        i += 1 + n_options
    return results
