"""
test_v2_api.py — Verifies the bug fixes, library extraction, and stateless
v2 API landed in the recent commits:

  • export.py BeautifulSoup parser + EmptyExportError + parse_export_str dispatch
  • generate.py TruncationError raised on stop_reason / finish_reason == length
  • pipeline_runner.py LRU cap + cancel_stage + last_error
  • verify.py atomic write + corrupt-state recovery + 32 MB cap
  • verify.py /api/v2/* endpoints (parse, sections, llm, build, build_download)
  • verify.py /static/prompts/ pass-through + path-traversal rejection

All tests run without real LLM calls.
"""

import copy
import json
import os
import sys
import time
from io import BytesIO
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

FIXTURES = ROOT / "tests" / "fixtures" / "civil_rights_m7"
ANNOTATIONS_HTML = ROOT / "sample-data" / "Annotations.html"  # real Zotero export

with open(FIXTURES / "annotations.json") as f:
    GOLD_ANNOTATIONS = json.load(f)
with open(FIXTURES / "state.json") as f:
    GOLD_STATE = json.load(f)


# ===========================================================================
# Helpers
# ===========================================================================

def make_client(state_path=None, sections_path=None):
    """Return a Flask test client with verify configured to tmp paths."""
    import zsg.verify as verify
    verify.STATE_PATH = state_path or (ROOT / "state.json")
    verify.SECTIONS_PATH = sections_path or (ROOT / "sections.json")
    verify.APP_CONFIG_PATH = ROOT / "app_config.json"
    verify.app.config["TESTING"] = True
    return verify.app.test_client()


# ===========================================================================
# export.py — pure-function library and parser hardening
# ===========================================================================

class TestExportLibrary:
    """parse_export_str dispatches correctly and HTML/MD/CSV/JSON parsers
    accept in-memory strings."""

    def test_parse_export_str_html_dispatch(self):
        import zsg.export as export
        html = ANNOTATIONS_HTML.read_text(encoding="utf-8")
        anns = export.parse_export_str("html", html)
        assert len(anns) > 0
        assert all("id" in a and "text" in a and "color" in a for a in anns)

    def test_parse_export_str_with_leading_dot(self):
        import zsg.export as export
        html = ANNOTATIONS_HTML.read_text(encoding="utf-8")
        anns = export.parse_export_str(".html", html)
        assert anns

    def test_parse_export_str_json_native_format(self):
        import zsg.export as export
        # GOLD_ANNOTATIONS is the {color_config, annotations} envelope
        native = GOLD_ANNOTATIONS["annotations"]
        anns = export.parse_export_str("json", json.dumps(native))
        assert len(anns) == len(native)

    def test_parse_export_str_csv(self):
        import zsg.export as export
        csv_text = (
            "Annotation Text,Color,Page,Comment\n"
            '"Hello world",yellow,5,"a note"\n'
            '"Another",red,6,""\n'
        )
        anns = export.parse_export_str("csv", csv_text)
        assert len(anns) == 2
        assert anns[0]["color"] == "yellow"
        assert anns[1]["color"] == "red"

    def test_parse_export_str_unknown_format(self):
        import zsg.export as export
        with pytest.raises(ValueError, match="Unsupported export format"):
            export.parse_export_str("pdf", "x")

    def test_empty_string_returns_empty_list(self):
        import zsg.export as export
        assert export.from_zotero_html_str("") == []
        assert export.from_zotero_html_str("   \n  ") == []

    def test_empty_export_error_on_large_non_zotero_html(self):
        """A >1KB HTML file that yields zero annotations should raise."""
        import zsg.export as export
        junk = "<html><body>" + ("x" * 2000) + "</body></html>"
        with pytest.raises(export.EmptyExportError):
            export.from_zotero_html_str(junk)

    def test_short_non_zotero_html_silent(self):
        """A small junk file should not raise — too small to be a misclassified Zotero export."""
        import zsg.export as export
        assert export.from_zotero_html_str("<html><body>hi</body></html>") == []

    def test_null_color_in_native_json_defaults_to_yellow(self):
        """Bug surfaced by benchmark: `"color": null` in native JSON crashed normalize_color."""
        import zsg.export as export
        anns = export.parse_export_str("json", json.dumps([
            {"id": "x", "text": "t", "color": None, "page": 1,
             "instructor_note": "", "source_document": ""},
        ]))
        assert anns == [{
            "id": "x", "text": "t", "color": "yellow", "page": 1,
            "instructor_note": "", "source_document": "",
        }]

    def test_unknown_color_name_defaults_to_yellow(self):
        import zsg.export as export
        assert export.normalize_color("chartreuse") == "yellow"
        assert export.normalize_color("") == "yellow"
        assert export.normalize_color(None) == "yellow"


class TestZoteroHtmlParserBs4(object):
    """The BeautifulSoup parser handles the real Zotero export."""

    def test_parses_real_annotations_html(self):
        import zsg.export as export
        html = ANNOTATIONS_HTML.read_text(encoding="utf-8")
        anns = export.from_zotero_html_str(html)
        assert len(anns) > 50, f"expected many annotations, got {len(anns)}"
        first = anns[0]
        assert first["id"] == "ann_001"
        assert first["text"]
        assert first["color"] in {"yellow", "red", "green", "blue", "purple", "orange", "gray", "pink"}

    def test_regex_fallback_equivalent_to_bs4(self):
        """When bs4 is disabled, the regex fallback still produces results."""
        import zsg.export as export
        html = ANNOTATIONS_HTML.read_text(encoding="utf-8")
        bs4_anns = export.from_zotero_html_str(html)

        # Disable bs4 path
        with patch.object(export, "_HAS_BS4", False):
            regex_anns = export.from_zotero_html_str(html)

        assert len(regex_anns) == len(bs4_anns)
        # Both should produce the same first-annotation text
        assert regex_anns[0]["text"] == bs4_anns[0]["text"]

    def test_html_parser_extracts_instructor_notes(self):
        """A <p> with annotation + citation + trailing note should populate instructor_note."""
        import zsg.export as export
        html = """<html><body>
        <p><span class="highlight"><span style="background-color: rgba(255, 212, 0, 0.5);">Important quote</span></span>
        <span class="citation">(<span class="citation-item">Yoo, p. 5</span>)</span>
         instructor note here</p>
        </body></html>"""
        anns = export.from_zotero_html_str(html)
        assert len(anns) == 1
        assert "instructor note here" in anns[0]["instructor_note"]
        assert anns[0]["source_document"] == "Yoo"
        assert anns[0]["page"] == 5


# ===========================================================================
# generate.py — TruncationError detection across providers
# ===========================================================================

class TestTruncationDetection:
    """call_llm raises TruncationError when the provider signals max_tokens."""

    def test_anthropic_truncation_raises(self):
        import zsg.generate as generate

        fake_msg = MagicMock()
        fake_msg.content = [MagicMock(type="text", text='"foo": "bar"}')]
        fake_msg.stop_reason = "max_tokens"

        fake_client = MagicMock()
        fake_client.messages.create.return_value = fake_msg

        with patch("anthropic.Anthropic", return_value=fake_client):
            cfg = {
                "provider": "anthropic", "model": "claude-haiku-4-5-20251001",
                "api_key": "sk-test", "max_tokens": 100, "temperature": 0.1,
            }
            with pytest.raises(generate.TruncationError, match="truncated"):
                generate.call_llm("hi", cfg)

    def test_anthropic_normal_stop_returns_text(self):
        import zsg.generate as generate

        fake_msg = MagicMock()
        fake_msg.content = [MagicMock(type="text", text='"foo": "bar"}')]
        fake_msg.stop_reason = "end_turn"

        fake_client = MagicMock()
        fake_client.messages.create.return_value = fake_msg

        with patch("anthropic.Anthropic", return_value=fake_client):
            cfg = {
                "provider": "anthropic", "model": "claude-haiku-4-5-20251001",
                "api_key": "sk-test", "max_tokens": 100, "temperature": 0.1,
            }
            result = generate.call_llm("hi", cfg)
            assert result.startswith("{")  # because we prepend "{"

    def test_openai_compat_truncation_raises(self):
        import zsg.generate as generate

        fake_response = MagicMock()
        fake_response.json.return_value = {
            "choices": [{"finish_reason": "length", "message": {"content": "{partial"}}],
        }
        fake_response.raise_for_status = MagicMock()

        with patch("requests.post", return_value=fake_response):
            cfg = {
                "provider": "openai", "model": "gpt-4o-mini",
                "base_url": "http://localhost:8080", "max_tokens": 50,
            }
            with pytest.raises(generate.TruncationError):
                generate.call_llm("hi", cfg)

    def test_openai_compat_normal_returns_content(self):
        import zsg.generate as generate

        fake_response = MagicMock()
        fake_response.json.return_value = {
            "choices": [{"finish_reason": "stop", "message": {"content": "{}"}}],
        }
        fake_response.raise_for_status = MagicMock()

        with patch("requests.post", return_value=fake_response):
            cfg = {"provider": "openai", "model": "gpt-4o", "base_url": "http://localhost:8080"}
            assert generate.call_llm("hi", cfg) == "{}"

    def test_purdue_genai_normal_returns_content(self):
        """The default provider: Bearer-auth POST to the GenAI Studio endpoint,
        content returned verbatim on a normal finish_reason."""
        import zsg.generate as generate

        fake_response = MagicMock()
        fake_response.json.return_value = {
            "choices": [{"finish_reason": "stop", "message": {"content": "{}"}}],
        }
        fake_response.raise_for_status = MagicMock()

        with patch("requests.post", return_value=fake_response) as mock_post:
            cfg = {
                "provider": "purdue_genai", "model": "llama3.1:latest",
                "base_url": "https://genai.rcac.purdue.edu/api/chat/completions",
                "api_key": "purdue-test-key",
            }
            assert generate.call_llm("hi", cfg) == "{}"

        # Verify it hit the GenAI Studio endpoint with a Bearer token.
        args, kwargs = mock_post.call_args
        assert args[0] == "https://genai.rcac.purdue.edu/api/chat/completions"
        assert kwargs["headers"]["Authorization"] == "Bearer purdue-test-key"

    def test_purdue_genai_truncation_raises(self):
        import zsg.generate as generate

        fake_response = MagicMock()
        fake_response.json.return_value = {
            "choices": [{"finish_reason": "length", "message": {"content": "{partial"}}],
        }
        fake_response.raise_for_status = MagicMock()

        with patch("requests.post", return_value=fake_response):
            cfg = {
                "provider": "purdue_genai", "model": "llama3.1:latest",
                "base_url": "https://genai.rcac.purdue.edu/api/chat/completions",
                "api_key": "purdue-test-key", "max_tokens": 50,
            }
            with pytest.raises(generate.TruncationError):
                generate.call_llm("hi", cfg)

    def test_purdue_genai_requires_api_key(self):
        """No key in cfg and no env var → a clear ValueError, not a silent
        unauthenticated request."""
        import os
        import zsg.generate as generate

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PURDUE_GENAI_API_KEY", None)
            cfg = {
                "provider": "purdue_genai", "model": "llama3.1:latest",
                "base_url": "https://genai.rcac.purdue.edu/api/chat/completions",
            }
            with pytest.raises(ValueError, match="API key"):
                generate.call_llm("hi", cfg)

    def test_truncation_error_is_runtimeerror_subclass(self):
        import zsg.generate as generate
        assert issubclass(generate.TruncationError, RuntimeError)


# ===========================================================================
# pipeline_runner.py — LRU bound, cancel, last_error
# ===========================================================================

class TestRunnerLruAndCancel:

    def _wait_done(self, runner, run_id, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            status = runner.get_status(run_id)["status"]
            if status not in ("running",):
                return status
            time.sleep(0.05)
        return runner.get_status(run_id)["status"]

    def test_runs_dict_bounded_by_max_runs(self):
        import zsg.pipeline_runner as runner
        # Clear by exceeding the cap
        with runner._runs_lock:
            runner._runs.clear()
        n = runner.MAX_RUNS + 10
        ids = []
        for _ in range(n):
            rid = runner.start_stage([sys.executable, "-c", "pass"])
            ids.append(rid)
        # Wait for last run to settle
        self._wait_done(runner, ids[-1])
        with runner._runs_lock:
            assert len(runner._runs) <= runner.MAX_RUNS, (
                f"LRU cap not enforced: {len(runner._runs)} > {runner.MAX_RUNS}"
            )

    def test_last_error_populated_on_nonzero_exit(self):
        import zsg.pipeline_runner as runner
        rid = runner.start_stage([
            sys.executable, "-c",
            "import sys; print('boom-line-1'); print('boom-line-2'); sys.exit(3)",
        ])
        self._wait_done(runner, rid)
        status = runner.get_status(rid)
        assert status["status"] == "error"
        assert status["returncode"] == 3
        assert status["last_error"] is not None
        assert "boom-line-2" in status["last_error"]

    def test_cancel_stage_terminates_running_subprocess(self):
        import zsg.pipeline_runner as runner
        rid = runner.start_stage([
            sys.executable, "-c", "import time; time.sleep(30)",
        ])
        # Wait until the runner has captured the Popen handle
        for _ in range(50):
            with runner._runs_lock:
                proc = runner._runs.get(rid, {}).get("_proc")
            if proc is not None:
                break
            time.sleep(0.05)
        result = runner.cancel_stage(rid)
        assert result.get("status") == "cancelled"
        # The subprocess should die quickly
        self._wait_done(runner, rid, timeout=3.0)

    def test_cancel_unknown_id_returns_not_found(self):
        import zsg.pipeline_runner as runner
        result = runner.cancel_stage("no_such_run")
        assert result == {"status": "not_found"}

    def test_status_does_not_leak_popen_handle(self):
        import zsg.pipeline_runner as runner
        rid = runner.start_stage([sys.executable, "-c", "pass"])
        self._wait_done(runner, rid)
        status = runner.get_status(rid)
        assert "_proc" not in status, "Popen handle leaked through get_status"
        assert "status" in status and "lines" in status


# ===========================================================================
# verify.py — atomic write + corrupt-state recovery
# ===========================================================================

class TestAtomicWriteAndRecovery:

    def test_atomic_write_replaces_existing_file(self, tmp_path):
        import zsg.verify as verify
        target = tmp_path / "state.json"
        target.write_text('{"old": true}', encoding="utf-8")
        verify._atomic_write_text(target, '{"new": true}')
        assert target.read_text() == '{"new": true}'
        # Tmp sibling should have been os.replace'd away
        tmp = target.with_suffix(target.suffix + ".tmp")
        assert not tmp.exists(), "atomic write left a .tmp file behind"

    def test_load_state_recovers_from_corrupt_json(self, tmp_path):
        import zsg.verify as verify
        verify.STATE_PATH = tmp_path / "state.json"
        verify.STATE_PATH.write_text("{ corrupt not json", encoding="utf-8")
        state = verify.load_state()
        assert state.get("_recovered_from_corrupt") is True
        assert state.get("sections") == {}
        assert state.get("section_order") == []

    def test_save_state_creates_parent_dirs(self, tmp_path):
        import zsg.verify as verify
        nested = tmp_path / "deep" / "nest" / "state.json"
        verify.STATE_PATH = nested
        verify.save_state({"sections": {"a": {}}, "section_order": ["a"]})
        assert nested.exists()
        assert json.loads(nested.read_text())["section_order"] == ["a"]

    def test_locked_state_writes_atomically(self, tmp_path):
        import zsg.verify as verify
        verify.STATE_PATH = tmp_path / "state.json"
        verify.STATE_PATH.write_text(json.dumps({"sections": {}, "section_order": []}))
        with verify.locked_state() as state:
            state["sections"]["x"] = {"narrative_approved": True}
        saved = json.loads(verify.STATE_PATH.read_text())
        assert saved["sections"]["x"]["narrative_approved"] is True
        # tmp sibling should not survive
        assert not (verify.STATE_PATH.with_suffix(verify.STATE_PATH.suffix + ".tmp")).exists()

    def test_max_content_length_configured(self):
        import zsg.verify as verify
        assert verify.app.config.get("MAX_CONTENT_LENGTH") == 32 * 1024 * 1024


# ===========================================================================
# verify.py /static/prompts/ pass-through
# ===========================================================================

class TestStaticPromptsRoute:

    def test_serves_narrative_prompt(self):
        client = make_client()
        resp = client.get("/static/prompts/narrative.txt")
        assert resp.status_code == 200
        assert "text/plain" in resp.content_type
        assert len(resp.data) > 0

    def test_serves_quiz_prompt(self):
        client = make_client()
        resp = client.get("/static/prompts/quiz.txt")
        assert resp.status_code == 200

    def test_rejects_traversal_attempt(self):
        client = make_client()
        # NOTE: Flask normalizes path segments before routing, so this is also
        # protected by send_from_directory's safe_join. The check in the route
        # is belt-and-suspenders. Either a 400 (our explicit guard) or 404
        # (Flask refusing the URL) is acceptable.
        resp = client.get("/static/prompts/..%2F..%2Fapp.py")
        assert resp.status_code in (400, 404)


# ===========================================================================
# verify.py — /api/v2/parse
# ===========================================================================

class TestV2Parse:

    def test_html_parse_happy_path(self):
        client = make_client()
        html = ANNOTATIONS_HTML.read_text(encoding="utf-8")
        resp = client.post("/api/v2/parse", json={"format": "html", "content": html})
        assert resp.status_code == 200
        data = resp.get_json()
        assert "annotations" in data and "color_config" in data
        assert len(data["annotations"]) > 0

    def test_missing_content_400(self):
        client = make_client()
        resp = client.post("/api/v2/parse", json={"format": "html"})
        assert resp.status_code == 400
        assert "content" in resp.get_json()["error"].lower()

    def test_unknown_format_400(self):
        client = make_client()
        resp = client.post("/api/v2/parse", json={"format": "pdf", "content": "x"})
        assert resp.status_code == 400

    def test_non_zotero_html_returns_empty_export_code(self):
        client = make_client()
        junk = "<html><body>" + ("x" * 2000) + "</body></html>"
        resp = client.post("/api/v2/parse", json={"format": "html", "content": junk})
        assert resp.status_code == 422
        body = resp.get_json()
        assert body.get("code") == "empty_export"

    def test_json_native_format_roundtrip(self):
        client = make_client()
        native = GOLD_ANNOTATIONS["annotations"]
        resp = client.post("/api/v2/parse", json={
            "format": "json",
            "content": json.dumps(native),
        })
        assert resp.status_code == 200
        out = resp.get_json()["annotations"]
        assert len(out) == len(native)


class TestV2ParseUpload:

    def test_upload_html(self):
        client = make_client()
        html_bytes = ANNOTATIONS_HTML.read_bytes()
        resp = client.post(
            "/api/v2/parse_upload",
            data={"file": (BytesIO(html_bytes), "Annotations.html")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 200
        assert len(resp.get_json()["annotations"]) > 0

    def test_upload_rejects_unsupported_extension(self):
        client = make_client()
        resp = client.post(
            "/api/v2/parse_upload",
            data={"file": (BytesIO(b"%PDF-1.4 fake"), "Annotations.pdf")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 400
        assert "unsupported" in resp.get_json()["error"].lower()

    def test_upload_no_file_400(self):
        client = make_client()
        resp = client.post("/api/v2/parse_upload", data={}, content_type="multipart/form-data")
        assert resp.status_code == 400


# ===========================================================================
# verify.py — /api/v2/sections
# ===========================================================================

class TestV2Sections:

    def test_groups_annotations(self):
        client = make_client()
        anns = GOLD_ANNOTATIONS["annotations"]
        resp = client.post("/api/v2/sections", json={
            "annotations": anns,
            "strategy": "auto",
            "page_window": 6,
        })
        assert resp.status_code == 200
        sections = resp.get_json()["sections"]
        assert len(sections) >= 1
        assert all("section_id" in s and "source_annotations" in s for s in sections)

    def test_missing_annotations_400(self):
        client = make_client()
        resp = client.post("/api/v2/sections", json={"strategy": "auto"})
        assert resp.status_code == 400


# ===========================================================================
# verify.py — /api/v2/llm
# ===========================================================================

class TestV2Llm:

    def test_returns_text_and_parsed(self):
        client = make_client()
        with patch("zsg.generate.call_llm", return_value='{"hello": "world"}'):
            resp = client.post("/api/v2/llm", json={
                "provider": "anthropic", "model": "claude-haiku-4-5-20251001",
                "api_key": "sk-test", "prompt": "<system>x</system>hi",
            })
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["text"] == '{"hello": "world"}'
        assert body["parsed"] == {"hello": "world"}

    def test_missing_prompt_400(self):
        client = make_client()
        resp = client.post("/api/v2/llm", json={"provider": "anthropic"})
        assert resp.status_code == 400

    def test_truncation_returns_422_with_code(self):
        client = make_client()
        import zsg.generate as generate
        with patch("zsg.generate.call_llm", side_effect=generate.TruncationError("hit max")):
            resp = client.post("/api/v2/llm", json={
                "provider": "anthropic", "api_key": "x", "prompt": "hi",
            })
        assert resp.status_code == 422
        body = resp.get_json()
        assert body.get("code") == "truncated"

    def test_cache_control_no_store(self):
        client = make_client()
        with patch("zsg.generate.call_llm", return_value="{}"):
            resp = client.post("/api/v2/llm", json={"prompt": "hi", "api_key": "x"})
        assert resp.headers.get("Cache-Control") == "no-store"


# ===========================================================================
# verify.py — /api/v2/build, /api/v2/build_download
# ===========================================================================

class TestV2Build:

    def test_build_returns_html(self):
        client = make_client()
        resp = client.post("/api/v2/build", json={
            "state": copy.deepcopy(GOLD_STATE),
            "title": "Test Guide",
            "theme": "light",
        })
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["html"].startswith("<!DOCTYPE html>")
        assert body["sections"] > 0
        assert body["size_kb"] > 0

    def test_build_cache_control_no_store(self):
        client = make_client()
        resp = client.post("/api/v2/build", json={"state": GOLD_STATE})
        assert resp.headers.get("Cache-Control") == "no-store"

    def test_build_download_returns_html_attachment(self):
        client = make_client()
        resp = client.post("/api/v2/build_download", json={
            "state": copy.deepcopy(GOLD_STATE),
            "title": "My Guide",
        })
        assert resp.status_code == 200
        assert resp.content_type.startswith("text/html")
        cd = resp.headers.get("Content-Disposition", "")
        assert "attachment" in cd
        assert "My_Guide.html" in cd
        assert b"<!DOCTYPE html>" in resp.data

    def test_build_download_sanitizes_filename(self):
        client = make_client()
        resp = client.post("/api/v2/build_download", json={
            "state": copy.deepcopy(GOLD_STATE),
            "title": "../etc/passwd & danger",
        })
        assert resp.status_code == 200
        cd = resp.headers.get("Content-Disposition", "")
        # No slashes, no spaces, no special chars in the filename
        assert "../" not in cd
        assert "&" not in cd

    def test_build_empty_state_succeeds(self):
        client = make_client()
        resp = client.post("/api/v2/build", json={
            "state": {"sections": {}, "section_order": []},
            "title": "Empty",
        })
        assert resp.status_code == 200
        assert resp.get_json()["sections"] == 0


# ===========================================================================
# verify.py — /api/pipeline/cancel/<id>
# ===========================================================================

class TestPipelineCancelRoute:

    def test_cancel_unknown_returns_not_found(self):
        client = make_client()
        resp = client.post("/api/pipeline/cancel/nonexistent")
        assert resp.status_code == 200
        assert resp.get_json() == {"status": "not_found"}


# ===========================================================================
# verify.py — legacy /api/section/<id>/generate_* error routing
# ===========================================================================

class TestLegacyGenerateErrorRouting:
    """The three legacy generate routes (narrative, quiz, regenerate_question)
    must (1) surface TruncationError as 422+code=truncated, (2) persist the
    failure on state.sections[sid].{narrative_error,quiz_error}, (3) expose
    that field via /api/state, and (4) clear it on the next successful run."""

    def _make_client_with_fixtures(self, tmp_path):
        """Spin up a Flask client backed by tmp state + the gold sections."""
        state_path    = tmp_path / "state.json"
        sections_path = tmp_path / "sections.json"
        # Use the real fixture sections so sections_index() can find the id
        with open(FIXTURES / "sections.json") as f:
            sections_data = json.load(f)
        sections_path.write_text(json.dumps(sections_data), encoding="utf-8")
        # Seed empty state
        state_path.write_text(
            json.dumps({"sections": {}, "section_order": []}),
            encoding="utf-8",
        )
        return make_client(state_path, sections_path), state_path, sections_data

    def _first_section_id(self, sections_data):
        sections = sections_data.get("sections", sections_data) \
            if isinstance(sections_data, dict) else sections_data
        return sections[0]["section_id"]

    def test_narrative_truncation_returns_422_with_code(self, tmp_path):
        client, state_path, sections_data = self._make_client_with_fixtures(tmp_path)
        sid = self._first_section_id(sections_data)

        import zsg.generate as generate
        with patch("zsg.generate.load_llm_config", return_value={
            "provider": "anthropic", "model": "x", "api_key": "y",
        }), patch(
            "zsg.generate.call_llm",
            side_effect=generate.TruncationError("output truncated at 4096 tokens"),
        ):
            resp = client.post(f"/api/section/{sid}/generate_narrative")

        assert resp.status_code == 422, resp.get_json()
        body = resp.get_json()
        assert body.get("code") == "truncated"
        assert "truncated" in body.get("error", "").lower()

        # The error was persisted on the section
        saved = json.loads(state_path.read_text())
        assert "truncated" in saved["sections"][sid]["narrative_error"].lower()

    def test_narrative_error_visible_via_api_state(self, tmp_path):
        """A narrative_error written into state.json must surface in /api/state."""
        client, state_path, sections_data = self._make_client_with_fixtures(tmp_path)
        sid = self._first_section_id(sections_data)

        seeded = {
            "sections": {
                sid: {
                    "narrative_error": "boom: model returned garbage",
                    "quiz_error": "quiz boom",
                },
            },
            "section_order": [sid],
        }
        state_path.write_text(json.dumps(seeded), encoding="utf-8")

        resp = client.get("/api/state")
        assert resp.status_code == 200
        body = resp.get_json()
        sec = body["sections"][sid]
        assert sec.get("narrative_error") == "boom: model returned garbage"
        assert sec.get("quiz_error") == "quiz boom"

    def test_narrative_error_cleared_on_successful_retry(self, tmp_path):
        """After a failed generate stashes narrative_error, the next successful
        generate must remove it from state."""
        client, state_path, sections_data = self._make_client_with_fixtures(tmp_path)
        sid = self._first_section_id(sections_data)

        import zsg.generate as generate

        # 1) First call fails with truncation → narrative_error gets written
        with patch("zsg.generate.load_llm_config", return_value={
            "provider": "anthropic", "model": "x", "api_key": "y",
        }), patch(
            "zsg.generate.call_llm",
            side_effect=generate.TruncationError("hit max_tokens"),
        ):
            resp = client.post(f"/api/section/{sid}/generate_narrative")
        assert resp.status_code == 422
        saved = json.loads(state_path.read_text())
        assert "narrative_error" in saved["sections"][sid]

        # 2) Retry succeeds — narrative_error must be popped
        good_json = '{"heading": "H", "intro": "I", "key_points": [], "figures": [], "source_annotation_ids_used": []}'
        with patch("zsg.generate.load_llm_config", return_value={
            "provider": "anthropic", "model": "x", "api_key": "y",
        }), patch("zsg.generate.call_llm", return_value=good_json):
            resp = client.post(f"/api/section/{sid}/generate_narrative")
        assert resp.status_code == 200
        saved = json.loads(state_path.read_text())
        assert "narrative_error" not in saved["sections"][sid]
        assert saved["sections"][sid]["narrative"]["heading"] == "H"

    def test_quiz_truncation_returns_422_with_code(self, tmp_path):
        client, state_path, sections_data = self._make_client_with_fixtures(tmp_path)
        sid = self._first_section_id(sections_data)

        # Quiz route requires narrative_approved first
        state_path.write_text(json.dumps({
            "sections": {sid: {"narrative_approved": True, "narrative": {"heading": "H"}}},
            "section_order": [sid],
        }), encoding="utf-8")

        import zsg.generate as generate
        with patch("zsg.generate.load_llm_config", return_value={
            "provider": "anthropic", "model": "x", "api_key": "y",
        }), patch(
            "zsg.generate.call_llm",
            side_effect=generate.TruncationError("quiz truncated"),
        ):
            resp = client.post(f"/api/section/{sid}/generate_quiz")

        assert resp.status_code == 422, resp.get_json()
        body = resp.get_json()
        assert body.get("code") == "truncated"

        saved = json.loads(state_path.read_text())
        assert "truncated" in saved["sections"][sid]["quiz_error"].lower()


class TestDeleteSectionRoute:
    """DELETE /api/section/<id>/delete removes a section from BOTH sections.json
    (the source of truth /api/state rebuilds order from) and state.json, so the
    deleted section does not resurrect on the next /api/state load."""

    def _make_client(self, tmp_path):
        state_path    = tmp_path / "state.json"
        sections_path = tmp_path / "sections.json"
        sections_path.write_text(json.dumps({
            "color_config": {"red": {"label": "Quiz-worthy", "description": "x"}},
            "sections": [
                {"section_id": "junk", "page_range": {"start": None, "end": None},
                 "annotation_count": 1, "source_annotations": [
                     {"id": "a1", "text": "junk", "color": "yellow", "page": "vii",
                      "instructor_note": "", "source_document": "d"}]},
                {"section_id": "real", "page_range": {"start": 1, "end": 5},
                 "annotation_count": 1, "source_annotations": [
                     {"id": "a2", "text": "real", "color": "red", "page": 1,
                      "instructor_note": "", "source_document": "d"}]},
            ],
        }), encoding="utf-8")
        state_path.write_text(json.dumps({
            "sections": {"junk": {"narrative_approved": False},
                         "real": {"narrative_approved": True}},
            "section_order": ["junk", "real"],
        }), encoding="utf-8")
        return make_client(state_path, sections_path), state_path, sections_path

    def test_delete_missing_section_returns_404(self, tmp_path):
        client, _, _ = self._make_client(tmp_path)
        assert client.delete("/api/section/nope/delete").status_code == 404

    def test_delete_removes_from_sections_and_state(self, tmp_path):
        client, state_path, sections_path = self._make_client(tmp_path)

        resp = client.delete("/api/section/junk/delete")
        assert resp.status_code == 200, resp.get_json()

        secs = json.loads(sections_path.read_text())
        assert [s["section_id"] for s in secs["sections"]] == ["real"]
        # The surrounding wrapper (color_config) is preserved.
        assert "color_config" in secs

        st = json.loads(state_path.read_text())
        assert "junk" not in st["sections"]
        assert st["section_order"] == ["real"]

    def test_deleted_section_does_not_resurrect_via_api_state(self, tmp_path):
        client, _, _ = self._make_client(tmp_path)
        assert client.delete("/api/section/junk/delete").status_code == 200

        order = client.get("/api/state").get_json()["section_order"]
        assert "junk" not in order
        assert order == ["real"]
