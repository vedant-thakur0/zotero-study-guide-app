"""
test_metrics.py — Unit tests for the metrics module and the call_llm seam.

Covers:
- Successful purdue_genai call writes exactly one record with 6 fields.
- The record contains neither the raw key, the prompt, nor the response body.
- A failing call (HTTP exception) still writes one record with ok=False and re-raises.
- Two different keys produce different fingerprints; same key produces same fingerprint.
- A keyless provider (ollama with no api_key) yields user="none".
- dry_run=True writes no record.
- Web path (/api/v2/llm) via Flask test client also writes a record (stub
  requests.post, NOT call_llm, so the seam is actually exercised).
"""

import hashlib
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_purdue_response(content="{}", finish_reason="stop"):
    """Build a MagicMock that mimics a successful purdue_genai HTTP response."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {
        "choices": [{"finish_reason": finish_reason, "message": {"content": content}}]
    }
    return resp


def _read_records(log_path: Path) -> list[dict]:
    """Parse all JSONL records from the metrics log file."""
    if not log_path.exists():
        return []
    lines = log_path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _purdue_cfg(api_key="test-key-abc"):
    return {
        "provider": "purdue_genai",
        "model": "llama3.1:latest",
        "base_url": "https://genai.rcac.purdue.edu/api/chat/completions",
        "api_key": api_key,
    }


# ---------------------------------------------------------------------------
# make_client helper (mirrors test_v2_api.py style)
# ---------------------------------------------------------------------------

def make_client(state_path=None, sections_path=None):
    """Return a Flask test client with verify configured to tmp paths."""
    import zsg.verify as verify
    verify.STATE_PATH = state_path or (ROOT / "state.json")
    verify.SECTIONS_PATH = sections_path or (ROOT / "sections.json")
    verify.APP_CONFIG_PATH = ROOT / "app_config.json"
    verify.app.config["TESTING"] = True
    return verify.app.test_client()


# ===========================================================================
# zsg.metrics — unit tests for the module itself
# ===========================================================================

class TestKeyFingerprint:

    def test_fingerprint_is_16_hex_chars(self):
        import zsg.metrics as metrics
        fp = metrics.key_fingerprint("some-api-key")
        assert len(fp) == 16
        assert all(c in "0123456789abcdef" for c in fp)

    def test_empty_key_returns_none(self):
        import zsg.metrics as metrics
        assert metrics.key_fingerprint("") == "none"

    def test_none_key_returns_none(self):
        import zsg.metrics as metrics
        assert metrics.key_fingerprint(None) == "none"

    def test_same_key_produces_same_fingerprint(self):
        import zsg.metrics as metrics
        key = "reproducible-key-xyz"
        assert metrics.key_fingerprint(key) == metrics.key_fingerprint(key)

    def test_two_different_keys_produce_different_fingerprints(self):
        import zsg.metrics as metrics
        fp1 = metrics.key_fingerprint("key-alpha")
        fp2 = metrics.key_fingerprint("key-beta")
        assert fp1 != fp2

    def test_fingerprint_matches_expected_sha256(self):
        import zsg.metrics as metrics
        key = "deterministic-key"
        expected = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
        # Without salt (no ZSG_METRICS_SALT set)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ZSG_METRICS_SALT", None)
            assert metrics.key_fingerprint(key) == expected

    def test_salt_changes_fingerprint(self):
        import zsg.metrics as metrics
        key = "some-key"
        fp_unsalted = metrics.key_fingerprint(key)
        with patch.dict(os.environ, {"ZSG_METRICS_SALT": "mysalt"}, clear=False):
            fp_salted = metrics.key_fingerprint(key)
        assert fp_unsalted != fp_salted

    def test_fingerprint_does_not_contain_raw_key(self):
        import zsg.metrics as metrics
        key = "sk-supersecret-12345"
        fp = metrics.key_fingerprint(key)
        assert key not in fp
        assert "sk-" not in fp


# ===========================================================================
# call_llm metrics wiring — purdue_genai (success path)
# ===========================================================================

class TestCallLlmSuccessRecord:

    def test_success_writes_exactly_one_record(self, tmp_path):
        import zsg.generate as generate
        log = tmp_path / "metrics.jsonl"

        with patch.dict(os.environ, {"ZSG_METRICS_PATH": str(log)}):
            with patch("requests.post", return_value=_fake_purdue_response()):
                generate.call_llm("hello world prompt", _purdue_cfg())

        records = _read_records(log)
        assert len(records) == 1

    def test_success_record_has_exactly_six_fields(self, tmp_path):
        import zsg.generate as generate
        log = tmp_path / "metrics.jsonl"

        with patch.dict(os.environ, {"ZSG_METRICS_PATH": str(log)}):
            with patch("requests.post", return_value=_fake_purdue_response()):
                generate.call_llm("prompt text", _purdue_cfg())

        rec = _read_records(log)[0]
        assert set(rec.keys()) == {"ts", "provider", "model", "ok", "round_trip_ms", "user"}

    def test_success_record_fields_correct(self, tmp_path):
        import zsg.generate as generate
        log = tmp_path / "metrics.jsonl"

        cfg = _purdue_cfg(api_key="my-api-key")
        with patch.dict(os.environ, {"ZSG_METRICS_PATH": str(log), "ZSG_METRICS_SALT": ""}):
            with patch("requests.post", return_value=_fake_purdue_response()):
                generate.call_llm("some prompt", cfg)

        rec = _read_records(log)[0]
        assert rec["provider"] == "purdue_genai"
        assert rec["model"] == "llama3.1:latest"
        assert rec["ok"] is True
        assert isinstance(rec["round_trip_ms"], (int, float))
        assert rec["round_trip_ms"] >= 0
        # user must be a 16-char hex fingerprint
        assert len(rec["user"]) == 16
        assert all(c in "0123456789abcdef" for c in rec["user"])

    def test_record_does_not_contain_raw_key(self, tmp_path):
        import zsg.generate as generate
        log = tmp_path / "metrics.jsonl"
        raw_key = "sk-purdue-real-secret-9999"

        with patch.dict(os.environ, {"ZSG_METRICS_PATH": str(log)}):
            with patch("requests.post", return_value=_fake_purdue_response()):
                generate.call_llm("the actual prompt text", _purdue_cfg(api_key=raw_key))

        log_text = log.read_text(encoding="utf-8")
        assert raw_key not in log_text
        assert "sk-purdue" not in log_text

    def test_record_does_not_contain_prompt(self, tmp_path):
        import zsg.generate as generate
        log = tmp_path / "metrics.jsonl"
        prompt = "TOP SECRET PROMPT CONTENTS MUST NOT APPEAR IN LOG"

        with patch.dict(os.environ, {"ZSG_METRICS_PATH": str(log)}):
            with patch("requests.post", return_value=_fake_purdue_response()):
                generate.call_llm(prompt, _purdue_cfg())

        log_text = log.read_text(encoding="utf-8")
        assert "TOP SECRET PROMPT" not in log_text

    def test_record_does_not_contain_response_body(self, tmp_path):
        import zsg.generate as generate
        log = tmp_path / "metrics.jsonl"
        response_text = '{"SECRET_RESPONSE_TEXT": "must not appear"}'

        with patch.dict(os.environ, {"ZSG_METRICS_PATH": str(log)}):
            with patch("requests.post", return_value=_fake_purdue_response(content=response_text)):
                generate.call_llm("prompt", _purdue_cfg())

        log_text = log.read_text(encoding="utf-8")
        assert "SECRET_RESPONSE_TEXT" not in log_text

    def test_ts_is_iso_utc_string(self, tmp_path):
        import zsg.generate as generate
        log = tmp_path / "metrics.jsonl"

        with patch.dict(os.environ, {"ZSG_METRICS_PATH": str(log)}):
            with patch("requests.post", return_value=_fake_purdue_response()):
                generate.call_llm("prompt", _purdue_cfg())

        rec = _read_records(log)[0]
        ts = rec["ts"]
        # Should look like 2026-06-17T12:34:56.789012+00:00
        assert "T" in ts
        assert ts.endswith("+00:00") or ts.endswith("Z")


# ===========================================================================
# call_llm metrics wiring — failure path
# ===========================================================================

class TestCallLlmFailureRecord:

    def test_http_exception_writes_record_with_ok_false(self, tmp_path):
        import zsg.generate as generate
        log = tmp_path / "metrics.jsonl"

        import requests as req_lib
        with patch.dict(os.environ, {"ZSG_METRICS_PATH": str(log)}):
            with patch("requests.post", side_effect=req_lib.exceptions.ConnectionError("timeout")):
                with pytest.raises(req_lib.exceptions.ConnectionError):
                    generate.call_llm("prompt", _purdue_cfg())

        records = _read_records(log)
        assert len(records) == 1
        assert records[0]["ok"] is False

    def test_http_exception_reraises(self, tmp_path):
        import zsg.generate as generate
        log = tmp_path / "metrics.jsonl"

        import requests as req_lib
        with patch.dict(os.environ, {"ZSG_METRICS_PATH": str(log)}):
            with patch("requests.post", side_effect=req_lib.exceptions.Timeout("timed out")):
                with pytest.raises(req_lib.exceptions.Timeout):
                    generate.call_llm("prompt", _purdue_cfg())

    def test_failure_record_has_six_fields(self, tmp_path):
        import zsg.generate as generate
        log = tmp_path / "metrics.jsonl"

        import requests as req_lib
        with patch.dict(os.environ, {"ZSG_METRICS_PATH": str(log)}):
            with patch("requests.post", side_effect=req_lib.exceptions.ConnectionError("fail")):
                with pytest.raises(req_lib.exceptions.ConnectionError):
                    generate.call_llm("prompt", _purdue_cfg())

        rec = _read_records(log)[0]
        assert set(rec.keys()) == {"ts", "provider", "model", "ok", "round_trip_ms", "user"}
        assert rec["ok"] is False

    def test_failure_record_does_not_contain_key(self, tmp_path):
        """Even on failure the raw API key must not appear in the log."""
        import zsg.generate as generate
        log = tmp_path / "metrics.jsonl"
        raw_key = "sk-fail-secret-key-xyz"

        import requests as req_lib
        with patch.dict(os.environ, {"ZSG_METRICS_PATH": str(log)}):
            with patch("requests.post", side_effect=req_lib.exceptions.ConnectionError("x")):
                with pytest.raises(req_lib.exceptions.ConnectionError):
                    generate.call_llm("prompt", _purdue_cfg(api_key=raw_key))

        log_text = log.read_text(encoding="utf-8")
        assert raw_key not in log_text


# ===========================================================================
# call_llm — dry_run must not write a record
# ===========================================================================

class TestDryRunNoRecord:

    def test_dry_run_writes_no_record(self, tmp_path):
        import zsg.generate as generate
        log = tmp_path / "metrics.jsonl"

        with patch.dict(os.environ, {"ZSG_METRICS_PATH": str(log)}):
            generate.call_llm("prompt", _purdue_cfg(), dry_run=True)

        assert not log.exists() or _read_records(log) == []


# ===========================================================================
# call_llm — key fingerprinting per provider
# ===========================================================================

class TestKeyFingerprintPerProvider:

    def test_same_key_different_calls_same_fingerprint(self, tmp_path):
        import zsg.generate as generate
        log = tmp_path / "metrics.jsonl"

        with patch.dict(os.environ, {"ZSG_METRICS_PATH": str(log)}):
            with patch("requests.post", return_value=_fake_purdue_response()):
                generate.call_llm("p1", _purdue_cfg(api_key="shared-key"))
            with patch("requests.post", return_value=_fake_purdue_response()):
                generate.call_llm("p2", _purdue_cfg(api_key="shared-key"))

        records = _read_records(log)
        assert len(records) == 2
        assert records[0]["user"] == records[1]["user"]

    def test_different_keys_different_fingerprints(self, tmp_path):
        import zsg.generate as generate
        log = tmp_path / "metrics.jsonl"

        with patch.dict(os.environ, {"ZSG_METRICS_PATH": str(log)}):
            with patch("requests.post", return_value=_fake_purdue_response()):
                generate.call_llm("p1", _purdue_cfg(api_key="key-alpha-111"))
            with patch("requests.post", return_value=_fake_purdue_response()):
                generate.call_llm("p2", _purdue_cfg(api_key="key-beta-222"))

        records = _read_records(log)
        assert len(records) == 2
        assert records[0]["user"] != records[1]["user"]

    def test_ollama_keyless_yields_user_none(self, tmp_path):
        """ollama with no api_key in cfg should produce user='none'."""
        import zsg.generate as generate
        log = tmp_path / "metrics.jsonl"

        ollama_resp = MagicMock()
        ollama_resp.raise_for_status = MagicMock()
        ollama_resp.json.return_value = {"response": "{}"}

        cfg = {
            "provider": "ollama",
            "model": "mistral:latest",
            "base_url": "http://localhost:11434",
            # No api_key field — simulates keyless local ollama
        }

        with patch.dict(os.environ, {"ZSG_METRICS_PATH": str(log)}):
            with patch("requests.post", return_value=ollama_resp):
                generate.call_llm("some prompt", cfg)

        records = _read_records(log)
        assert len(records) == 1
        assert records[0]["user"] == "none"

    def test_vllm_no_key_yields_user_none(self, tmp_path):
        """vllm with no api_key and no OPENAI_API_KEY env → user='none'."""
        import zsg.generate as generate
        log = tmp_path / "metrics.jsonl"

        vllm_resp = MagicMock()
        vllm_resp.raise_for_status = MagicMock()
        vllm_resp.json.return_value = {
            "choices": [{"finish_reason": "stop", "message": {"content": "{}"}}]
        }

        cfg = {
            "provider": "vllm",
            "model": "mistral-7b",
            "base_url": "http://localhost:8000",
        }

        with patch.dict(os.environ, {"ZSG_METRICS_PATH": str(log)}, clear=False):
            os.environ.pop("OPENAI_API_KEY", None)
            with patch("requests.post", return_value=vllm_resp):
                generate.call_llm("some prompt", cfg)

        records = _read_records(log)
        assert len(records) == 1
        assert records[0]["user"] == "none"


# ===========================================================================
# Web path — /api/v2/llm route also writes a record (seam coverage)
# ===========================================================================

class TestWebPathMetricsRecord:

    def test_v2_llm_route_writes_record_via_seam(self, tmp_path):
        """Patch requests.post (not call_llm) so the seam executes and writes
        a metrics record for the /api/v2/llm web route."""
        log = tmp_path / "web_metrics.jsonl"
        client = make_client()

        with patch.dict(os.environ, {"ZSG_METRICS_PATH": str(log)}):
            with patch("requests.post", return_value=_fake_purdue_response('{"hello": "world"}')):
                resp = client.post("/api/v2/llm", json={
                    "provider": "purdue_genai",
                    "model": "llama3.1:latest",
                    "base_url": "https://genai.rcac.purdue.edu/api/chat/completions",
                    "api_key": "web-test-key-456",
                    "prompt": "web test prompt",
                })

        assert resp.status_code == 200
        records = _read_records(log)
        assert len(records) == 1
        rec = records[0]
        assert set(rec.keys()) == {"ts", "provider", "model", "ok", "round_trip_ms", "user"}
        assert rec["ok"] is True
        assert rec["provider"] == "purdue_genai"

    def test_v2_llm_route_no_key_leak(self, tmp_path):
        """The raw api_key passed through the web route must not appear in the log."""
        log = tmp_path / "web_metrics.jsonl"
        client = make_client()
        raw_key = "sk-web-secret-should-not-log"

        with patch.dict(os.environ, {"ZSG_METRICS_PATH": str(log)}):
            with patch("requests.post", return_value=_fake_purdue_response()):
                client.post("/api/v2/llm", json={
                    "provider": "purdue_genai",
                    "model": "llama3.1:latest",
                    "base_url": "https://genai.rcac.purdue.edu/api/chat/completions",
                    "api_key": raw_key,
                    "prompt": "hello",
                })

        log_text = log.read_text(encoding="utf-8")
        assert raw_key not in log_text
        assert "sk-web" not in log_text
        assert "Bearer" not in log_text
        assert "Authorization" not in log_text


class TestDashboardExport:
    """The self-contained HTML analytics dashboard (python -m zsg.metrics --html)."""

    def _fixture(self, log: Path):
        rows = [
            {"ts": "2026-06-17T02:00:00+00:00", "provider": "purdue_genai",
             "model": "llama3.1:latest", "ok": True, "round_trip_ms": 100.0, "user": "aaaa111122223333"},
            {"ts": "2026-06-17T02:01:00+00:00", "provider": "purdue_genai",
             "model": "llama3.1:latest", "ok": True, "round_trip_ms": 300.0, "user": "aaaa111122223333"},
            {"ts": "2026-06-17T02:02:00+00:00", "provider": "ollama",
             "model": "llama3.1:70b", "ok": True, "round_trip_ms": 200.0, "user": "none"},
            {"ts": "2026-06-17T02:03:00+00:00", "provider": "purdue_genai",
             "model": "llama3.1:latest", "ok": False, "round_trip_ms": 999.0, "user": "bbbb444455556666"},
        ]
        log.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    def test_export_writes_self_contained_html(self, tmp_path):
        import zsg.metrics_dashboard as dash
        import zsg.metrics as metrics
        log = tmp_path / "metrics.jsonl"
        self._fixture(log)
        out = tmp_path / "dash.html"
        with patch.dict(os.environ, {"ZSG_METRICS_PATH": str(log)}):
            dash.write_dashboard(metrics.load_records(), out)
        html = out.read_text(encoding="utf-8")
        assert html.lstrip().startswith("<!DOCTYPE html>")
        # Self-contained: no external scripts/stylesheets/CDNs.
        assert "<script src" not in html
        assert "cdn" not in html.lower()
        assert "https://cdn" not in html
        # Data + stats are inlined.
        assert "total_calls" in html
        assert "aaaa111122223333" in html  # a user fingerprint made it into the payload

    def test_dashboard_contains_no_secrets(self, tmp_path):
        import zsg.metrics_dashboard as dash
        log = tmp_path / "metrics.jsonl"
        self._fixture(log)
        html = dash.build_html(
            [json.loads(l) for l in log.read_text().splitlines() if l.strip()]
        )
        for bad in ("sk-", "Bearer", "Authorization", "prompt", "response"):
            assert bad not in html

    def test_empty_log_produces_valid_html(self, tmp_path):
        import zsg.metrics_dashboard as dash
        html = dash.build_html([])
        assert html.lstrip().startswith("<!DOCTYPE html>")
        assert "__DATA__" not in html  # placeholder fully substituted

    def test_cli_html_flag_writes_file(self, tmp_path):
        import zsg.metrics as metrics
        log = tmp_path / "metrics.jsonl"
        self._fixture(log)
        out = tmp_path / "out.html"
        with patch.dict(os.environ, {"ZSG_METRICS_PATH": str(log)}):
            with patch.object(sys, "argv", ["zsg.metrics", "--html", str(out)]):
                metrics._main()
        assert out.exists()
        assert "<!DOCTYPE html>" in out.read_text(encoding="utf-8")

    def test_export_is_read_only(self, tmp_path):
        import zsg.metrics_dashboard as dash
        import zsg.metrics as metrics
        log = tmp_path / "metrics.jsonl"
        self._fixture(log)
        before = log.read_bytes()
        out = tmp_path / "dash.html"
        with patch.dict(os.environ, {"ZSG_METRICS_PATH": str(log)}):
            dash.write_dashboard(metrics.load_records(), out)
        assert log.read_bytes() == before  # log untouched
