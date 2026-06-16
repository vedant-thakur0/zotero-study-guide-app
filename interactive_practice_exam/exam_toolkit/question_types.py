"""
question_types.py — Block builder functions for the exam toolkit.

Each function returns a dict in the state-schema format consumed by build_guide.py
(extended with new block types registered in exam_toolkit/renderers.py).

Block types defined here:
  matching       — two-column click-to-pair vocabulary matching
  free_text      — one or more labeled textareas + a reveal button + sample answers
  text_input     — fill-in-the-blank inline text inputs with verb hints + check/reveal
  letter_choice  — small letter inputs (a/b/c) with inline option list + check/reveal
  text_passage   — styled reading-text box (warm bg + accent border)
  reveal_qa      — N labeled textareas (open-ended Qs) + single reveal button + model answers
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Helpers shared with build_guide (avoid circular import — keep standalone)
# ---------------------------------------------------------------------------

def _approved_section(sid: str, heading: str, intro: str, blocks: list) -> dict:
    """Wrap blocks in the state-schema section envelope."""
    return {
        "narrative_approved": True,
        "narrative": {
            "heading": heading,
            "intro": intro,
            "key_points": [],
            "figures": [],
        },
        "quiz": {"questions": []},
        "source_annotations": [],
        "_blocks_override": blocks,   # picked up by patched state_to_sections()
    }


# ---------------------------------------------------------------------------
# Block builders
# ---------------------------------------------------------------------------

def narrative_block(heading: str, intro: str) -> dict:
    """Plain heading + intro paragraph."""
    return {"type": "narrative", "data": {"heading": heading, "intro": intro}}


def key_points_block(points: list[tuple[str, str]]) -> dict:
    """
    Always-visible term/explanation pairs (read-only).
    points: list of (term, explanation) tuples.
    """
    return {
        "type": "key_points",
        "data": {
            "points": [{"term": t, "explanation": e} for t, e in points],
        },
    }


def matching_block(
    left_words: list[str],
    right_words: list[str],
    correct_map: list[int],      # correct_map[i] = index into right_words for left_words[i]
    reveal_text: str = "",
) -> dict:
    """
    Interactive two-column click-to-pair matching widget.

    correct_map[i] is the 0-based index in right_words that matches left_words[i].
    reveal_text is the full-sentence answer shown on "Voir les bonnes réponses".
    """
    return {
        "type": "matching",
        "data": {
            "left_words": left_words,
            "right_words": right_words,
            "correct_map": correct_map,
            "reveal_text": reveal_text,
        },
    }


def free_text_block(
    prompts: list[str],
    placeholder: str = "Écrivez votre réponse...",
    reveal_label: str = "💡 Exemples de réponses",
    sample_answers: list[str] | None = None,
    expression_list: str = "",
) -> dict:
    """
    One or more labeled textarea inputs + optional reveal button with sample answers.

    prompts: list of label strings (one textarea per label).
    expression_list: optional introductory line shown above textareas (e.g. vocab list).
    sample_answers: list of bullet strings shown when reveal is clicked.
    """
    return {
        "type": "free_text",
        "data": {
            "expression_list": expression_list,
            "prompts": prompts,
            "placeholder": placeholder,
            "reveal_label": reveal_label,
            "sample_answers": sample_answers or [],
        },
    }


def text_input_block(
    items: list[dict],
    check_label: str = "✔️ Vérifier",
    reveal_label: str = "📖 Afficher corrigé",
    preamble: str = "",
) -> dict:
    """
    Fill-in-the-blank with inline text inputs, accent-tolerant JS checking,
    and a full correction reveal.

    items: list of dicts with keys:
        stem        — sentence with ____ placeholder
        hint        — optional (verb1 / verb2) hint shown as inline code
        answer      — correct text (used for checking + correction display)
        answer_display — optional richer HTML shown in corrigé (defaults to answer)

    preamble: optional instruction line shown above all inputs.
    """
    return {
        "type": "text_input",
        "data": {
            "preamble": preamble,
            "items": items,
            "check_label": check_label,
            "reveal_label": reveal_label,
        },
    }


def letter_choice_block(
    items: list[dict],
    check_label: str = "🔎 Vérifier mes réponses",
    reveal_label: str = "📝 Montrer le corrigé",
    correct_answers: list[str] | None = None,
) -> dict:
    """
    Multiple-choice via small letter inputs (student types 'a', 'b', or 'c').

    items: list of dicts with keys:
        stem     — sentence with ___ at the end
        options  — list of 3 strings [a_text, b_text, c_text]

    correct_answers: list of single-letter strings ['b','b','a',...] parallel to items.
    The full answer key line (e.g. "1-b, 2-b, 3-a...") is derived automatically.
    """
    return {
        "type": "letter_choice",
        "data": {
            "items": items,
            "correct_answers": correct_answers or [],
            "check_label": check_label,
            "reveal_label": reveal_label,
        },
    }


def text_passage_block(title: str, paragraphs: list[str]) -> dict:
    """
    Styled reading-text box: warm background, left accent border.
    title: bold label displayed above paragraphs.
    paragraphs: list of paragraph strings.
    """
    return {
        "type": "text_passage",
        "data": {
            "title": title,
            "paragraphs": paragraphs,
        },
    }


def reveal_qa_block(
    preamble: str,
    questions: list[str],
    model_answers: list[str],
    check_label: str = "📖 Vérifier compréhension",
) -> dict:
    """
    Open-ended comprehension questions: one textarea per question,
    a single reveal button, and a hidden div with all model answers bulleted.

    questions: list of question label strings.
    model_answers: parallel list of model answer strings (may contain HTML).
    """
    return {
        "type": "reveal_qa",
        "data": {
            "preamble": preamble,
            "questions": questions,
            "model_answers": model_answers,
            "check_label": check_label,
        },
    }


# ---------------------------------------------------------------------------
# Section assembler
# ---------------------------------------------------------------------------

def exam_section(
    sid: str,
    heading: str,
    intro: str,
    blocks: list[dict],
) -> tuple[str, dict]:
    """
    Return (section_id, section_dict) ready to drop into state['sections'].
    blocks: ordered list of block dicts produced by the builders above.
    The first block should be a narrative_block() so the sidebar title resolves.
    """
    return sid, _approved_section(sid, heading, intro, blocks)
