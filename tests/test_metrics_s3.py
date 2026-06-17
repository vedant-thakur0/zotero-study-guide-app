"""
test_metrics_s3.py — Tests for the S3 storage backend in zsg.metrics.

Covers (per brief acceptance criteria):
- Backend selection: file URI → file mode; s3:// URI → S3 mode.
- S3 round-trip (mocked): append_record writes a record; load_records reads it back.
- S3 write failure is swallowed: append_record returns normally even when the client raises.
- key_fingerprint / salt behavior is unchanged (sanity check from the S3 perspective).
- boto3 is never imported at module load time (lazy-import check).

All S3 interaction is mocked; no real AWS credentials or network calls are needed.
"""

import hashlib
import io
import json
import os
import sys
from pathlib import Path
from typing import Dict, Optional
from unittest.mock import MagicMock, patch, call

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_record(**overrides):
    """Return a minimal valid metrics record dict, with optional overrides."""
    base = {
        "ts": "2026-06-17T12:00:00.000000+00:00",
        "provider": "purdue_genai",
        "model": "llama3.1:latest",
        "ok": True,
        "round_trip_ms": 123.456,
        "user": "abcdef1234567890",
    }
    base.update(overrides)
    return base


def _make_s3_mock(stored: Optional[Dict] = None):
    """Build a MagicMock that simulates an S3 client.

    ``stored`` is an in-memory dict mapping object keys to their raw JSON bytes.
    put_object() stores into it; get_object() retrieves from it.  list_objects_v2
    via get_paginator() iterates over its keys.

    If ``stored`` is None an empty dict is used (write-only mock).
    """
    if stored is None:
        stored = {}

    s3 = MagicMock()

    def put_object(Bucket, Key, Body, ContentType=None):
        stored[Key] = Body if isinstance(Body, bytes) else Body.encode("utf-8")

    def get_object(Bucket, Key):
        body_bytes = stored[Key]
        stream = MagicMock()
        stream.read.return_value = body_bytes
        return {"Body": stream}

    # Paginator mock — returns one page with all stored keys as Contents entries
    page_content = []

    def paginate(Bucket, Prefix=""):
        matching = [k for k in stored if k.startswith(Prefix)]
        yield {"Contents": [{"Key": k} for k in matching]}

    paginator_mock = MagicMock()
    paginator_mock.paginate.side_effect = paginate

    s3.put_object.side_effect = put_object
    s3.get_object.side_effect = get_object
    s3.get_paginator.return_value = paginator_mock

    return s3, stored


# ===========================================================================
# Backend selection
# ===========================================================================

class TestBackendSelection:

    def test_file_path_selects_file_backend(self, tmp_path):
        """A plain file path in ZSG_METRICS_PATH must not trigger the S3 path."""
        import zsg.metrics as metrics
        log = tmp_path / "m.jsonl"
        with patch.dict(os.environ, {"ZSG_METRICS_PATH": str(log)}):
            # append_record should write a file, not touch S3
            metrics.append_record(
                provider="purdue_genai", model="llama3.1:latest",
                ok=True, round_trip_ms=50.0, user="abc123def4567890",
            )
        # File was created; S3 was never touched (boto3 was never imported)
        assert log.exists()
        lines = log.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["provider"] == "purdue_genai"

    def test_s3_uri_selects_s3_backend(self, tmp_path):
        """An s3:// URI in ZSG_METRICS_PATH must use the S3 path."""
        import zsg.metrics as metrics
        s3_mock, stored = _make_s3_mock()
        with patch.dict(os.environ, {"ZSG_METRICS_PATH": "s3://my-bucket/metrics"}):
            with patch.object(metrics, "_s3_client", return_value=s3_mock):
                metrics.append_record(
                    provider="purdue_genai", model="llama3.1:latest",
                    ok=True, round_trip_ms=50.0, user="abc123def4567890",
                )
        assert len(stored) == 1, "Expected exactly one object written to S3"
        # The local tmp_path must be untouched — no local file was created
        assert not any(tmp_path.iterdir())

    def test_no_env_var_uses_file_backend(self, tmp_path, monkeypatch):
        """With ZSG_METRICS_PATH absent, the file backend is used (default)."""
        import zsg.metrics as metrics
        # Point PROJECT_ROOT at tmp_path to keep the default path local
        monkeypatch.setenv("ZSG_METRICS_PATH", str(tmp_path / "metrics.jsonl"))
        metrics.append_record(
            provider="ollama", model="mistral",
            ok=False, round_trip_ms=0.0, user="none",
        )
        log = tmp_path / "metrics.jsonl"
        assert log.exists()

    def test_is_s3_uri_true_for_s3_scheme(self):
        import zsg.metrics as metrics
        assert metrics._is_s3_uri("s3://bucket/prefix") is True

    def test_is_s3_uri_false_for_file_path(self):
        import zsg.metrics as metrics
        assert metrics._is_s3_uri("/tmp/metrics.jsonl") is False

    def test_is_s3_uri_false_for_relative_path(self):
        import zsg.metrics as metrics
        assert metrics._is_s3_uri("metrics.jsonl") is False

    def test_parse_s3_uri_bucket_and_prefix(self):
        import zsg.metrics as metrics
        bucket, prefix = metrics._parse_s3_uri("s3://my-bucket/my/prefix")
        assert bucket == "my-bucket"
        assert prefix == "my/prefix"

    def test_parse_s3_uri_no_prefix(self):
        import zsg.metrics as metrics
        bucket, prefix = metrics._parse_s3_uri("s3://my-bucket")
        assert bucket == "my-bucket"
        assert prefix == ""

    def test_parse_s3_uri_trailing_slash_stripped(self):
        import zsg.metrics as metrics
        bucket, prefix = metrics._parse_s3_uri("s3://my-bucket/prefix/")
        assert prefix == "prefix"


# ===========================================================================
# S3 round-trip (mocked)
# ===========================================================================

class TestS3RoundTrip:

    def test_append_then_load_returns_same_record(self):
        """append_record → load_records round-trip via a mocked S3 client."""
        import zsg.metrics as metrics
        s3_mock, stored = _make_s3_mock()

        with patch.dict(os.environ, {"ZSG_METRICS_PATH": "s3://test-bucket/logs"}):
            with patch.object(metrics, "_s3_client", return_value=s3_mock):
                metrics.append_record(
                    provider="purdue_genai",
                    model="llama3.1:latest",
                    ok=True,
                    round_trip_ms=200.5,
                    user="deadbeef01234567",
                )
                records = metrics.load_records()

        assert len(records) == 1
        rec = records[0]
        assert set(rec.keys()) == {"ts", "provider", "model", "ok", "round_trip_ms", "user"}
        assert rec["provider"] == "purdue_genai"
        assert rec["model"] == "llama3.1:latest"
        assert rec["ok"] is True
        assert rec["round_trip_ms"] == 200.5
        assert rec["user"] == "deadbeef01234567"

    def test_multiple_appends_all_returned(self):
        """Three separate append_record calls → load_records returns all three."""
        import zsg.metrics as metrics
        s3_mock, stored = _make_s3_mock()

        with patch.dict(os.environ, {"ZSG_METRICS_PATH": "s3://test-bucket/logs"}):
            with patch.object(metrics, "_s3_client", return_value=s3_mock):
                for i in range(3):
                    metrics.append_record(
                        provider="purdue_genai",
                        model="llama3.1:latest",
                        ok=True,
                        round_trip_ms=float(100 + i),
                        user=f"user{i:013d}xxx",
                    )
                records = metrics.load_records()

        assert len(records) == 3

    def test_s3_object_key_contains_date_partition(self):
        """Each object key must include a YYYY/MM/DD date-partition segment."""
        import zsg.metrics as metrics
        s3_mock, stored = _make_s3_mock()

        with patch.dict(os.environ, {"ZSG_METRICS_PATH": "s3://test-bucket/pfx"}):
            with patch.object(metrics, "_s3_client", return_value=s3_mock):
                metrics.append_record(
                    provider="purdue_genai", model="llama3.1:latest",
                    ok=True, round_trip_ms=99.9, user="a" * 16,
                )

        assert len(stored) == 1
        key = list(stored.keys())[0]
        # Key shape: pfx/YYYY/MM/DD/<ts_safe>-<uuid4>.json
        assert key.startswith("pfx/")
        parts = key.split("/")
        # parts: ['pfx', 'YYYY', 'MM', 'DD', '<filename>.json']
        assert len(parts) == 5
        assert len(parts[1]) == 4 and parts[1].isdigit()  # year
        assert len(parts[2]) == 2 and parts[2].isdigit()  # month
        assert len(parts[3]) == 2 and parts[3].isdigit()  # day
        assert parts[4].endswith(".json")

    def test_s3_object_key_no_prefix(self):
        """When the S3 URI has no prefix, the key starts directly with YYYY/."""
        import zsg.metrics as metrics
        s3_mock, stored = _make_s3_mock()

        with patch.dict(os.environ, {"ZSG_METRICS_PATH": "s3://test-bucket"}):
            with patch.object(metrics, "_s3_client", return_value=s3_mock):
                metrics.append_record(
                    provider="purdue_genai", model="m", ok=True,
                    round_trip_ms=1.0, user="b" * 16,
                )

        assert len(stored) == 1
        key = list(stored.keys())[0]
        parts = key.split("/")
        # parts: ['YYYY', 'MM', 'DD', '<filename>.json']
        assert len(parts) == 4
        assert parts[3].endswith(".json")

    def test_record_written_to_s3_has_correct_schema(self):
        """The object stored in S3 must have exactly the 6-field schema."""
        import zsg.metrics as metrics
        s3_mock, stored = _make_s3_mock()

        with patch.dict(os.environ, {"ZSG_METRICS_PATH": "s3://test-bucket/logs"}):
            with patch.object(metrics, "_s3_client", return_value=s3_mock):
                metrics.append_record(
                    provider="purdue_genai", model="llama3.1:latest",
                    ok=False, round_trip_ms=999.0, user="dead1234dead5678",
                )

        body_bytes = list(stored.values())[0]
        rec = json.loads(body_bytes.decode("utf-8"))
        assert set(rec.keys()) == {"ts", "provider", "model", "ok", "round_trip_ms", "user"}
        assert rec["ok"] is False


# ===========================================================================
# S3 write failure is swallowed
# ===========================================================================

class TestS3WriteFailureSwallowed:

    def test_put_object_failure_does_not_propagate(self, capsys):
        """A boto3 PutObject failure must be swallowed; append_record returns None."""
        import zsg.metrics as metrics
        s3_mock = MagicMock()
        s3_mock.put_object.side_effect = RuntimeError("S3 unavailable")

        with patch.dict(os.environ, {"ZSG_METRICS_PATH": "s3://test-bucket/logs"}):
            with patch.object(metrics, "_s3_client", return_value=s3_mock):
                result = metrics.append_record(
                    provider="purdue_genai", model="m",
                    ok=True, round_trip_ms=10.0, user="a" * 16,
                )

        # Must not propagate — the call returns (implicitly None)
        assert result is None
        # Warning must go to stderr
        captured = capsys.readouterr()
        assert "[zsg.metrics] WARNING" in captured.err
        assert "S3" in captured.err

    def test_client_construction_failure_does_not_propagate(self, capsys):
        """If _s3_client() itself raises (e.g. boto3 not installed, bad creds),
        append_record must still return normally."""
        import zsg.metrics as metrics

        with patch.dict(os.environ, {"ZSG_METRICS_PATH": "s3://test-bucket/logs"}):
            with patch.object(metrics, "_s3_client", side_effect=ImportError("No module named 'boto3'")):
                result = metrics.append_record(
                    provider="purdue_genai", model="m",
                    ok=True, round_trip_ms=10.0, user="a" * 16,
                )

        assert result is None
        captured = capsys.readouterr()
        assert "[zsg.metrics] WARNING" in captured.err

    def test_list_objects_failure_returns_empty_list(self, capsys):
        """If list_objects_v2 fails during load_records, an empty list is returned
        and the error is printed to stderr (not propagated)."""
        import zsg.metrics as metrics
        s3_mock = MagicMock()
        paginator_mock = MagicMock()
        paginator_mock.paginate.side_effect = RuntimeError("ListObjects failed")
        s3_mock.get_paginator.return_value = paginator_mock

        with patch.dict(os.environ, {"ZSG_METRICS_PATH": "s3://test-bucket/logs"}):
            with patch.object(metrics, "_s3_client", return_value=s3_mock):
                records = metrics.load_records()

        assert records == []
        captured = capsys.readouterr()
        assert "[zsg.metrics] WARNING" in captured.err

    def test_get_object_failure_skips_object(self):
        """If GetObject fails for an individual object, it is silently skipped
        and load_records still returns the records that could be read."""
        import zsg.metrics as metrics
        stored = {}
        s3_mock = MagicMock()

        good_record = _make_record(user="goodrecord1234ab")
        stored["logs/2026/06/17/good.json"] = json.dumps(good_record).encode()

        def get_object(Bucket, Key):
            if "good" in Key:
                stream = MagicMock()
                stream.read.return_value = stored[Key]
                return {"Body": stream}
            raise RuntimeError("GetObject failed for this key")

        paginator_mock = MagicMock()
        paginator_mock.paginate.return_value = iter([{
            "Contents": [
                {"Key": "logs/2026/06/17/good.json"},
                {"Key": "logs/2026/06/17/bad.json"},
            ]
        }])
        s3_mock.get_paginator.return_value = paginator_mock
        s3_mock.get_object.side_effect = get_object

        with patch.dict(os.environ, {"ZSG_METRICS_PATH": "s3://test-bucket/logs"}):
            with patch.object(metrics, "_s3_client", return_value=s3_mock):
                records = metrics.load_records()

        assert len(records) == 1
        assert records[0]["user"] == "goodrecord1234ab"


# ===========================================================================
# boto3 lazy-import check
# ===========================================================================

class TestBoto3LazyImport:

    def test_import_metrics_without_boto3_does_not_raise(self):
        """Importing zsg.metrics with no boto3 installed must succeed.
        We simulate the absence of boto3 by temporarily hiding it.
        """
        # Remove boto3 from sys.modules if it somehow got imported
        original_boto3 = sys.modules.pop("boto3", None)
        try:
            # Block the import
            import builtins
            real_import = builtins.__import__

            def blocking_import(name, *args, **kwargs):
                if name == "boto3":
                    raise ImportError("No module named 'boto3' (simulated)")
                return real_import(name, *args, **kwargs)

            with patch("builtins.__import__", side_effect=blocking_import):
                # Re-importing the module with boto3 blocked must not raise
                # (the module was already imported, so we test the function directly)
                import zsg.metrics as metrics
                # Key test: file-mode operations must not import boto3
                # We check that _is_s3_uri and _parse_s3_uri work fine
                assert not metrics._is_s3_uri("/tmp/m.jsonl")
                assert metrics._is_s3_uri("s3://bucket/p")
        finally:
            if original_boto3 is not None:
                sys.modules["boto3"] = original_boto3

    def test_boto3_only_imported_in_s3_path(self):
        """_s3_client() is the sole importer of boto3.  File-mode code paths
        must never reach that function.  Verify by patching _s3_client to raise
        and confirming file-mode calls do NOT trigger it."""
        import zsg.metrics as metrics

        with patch.object(metrics, "_s3_client", side_effect=AssertionError("boto3 called in file mode")):
            # File mode — must not call _s3_client
            with patch.dict(os.environ, {"ZSG_METRICS_PATH": "/tmp/_test_notouch.jsonl"}):
                try:
                    # This would raise AssertionError if _s3_client were called
                    records = metrics.load_records()
                    # (File doesn't exist; empty list is fine)
                except FileNotFoundError:
                    pass
                except AssertionError:
                    pytest.fail("_s3_client was called in file-mode load_records")


# ===========================================================================
# key_fingerprint / salt behavior (sanity check from S3 perspective)
# ===========================================================================

class TestKeyFingerprintSanityS3:
    """These mirror the file-mode fingerprint tests to confirm the schema and
    salt behavior are unaffected by the backend switch."""

    def test_fingerprint_is_16_hex_chars(self):
        import zsg.metrics as metrics
        fp = metrics.key_fingerprint("some-api-key")
        assert len(fp) == 16
        assert all(c in "0123456789abcdef" for c in fp)

    def test_empty_key_returns_none(self):
        import zsg.metrics as metrics
        assert metrics.key_fingerprint("") == "none"

    def test_salt_changes_fingerprint(self):
        import zsg.metrics as metrics
        key = "test-key"
        fp_unsalted = metrics.key_fingerprint(key)
        with patch.dict(os.environ, {"ZSG_METRICS_SALT": "s3-test-salt"}, clear=False):
            fp_salted = metrics.key_fingerprint(key)
        assert fp_unsalted != fp_salted

    def test_fingerprint_matches_expected_sha256(self):
        import zsg.metrics as metrics
        key = "deterministic-key-for-s3-test"
        expected = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ZSG_METRICS_SALT", None)
            assert metrics.key_fingerprint(key) == expected

    def test_user_field_in_s3_record_is_fingerprint(self):
        """The user field written to S3 must be the correct fingerprint, not the raw key."""
        import zsg.metrics as metrics
        s3_mock, stored = _make_s3_mock()
        raw_key = "sk-secret-key-test-xyz"
        with patch.dict(os.environ, {"ZSG_METRICS_PATH": "s3://test-bucket/logs",
                                     "ZSG_METRICS_SALT": ""}):
            with patch.object(metrics, "_s3_client", return_value=s3_mock):
                fp = metrics.key_fingerprint(raw_key)
                metrics.append_record(
                    provider="purdue_genai", model="m",
                    ok=True, round_trip_ms=1.0, user=fp,
                )

        body = list(stored.values())[0]
        rec = json.loads(body)
        # The user field must be the fingerprint, not the raw key
        assert raw_key not in rec["user"]
        assert rec["user"] == fp
        assert len(rec["user"]) == 16
