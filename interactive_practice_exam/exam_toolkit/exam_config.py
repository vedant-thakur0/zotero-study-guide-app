"""
exam_config.py — Dataclass schema for a complete exam definition.

An ExamConfig is the single object an exam module (e.g. exams/fr202_mock.py)
constructs and returns.  The build pipeline converts it to a state dict, then
passes that to build_guide.build().

Design goals:
  • One config file per exam document (drop a new one in exams/ for each new exam).
  • All answer keys and sample answers live in the exam config — not scattered
    through parser code.
  • Section type is declared explicitly so the renderer can dispatch correctly.

Typical exam module (exams/my_exam.py):

    from exam_toolkit.exam_config import ExamConfig, SectionConfig
    from exam_toolkit.question_types import matching_block, free_text_block, ...

    def build_config() -> ExamConfig:
        return ExamConfig(
            title="FRANÇAIS 202 — Examen Final",
            theme="light",
            show_progress=True,
            author="Prof. Tetne",
            sections=[
                SectionConfig(
                    id="section_0",
                    heading="A. Vocabulaire (5 pts)",
                    intro="Associez chaque mot à son contraire.",
                    blocks=[
                        matching_block(
                            left_words=[...],
                            right_words=[...],
                            correct_map=[...],
                            reveal_text="1→b, 2→c, ...",
                        )
                    ],
                ),
                ...
            ],
        )
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SectionConfig:
    """
    One exam section (maps 1-to-1 to a <section> element in the output HTML).

    id       — unique string key, e.g. "section_0"
    heading  — displayed in sidebar nav and as the section <h2>
    intro    — sub-heading paragraph shown below the heading
    blocks   — ordered list of block dicts from question_types.py builders
    """
    id: str
    heading: str
    intro: str
    blocks: list[dict] = field(default_factory=list)


@dataclass
class ExamConfig:
    """
    Top-level exam configuration object.

    title         — HTML <title> and sidebar header
    theme         — "light" | "dark" | "high-contrast"
    show_progress — whether to display the progress bar in the sidebar
    author        — optional footer attribution string
    sections      — ordered list of SectionConfig objects
    """
    title: str
    sections: list[SectionConfig]
    theme: str = "light"
    show_progress: bool = True
    author: str = ""

    # ------------------------------------------------------------------
    # Conversion to the state dict format consumed by build_guide.build()
    # ------------------------------------------------------------------

    def to_state(self) -> dict[str, Any]:
        """
        Convert this ExamConfig to the state dict format expected by
        build_guide.build().  The `_blocks_override` key on each section
        is picked up by the patched state_to_sections() in renderers.py.
        """
        sections: dict[str, dict] = {}
        order: list[str] = []

        for sec in self.sections:
            order.append(sec.id)
            # Derive heading/intro from the first narrative block if present,
            # or use the SectionConfig fields directly as fallback.
            first_narrative = next(
                (b for b in sec.blocks if b.get("type") == "narrative"), None
            )
            heading = (
                first_narrative["data"]["heading"]
                if first_narrative
                else sec.heading
            )
            intro = (
                first_narrative["data"]["intro"]
                if first_narrative
                else sec.intro
            )

            sections[sec.id] = {
                "narrative_approved": True,
                "narrative": {
                    "heading": heading,
                    "intro": intro,
                    "key_points": [],
                    "figures": [],
                },
                "quiz": {"questions": []},
                "source_annotations": [],
                "_blocks_override": sec.blocks,
            }

        return {
            "global_settings": {
                "title": self.title,
                "theme": self.theme,
                "show_progress": self.show_progress,
                "author": self.author,
            },
            "section_order": order,
            "sections": sections,
        }
