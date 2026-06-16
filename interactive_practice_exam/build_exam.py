#!/usr/bin/env python3
"""build_exam.py — Convert examen_recree.docx into exam.html via the existing pipeline."""

import sys
from pathlib import Path

# Import build_guide from the parent directory; SCRIPT_DIR inside it resolves correctly.
sys.path.insert(0, str(Path(__file__).parent.parent))

from build_guide import build       # assembles full HTML with inlined CSS/JS
from parse_exam import parse_exam   # produces state dict from DOCX

DOCX  = Path(__file__).parent / "examen_recree.docx"
OUT   = Path(__file__).parent / "exam.html"
TITLE = "FRANÇAIS 201 — Examen Final (Practice)"
THEME = "light"


def main():
    print(f"Parsing {DOCX} ...")
    state = parse_exam(DOCX)

    print("Building HTML ...")
    html = build(state, TITLE, THEME)

    OUT.write_text(html, encoding="utf-8")
    print(f"Written: {OUT}  ({len(html):,} bytes)")

    q_count = sum(
        len(sec.get("quiz", {}).get("questions", []))
        for sec in state["sections"].values()
    )
    print(f"Interactive questions: {q_count}")
    print(f"Total sections: {len(state['sections'])}")


if __name__ == "__main__":
    main()
