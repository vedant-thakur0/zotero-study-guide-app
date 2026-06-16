"""Happy-path E2E test for client mode.

Covers the full pipeline through the browser:
  1. Boot the app in `?mode=client` so /api/* is served by the IndexedDB
     fetch interceptor in static/client-mode.js.
  2. Upload a hand-crafted Zotero export on the Pipeline tab (Stage 1).
  3. Run preprocess (Stage 2) — groups annotations into sections.
  4. Stub /api/v2/llm at the browser level (so no API key is needed), then
     generate + approve a narrative on the Narrative Review tab (Stage 3).
  5. Build the final HTML via /api/v2/build and assert the stubbed heading
     ends up in the output.

The point is to exercise the client-mode interceptor — ~900 lines of JS that
otherwise has zero automated coverage — not to re-test the Python pipeline.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# Skip the whole module cleanly if pytest-playwright isn't installed. The
# scaffolding still has documentary value.
playwright = pytest.importorskip(
    "playwright.sync_api",
    reason="Install with `pip install pytest-playwright && playwright install chromium`.",
)

FIXTURE = Path(__file__).parent / "fixtures" / "sample_zotero.html"


# ---------------------------------------------------------------------------
# Stubbed LLM response
# ---------------------------------------------------------------------------
# /api/v2/llm normally proxies to Anthropic. client-mode.js's callLLM() reads
# `result.parsed` and stores it as the narrative. We return a minimally valid
# narrative shape ({heading, intro, key_points, figures}) so the approve flow
# and the final build both work end-to-end without any real LLM call.

_STUB_LLM_PARSED = {
    "heading": "Stubbed",
    "intro": "",
    "key_points": [],
    "figures": [],
}


def _install_llm_stub(page) -> None:
    import json

    def handle_llm(route, request):  # noqa: ARG001 - playwright passes both
        body = json.dumps({
            "text": json.dumps(_STUB_LLM_PARSED),
            "parsed": _STUB_LLM_PARSED,
        })
        route.fulfill(status=200, content_type="application/json", body=body)

    page.route("**/api/v2/llm", handle_llm)


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------

def test_client_mode_full_flow(page, app_url):
    _install_llm_stub(page)

    # Capture browser console for debuggability if anything goes wrong.
    page.on("console", lambda msg: print(f"[browser:{msg.type}] {msg.text} | loc={getattr(msg, 'location', None)}"))
    page.on("pageerror", lambda e: print(f"[pageerror] {e.name}: {e.message} stack={e.stack}"))
    # Install an early error listener that captures filename + line.
    page.add_init_script("""
        window.addEventListener('error', e => {
            console.error('[early-err]', e.message, 'at', e.filename, ':', e.lineno, ':', e.colno);
        }, true);
    """)

    # Land on the page in client mode. client-mode.js installs the fetch
    # interceptor synchronously on script-load, so by the time the banner is
    # in the DOM, /api/* calls from app.js/review.js are being routed.
    page.goto(f"{app_url}/?mode=client")
    # Diagnostic: dump the relevant globals so we can see why client mode
    # didn't activate, if it didn't.
    page.wait_for_load_state("domcontentloaded")
    diag = page.evaluate("""() => ({
        hasStore: !!window.ZSGStore,
        hasClientMode: !!window.ZSGClientMode,
        lsFlag: (() => { try { return localStorage.getItem('zsg_client_mode'); } catch { return 'err'; }})(),
        urlFlag: new URLSearchParams(location.search).get('mode'),
        fetchSrc: window.fetch.toString().slice(0, 80),
    })""")
    print(f"[diag] {diag}")
    page.wait_for_selector("#client-mode-banner", timeout=5000)

    # ---- Pipeline tab: upload + preprocess ----
    # The pipeline tab renders the file input and the stage-2 button. The
    # default project is auto-created by ensureActive() inside the
    # interceptor on the first /api/state call (made by review.js's init()).
    page.click('.tab-btn[data-tab="tab-pipeline"]')

    # The pipeline tab does a /api/projects fetch + render; wait for the
    # actual file input to appear (means the "active project" branch ran).
    page.wait_for_selector("#zotero-file", timeout=5000)

    # set_input_files triggers the change handler which auto-uploads, which
    # in turn calls runStage("export") (a no-op in client mode that just
    # records a synthetic "done" run). Wait for stage 1 to be marked done
    # by polling for the run-stage-2-btn losing its `disabled` attribute.
    page.set_input_files("#zotero-file", str(FIXTURE))

    # The upload + stage-1 "export" no-op + re-render cascade is async. Wait
    # for the stage-2 button to become enabled (its `disabled` attr is set
    # based on activeProject.stages["1"]).
    page.wait_for_function(
        "() => { const b = document.querySelector('#run-stage-2-btn');"
        " return b && !b.disabled; }",
        timeout=10000,
    )

    # Stage 2: preprocess. Wait for the stage-3 button to become enabled,
    # which means /api/v2/sections returned and stages["2"] is true.
    page.click("#run-stage-2-btn")
    page.wait_for_function(
        "() => { const b = document.querySelector('#run-stage-3-btn');"
        " return b && !b.disabled; }",
        timeout=15000,
    )

    # ---- Narrative Review tab: generate + approve ----
    # review.js loads appState once at DOMContentLoaded — before any sections
    # exist — so clicking the tab alone would render an empty-state with the
    # stale order. Re-run init() so it re-fetches /api/state (now intercepted
    # by client-mode.js with sections populated) and re-renders. This mirrors
    # what the "Open Review" button does in normal use after stage 3, except
    # we don't gate on stage 3 because we want to test the inline-gen path
    # (single section) rather than the bulk generate_narrative runner.
    page.evaluate("async () => { if (typeof init === 'function') await init(); }")
    page.click('.tab-btn[data-tab="tab-narrative"]')

    # renderNarrativeTab() emits one .section-card per section, each with
    # a .generate-btn (text "Generate with LLM"). Click the first.
    page.wait_for_selector(".section-card", timeout=5000)
    page.locator(".generate-btn.inline-gen").first.click()

    # After generation, the card re-renders. The approve button now exists.
    page.wait_for_selector(".approve-btn", timeout=10000)
    page.locator(".approve-btn").first.click()

    # The approve handler flips data-approved to "true" in place. Wait for it.
    page.wait_for_function(
        "() => document.querySelector('.approve-btn[data-approved=\"true\"]') !== null",
        timeout=5000,
    )

    # ---- Final assertion: build via /api/v2/build ----
    # Pull the current state from the interceptor's in-memory cache and POST
    # it to the real /api/v2/build endpoint (which is NOT intercepted —
    # /api/v2/* always hits the real server, see client-mode.js line ~633).
    html = page.evaluate(
        """async () => {
            const state = window.ZSGClientMode && window.ZSGClientMode.state();
            const r = await fetch('/api/v2/build', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({state: state, title: 'T', theme: 'light'})
            });
            const j = await r.json();
            return j.html;
        }"""
    )

    assert html, "build returned empty HTML"
    assert html.startswith("<!DOCTYPE html>"), (
        f"build did not return a full HTML document: {html[:120]!r}"
    )
    assert "Stubbed" in html, (
        "stubbed narrative heading missing from build output — the approve "
        "flow may not have persisted, or the build did not pick up the "
        "approved section."
    )
