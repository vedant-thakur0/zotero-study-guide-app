"""
wsgi.py — gunicorn entrypoint for the Zotero Study Guide app.

Usage (gunicorn):
    PORT=8080 ZSG_METRICS_PATH=/tmp/metrics.jsonl PYTHONPATH=src \
        PURDUE_GENAI_API_KEY=<key> \
        gunicorn --bind 0.0.0.0:${PORT:-8080} --workers 2 wsgi:app

Environment variables:
    PURDUE_GENAI_API_KEY    Runtime LLM key (never bake into the image).
    ZSG_METRICS_PATH        Where metrics.jsonl is written (default: /tmp/metrics.jsonl).
    PORT                    Gunicorn bind port (gunicorn reads this from --bind; the
                            Dockerfile CMD passes it through ${PORT:-8080}).

Initialization strategy (without app_config.json):
    app_config.json is NOT present in the production image (it contains the live
    API key and per-developer project paths). This shim initialises verify.py's
    path globals to safe defaults so the server does not crash:

      * STATE_PATH    → PROJECT_ROOT/state.json   (v1 server-state, unused in client mode)
      * SECTIONS_PATH → PROJECT_ROOT/sections.json (same)
      * APP_CONFIG_PATH → PROJECT_ROOT/app_config.json (path is set; file need not exist)

    load_app_config() in verify.py already guards against a missing file (returns {}),
    and every route that touches STATE_PATH/SECTIONS_PATH checks the file before
    reading, so a missing file causes a graceful empty response, not a crash.

    The primary hosted path is the stateless /api/v2/* set + client-mode.js, which
    needs no server-side state at all.
"""

import os
import sys
from pathlib import Path

# Ensure src/ is on the path when run from repo root (not needed inside the
# Docker image where the package is installed, but harmless).
_HERE = Path(__file__).resolve().parent
_SRC = _HERE / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from zsg import PROJECT_ROOT  # noqa: E402 — must come after sys.path patch

# ---------------------------------------------------------------------------
# Path wiring — mirrors app.py:setup_flask() but tolerates a missing config.
# ---------------------------------------------------------------------------

import zsg.verify as verify  # noqa: E402

APP_CONFIG_PATH = PROJECT_ROOT / "app_config.json"
verify.APP_CONFIG_PATH = APP_CONFIG_PATH

# Read active_project if the config file happens to exist (local dev convenience).
_active_slug = None
if APP_CONFIG_PATH.exists():
    import json as _json
    try:
        _cfg = _json.loads(APP_CONFIG_PATH.read_text())
        _active_slug = _cfg.get("active_project")
    except Exception:
        pass  # corrupt or unreadable — fall through to defaults

if _active_slug:
    _proj_dir = PROJECT_ROOT / "projects" / _active_slug
    verify.STATE_PATH    = _proj_dir / "state.json"
    verify.SECTIONS_PATH = _proj_dir / "sections.json"
else:
    verify.STATE_PATH    = PROJECT_ROOT / "state.json"
    verify.SECTIONS_PATH = PROJECT_ROOT / "sections.json"

# Expose the initialized app object for gunicorn: `gunicorn wsgi:app`
app = verify.app
