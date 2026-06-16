"""
build_guide.py — Stage 5: Assemble approved JSON into a self-contained HTML study guide

Reads state.json (produced and edited in the verification app) and renders
a single portable .html file the instructor can distribute to students.

Usage:
    python -m zsg.build_guide --state   projects/my_project/state.json \
                              --output  projects/my_project/output.html \
                              --title   "The Quest for Equality" \
                              --theme   light

    python -m zsg.build_guide --state   projects/civil_rights_m7/state.json \
                              --output  projects/civil_rights_m7/output.html
"""

import argparse
import json
import random
import sys
from pathlib import Path

from zsg import PKG_DIR, PROJECT_ROOT

# ---------------------------------------------------------------------------
# Exam toolkit — optional extended block renderers
# Load lazily so build_guide.py stays usable without the exam_toolkit package.
# ---------------------------------------------------------------------------
_EXAM_TOOLKIT_DIR = PROJECT_ROOT / "interactive_practice_exam" / "exam_toolkit"
_exam_renderers_loaded = False
_EXAM_BLOCK_RENDERERS: dict = {}
_EXAM_CSS = ""
_EXAM_JS  = ""

def _load_exam_toolkit():
    global _exam_renderers_loaded, _EXAM_BLOCK_RENDERERS, _EXAM_CSS, _EXAM_JS
    if _exam_renderers_loaded:
        return
    if _EXAM_TOOLKIT_DIR.exists():
        sys.path.insert(0, str(_EXAM_TOOLKIT_DIR.parent.parent))
        try:
            from interactive_practice_exam.exam_toolkit.renderers import (
                EXAM_BLOCK_RENDERERS, EXAM_CSS, EXAM_JS,
            )
            _EXAM_BLOCK_RENDERERS = EXAM_BLOCK_RENDERERS
            _EXAM_CSS = EXAM_CSS
            _EXAM_JS  = EXAM_JS
        except ImportError:
            pass
    _exam_renderers_loaded = True


# ---------------------------------------------------------------------------
# Block renderers — each returns an HTML string for one block type
# ---------------------------------------------------------------------------

def render_narrative_block(data: dict) -> str:
    heading = data.get("heading", "")
    intro   = data.get("intro", "")
    return f"""
      <div class="block block-narrative">
        <h2 class="section-heading">{_e(heading)}</h2>
        <p class="section-intro">{_e(intro)}</p>
      </div>"""


def render_key_points_block(data: dict) -> str:
    points = data.get("points", [])
    if not points:
        return ""
    items = "".join(
        f"""<li class="key-point">
          <span class="kp-term">{_e(p.get("term",""))}</span>
          <span class="kp-explanation">{_e(p.get("explanation",""))}</span>
        </li>"""
        for p in points
    )
    return f'<div class="block block-key-points"><ul class="key-points-list">{items}</ul></div>'


def render_figures_block(data: dict) -> str:
    figures = data.get("figures", [])
    if not figures:
        return ""
    cards = "".join(
        f"""<div class="figure-card">
          <div class="figure-name">{_e(f.get("name",""))}</div>
          <div class="figure-desc">{_e(f.get("description",""))}</div>
        </div>"""
        for f in figures
    )
    return f'<div class="block block-figures"><div class="figure-grid">{cards}</div></div>'


def render_quiz_block(data: dict, section_idx: int) -> str:
    questions = data.get("questions", [])
    if not questions:
        return ""
    cards = "".join(
        _render_question(q, section_idx, q_idx)
        for q_idx, q in enumerate(questions)
    )
    return f'<div class="block block-quiz" data-section="{section_idx}">{cards}</div>'


def _render_question(q: dict, section_idx: int, q_idx: int) -> str:
    qid = f"q_{section_idx}_{q_idx}"

    options  = [q.get("correct_answer", "")] + list(q.get("distractors", []))
    rnd = random.Random(qid)
    rnd.shuffle(options)

    opts_html = "".join(
        f"""<button class="option-btn" data-correct="{str(opt == q.get('correct_answer','')).lower()}"
                data-qid="{qid}" data-exp-ok="{_ea(q.get('explanation_if_correct',''))}" data-exp-bad="{_ea(q.get('explanation_if_incorrect',''))}">
          {_e(opt)}
        </button>"""
        for opt in options
    )

    return f"""
      <div class="question-block" id="{qid}">
        <p class="question-text">{_e(q.get("question_text",""))}</p>
        <div class="options">{opts_html}</div>
        <div class="feedback" id="{qid}-feedback" aria-live="polite"></div>
      </div>"""


def render_source_panel_block(data: dict) -> str:
    sources = data.get("sources", [])
    if not sources:
        return ""
    items = "".join(f"<li>{_e(s)}</li>" for s in sources)
    return f'<div class="block block-source-panel"><h4>Sources</h4><ul>{items}</ul></div>'


BLOCK_RENDERERS = {
    "narrative":    lambda d, si: render_narrative_block(d),
    "key_points":   lambda d, si: render_key_points_block(d),
    "figures":      lambda d, si: render_figures_block(d),
    "quiz":         lambda d, si: render_quiz_block(d, si),
    "source_panel": lambda d, si: render_source_panel_block(d),
}

def _get_block_renderers() -> dict:
    _load_exam_toolkit()
    return {**BLOCK_RENDERERS, **_EXAM_BLOCK_RENDERERS}


# ---------------------------------------------------------------------------
# State → section config conversion
# ---------------------------------------------------------------------------

def state_to_sections(state: dict) -> list[dict]:
    """
    Convert verify-app state.json into the modular section/block format
    consumed by the template engine.

    Sections produced by ExamConfig.to_state() store their block list in
    _blocks_override and bypass the legacy narrative/quiz reconstruction.
    """
    order    = state.get("section_order") or list(state.get("sections", {}).keys())
    sections = []

    for sid in order:
        sec = state.get("sections", {}).get(sid, {})
        if not sec.get("narrative_approved"):
            continue  # skip unapproved sections

        # Exam-toolkit path: blocks fully specified by the exam config
        if "_blocks_override" in sec:
            sections.append({"section_id": sid, "blocks": sec["_blocks_override"]})
            continue

        # Legacy study-guide path: reconstruct blocks from narrative/quiz
        narrative  = sec.get("narrative", {})
        quiz_data  = sec.get("quiz", {})

        # Collect unique source documents for source panel
        sources = sorted({
            a.get("source_document", "")
            for a in sec.get("source_annotations", [])
            if a.get("source_document")
        })

        blocks = [
            {"type": "narrative", "data": {
                "heading": narrative.get("heading", sid),
                "intro":   narrative.get("intro", ""),
            }},
            {"type": "key_points", "data": {
                "points": narrative.get("key_points", []),
            }},
            {"type": "figures", "data": {
                "figures": narrative.get("figures", []),
            }},
        ]

        if quiz_data.get("questions"):
            blocks.append({"type": "quiz", "data": quiz_data})

        if sources:
            blocks.append({"type": "source_panel", "data": {"sources": sources}})

        sections.append({"section_id": sid, "blocks": blocks})

    return sections


# ---------------------------------------------------------------------------
# Template assembly
# ---------------------------------------------------------------------------

def build(state: dict, title: str, theme: str) -> str:
    sections = state_to_sections(state)
    settings = state.get("global_settings", {})
    show_progress = settings.get("show_progress", True)

    if not sections:
        print("Warning: no approved sections found. Output will be empty.", file=sys.stderr)

    # Sidebar nav items
    nav_items = "".join(
        f'<li><a class="nav-link" href="#section-{i}" data-idx="{i}">'
        f'{_e(s["blocks"][0]["data"].get("heading", s["section_id"]) if s["blocks"] else s["section_id"])}'
        f'<span class="nav-check" id="nav-check-{i}"></span></a></li>'
        for i, s in enumerate(sections)
    )

    # Section HTML
    renderers = _get_block_renderers()
    section_html = ""
    for i, sec in enumerate(sections):
        blocks_html = "".join(
            renderers.get(b["type"], lambda d, si: "")(b["data"], i)
            for b in sec["blocks"]
        )
        section_html += f"""
        <section class="guide-section" id="section-{i}" data-idx="{i}">
          {blocks_html}
        </section>"""

    progress_bar = """
        <div id="progress-bar-wrap" aria-label="Progress">
          <div id="progress-bar"></div>
          <span id="progress-text"></span>
        </div>""" if show_progress else ""

    css  = (PKG_DIR / "static" / "guide.css").read_text(encoding="utf-8") + _EXAM_CSS
    js   = (PKG_DIR / "static" / "guide.js").read_text(encoding="utf-8") + _EXAM_JS
    theme_class = f"theme-{theme}"

    # Optional exam footer
    author = state.get("global_settings", {}).get("author", "")
    footer_html = (
        f'\n    <div class="exam-footer">Mock Exam</div>'
        if author else ""
    )

    return f"""<!DOCTYPE html>
<html lang="en" class="{theme_class}">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{_e(title)}</title>
  <style>{css}</style>
</head>
<body>

<div id="app">

  <!-- Sidebar -->
  <nav id="sidebar" aria-label="Sections">
    <div id="sidebar-header">
      <span id="guide-title">{_e(title)}</span>
      <button id="sidebar-close" aria-label="Close sidebar">✕</button>
    </div>
    {progress_bar}
    <ul id="nav-list">{nav_items}</ul>
  </nav>

  <!-- Main content -->
  <div id="main-wrap">
    <header id="sticky-header">
      <button id="sidebar-toggle" aria-label="Toggle sidebar">☰</button>
      <span id="current-section-label"></span>
    </header>

    <main id="content">
      {section_html}
      {footer_html}
    </main>

    <div id="summary-panel" hidden>
      <h2>You're done!</h2>
      <p id="summary-text"></p>
      <button id="restart-btn">Start Over</button>
    </div>
  </div>

</div>

<script>{js}</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Escaping helpers
# ---------------------------------------------------------------------------

def _e(s) -> str:
    """HTML-escape a value."""
    return (str(s)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))


def _ea(s) -> str:
    """Escape for use inside an HTML attribute (double-quote safe)."""
    return _e(s).replace("'", "&#39;")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Build interactive study guide HTML.")
    parser.add_argument("--state",  "-s", required=True, help="Path to state.json")
    parser.add_argument("--output", "-o", required=True, help="Output .html path")
    parser.add_argument("--title",  default="Study Guide", help="Guide title")
    parser.add_argument("--theme",  choices=["light", "dark", "high-contrast"], default="light")
    args = parser.parse_args()

    state_path = Path(args.state)
    if not state_path.exists():
        print(f"Error: {state_path} not found.", file=sys.stderr)
        sys.exit(1)

    with open(state_path) as f:
        state = json.load(f)

    html = build(state, args.title, args.theme)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")

    approved = [
        sid for sid, sec in state.get("sections", {}).items()
        if sec.get("narrative_approved")
    ]
    print(f"Built: {output_path}")
    print(f"Sections included: {len(approved)}")
    print(f"Theme: {args.theme}")
    print(f"Size: {len(html):,} bytes")


if __name__ == "__main__":
    main()
