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
"""

import hashlib
import json
import os
import statistics
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from zsg import PROJECT_ROOT

_log_lock = threading.Lock()


def _log_path() -> Path:
    """Resolve the metrics log path.

    Checks ZSG_METRICS_PATH first (for test redirection), then falls back to
    PROJECT_ROOT/metrics.jsonl.
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


def append_record(
    *,
    provider: str,
    model: str,
    ok: bool,
    round_trip_ms: float,
    user: str,
) -> None:
    """Append one metrics record to the log file.

    This function is best-effort: any I/O or serialisation error is caught,
    printed to stderr, and swallowed so callers always continue normally.

    The record written is exactly::

        {"ts": "<ISO-UTC>", "provider": "...", "model": "...",
         "ok": true/false, "round_trip_ms": 123.4, "user": "abc123..."}

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
    try:
        line = json.dumps(record, ensure_ascii=False) + "\n"
        log_file = _log_path()
        with _log_lock:
            with open(log_file, "a", encoding="utf-8") as fh:
                fh.write(line)
    except Exception as exc:  # pragma: no cover — defensive only
        print(f"[zsg.metrics] WARNING: failed to write metrics record: {exc}",
              file=sys.stderr)


def load_records() -> list[dict]:
    """Read and parse all records from the metrics log.

    Read-only.  Honors ZSG_METRICS_PATH.  Returns an empty list for a
    missing/empty log; silently skips malformed lines.
    """
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
