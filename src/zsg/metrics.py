"""
metrics.py — Best-effort, thread-safe per-call metrics log for LLM calls.

Every LLM call through generate.call_llm() appends one JSONL record to the
metrics log.  The log path defaults to PROJECT_ROOT/metrics.jsonl and can be
overridden via the ZSG_METRICS_PATH environment variable (useful in tests).

Record schema (exactly these six fields, nothing else):
    ts             — ISO-8601 UTC timestamp of the call start
    provider       — LLM provider string (e.g. "purdue_genai", "ollama")
    model          — model identifier string
    ok             — bool: True if the HTTP round-trip succeeded
    round_trip_ms  — wall-clock milliseconds for the outbound HTTP call only
    user           — 16-char hex fingerprint of the resolved API key,
                     or the literal "none" for keyless providers

Security invariant
------------------
The raw API key, Authorization/Bearer headers, prompt text, and response body
are NEVER written to the log.  The `user` field is sha256(key)[:16] — a
one-way, stable fingerprint:
  - Non-empty key  →  sha256(key.encode()).hexdigest()[:16]
  - Missing/empty  →  "none"

Salting: if ZSG_METRICS_SALT is set in the environment it is prepended to the
key before hashing (sha256(salt + key)).  This prevents offline dictionary
attacks on short/common API keys.  If the env var is absent the hash is
unsalted.  The same salt must be present in both writes to get a matching
fingerprint across sessions — document this if you change the salt.

Thread safety
-------------
A module-level threading.Lock serialises all appends.  This is required
because the CLI runs sections in a ThreadPoolExecutor and Flask is started with
threaded=True.

Best-effort
-----------
Any exception raised during logging is caught, printed to stderr, and
swallowed so that a metrics failure never breaks an LLM generation call.

S3 backend (opt-in)
-------------------
Set ZSG_METRICS_PATH to an ``s3://bucket/prefix`` URI to enable the S3
backend.  Any other value (or no value) uses the local file backend (the
default).  The backend is selected at call time by inspecting the value of
ZSG_METRICS_PATH, so it can change between calls in the same process (e.g.
during tests).

Object layout: one JSON object per record, keyed as
    <prefix>/YYYY/MM/DD/<ts_hex>-<uuid4>.json
where <ts_hex> is the record's ISO-8601 timestamp with colons/dots replaced so
it is a valid S3 key component.  One-object-per-record avoids S3's lack of
append, makes PutObject a single cheap network call per record (no read-modify-
write, no lock contention across tasks), and makes load_records a ListObjects +
GetObject fan-out.  Reads are rare (offline operator runs); writes are hot-path
— so trading read cost for write simplicity is correct here.

boto3 is imported LAZILY inside the S3 code path only.  Importing zsg.metrics
with no boto3 installed must keep working for file mode and the test suite.
"""

import hashlib
import json
import os
import statistics
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from zsg import PROJECT_ROOT

_log_lock = threading.Lock()


def _metrics_uri() -> str:
    """Return the raw ZSG_METRICS_PATH value, or the default file path string."""
    override = os.environ.get("ZSG_METRICS_PATH")
    return override if override else str(PROJECT_ROOT / "metrics.jsonl")


def _is_s3_uri(uri: str) -> bool:
    """Return True if *uri* looks like an s3:// URI."""
    return uri.startswith("s3://")


def _parse_s3_uri(uri: str):
    """Parse ``s3://bucket/prefix`` into ``(bucket, prefix)``.

    The prefix may be empty ("") if the URI is exactly ``s3://bucket`` or
    ``s3://bucket/``.  The trailing slash on the prefix is stripped; callers
    should append their own ``/<key>`` separator.
    """
    # Strip the scheme
    without_scheme = uri[len("s3://"):]
    slash = without_scheme.find("/")
    if slash == -1:
        return without_scheme, ""
    bucket = without_scheme[:slash]
    prefix = without_scheme[slash + 1:].rstrip("/")
    return bucket, prefix


def _log_path() -> Path:
    """Resolve the metrics log path (file-mode only).

    Checks ZSG_METRICS_PATH first (for test redirection), then falls back to
    PROJECT_ROOT/metrics.jsonl.  Only call this when _is_s3_uri() is False.
    """
    override = os.environ.get("ZSG_METRICS_PATH")
    return Path(override) if override else PROJECT_ROOT / "metrics.jsonl"


def key_fingerprint(api_key: Optional[str]) -> str:
    """Return a 16-char hex fingerprint of *api_key*, or ``"none"``.

    The fingerprint is sha256(salt + key) where *salt* is the value of the
    ZSG_METRICS_SALT environment variable (empty string if unset).  Only the
    first 16 hex characters are kept — enough to distinguish keys without
    being reversible.

    Args:
        api_key: The resolved (post env-fallback) API key string, or None/""
                 for keyless providers such as local ollama or vllm.

    Returns:
        A 16-character lowercase hex string, or the literal string ``"none"``.
    """
    if not api_key:
        return "none"
    salt = os.environ.get("ZSG_METRICS_SALT", "")
    raw = (salt + api_key).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def _s3_object_key(prefix: str, record: dict) -> str:
    """Build a date-partitioned S3 object key for *record*.

    Layout: ``<prefix>/YYYY/MM/DD/<ts_safe>-<uuid4>.json``

    The timestamp component has colons and dots replaced with dashes so the key
    is URL-safe and easy to sort lexicographically.  The uuid4 suffix avoids
    collisions when two calls land in the same millisecond (e.g. under
    ThreadPoolExecutor).  One object per record — no append, no lock contention
    across tasks.
    """
    ts: str = record["ts"]  # e.g. "2026-06-17T12:34:56.789012+00:00"
    # Parse just the date portion for the partition key
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        dt = datetime.now(timezone.utc)
    date_part = dt.strftime("%Y/%m/%d")
    ts_safe = ts.replace(":", "-").replace(".", "-").replace("+", "-")
    key_name = f"{ts_safe}-{uuid.uuid4().hex}.json"
    if prefix:
        return f"{prefix}/{date_part}/{key_name}"
    return f"{date_part}/{key_name}"


def _s3_client():
    """Return a boto3 S3 client.  boto3 is imported lazily here.

    Raises ImportError if boto3 is not installed (caller must handle).
    """
    import boto3  # noqa: PLC0415 — intentional lazy import
    return boto3.client("s3")


def _append_record_s3(record: dict, bucket: str, prefix: str) -> None:
    """Write *record* as a single JSON object to S3 (best-effort).

    Any error — including a missing boto3 installation, missing credentials,
    throttling, or network failure — is caught, printed to stderr, and
    swallowed so the caller always continues normally.

    S3 writes are independent (unique keys), so _log_lock is not needed here
    for correctness, but thread safety is preserved because each call touches
    a distinct key.
    """
    try:
        s3 = _s3_client()
        key = _s3_object_key(prefix, record)
        body = json.dumps(record, ensure_ascii=False).encode("utf-8")
        s3.put_object(Bucket=bucket, Key=key, Body=body,
                      ContentType="application/json")
    except Exception as exc:
        print(f"[zsg.metrics] WARNING: failed to write metrics record to S3: {exc}",
              file=sys.stderr)


def _load_records_s3(bucket: str, prefix: str) -> list:
    """Read all records from S3 under *bucket*/*prefix* (best-effort).

    Lists objects under the prefix, fetches each one, and parses the JSON body.
    Malformed objects are silently skipped.  Returns an empty list on any error.

    This is intentionally synchronous and sequential — reads are offline/operator
    runs, not on the hot path.
    """
    try:
        s3 = _s3_client()
        list_prefix = (prefix + "/") if prefix else ""
        paginator = s3.get_paginator("list_objects_v2")
        keys = []
        for page in paginator.paginate(Bucket=bucket, Prefix=list_prefix):
            for obj in page.get("Contents", []):
                keys.append(obj["Key"])
    except Exception as exc:
        print(f"[zsg.metrics] WARNING: failed to list S3 metrics objects: {exc}",
              file=sys.stderr)
        return []

    records = []
    for key in keys:
        try:
            resp = s3.get_object(Bucket=bucket, Key=key)
            body = resp["Body"].read().decode("utf-8")
            records.append(json.loads(body))
        except Exception:
            pass  # skip unreadable/malformed objects
    return records


def append_record(
    *,
    provider: str,
    model: str,
    ok: bool,
    round_trip_ms: float,
    user: str,
) -> None:
    """Append one metrics record to the active backend (file or S3).

    This function is best-effort: any I/O or serialisation error is caught,
    printed to stderr, and swallowed so callers always continue normally.

    The record written is exactly::

        {"ts": "<ISO-UTC>", "provider": "...", "model": "...",
         "ok": true/false, "round_trip_ms": 123.4, "user": "abc123..."}

    Backend selection: if ZSG_METRICS_PATH starts with ``s3://``, the record is
    written to S3; otherwise it is appended to a local file (the default).

    Args:
        provider:      The LLM provider string (e.g. "purdue_genai").
        model:         The model identifier string.
        ok:            True if the HTTP call completed without an exception.
        round_trip_ms: Wall-clock time of the outbound HTTP call in ms.
        user:          The 16-char key fingerprint from :func:`key_fingerprint`.
    """
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "provider": provider,
        "model": model,
        "ok": ok,
        "round_trip_ms": round(round_trip_ms, 3),
        "user": user,
    }
    uri = _metrics_uri()
    if _is_s3_uri(uri):
        bucket, prefix = _parse_s3_uri(uri)
        _append_record_s3(record, bucket, prefix)
    else:
        try:
            line = json.dumps(record, ensure_ascii=False) + "\n"
            log_file = _log_path()
            with _log_lock:
                with open(log_file, "a", encoding="utf-8") as fh:
                    fh.write(line)
        except Exception as exc:  # pragma: no cover — defensive only
            print(f"[zsg.metrics] WARNING: failed to write metrics record: {exc}",
                  file=sys.stderr)


def load_records() -> list:
    """Read and parse all records from the active backend (file or S3).

    Read-only.  Honors ZSG_METRICS_PATH.  Returns an empty list for a
    missing/empty log; silently skips malformed lines (file) or objects (S3).

    Backend selection: if ZSG_METRICS_PATH starts with ``s3://``, records are
    read from S3 via list + get; otherwise from the local file (the default).
    """
    uri = _metrics_uri()
    if _is_s3_uri(uri):
        bucket, prefix = _parse_s3_uri(uri)
        return _load_records_s3(bucket, prefix)
    # File backend
    log_path = _log_path()
    if not log_path.exists():
        return []
    records = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                pass  # skip malformed lines
    return records


def compute_stats(records: list[dict]) -> dict:
    """Aggregate metrics records into summary statistics.

    Latency percentiles are computed over *successful* calls only (a failed
    round-trip's latency is the time-to-failure, which would skew the numbers).

    Returns a dict with: total_calls, successful, failed, latency_min_ms,
    latency_median_ms, latency_p95_ms, latency_max_ms, unique_users.
    """
    total = len(records)
    successful = sum(1 for r in records if r.get("ok") is True)
    failed = sum(1 for r in records if r.get("ok") is False)

    ok_latencies = sorted(
        r["round_trip_ms"] for r in records
        if r.get("ok") is True and isinstance(r.get("round_trip_ms"), (int, float))
    )
    if ok_latencies:
        latency_min = ok_latencies[0]
        latency_median = statistics.median(ok_latencies)
        latency_max = ok_latencies[-1]
        # nearest-rank p95
        idx = max(0, int(round(0.95 * len(ok_latencies))) - 1)
        latency_p95 = ok_latencies[idx]
    else:
        latency_min = latency_median = latency_p95 = latency_max = 0

    unique_users = len(set(r.get("user") for r in records))

    return {
        "total_calls": total,
        "successful": successful,
        "failed": failed,
        "latency_min_ms": latency_min,
        "latency_median_ms": latency_median,
        "latency_p95_ms": latency_p95,
        "latency_max_ms": latency_max,
        "unique_users": unique_users,
    }


def report() -> None:
    """Read-only metrics summary printed to stdout.

    Reads the log (honoring ZSG_METRICS_PATH), handles missing/empty logs
    gracefully (zeros), and never writes to the log.  Prints total calls,
    success/fail split, latency min/median/p95/max over successful calls, and
    the number of unique users (distinct key fingerprints).
    """
    s = compute_stats(load_records())
    print(f"total_calls: {s['total_calls']}")
    print(f"successful: {s['successful']}")
    print(f"failed: {s['failed']}")
    print(f"latency_min_ms: {s['latency_min_ms']}")
    print(f"latency_median_ms: {s['latency_median_ms']}")
    print(f"latency_p95_ms: {s['latency_p95_ms']}")
    print(f"latency_max_ms: {s['latency_max_ms']}")
    print(f"unique_users: {s['unique_users']}")


def _main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m zsg.metrics",
        description="Summarize the LLM metrics log (read-only).",
    )
    parser.add_argument(
        "--html", metavar="PATH", nargs="?", const="metrics_dashboard.html",
        help="Write a self-contained HTML analytics dashboard to PATH "
             "(default: metrics_dashboard.html) instead of printing a summary.",
    )
    args = parser.parse_args()

    if args.html is not None:
        from zsg.metrics_dashboard import write_dashboard
        out = write_dashboard(load_records(), Path(args.html))
        print(f"Wrote dashboard to {out}")
    else:
        report()


if __name__ == "__main__":
    _main()
