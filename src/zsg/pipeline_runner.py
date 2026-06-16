"""
pipeline_runner.py — Background subprocess runner for pipeline stages

Spawns Python scripts as subprocesses, captures stdout/stderr line-by-line,
and exposes status via a polling endpoint. Thread-safe.

The active-runs table is bounded by a small LRU cap so a long-running app
session does not leak memory. The Popen handle is retained so the runner
can cancel a running stage on request.
"""

import os
import subprocess
import threading
import uuid
from collections import OrderedDict
from pathlib import Path

from zsg import PKG_DIR, PROJECT_ROOT

MAX_RUNS = 50          # cap on retained run records (LRU eviction)
TAIL_LINES = 20        # number of stdout lines kept as 'last_error' on failure

_runs: "OrderedDict[str, dict]" = OrderedDict()
_runs_lock = threading.Lock()


def _record_run(run_id: str, record: dict) -> None:
    """Insert a new run record and evict oldest entries past MAX_RUNS."""
    _runs[run_id] = record
    while len(_runs) > MAX_RUNS:
        oldest_id, oldest = _runs.popitem(last=False)
        proc = oldest.get("_proc")
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass


def start_stage(cmd: list, env: dict = None) -> str:
    """
    Spawn a subprocess running cmd. Capture stdout+stderr line-by-line.
    Returns a run_id for polling status.
    """
    run_id = str(uuid.uuid4())[:8]

    with _runs_lock:
        _record_run(run_id, {
            "status": "running",
            "lines": [],
            "returncode": None,
            "error": None,
            "last_error": None,
            "_proc": None,
        })

    # Stages run as `python -m zsg.<stage>` from the project root, so data paths
    # in cmd resolve naturally. Ensure src/ is importable for the `-m zsg.*` form.
    sub_env = dict(env or os.environ)
    src_dir = str(PKG_DIR.parent)
    existing = sub_env.get("PYTHONPATH", "")
    sub_env["PYTHONPATH"] = src_dir + (os.pathsep + existing if existing else "")

    def _worker():
        proc = None
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=sub_env,
                cwd=PROJECT_ROOT,
            )
            with _runs_lock:
                if run_id in _runs:
                    _runs[run_id]["_proc"] = proc
            for line in proc.stdout:
                with _runs_lock:
                    if run_id in _runs:
                        _runs[run_id]["lines"].append(line.rstrip())
            proc.wait()
            with _runs_lock:
                if run_id in _runs:
                    _runs[run_id]["returncode"] = proc.returncode
                    if proc.returncode == 0:
                        _runs[run_id]["status"] = "done"
                    else:
                        _runs[run_id]["status"] = "error"
                        tail = _runs[run_id]["lines"][-TAIL_LINES:]
                        _runs[run_id]["last_error"] = "\n".join(tail) or f"exit {proc.returncode}"
        except Exception as e:
            with _runs_lock:
                if run_id in _runs:
                    _runs[run_id]["status"] = "error"
                    _runs[run_id]["error"] = str(e)
                    _runs[run_id]["last_error"] = str(e)
        finally:
            # Drop the Popen handle once the process is done so it can be GC'd.
            with _runs_lock:
                if run_id in _runs:
                    _runs[run_id]["_proc"] = None

    threading.Thread(target=_worker, daemon=True).start()
    return run_id


def cancel_stage(run_id: str) -> dict:
    """Terminate a running stage. Returns the updated status."""
    with _runs_lock:
        record = _runs.get(run_id)
        if not record:
            return {"status": "not_found"}
        proc = record.get("_proc")
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError as e:
                return {"status": record.get("status"), "error": str(e)}
            record["status"] = "cancelled"
            record["last_error"] = "Cancelled by user"
        return _public_status(record)


def get_status(run_id: str) -> dict:
    """Get the current public status of a run."""
    with _runs_lock:
        record = _runs.get(run_id)
        if record is None:
            return {"status": "not_found"}
        return _public_status(record)


def get_all_runs() -> dict:
    """Get all runs (for debugging). Excludes Popen handles."""
    with _runs_lock:
        return {rid: _public_status(rec) for rid, rec in _runs.items()}


def _public_status(record: dict) -> dict:
    """Return a status dict safe to serialize (no Popen handle)."""
    return {k: v for k, v in record.items() if not k.startswith("_")}
