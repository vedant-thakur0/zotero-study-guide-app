"""
test_concurrency.py — Verifies the two levers introduced in
feat/parallel-gen-cache:

  1. Prompt cache reuse: the leading <system>…</system> block in
     build_narrative_prompt / build_quiz_prompt is byte-identical across
     distinct sections. (Anthropic's ephemeral cache only hits when the cached
     text is byte-identical.)

  2. ThreadPoolExecutor parallelism in run_narrative / run_quiz: 4 sections
     finish in <2× the per-call time, and a single-section failure does NOT
     poison its siblings.
"""

import re
import sys
import time
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))


# ===========================================================================
# Helpers
# ===========================================================================

def _extract_system_block(prompt: str) -> str:
    """Replicate the extraction logic in generate.call_llm (Anthropic branch)."""
    m = re.match(
        r"\s*(?:<!--.*?-->\s*)?<system>(.*?)</system>", prompt, re.DOTALL
    )
    assert m is not None, f"no <system> block found in prompt:\n{prompt[:200]}"
    return m.group(1)


def _make_section(sid: str, n_anns: int = 3, with_red: bool = True) -> dict:
    """Build a minimal section with deterministic-but-distinct annotations."""
    anns = []
    colors = ["yellow", "blue", "green", "red", "orange", "purple"]
    for i in range(n_anns):
        anns.append({
            "id": f"{sid}_ann{i:03d}",
            "text": f"Fact {i} for {sid}",
            "color": colors[i % len(colors)] if (with_red or colors[i % len(colors)] != "red")
                     else "yellow",
            "page": 100 + i,
            "instructor_note": "",
            "source_document": "Test",
        })
    if with_red and not any(a["color"] == "red" for a in anns):
        anns[0]["color"] = "red"
    return {"section_id": sid, "source_annotations": anns}


# ===========================================================================
# 1. Byte-equality of the system block across distinct sections
# ===========================================================================

class TestPromptCacheReuse:
    """The <system> block must be byte-identical across distinct sections so
    the Anthropic ephemeral cache hits on call #2..N."""

    def test_narrative_system_block_identical_across_sections(self):
        import zsg.generate as generate
        a = _make_section("section_alpha", n_anns=4)
        b = _make_section("section_beta",  n_anns=7)
        prompt_a = generate.build_narrative_prompt(a)
        prompt_b = generate.build_narrative_prompt(b)
        assert prompt_a != prompt_b, "sanity: full prompts should differ"
        sys_a = _extract_system_block(prompt_a)
        sys_b = _extract_system_block(prompt_b)
        assert sys_a == sys_b, (
            "narrative <system> block diverged between sections — "
            "Anthropic ephemeral cache will not hit"
        )

    def test_quiz_system_block_identical_across_sections(self):
        import zsg.generate as generate
        a = _make_section("section_alpha", n_anns=3)
        b = _make_section("section_beta",  n_anns=5)
        nar_a = {"section_id": "section_alpha", "heading": "A", "intro": "i",
                 "key_points": [], "figures": []}
        nar_b = {"section_id": "section_beta",  "heading": "B", "intro": "j",
                 "key_points": [], "figures": []}
        prompt_a = generate.build_quiz_prompt(a, nar_a)
        prompt_b = generate.build_quiz_prompt(b, nar_b)
        assert prompt_a != prompt_b, "sanity: full prompts should differ"
        sys_a = _extract_system_block(prompt_a)
        sys_b = _extract_system_block(prompt_b)
        assert sys_a == sys_b, (
            "quiz <system> block diverged between sections — "
            "Anthropic ephemeral cache will not hit"
        )

    def test_placeholders_live_after_system_block(self):
        """No placeholder substitution should touch text inside <system>."""
        import zsg.generate as generate
        # Use sentinel section_id; if any placeholder is inside the system
        # block the sentinel will appear there too.
        s = _make_section("SENTINEL_SID_XYZ", n_anns=2)
        prompt = generate.build_narrative_prompt(s)
        sys_block = _extract_system_block(prompt)
        assert "SENTINEL_SID_XYZ" not in sys_block, (
            "section_id leaked into <system> block — cache will not hit"
        )
        assert "SENTINEL_SID_XYZ_ann000" not in sys_block, (
            "annotation IDs leaked into <system> block — cache will not hit"
        )


# ===========================================================================
# 2. Concurrency: parallel speedup + sibling-failure isolation
# ===========================================================================

class TestParallelNarrative:
    """ThreadPoolExecutor cuts wall time and isolates failures."""

    def test_concurrency_4_is_faster_than_serial(self):
        import zsg.generate as generate

        sections = [_make_section(f"sec_{i}", n_anns=3) for i in range(4)]
        state = {"sections": {}}
        cfg = {"provider": "anthropic", "model": "claude-haiku-4-5-20251001"}

        def slow_llm(prompt, cfg_, dry_run=False):
            time.sleep(0.5)
            return '{"heading": "h", "intro": "i", "key_points": [], "figures": []}'

        t0 = time.perf_counter()
        with patch("zsg.generate.call_llm", side_effect=slow_llm):
            generate.run_narrative(
                sections, state, cfg,
                only=None, dry_run=False, force=False,
                concurrency=4,
            )
        elapsed = time.perf_counter() - t0

        # 4 calls × 0.5s = 2.0s serial. Parallel should be well under 1.0s
        # (effectively one ~0.5s slot + scheduling overhead).
        assert elapsed < 1.0, (
            f"parallel run took {elapsed:.2f}s — expected <1.0s "
            f"(serial baseline would be ~2.0s)"
        )
        assert len(state["sections"]) == 4
        for i in range(4):
            assert state["sections"][f"sec_{i}"].get("narrative") is not None

    def test_serial_baseline_matches_per_call_time(self):
        """Concurrency=1 keeps the old serial behavior — no thread pool."""
        import zsg.generate as generate

        sections = [_make_section(f"sec_{i}", n_anns=2) for i in range(3)]
        state = {"sections": {}}
        cfg = {"provider": "anthropic", "model": "claude-haiku-4-5-20251001"}

        def slow_llm(prompt, cfg_, dry_run=False):
            time.sleep(0.2)
            return "{}"

        t0 = time.perf_counter()
        with patch("zsg.generate.call_llm", side_effect=slow_llm):
            generate.run_narrative(
                sections, state, cfg, only=None, dry_run=False, force=False,
                concurrency=1,
            )
        elapsed = time.perf_counter() - t0

        # 3 calls × 0.2s = 0.6s serial; should be at least 0.55s.
        assert elapsed >= 0.55, (
            f"serial run took {elapsed:.2f}s — expected ≥0.55s "
            f"(suggests parallelism leaked into concurrency=1)"
        )

    def test_one_failed_section_does_not_poison_siblings(self):
        import zsg.generate as generate

        sections = [_make_section(f"sec_{i}", n_anns=2) for i in range(4)]
        state = {"sections": {}}
        cfg = {"provider": "anthropic", "model": "claude-haiku-4-5-20251001"}

        # The third call (sec_2) raises. Workers run concurrently so order is
        # not deterministic — track by prompt content.
        def flaky_llm(prompt, cfg_, dry_run=False):
            if "sec_2" in prompt:
                raise RuntimeError("simulated provider error for sec_2")
            return '{"heading": "h", "intro": "i", "key_points": [], "figures": []}'

        with patch("zsg.generate.call_llm", side_effect=flaky_llm):
            generate.run_narrative(
                sections, state, cfg, only=None, dry_run=False, force=False,
                concurrency=4,
            )

        # sec_2 failed cleanly with an error captured under its own key.
        assert "sec_2" in state["sections"]
        assert "narrative_error" in state["sections"]["sec_2"]
        assert "sec_2" in state["sections"]["sec_2"]["narrative_error"]
        assert "narrative" not in state["sections"]["sec_2"], \
            "failed section should not have a narrative payload"

        # All 3 siblings succeeded and have a narrative payload.
        for i in (0, 1, 3):
            sid = f"sec_{i}"
            assert sid in state["sections"], f"{sid} missing from state"
            assert "narrative" in state["sections"][sid], (
                f"{sid} should have a narrative — sibling failure poisoned it"
            )
            assert "narrative_error" not in state["sections"][sid], (
                f"{sid} should NOT have an error — got "
                f"{state['sections'][sid].get('narrative_error')!r}"
            )

    def test_concurrency_argument_threadsafe_state_writes(self):
        """Many concurrent workers writing to state shouldn't drop any keys."""
        import zsg.generate as generate

        n = 16
        sections = [_make_section(f"big_{i:02d}", n_anns=2) for i in range(n)]
        state = {"sections": {}}
        cfg = {"provider": "anthropic", "model": "claude-haiku-4-5-20251001"}

        def fast_llm(prompt, cfg_, dry_run=False):
            return '{"heading": "h", "intro": "i", "key_points": [], "figures": []}'

        with patch("zsg.generate.call_llm", side_effect=fast_llm):
            generate.run_narrative(
                sections, state, cfg, only=None, dry_run=False, force=False,
                concurrency=8,
            )

        # All 16 sections must be present.
        assert len(state["sections"]) == n
        for i in range(n):
            assert f"big_{i:02d}" in state["sections"]


class TestParallelQuiz:
    """Quiz path mirrors narrative — sanity-check it also parallelizes."""

    def test_quiz_concurrency_4_is_faster_than_serial(self):
        import zsg.generate as generate

        sections = [_make_section(f"q_{i}", n_anns=3, with_red=True) for i in range(4)]
        # Pre-approve narratives so run_quiz will actually call the LLM.
        state = {"sections": {}}
        for s in sections:
            state["sections"][s["section_id"]] = {
                "narrative_approved": True,
                "narrative": {"heading": "h", "intro": "i",
                              "key_points": [], "figures": []},
            }
        cfg = {"provider": "anthropic", "model": "claude-haiku-4-5-20251001"}

        def slow_llm(prompt, cfg_, dry_run=False):
            time.sleep(0.5)
            return '{"section_id": "s", "questions": []}'

        t0 = time.perf_counter()
        with patch("zsg.generate.call_llm", side_effect=slow_llm):
            generate.run_quiz(
                sections, state, cfg, only=None, dry_run=False, force=False,
                concurrency=4,
            )
        elapsed = time.perf_counter() - t0
        assert elapsed < 1.0, (
            f"parallel quiz run took {elapsed:.2f}s — expected <1.0s"
        )
        for i in range(4):
            assert state["sections"][f"q_{i}"].get("quiz") is not None
