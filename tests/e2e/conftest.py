"""Shared fixtures for the end-to-end Playwright suite.

The `page` fixture is provided by the pytest-playwright plugin — we deliberately
do not redefine it. The only fixture defined here is `app_url`, which boots the
Flask app on a free localhost port for the duration of the test session.

The app is run in a background thread via werkzeug's `make_server` (rather than
spawning a subprocess) so the test driver shares the parent process's stdout
and can be torn down deterministically. No API key is required: the test stubs
the `/api/v2/llm` endpoint at the browser level via `page.route`, so the Flask
process never makes a real upstream LLM call.
"""

from __future__ import annotations

import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

# Make the zsg package importable so `from zsg.verify import app` works
# regardless of where pytest is invoked from.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _free_port() -> int:
    """Return a port that is free at the moment of the call.

    There is an inherent TOCTOU window between releasing the socket and the
    Flask server binding to it, but for a single-process test session this is
    fine in practice.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _wait_for_ready(url: str, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    last_err: Exception | None = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as r:
                if r.status == 200:
                    return
        except Exception as e:  # noqa: BLE001 - any failure is "not ready yet"
            last_err = e
            time.sleep(0.1)
    raise RuntimeError(f"Flask app at {url} did not become ready in {timeout}s "
                       f"(last error: {last_err!r})")


@pytest.fixture(scope="session")
def app_url() -> str:
    """Boot the Flask app on a free port and yield its base URL.

    Uses werkzeug's threaded dev server. Teardown calls `server.shutdown()`,
    which unblocks `serve_forever` and lets the thread exit cleanly.
    """
    from werkzeug.serving import make_server
    from zsg.verify import app  # noqa: WPS433 - intentional late import

    port = _free_port()
    server = make_server("127.0.0.1", port, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    base = f"http://127.0.0.1:{port}"
    try:
        _wait_for_ready(f"{base}/", timeout=10.0)
        yield base
    finally:
        server.shutdown()
        thread.join(timeout=5.0)
