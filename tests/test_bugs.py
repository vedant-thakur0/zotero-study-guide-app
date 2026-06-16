"""
test_bugs.py — Regression suite for logic bugs identified in code review.

All tests run without LLM calls. Fixtures are loaded from the checked-in
civil_rights_m7 project data (annotations.json, sections.json, state.json,
output.html).  The gold-standard output.html is used to verify build_guide
output fidelity.

Bugs covered
------------
Bug 2  — generate_quiz KeyError: section key missing from state
Bug 3  — locked_state() silently writes corrupted state on exception
Bug 4  — open_project returns wrong stage-3 status (reads sections.json, not state.json)
Bug 6  — export_build does not persist global_settings to state.json
Bug 7  — json_repair cumulative application can mask a clean intermediate
Bug 8  — upload_zotero_export missing file-extension allowlist (path traversal)
Bug 10 — _fix_python_literals replaces inside quoted strings
Bug 11 — from_zotero_html targets first </span> instead of citation </span> for notes
Bug 12 — pipeline polling interval never clears on error status (JS logic, Python-side sim)
Bug 13 — pipeline_runner._runs grows without bound
"""

import copy
import json
import os
import re
import sys
import tempfile
import threading
import time
from io import BytesIO
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Path bootstrap — allow imports from the project root
# ---------------------------------------------------------------------------

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

FIXTURES = ROOT / "tests" / "fixtures" / "civil_rights_m7"
GOLD_HTML = (FIXTURES / "output.html").read_text(encoding="utf-8")

# Load shared fixture data once
with open(FIXTURES / "state.json") as f:
    GOLD_STATE = json.load(f)

with open(FIXTURES / "sections.json") as f:
    GOLD_SECTIONS = json.load(f)

with open(FIXTURES / "annotations.json") as f:
    GOLD_ANNOTATIONS = json.load(f)


# ===========================================================================
# Helpers
# ===========================================================================

def make_flask_app(state_path: Path, sections_path: Path):
    """Return a Flask test client wired to the given paths."""
    import zsg.verify as verify
    # Reset module-level globals for each test
    verify.STATE_PATH = state_path
    verify.SECTIONS_PATH = sections_path
    verify.APP_CONFIG_PATH = ROOT / "app_config.json"
    verify.app.config["TESTING"] = True
    return verify.app.test_client()


def write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


# ===========================================================================
# Bug 2 — generate_quiz KeyError when section not initialised in state
# ===========================================================================

class TestBug2GenerateQuizKeyError:
    """
    /api/section/<id>/generate_quiz writes state["sections"][section_id]["quiz"]
    without a setdefault guard.  If the section_id is present in sections.json
    but has never been initialised into state.json, this raises KeyError.
    """

    def test_keyerror_when_section_missing_from_state(self, tmp_path):
        # sections.json has section_p42_p53; state has it approved
        # sections.json also has section_p78_p82 — we put ONLY section_p42_p53
        # in state so that section_p78_p82 is 'approved' in state but with
        # a missing key if we manipulate it directly.
        sections_path = tmp_path / "sections.json"
        write_json(sections_path, GOLD_SECTIONS)

        # State: section_p78_p82 exists but has no "quiz" key yet; narrative approved
        state = copy.deepcopy(GOLD_STATE)
        state["sections"]["section_p78_p82"]["narrative_approved"] = True
        # Remove the quiz entirely — simulates a fresh section
        state["sections"]["section_p78_p82"].pop("quiz", None)
        state_path = tmp_path / "state.json"
        write_json(state_path, state)

        client = make_flask_app(state_path, sections_path)

        # Patch call_llm so no real API call is made
        fake_quiz = {
            "section_id": "section_p78_p82",
            "questions": [{"question_text": "Q?", "correct_answer": "A",
                           "distractors": ["B", "C"],
                           "explanation_if_correct": "", "explanation_if_incorrect": "",
                           "source_annotation_ids": ["ann_009"]}],
        }
        with patch("zsg.generate.call_llm", return_value=json.dumps(fake_quiz)):
            resp = client.post("/api/section/section_p78_p82/generate_quiz")

        # Bug 2: without setdefault, this raises KeyError → 500
        # The test asserts it should NOT 500
        assert resp.status_code == 200, (
            f"Bug 2: generate_quiz returns {resp.status_code} (expected 200). "
            "Missing setdefault guard causes KeyError."
        )
        data = resp.get_json()
        assert data.get("ok") is True

    def test_state_file_updated_after_generate_quiz(self, tmp_path):
        """Even when the section key was absent, the quiz should land in state.json."""
        sections_path = tmp_path / "sections.json"
        write_json(sections_path, GOLD_SECTIONS)

        state = copy.deepcopy(GOLD_STATE)
        state["sections"]["section_p78_p82"].pop("quiz", None)
        state["sections"]["section_p78_p82"]["narrative_approved"] = True
        state_path = tmp_path / "state.json"
        write_json(state_path, state)

        client = make_flask_app(state_path, sections_path)
        fake_quiz = {"section_id": "section_p78_p82", "questions": []}
        with patch("zsg.generate.call_llm", return_value=json.dumps(fake_quiz)):
            resp = client.post("/api/section/section_p78_p82/generate_quiz")

        if resp.status_code == 200:
            saved = json.loads(state_path.read_text())
            assert "quiz" in saved["sections"]["section_p78_p82"], (
                "Bug 2: quiz not written to state.json after generate_quiz"
            )


# ===========================================================================
# Bug 3 — locked_state() writes partial state on exception inside block
# ===========================================================================

class TestBug3LockedStateExceptionCorruption:
    """
    locked_state() uses @contextmanager without try/finally.

    When an exception is raised inside the 'with' block, the @contextmanager
    protocol throws the exception back into the generator at the yield point.
    Because there is no try/finally, the write code after 'yield' does NOT run —
    the exception propagates without flushing the mutated state.

    This means Bug 3's original framing was backwards: the state is NOT written
    on exception (safe), but the consequence is that any valid edits made before
    the exception are also silently dropped — lost work, not corruption.
    """

    def test_partial_state_NOT_written_on_exception(self, tmp_path):
        """Verify @contextmanager skips the write on exception (no corruption)."""
        import zsg.verify as verify

        state_path = tmp_path / "state.json"
        write_json(state_path, copy.deepcopy(GOLD_STATE))
        verify.STATE_PATH = state_path

        try:
            with verify.locked_state() as state:
                state["sections"]["INJECTED_GHOST"] = {"narrative_approved": True}
                raise RuntimeError("simulated error mid-block")
        except RuntimeError:
            pass

        saved = json.loads(state_path.read_text())
        # The write was skipped entirely — ghost key is absent (no corruption)
        assert "INJECTED_GHOST" not in saved["sections"], (
            "Unexpected: locked_state() wrote partial state after exception."
        )

    def test_valid_edits_lost_on_exception(self, tmp_path):
        """
        Flip side of the same issue: legitimate edits made before an exception
        inside locked_state() are silently discarded.  This is the real bug:
        no partial-save or rollback — the caller has no way to know work was lost.
        """
        import zsg.verify as verify

        state_path = tmp_path / "state.json"
        original = copy.deepcopy(GOLD_STATE)
        write_json(state_path, original)
        verify.STATE_PATH = state_path

        try:
            with verify.locked_state() as state:
                # This is a legitimate, desirable edit
                state["sections"]["section_p42_p53"]["narrative_approved"] = False
                # But an exception follows — the edit will be lost
                raise RuntimeError("something went wrong")
        except RuntimeError:
            pass

        saved = json.loads(state_path.read_text())
        # Still True — the update was lost
        assert saved["sections"]["section_p42_p53"]["narrative_approved"] is True, (
            "Bug 3 (lost-write variant): legitimate edit before exception was silently "
            "discarded. locked_state() should use try/finally or explicit rollback."
        )


# ===========================================================================
# Bug 4 — open_project returns wrong stage-3 status
# ===========================================================================

class TestBug4OpenProjectStage3Status:
    """
    open_project() checks stage-3 status by counting sections in sections.json.
    That is Stage 2 output. Stage 3 is LLM generation; the correct check is
    whether state.json has any narrative content.

    Scenario: sections.json has 2 sections (Stage 2 done) but state.json has
    no narratives (Stage 3 NOT done). open_project should return stages["3"] = False.
    """

    def test_stage3_false_when_only_sections_exist(self, tmp_path):
        # Write sections.json (Stage 2 done) but an empty state (Stage 3 not done)
        proj_dir = tmp_path / "projects" / "test_proj"
        proj_dir.mkdir(parents=True)

        write_json(proj_dir / "sections.json", GOLD_SECTIONS)
        write_json(proj_dir / "state.json", {"sections": {}, "section_order": []})
        (proj_dir / "annotations.json").write_text("{}")

        # Minimal app_config so load_app_config works
        cfg = {"projects": [{"slug": "test_proj", "name": "Test"}], "active_project": None}
        cfg_path = tmp_path / "app_config.json"
        write_json(cfg_path, cfg)

        import zsg.verify as verify
        verify.APP_CONFIG_PATH = cfg_path
        # Point PROJECT_ROOT into our tmp tree so slug-based path resolution works
        with patch.object(verify, "PROJECT_ROOT", tmp_path):
            verify.app.config["TESTING"] = True
            client = verify.app.test_client()
            resp = client.post(
                "/api/projects/open",
                json={"slug": "test_proj"},
                content_type="application/json",
            )

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["stages"]["3"] is False, (
            f"Bug 4: open_project reports stage-3 as {data['stages']['3']} "
            "but no LLM narrative exists yet — it is incorrectly reading sections.json count."
        )

    def test_stage3_true_when_narratives_exist(self, tmp_path):
        proj_dir = tmp_path / "projects" / "test_proj"
        proj_dir.mkdir(parents=True)

        write_json(proj_dir / "sections.json", GOLD_SECTIONS)
        write_json(proj_dir / "state.json", copy.deepcopy(GOLD_STATE))
        (proj_dir / "annotations.json").write_text("{}")

        cfg = {"projects": [{"slug": "test_proj", "name": "Test"}], "active_project": None}
        cfg_path = tmp_path / "app_config.json"
        write_json(cfg_path, cfg)

        import zsg.verify as verify
        verify.APP_CONFIG_PATH = cfg_path
        with patch.object(verify, "PROJECT_ROOT", tmp_path):
            verify.app.config["TESTING"] = True
            client = verify.app.test_client()
            resp = client.post(
                "/api/projects/open",
                json={"slug": "test_proj"},
                content_type="application/json",
            )

        data = resp.get_json()
        assert data["stages"]["3"] is True, (
            "Bug 4 (positive case): open_project should report stage-3 True when narratives exist"
        )


# ===========================================================================
# Bug 6 — export_build does not persist global_settings to state.json
# ===========================================================================

class TestBug6ExportBuildGlobalSettingsNotPersisted:
    """
    export_build() mutates an in-memory copy of state and passes it to
    build_guide.build(), but never writes the updated global_settings back to
    state.json.  Title and theme chosen in the export form are therefore lost
    after a page reload.
    """

    def test_global_settings_persisted_after_build(self, tmp_path):
        state_path = tmp_path / "state.json"
        sections_path = tmp_path / "sections.json"
        write_json(state_path, copy.deepcopy(GOLD_STATE))
        write_json(sections_path, GOLD_SECTIONS)

        client = make_flask_app(state_path, sections_path)

        resp = client.post(
            "/api/export/build",
            json={"title": "My Custom Title", "theme": "dark", "show_progress": False},
            content_type="application/json",
        )
        assert resp.status_code == 200

        saved = json.loads(state_path.read_text())
        gs = saved.get("global_settings", {})

        assert gs.get("title") == "My Custom Title", (
            f"Bug 6: global_settings.title not persisted after build — got {gs.get('title')!r}"
        )
        assert gs.get("theme") == "dark", (
            f"Bug 6: global_settings.theme not persisted after build — got {gs.get('theme')!r}"
        )
        assert gs.get("show_progress") is False, (
            f"Bug 6: global_settings.show_progress not persisted after build — got {gs.get('show_progress')!r}"
        )


# ===========================================================================
# Bug 7 — json_repair cumulative application can mask a clean intermediate
# ===========================================================================

class TestBug7JsonRepairCumulativeApplication:
    """
    attempt_repair() applies fixes cumulatively on the same 'current' string.
    If fix N produces valid JSON, that's returned. But if fix N corrupts output
    that fix N-1 had already made valid, we never return the fix-N-1 result.

    Concrete case: _strip_preamble on already-clean JSON produced by
    _strip_markdown_fence.  If the JSON payload starts with '{' _strip_preamble
    is a no-op — harmless.  The real failure mode is when _strip_trailing_commentary
    followed by _fix_trailing_commas corrupts a valid JSON string containing a
    literal '}' character inside a value.
    """

    def test_already_valid_json_returns_immediately(self):
        from zsg.json_repair import attempt_repair
        data = {"section_id": "section_p42_p53", "heading": "Reconstruction"}
        result, repaired = attempt_repair(json.dumps(data))
        assert result == data
        assert repaired is False

    def test_markdown_fenced_json_repaired(self):
        from zsg.json_repair import attempt_repair
        data = {"questions": [{"question_text": "What?", "correct_answer": "Yes"}]}
        raw = f"```json\n{json.dumps(data)}\n```"
        result, repaired = attempt_repair(raw)
        assert result == data
        assert repaired is True

    def test_preamble_stripped_json_repaired(self):
        from zsg.json_repair import attempt_repair
        data = {"heading": "Civil Rights"}
        raw = f"Here is the JSON you requested:\n{json.dumps(data)}"
        result, repaired = attempt_repair(raw)
        assert result == data
        assert repaired is True

    def test_cumulative_fix_does_not_corrupt_clean_intermediate(self):
        """
        _strip_markdown_fence produces valid JSON.  The subsequent
        _strip_preamble and _strip_trailing_commentary should not destroy it.

        Bug 7: if fixes are applied cumulatively and a later fix corrupts the
        output of an earlier one, attempt_repair will raise ValueError instead
        of returning the clean intermediate.
        """
        from zsg.json_repair import attempt_repair
        # JSON whose last character is } — _strip_trailing_commentary is a no-op,
        # but _fix_trailing_commas might interact with a value containing ","
        data = {
            "section_id": "section_p42_p53",
            "intro": "Reconstruction, post-war era.",
        }
        fenced = f"```json\n{json.dumps(data)}\n```"
        result, repaired = attempt_repair(fenced)
        assert result == data, (
            f"Bug 7: cumulative repair corrupted a valid intermediate — got {result!r}"
        )

    def test_trailing_comma_fix_applied_after_preamble_strip(self):
        from zsg.json_repair import attempt_repair
        # Trailing comma after last key (invalid JSON LLMs often emit)
        raw = '{"heading": "Reconstruction", "intro": "Post-war.",}'
        result, repaired = attempt_repair(raw)
        assert result == {"heading": "Reconstruction", "intro": "Post-war."}
        assert repaired is True


# ===========================================================================
# Bug 8 — upload_zotero_export missing file-extension allowlist
# ===========================================================================

class TestBug8UploadExtensionAllowlist:
    """
    upload_zotero_export() derives the saved extension from the uploaded
    filename using Path().suffix without validating against an allowlist.
    An attacker (or test of safety) could upload a file named 'x.exe' and
    it would be saved as source_export.exe in the project directory.
    """

    def _upload(self, client, filename: str, content: bytes = b"test"):
        return client.post(
            "/api/upload/zotero_export",
            data={"file": (BytesIO(content), filename)},
            content_type="multipart/form-data",
        )

    def test_valid_extensions_accepted(self, tmp_path):
        state_path = tmp_path / "state.json"
        write_json(state_path, {})
        sections_path = tmp_path / "sections.json"
        write_json(sections_path, {})

        # Set up a project
        cfg = {"projects": [{"slug": "cr", "name": "CR"}], "active_project": "cr"}
        cfg_path = tmp_path / "app_config.json"
        write_json(cfg_path, cfg)
        (tmp_path / "projects" / "cr").mkdir(parents=True)

        import zsg.verify as verify
        verify.APP_CONFIG_PATH = cfg_path
        with patch.object(verify, "PROJECT_ROOT", tmp_path):
            verify.app.config["TESTING"] = True
            client = verify.app.test_client()
            for ext in [".html", ".htm", ".csv", ".md", ".json"]:
                resp = self._upload(client, f"export{ext}")
                assert resp.status_code == 200, f"Valid extension {ext!r} rejected"

    def test_dangerous_extensions_rejected(self, tmp_path):
        """
        Bug 8: without an allowlist, these all succeed today (status 200 +
        file written).  After the fix, they should return 400.
        """
        state_path = tmp_path / "state.json"
        write_json(state_path, {})

        cfg = {"projects": [{"slug": "cr", "name": "CR"}], "active_project": "cr"}
        cfg_path = tmp_path / "app_config.json"
        write_json(cfg_path, cfg)
        (tmp_path / "projects" / "cr").mkdir(parents=True)

        import zsg.verify as verify
        verify.APP_CONFIG_PATH = cfg_path
        with patch.object(verify, "PROJECT_ROOT", tmp_path):
            verify.app.config["TESTING"] = True
            client = verify.app.test_client()
            for bad_ext in [".exe", ".sh", ".py", ".bat"]:
                resp = self._upload(client, f"evil{bad_ext}")
                assert resp.status_code == 400, (
                    f"Bug 8: dangerous extension {bad_ext!r} was accepted "
                    f"(status {resp.status_code}). Missing allowlist."
                )


# ===========================================================================
# Bug 10 — _fix_python_literals replaces inside quoted strings
# ===========================================================================

class TestBug10PythonLiteralsInsideStrings:
    """
    _fix_python_literals uses bare \bTrue\b which matches 'True' inside JSON
    string values. "True north" → "true north".
    """

    def test_true_inside_string_value_untouched(self):
        from zsg.json_repair import _fix_python_literals
        # "True" inside a quoted string value must NOT be replaced
        raw = '{"direction": "True north", "valid": True}'
        fixed = _fix_python_literals(raw)
        result = json.loads(fixed)
        assert result["direction"] == "True north", (
            f"Bug 10: _fix_python_literals corrupted string value — "
            f"got {result['direction']!r} instead of 'True north'"
        )
        assert result["valid"] is True  # the bare True should be replaced

    def test_false_inside_string_value_untouched(self):
        from zsg.json_repair import _fix_python_literals
        raw = '{"label": "False positive", "flag": False}'
        fixed = _fix_python_literals(raw)
        result = json.loads(fixed)
        assert result["label"] == "False positive", (
            f"Bug 10: 'False' inside string corrupted — got {result['label']!r}"
        )

    def test_none_inside_string_value_untouched(self):
        from zsg.json_repair import _fix_python_literals
        raw = '{"note": "None of the above", "value": None}'
        fixed = _fix_python_literals(raw)
        result = json.loads(fixed)
        assert result["note"] == "None of the above", (
            f"Bug 10: 'None' inside string corrupted — got {result['note']!r}"
        )

    def test_civil_rights_heading_unchanged(self):
        """Smoke test on real-world narrative data from civil_rights_m7."""
        from zsg.json_repair import _fix_python_literals
        narrative = GOLD_STATE["sections"]["section_p42_p53"]["narrative"]
        raw = json.dumps(narrative)
        fixed = _fix_python_literals(raw)
        # Must still be valid JSON with no content corruption
        result = json.loads(fixed)
        assert result["heading"] == narrative["heading"]
        assert result["intro"] == narrative["intro"]


# ===========================================================================
# Bug 11 — from_zotero_html targets first </span> for instructor note
# ===========================================================================

class TestBug11ZoteroHtmlNoteExtraction:
    """
    from_zotero_html strips everything up to the first </span> to find the
    instructor note.  But the highlighted text span itself may contain a
    </span>, causing note extraction to grab text from inside the highlight
    rather than after the citation.
    """

    def test_simple_annotation_note_correct(self, tmp_path):
        from zsg.export import from_zotero_html
        # Minimal valid Zotero HTML with a clear instructor note after citation
        html = """<html><body>
<p><span class="highlight"><span style="background-color: rgba(255, 212, 0, 0.5);">Key concept text</span></span>
 (<span class="citation">(<span class="citation-item">Author, p. 5</span>)</span>)
 This is the instructor note.</p>
</body></html>"""
        p = tmp_path / "test.html"
        p.write_text(html, encoding="utf-8")
        anns = from_zotero_html(p)
        assert len(anns) == 1
        assert anns[0]["text"] == "Key concept text"
        assert "instructor note" in anns[0]["instructor_note"], (
            f"Bug 11: expected 'instructor note' in note, got {anns[0]['instructor_note']!r}"
        )

    def test_nested_span_in_highlight_does_not_steal_note(self, tmp_path):
        """
        Bug 11 trigger: highlighted text contains a </span> (e.g., Zotero
        sometimes wraps sub-text in extra spans). The regex ^.*?</span> will
        consume up to the FIRST </span> — which is inside the highlight — and
        treat the rest of the highlight text as the instructor note.
        """
        from zsg.export import from_zotero_html
        html = """<html><body>
<p><span class="highlight"><span style="background-color: rgba(255, 212, 0, 0.5);"><em>Nested</em> highlight text</span></span>
 (<span class="citation">(<span class="citation-item">Author, p. 10</span>)</span>)
 Real note here.</p>
</body></html>"""
        p = tmp_path / "nested.html"
        p.write_text(html, encoding="utf-8")
        anns = from_zotero_html(p)
        assert len(anns) == 1
        assert "Real note here" in anns[0]["instructor_note"], (
            f"Bug 11: instructor note incorrectly extracted — "
            f"got {anns[0]['instructor_note']!r}, expected 'Real note here'"
        )

    def test_civil_rights_html_parses_all_annotations(self):
        """
        Smoke test: the real Zotero export parses to the expected 10 annotations.

        Investigation revealed that CIVIL RIGHTS M7.html is a compiled Twine/
        SugarCube interactive story file — not a Zotero HTML annotation export.
        The civil_rights_m7 annotations.json was generated from demo_annotations()
        in export.py, not parsed from this file. There is no Zotero HTML export
        for this project, so this test is skipped.

        Bug 11 (the note extraction regex targeting the wrong </span>) is still
        covered by test_nested_span_in_highlight_does_not_steal_note above.
        """
        html_path = FIXTURES / "CIVIL RIGHTS M7.html"
        if not html_path.exists():
            pytest.skip("CIVIL RIGHTS M7.html not found in fixture dir")
        pytest.skip(
            "CIVIL RIGHTS M7.html is a Twine/SugarCube story file, not a Zotero "
            "HTML export. No Zotero HTML fixture exists for this project."
        )


# ===========================================================================
# Bug 12 — polling interval not cleared on error status
# ===========================================================================

class TestBug12PollingNotClearedOnError:
    """
    The pipeline status endpoint returns {"status": "error", ...} when a
    subprocess fails.  The JavaScript polling loop only calls clearInterval
    on "done", never on "error".  We test the Python side: confirm that
    pipeline_runner correctly reports "error" status and that the status dict
    is stable after the subprocess exits (i.e., not still "running").
    """

    def test_failed_subprocess_reports_error_status(self):
        import zsg.pipeline_runner as runner
        # Run a command that exits non-zero
        run_id = runner.start_stage([sys.executable, "-c", "import sys; sys.exit(1)"])
        # Give the thread time to complete
        for _ in range(20):
            time.sleep(0.1)
            status = runner.get_status(run_id)
            if status["status"] != "running":
                break

        assert status["status"] == "error", (
            f"Bug 12: expected status='error' for failing subprocess, got {status['status']!r}"
        )
        assert status["returncode"] == 1

    def test_successful_subprocess_reports_done_status(self):
        import zsg.pipeline_runner as runner
        run_id = runner.start_stage([sys.executable, "-c", "print('ok')"])
        for _ in range(20):
            time.sleep(0.1)
            status = runner.get_status(run_id)
            if status["status"] != "running":
                break

        assert status["status"] == "done", (
            f"Expected status='done', got {status['status']!r}"
        )

    def test_js_clear_interval_logic_simulation(self):
        """
        Simulate the JS setInterval polling loop to demonstrate Bug 12.

        The JS code only calls clearInterval on "done":
            if (status.status === "done") { clearInterval(pollInterval); }

        Bug: when a job fails, status becomes "error" but the interval is never
        cleared.  The browser keeps firing ticks and POSTing to /api/pipeline/status
        indefinitely.

        We verify the behaviour by tracking which ticks a correctly-stopped
        interval would NOT fire vs. what the buggy version actually processes.
        """
        # The server will return this sequence for a failed job.
        # After the first "error" the interval should stop (fixed), or keep running (buggy).
        server_statuses = ["running", "running", "error", "error", "error", "error"]
        first_terminal_idx = 2  # index of first "error"

        # --- Buggy code: only clears on "done" ---
        # The interval never stops; all 6 ticks fire.
        buggy_ticks_processed = 0
        for s in server_statuses:
            buggy_ticks_processed += 1
            if s == "done":     # Bug: never triggers for an errored job
                break

        # 6 ticks processed total; 3 of them were after the job already errored
        wasted_in_buggy = buggy_ticks_processed - (first_terminal_idx + 1)
        assert wasted_in_buggy == 3, (
            f"Bug 12: expected 3 wasted ticks in buggy code, got {wasted_in_buggy}"
        )

        # --- Fixed code: clears on both "done" and "error" ---
        fixed_ticks_processed = 0
        for s in server_statuses:
            fixed_ticks_processed += 1
            if s in ("done", "error"):  # Fix
                break

        # Stops at the first "error" (index 2) → 3 ticks, 0 wasted
        wasted_in_fixed = fixed_ticks_processed - (first_terminal_idx + 1)
        assert wasted_in_fixed == 0, (
            f"Bug 12 (fixed): expected 0 wasted ticks, got {wasted_in_fixed}"
        )
        assert fixed_ticks_processed == first_terminal_idx + 1


# ===========================================================================
# Bug 13 — pipeline_runner._runs grows without bound
# ===========================================================================

class TestBug13RunsMemoryLeak:
    """
    _runs dict is never pruned. Every start_stage() call adds an entry and
    nothing removes old completed runs.
    """

    def test_runs_dict_grows_with_each_start(self):
        import zsg.pipeline_runner as runner

        initial_count = len(runner._runs)
        n = 5
        ids = []
        for _ in range(n):
            run_id = runner.start_stage([sys.executable, "-c", "pass"])
            ids.append(run_id)

        # Wait for all to complete
        for _ in range(30):
            time.sleep(0.1)
            if all(runner.get_status(rid)["status"] != "running" for rid in ids):
                break

        current_count = len(runner._runs)
        assert current_count >= initial_count + n, (
            "Precondition: _runs grows with each call"
        )

        # Bug 13: there should be a prune mechanism, but there isn't.
        # Demonstrate the leak: after 5 runs, we still have them all.
        for rid in ids:
            assert rid in runner._runs, (
                f"Bug 13: run {rid} missing from _runs (unexpected early prune)"
            )

    def test_no_prune_after_status_fetch(self):
        """
        Fetching status should (after fix) prune completed runs.
        Currently it does NOT — this test documents the missing behaviour.
        """
        import zsg.pipeline_runner as runner

        run_id = runner.start_stage([sys.executable, "-c", "pass"])
        for _ in range(20):
            time.sleep(0.1)
            if runner.get_status(run_id)["status"] != "running":
                break

        # Fetch status twice — after fix, second fetch might trigger prune
        runner.get_status(run_id)
        runner.get_status(run_id)

        # Bug 13: without fix, run still in _runs after multiple fetches
        still_present = run_id in runner._runs
        # Document the current (buggy) behaviour: this will be True until fixed
        assert still_present, (
            "Bug 13 (documenting current state): completed run is NOT pruned from _runs. "
            "Fix: prune on second fetch or after a TTL."
        )


# ===========================================================================
# Gold-standard output.html fidelity tests
# ===========================================================================

class TestBuildGuideGoldStandard:
    """
    build_guide.build() on the civil_rights_m7 state.json must produce
    output that matches the gold-standard output.html at the content level
    (key headings, quiz question text, annotation IDs, section order).
    """

    def _build(self, state=None, title="Study Guide", theme="light"):
        import zsg.build_guide as bg
        s = copy.deepcopy(state or GOLD_STATE)
        return bg.build(s, title, theme)

    def test_both_section_headings_present(self):
        html = self._build()
        assert "Reconstruction and the Rise of Jim Crow" in html
        assert "Civil Rights Legal Victories and Direct Action" in html

    def test_key_points_present(self):
        html = self._build()
        assert "Forty Acres and a Mule" in html
        assert "Jim Crow Laws" in html
        assert "Montgomery Bus Boycott" in html

    def test_figures_present(self):
        html = self._build()
        assert "Booker T. Washington" in html
        assert "W.E.B. Du Bois" in html

    def test_quiz_questions_present(self):
        html = self._build()
        assert "Reconstruction Acts of 1867" in html
        assert "Plessy v. Ferguson" in html
        assert "Brown v. Board of Education" in html

    def test_section_order_respected(self):
        """section_p78_p82 appears before section_p42_p53 per state's section_order."""
        html = self._build()
        pos_78 = html.index("Civil Rights Legal Victories")
        pos_42 = html.index("Reconstruction and the Rise")
        assert pos_78 < pos_42, (
            "section_order not respected: section_p42_p53 appears before section_p78_p82"
        )

    def test_html_escaping_in_headings(self):
        """Headings containing & < > must be escaped in output."""
        state = copy.deepcopy(GOLD_STATE)
        state["sections"]["section_p42_p53"]["narrative"]["heading"] = "A & B <test>"
        html = self._build(state)
        assert "A &amp; B &lt;test&gt;" in html
        assert "A & B <test>" not in html

    def test_unapproved_sections_excluded(self):
        state = copy.deepcopy(GOLD_STATE)
        state["sections"]["section_p42_p53"]["narrative_approved"] = False
        html = self._build(state)
        assert "Reconstruction and the Rise of Jim Crow" not in html
        assert "Civil Rights Legal Victories and Direct Action" in html

    def test_empty_state_produces_warning_not_crash(self, capsys):
        import zsg.build_guide as bg
        html = bg.build({"sections": {}, "section_order": []}, "Empty", "light")
        captured = capsys.readouterr()
        assert "no approved sections" in captured.err.lower() or html  # warning or empty

    def test_output_is_valid_html_document(self):
        """Rebuilt output must be a complete HTML document (not truncated or empty)."""
        html = self._build()
        assert html.startswith("<!DOCTYPE html>"), "Output must start with DOCTYPE"
        assert "</html>" in html, "Output must contain closing </html> tag"
        assert "<script>" in html, "Output must embed JS (self-contained)"
        assert "<style>" in html, "Output must embed CSS (self-contained)"

    def test_theme_class_applied(self):
        html = self._build(theme="dark")
        assert 'class="theme-dark"' in html

    def test_self_contained_no_external_links(self):
        """Output must not reference external CDN URLs (self-contained requirement)."""
        import re
        html = self._build()
        external = re.findall(r'(?:src|href)=["\']https?://', html)
        assert not external, (
            f"Output.html contains external links (not self-contained): {external}"
        )


class TestQuizTemplateSelectionParity:
    """The quiz-prompt template-selection rule is implemented twice — server-side
    in generate.py:select_quiz_prompt and client-side in static/client-mode.js —
    because client mode builds the prompt in the browser. The two surfaces share
    only sync comments, so they can silently diverge. These tests pin the rule on
    both surfaces and guard the prompt cache."""

    def test_server_rule_zero_red_uses_narrative_template(self):
        """Server: zero red → quiz_from_narrative.txt; one+ red → quiz.txt."""
        import zsg.generate as gen
        assert gen.select_quiz_prompt([]) is gen.QUIZ_FROM_NARRATIVE_PROMPT
        assert gen.select_quiz_prompt([{"color": "red"}]) is gen.QUIZ_PROMPT

    def test_client_mirrors_the_same_rule(self):
        """Client (client-mode.js) must reference BOTH template URLs and select on
        a zero-red condition — so dropping a surface fails loudly here instead of
        leaving client-mode users with empty quizzes."""
        from zsg import PKG_DIR
        js = (PKG_DIR / "static" / "client-mode.js").read_text(encoding="utf-8")
        assert "quiz_from_narrative.txt" in js, (
            "client-mode.js no longer references the narrative quiz template — "
            "the zero-red path has diverged from generate.py:select_quiz_prompt"
        )
        assert "quiz.txt" in js
        # The selection branches on an empty red-annotation list (mirror of the
        # Python `if red_anns:` rule). Match the length-zero check loosely.
        assert re.search(r"redAnns\b[^\n]*\.length\s*===?\s*0", js), (
            "client-mode.js no longer selects the template on a zero-red "
            "condition — re-sync with generate.py:select_quiz_prompt"
        )

    def test_both_quiz_templates_share_byte_identical_system_block(self):
        """The cached <system> block must be byte-identical across both templates,
        or the Anthropic prompt cache silently stops hitting. Extract exactly as
        generate.py:call_llm does (leading comment skipped, then .strip())."""
        from zsg import PKG_DIR

        def system_block(name):
            text = (PKG_DIR / "prompts" / name).read_text(encoding="utf-8")
            m = re.match(r"\s*(?:<!--.*?-->\s*)?<system>(.*?)</system>",
                         text, re.DOTALL)
            assert m, f"{name}: no <system> block found"
            return m.group(1).strip().encode("utf-8")

        assert system_block("quiz.txt") == system_block("quiz_from_narrative.txt")


# ===========================================================================
# get_state — re-upload refresh + color_config exposure
# ===========================================================================

def _sections_doc(color_config, sections):
    return {"color_config": color_config, "sections": sections}


_DEFAULT_COLOR_CONFIG = {
    "red": {"label": "Quiz-worthy facts", "description": "..."},
    "yellow": {"label": "Key concepts", "description": "..."},
}


class TestGetStateReuploadAndColorConfig:
    """/api/state must (1) always refresh source_annotations from the current
    sections.json so a re-upload that adds red annotations is immediately
    visible to the gate, while preserving approval flags, and (2) expose the
    per-project color_config so the UI can resolve the Quiz-worthy role rather
    than assuming the literal "red"."""

    def _section(self, sid, colors):
        return {
            "section_id": sid,
            "page_range": {"start": 1, "end": 2},
            "source_annotations": [
                {"id": f"{sid}_a{i}", "color": c, "text": "x", "page": 1}
                for i, c in enumerate(colors)
            ],
        }

    def test_reupload_refreshes_annotations_but_keeps_approvals(self, tmp_path):
        sections_path = tmp_path / "sections.json"
        state_path = tmp_path / "state.json"

        # First upload: section s1 has no red; instructor approves its narrative.
        write_json(sections_path, _sections_doc(
            _DEFAULT_COLOR_CONFIG, [self._section("s1", ["yellow", "yellow"])]))
        write_json(state_path, {
            "section_order": ["s1"],
            "sections": {"s1": {"narrative_approved": True,
                                "narrative": {"heading": "H"},
                                "quiz_approved": True,
                                "quiz": {"questions": []}}},
        })

        client = make_flask_app(state_path, sections_path)

        # First GET caches the pre-reupload annotations into state.json. This is
        # what made the old setdefault bug bite: the cached value is later kept.
        first = client.get("/api/state").get_json()
        assert "red" not in [a["color"] for a in
                             first["sections"]["s1"]["source_annotations"]]

        # Re-upload: same section_id, now WITH a red annotation added.
        write_json(sections_path, _sections_doc(
            _DEFAULT_COLOR_CONFIG, [self._section("s1", ["yellow", "red"])]))

        data = client.get("/api/state").get_json()
        sec = data["sections"]["s1"]

        # The new red annotation is visible (refreshed, not stale).
        colors = [a["color"] for a in sec["source_annotations"]]
        assert "red" in colors, (
            "get_state did not refresh source_annotations on re-upload — "
            "the new red annotation is invisible to the gate"
        )
        # Approvals survived the re-upload (never clobbered).
        assert sec["narrative_approved"] is True
        assert sec["quiz_approved"] is True

    def test_state_exposes_color_config(self, tmp_path):
        sections_path = tmp_path / "sections.json"
        state_path = tmp_path / "state.json"
        write_json(sections_path, _sections_doc(
            _DEFAULT_COLOR_CONFIG, [self._section("s1", ["red"])]))
        write_json(state_path, {"section_order": ["s1"], "sections": {}})

        client = make_flask_app(state_path, sections_path)
        data = client.get("/api/state").get_json()

        assert "color_config" in data, "/api/state must expose color_config"
        # The Quiz-worthy role resolves by label (mirrors review.js quizWorthyRole).
        quiz_worthy = [c for c, m in data["color_config"].items()
                       if m.get("label", "").lower().startswith("quiz-worthy")]
        assert quiz_worthy == ["red"]


# ===========================================================================
# run_quiz — the --from-narrative gate (CLI backward-compat contract)
# ===========================================================================

class TestRunQuizFromNarrativeGate:
    """run_quiz skips a zero-red section by default (backward compatible), but
    generates from the narrative when from_narrative=True."""

    def _setup(self):
        import zsg.generate as gen
        section = {
            "section_id": "s1",
            "source_annotations": [
                {"id": "a1", "color": "yellow", "text": "x"},
            ],
        }
        state = {"sections": {"s1": {"narrative_approved": True,
                                     "narrative": {"heading": "H"}}}}
        cfg = {"provider": "ollama"}
        return gen, [section], state, cfg

    def test_zero_red_skips_without_flag(self):
        gen, sections, state, cfg = self._setup()
        with patch("zsg.generate.call_llm") as mock_llm:
            gen.run_quiz(sections, state, cfg, only=None, dry_run=False)
        mock_llm.assert_not_called()
        assert "quiz" not in state["sections"]["s1"]

    def test_zero_red_generates_with_flag(self):
        gen, sections, state, cfg = self._setup()
        fake = json.dumps({"section_id": "s1", "questions": [
            {"question_text": "Q?", "correct_answer": "A",
             "distractors": ["B", "C"], "explanation_if_correct": "",
             "explanation_if_incorrect": "", "source_annotation_ids": []}]})
        with patch("zsg.generate.call_llm", return_value=fake) as mock_llm:
            gen.run_quiz(sections, state, cfg, only=None, dry_run=False,
                         from_narrative=True)
        mock_llm.assert_called_once()
        assert state["sections"]["s1"]["quiz"]["questions"]
        # Generated quiz lands un-approved.
        assert state["sections"]["s1"]["quiz_approved"] is False
