"""
bench_pipeline.py — End-to-end pipeline run on a real Zotero export, with
per-stage wall-clock timing, plus a battery of edge-case probes.

Usage:
    python tests/bench_pipeline.py
    python tests/bench_pipeline.py --llm-calls 1   # do 1 real narrative call
    python tests/bench_pipeline.py --llm-calls 0   # skip the LLM entirely

The LLM-bypass cache is disabled for the run: we instantiate a fresh
generate.call_llm() per call rather than reusing any cached client state,
and we deliberately do NOT pass cache_control on the prompt template so
the Anthropic prompt cache does not engage (matters only if you switch
provider=anthropic). For openai-compatible / Purdue GenAI providers there
is no cache to disable.

Sample doc precedence: projects/civil_rights_m7/CIVIL RIGHTS M7.html (633 KB)
falls back to ../Annotations.html (~18 KB) if the larger isn't present.
"""

import argparse
import concurrent.futures
import gc
import json
import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))


# ────────────────────────────────────────────────────────────────────────
# Timing helper
# ────────────────────────────────────────────────────────────────────────

class Stopwatch:
    def __init__(self, label):
        self.label = label
        self.t0 = None
        self.elapsed = None

    def __enter__(self):
        gc.collect()
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, *exc):
        self.elapsed = time.perf_counter() - self.t0


def fmt_ms(seconds):
    if seconds is None:
        return "      —"
    return f"{seconds * 1000:>6.1f} ms" if seconds < 1 else f"{seconds:>6.2f} s "


def header(text):
    print()
    print("─" * 78)
    print(f" {text}")
    print("─" * 78)


# ────────────────────────────────────────────────────────────────────────
# Edge case probes
# ────────────────────────────────────────────────────────────────────────

def probe_edge_cases():
    import zsg.export as export
    import zsg.preprocess as preprocess
    import zsg.build_guide as build_guide
    from zsg.json_repair import attempt_repair

    findings = []  # (severity, area, finding)

    def add(sev, area, msg):
        findings.append((sev, area, msg))

    # ── export: empty / malformed / unicode / huge instruction notes ──
    try:
        assert export.from_zotero_html_str("") == []
        assert export.from_zotero_html_str("   \n\n  ") == []
    except Exception as e:
        add("BUG", "export", f"empty input raised: {e}")

    # Non-Zotero >1KB should raise EmptyExportError
    try:
        export.from_zotero_html_str("<html><body>" + ("x" * 2000) + "</body></html>")
        add("BUG", "export", "non-Zotero >1KB HTML did NOT raise EmptyExportError")
    except export.EmptyExportError:
        pass

    # Malformed rgba — should fall through to yellow with a warning
    bad_rgba = """<p><span class="highlight"><span style="background-color: rgba(not, a, color);">x</span></span>
    <span class="citation">(<span class="citation-item">Yoo, p. 1</span>)</span></p>"""
    anns = export.from_zotero_html_str(bad_rgba)
    if not anns:
        add("BUG", "export", "malformed rgba produced zero annotations (should default to yellow)")
    elif anns[0]["color"] != "yellow":
        add("INFO", "export", f"malformed rgba → color={anns[0]['color']!r}")

    # Unicode in highlight + note
    uni = """<p><span class="highlight"><span style="background-color: rgba(255, 212, 0, 0.5);">日本語テスト — 中文 — Ω</span></span>
    <span class="citation">(<span class="citation-item">Tang, p. 12</span>)</span> note: à é ñ 🌍</p>"""
    anns = export.from_zotero_html_str(uni)
    if not anns:
        add("BUG", "export", "unicode highlight produced zero annotations")
    elif "日本語" not in anns[0]["text"]:
        add("BUG", "export", "unicode text not preserved")
    elif "🌍" not in anns[0]["instructor_note"]:
        add("INFO", "export", "emoji in instructor_note lost")

    # HTML entities in text
    entities = """<p><span class="highlight"><span style="background-color: rgba(255, 212, 0, 0.5);">A &amp; B &lt;X&gt;</span></span>
    <span class="citation">(<span class="citation-item">Yoo, p. 1</span>)</span></p>"""
    anns = export.from_zotero_html_str(entities)
    if anns and ("&amp;" in anns[0]["text"] or "&lt;" in anns[0]["text"]):
        add("WARN", "export", "HTML entities not decoded in highlight text")

    # Non-numeric page label
    non_num = """<p><span class="highlight"><span style="background-color: rgba(255, 212, 0, 0.5);">x</span></span>
    <span class="citation">(<span class="citation-item">Yoo, p. vii</span>)</span></p>"""
    anns = export.from_zotero_html_str(non_num)
    if anns and anns[0]["page"] != "vii":
        add("INFO", "export", f"non-numeric page parsed as {anns[0]['page']!r}")

    # Citation missing "p. N"
    no_page = """<p><span class="highlight"><span style="background-color: rgba(255, 212, 0, 0.5);">x</span></span>
    <span class="citation">(<span class="citation-item">Yoo</span>)</span></p>"""
    anns = export.from_zotero_html_str(no_page)
    if not anns:
        add("BUG", "export", "missing page citation dropped the annotation")
    elif anns[0].get("source_document") != "Yoo":
        add("WARN", "export", "page-less citation lost source_document")

    # Underline annotations (text-decoration-color)
    underline = """<p><span class="underline"><u style="text-decoration-color: rgba(255, 102, 102, 1);">underlined claim</u></span>
    <span class="citation">(<span class="citation-item">Yoo, p. 9</span>)</span></p>"""
    anns = export.from_zotero_html_str(underline)
    if not anns:
        add("BUG", "export", "underline-style annotation not parsed")

    # Very long instructor note
    big_note = "lorem ipsum " * 5000  # ~60 KB
    long_p = f"""<p><span class="highlight"><span style="background-color: rgba(255, 212, 0, 0.5);">x</span></span>
    <span class="citation">(<span class="citation-item">Yoo, p. 1</span>)</span> {big_note}</p>"""
    anns = export.from_zotero_html_str(long_p)
    if not anns or len(anns[0]["instructor_note"]) < 1000:
        add("INFO", "export", "very long instructor_note truncated or dropped")

    # CSV with unicode + quoted commas
    csv_data = (
        "Annotation Text,Color,Page,Comment,Author\n"
        '"Hello, world",yellow,5,"with comma, see",Tang\n'
        '"日本語",red,vii,"",山田\n'
    )
    anns = export.parse_export_str("csv", csv_data)
    if len(anns) != 2:
        add("BUG", "export", f"csv with quoted commas: expected 2 anns, got {len(anns)}")

    # JSON with extra unknown keys (should pass through)
    extra = json.dumps([{"id": "x1", "text": "hello", "color": "yellow", "page": 1,
                        "instructor_note": "", "source_document": "Y", "extra_key": "ignored"}])
    anns = export.parse_export_str("json", extra)
    if not anns:
        add("BUG", "export", "json pipeline-native parse failed")

    # ── preprocess: pathological page gaps + non-numeric + duplicate IDs ──
    page_window = preprocess.PAGE_WINDOW_DEFAULT

    # All on the same page
    same_page = [
        {"id": f"a{i}", "text": "x", "color": "yellow", "page": 5,
         "instructor_note": "", "source_document": "Y"} for i in range(10)
    ]
    secs = preprocess.preprocess(same_page, "proximity", page_window)
    if len(secs) != 1:
        add("WARN", "preprocess", f"all-same-page: {len(secs)} sections (expected 1)")

    # Huge gaps
    far = [
        {"id": "a", "text": "x", "color": "yellow", "page": 1, "instructor_note": "", "source_document": ""},
        {"id": "b", "text": "y", "color": "yellow", "page": 999, "instructor_note": "", "source_document": ""},
    ]
    secs = preprocess.preprocess(far, "proximity", page_window)
    if len(secs) != 2:
        add("INFO", "preprocess", f"huge gap produced {len(secs)} sections")
    elif any(s["annotation_count"] < 2 for s in secs):
        # Both are singletons (<MIN_SECTION_SIZE=2) → second gets merged into first
        add("BUG", "preprocess", "MIN_SECTION_SIZE merges large-gap singletons into prior section (loses separation)")

    # Non-numeric pages all become page 0 → pile up
    mixed = [
        {"id": "a", "text": "x", "color": "yellow", "page": "vii", "instructor_note": "", "source_document": ""},
        {"id": "b", "text": "y", "color": "yellow", "page": "intro", "instructor_note": "", "source_document": ""},
        {"id": "c", "text": "z", "color": "yellow", "page": 5, "instructor_note": "", "source_document": ""},
    ]
    secs = preprocess.preprocess(mixed, "proximity", page_window)
    if len(secs) == 1:
        add("BUG", "preprocess", "non-numeric pages (vii/intro) fused into page-5 section")

    # Duplicate ids — preprocess doesn't dedupe
    dup = [
        {"id": "same", "text": f"v{i}", "color": "yellow", "page": i,
         "instructor_note": "", "source_document": ""} for i in range(3)
    ]
    secs = preprocess.preprocess(dup, "proximity", page_window)
    flat = [a for s in secs for a in s["source_annotations"]]
    if len(flat) == 3:
        add("INFO", "preprocess", "duplicate annotation ids passed through unchanged (LLM will see them)")

    # Empty annotations list
    secs = preprocess.preprocess([], "auto", page_window)
    if secs != []:
        add("BUG", "preprocess", f"empty input returned {secs}")

    # Tags strategy with NO tags falls through to proximity
    no_tag = [
        {"id": "a", "text": "x", "color": "yellow", "page": 1, "instructor_note": "", "source_document": ""},
    ]
    secs = preprocess.preprocess(no_tag, "tags", page_window)
    if not secs:
        add("BUG", "preprocess", "tags fallback dropped annotations")

    # Tag-based: untagged annotations should inherit 'general'
    tagged = [
        {"id": "a", "text": "x", "color": "yellow", "page": 1, "instructor_note": "", "source_document": ""},
        {"id": "b", "text": "y", "color": "yellow", "page": 2, "instructor_note": "#Reconstruction", "source_document": ""},
        {"id": "c", "text": "z", "color": "yellow", "page": 3, "instructor_note": "", "source_document": ""},
    ]
    secs = preprocess.preprocess(tagged, "tags", page_window)
    sids = [s["section_id"] for s in secs]
    if "general" in sids:
        add("WARN", "preprocess", "first untagged annotations land in implicit 'general' bucket")

    # ── build_guide: empty state, XSS in user fields, missing source_annotations ──
    empty_state = {"sections": {}, "section_order": []}
    html = build_guide.build(empty_state, "Test", "light")
    if not html or not html.startswith("<!DOCTYPE html>"):
        add("BUG", "build_guide", "empty state produced non-HTML output")

    # XSS attempt in heading — verify the active <script> tag is escaped
    xss_state = {
        "sections": {"s1": {
            "narrative_approved": True,
            "narrative": {
                "heading": "<script>alert('xss')</script>",
                "intro": 'hi <img src=x onerror=alert(1)>',
                "key_points": [{"term": "T<script>", "explanation": '<b onmouseover="bad">bold</b>'}],
                "figures": [],
            },
            "source_annotations": [],
        }},
        "section_order": ["s1"],
        "global_settings": {},
    }
    html = build_guide.build(xss_state, "X", "light")
    # Look for the raw payload as an executable tag (not the escaped form)
    raw_payloads = [
        "<script>alert('xss')</script>",
        "<img src=x onerror=alert(1)>",
        '<b onmouseover="bad">bold</b>',
        "T<script>",
    ]
    for raw in raw_payloads:
        if raw in html:
            add("BUG", "build_guide", f"XSS payload not escaped in output: {raw!r}")
            break
    else:
        if "&lt;script&gt;" not in html:
            add("WARN", "build_guide",
                "XSS payload neither rendered nor escaped — possibly silently dropped")

    # Section with narrative_approved but no narrative dict (orphan)
    orphan = {
        "sections": {"s1": {"narrative_approved": True, "source_annotations": []}},
        "section_order": ["s1"],
        "global_settings": {},
    }
    try:
        html = build_guide.build(orphan, "Orphan", "light")
        if "s1" not in html:
            add("INFO", "build_guide", "orphan section silently omitted")
    except Exception as e:
        add("BUG", "build_guide", f"orphan section crashed build: {e}")

    # Theme that doesn't exist
    html = build_guide.build({"sections": {}, "section_order": []}, "T", "invalid-theme")
    if 'class="theme-invalid-theme"' not in html:
        add("INFO", "build_guide", "invalid theme silently substituted")

    # ── json_repair: subtle inputs ──
    cases = [
        ('{"k": True}',         {"k": True}),   # python literal True
        ('{"k": None}',         {"k": None}),
        ('{"k": 1,}',           {"k": 1}),       # trailing comma
        ('```json\n{"k":1}\n```', {"k": 1}),    # fenced
        ('here is your json: {"k":2}', {"k": 2}),
        ('{"k":1} also some trailing commentary', {"k": 1}),
    ]
    for raw, expected in cases:
        try:
            parsed, _ = attempt_repair(raw)
        except Exception as e:
            add("BUG", "json_repair", f"failed on {raw[:40]!r}: {e}")
            continue
        if parsed != expected:
            add("BUG", "json_repair", f"mismatch for {raw[:40]!r}: got {parsed!r}")

    # Unicode string containing "True" should NOT be touched
    raw = '{"sentence": "True love wins"}'
    parsed, _ = attempt_repair(raw)
    if parsed != {"sentence": "True love wins"}:
        add("BUG", "json_repair", "rewrote literal inside a string")

    # ── More targeted probes ──────────────────────────────────────────
    # Empty highlight text should be dropped (no zero-length annotations)
    blank = """<p><span class="highlight"><span style="background-color: rgba(255, 212, 0, 0.5);">   </span></span>
    <span class="citation">(<span class="citation-item">Yoo, p. 5</span>)</span></p>"""
    anns = export.from_zotero_html_str(blank)
    if anns and not anns[0]["text"].strip():
        add("WARN", "export", "kept annotation with empty highlight text")

    # color: null in pipeline-native JSON should normalize to yellow
    null_color = json.dumps([{"id": "x", "text": "t", "color": None, "page": 1,
                              "instructor_note": "", "source_document": ""}])
    try:
        anns = export.parse_export_str("json", null_color)
        if anns and anns[0]["color"] != "yellow":
            add("WARN", "export", f"null color → {anns[0]['color']!r} (expected yellow)")
    except Exception as e:
        add("BUG", "export", f"null color crashed JSON parser: {e}")

    # Unknown color name should default to yellow with a warning
    unknown_color = "<p><span class=\"highlight\"><span style=\"background-color: chartreuse;\">x</span></span>" \
                    "<span class=\"citation\">(<span class=\"citation-item\">Yoo, p. 1</span>)</span></p>"
    anns = export.from_zotero_html_str(unknown_color)
    if anns and anns[0]["color"] != "yellow":
        add("WARN", "export", f"unknown color → {anns[0]['color']!r}")

    # Duplicate section_order entries should still build cleanly
    dup_order = {
        "sections": {"s1": {"narrative_approved": True,
                            "narrative": {"heading": "H", "intro": "I",
                                          "key_points": [], "figures": []},
                            "source_annotations": []}},
        "section_order": ["s1", "s1", "s1"],
        "global_settings": {},
    }
    try:
        html = build_guide.build(dup_order, "T", "light")
        if html.count("section-heading") > 1:
            add("WARN", "build_guide", "duplicate section_order entries render section N times")
    except Exception as e:
        add("BUG", "build_guide", f"duplicate section_order crashed build: {e}")

    # Non-ASCII title — make sure it's escaped not encoded weirdly
    html = build_guide.build({"sections": {}, "section_order": []}, "日本語 & <test>", "light")
    if "日本語" not in html:
        add("WARN", "build_guide", "non-ASCII title not preserved in output")
    if "& <test>" in html:
        add("BUG", "build_guide", "ampersand/angle-brackets in title not escaped")

    # Quiz block with no 'questions' key
    quiz_orphan = {
        "sections": {"s1": {
            "narrative_approved": True,
            "narrative": {"heading": "H", "intro": "I", "key_points": [], "figures": []},
            "quiz": {"section_id": "s1"},   # no 'questions' key
            "source_annotations": [],
        }},
        "section_order": ["s1"],
        "global_settings": {},
    }
    try:
        html = build_guide.build(quiz_orphan, "T", "light")
        # Look for an actual rendered quiz <div>, not the CSS class definition.
        if '<div class="block block-quiz"' in html:
            add("INFO", "build_guide", "quiz block rendered even with no questions")
    except Exception as e:
        add("BUG", "build_guide", f"quiz with no questions crashed build: {e}")

    # Pipeline state with no section_order (only the sections dict)
    no_order = {
        "sections": {"s1": {
            "narrative_approved": True,
            "narrative": {"heading": "H", "intro": "", "key_points": [], "figures": []},
            "source_annotations": [],
        }},
        "global_settings": {},
    }
    try:
        html = build_guide.build(no_order, "T", "light")
        if "H</h2>" not in html and "H&" not in html:
            add("WARN", "build_guide", "section dropped when section_order missing")
    except Exception as e:
        add("BUG", "build_guide", f"missing section_order crashed build: {e}")

    return findings


# ────────────────────────────────────────────────────────────────────────
# End-to-end run on the largest available doc
# ────────────────────────────────────────────────────────────────────────

def find_sample_doc():
    """Pick the largest available HTML that is actually a Zotero note export."""
    import zsg.export as export
    candidates = [
        ROOT / "projects" / "civil_rights_m7" / "CIVIL RIGHTS M7.html",
        ROOT.parent / "Annotations.html",
    ]
    # Filter by size descending, then verify each is parseable
    candidates = [p for p in candidates if p.exists()]
    candidates.sort(key=lambda p: p.stat().st_size, reverse=True)
    for p in candidates:
        try:
            anns = export.from_zotero_html_str(p.read_text(encoding="utf-8"))
            if anns:
                return p
        except export.EmptyExportError:
            print(f"  (skipping {p.name}: not a Zotero note export)")
    raise SystemExit("No usable Zotero HTML found")


def _llm_api_key_configured(cfg: dict) -> bool:
    """True iff the configured provider has the credentials it needs."""
    provider = cfg.get("provider")
    if provider == "anthropic":
        return bool(cfg.get("api_key") or os.environ.get("ANTHROPIC_API_KEY"))
    if provider == "purdue_genai":
        return bool(cfg.get("api_key") or os.environ.get("PURDUE_GENAI_API_KEY"))
    if provider in ("openai",):
        return bool(cfg.get("api_key") or os.environ.get("OPENAI_API_KEY"))
    # ollama / vllm / lmstudio assume a local server — treat as "configured".
    return True


def run_concurrency_bench(n: int, concurrency: int, page_window_override=None):
    """Compare serial vs parallel LLM throughput on `n` real sections.

    Only runs when the configured provider has credentials — otherwise prints
    a skip note and returns. The rest of the bench's behavior is unchanged.
    """
    import zsg.export as export
    import zsg.preprocess as preprocess
    import zsg.generate as generate

    cfg = generate.load_llm_config()
    if not _llm_api_key_configured(cfg):
        print(f"  (skipping LLM concurrency bench — provider {cfg.get('provider')!r} "
              f"has no API key configured)")
        return

    doc_path = find_sample_doc()
    print(f"Sample document: {doc_path.name}")
    annotations = export.from_zotero_html_str(doc_path.read_text(encoding="utf-8"))
    page_window = page_window_override or preprocess.PAGE_WINDOW_DEFAULT
    sections = preprocess.preprocess(annotations, "auto", page_window)[:n]
    if len(sections) < n:
        print(f"  (only {len(sections)} sections available, requested {n})")
    n = len(sections)
    if n == 0:
        print("  (no sections to benchmark)")
        return

    print(f"  LLM provider: {cfg.get('provider')} / {cfg.get('model')}")
    print(f"  Sections:     {n}   Concurrency:  {concurrency}")
    print()

    # ── Serial baseline ────────────────────────────────────────────────
    per_call_times = []
    t_serial_0 = time.perf_counter()
    for s in sections:
        prompt = generate.build_narrative_prompt(s)
        t0 = time.perf_counter()
        try:
            generate.call_llm(prompt, cfg)
            per_call_times.append(time.perf_counter() - t0)
        except Exception as e:
            print(f"    serial[{s['section_id']}] FAILED: {type(e).__name__}: {e}")
            per_call_times.append(None)
    t_serial = time.perf_counter() - t_serial_0
    good = [t for t in per_call_times if t is not None]
    per_call_serial = statistics.mean(good) if good else 0.0
    print(f"  llm/serial  n={n}  total={t_serial:.2f}s"
          f"  per-call={per_call_serial:.2f}s")

    # ── Parallel ───────────────────────────────────────────────────────
    par_per_call = []
    par_lock = __import__("threading").Lock()

    def _call(section):
        prompt = generate.build_narrative_prompt(section)
        t0 = time.perf_counter()
        try:
            generate.call_llm(prompt, cfg)
            dt = time.perf_counter() - t0
            with par_lock:
                par_per_call.append(dt)
        except Exception as e:
            with par_lock:
                par_per_call.append(None)
            print(f"    concur[{section['section_id']}] FAILED: {type(e).__name__}: {e}")

    t_par_0 = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        list(pool.map(_call, sections))
    t_par = time.perf_counter() - t_par_0
    good_par = [t for t in par_per_call if t is not None]
    per_call_par = statistics.mean(good_par) if good_par else 0.0
    speedup = (t_serial / t_par) if t_par > 0 else float("nan")
    print(f"  llm/concur  n={n}  total={t_par:.2f}s"
          f"  per-call mean={per_call_par:.2f}s, wall={t_par:.2f}s"
          f"  speedup={speedup:.2f}x")


def run_pipeline(llm_calls, page_window_override=None):
    import zsg.export as export
    import zsg.preprocess as preprocess
    import zsg.generate as generate
    import zsg.build_guide as build_guide

    doc_path = find_sample_doc()
    print(f"Sample document: {doc_path.relative_to(ROOT.parent) if doc_path.is_relative_to(ROOT.parent) else doc_path}")
    print(f"Size: {doc_path.stat().st_size:,} bytes")
    print()

    # ── 1. Parse ───────────────────────────────────────────────────────
    content = doc_path.read_text(encoding="utf-8")
    with Stopwatch("parse") as t_parse:
        annotations = export.from_zotero_html_str(content)
    print(f"  parse     {fmt_ms(t_parse.elapsed)}   → {len(annotations)} annotations")

    # Color distribution
    colors = {}
    for a in annotations:
        colors[a["color"]] = colors.get(a["color"], 0) + 1
    color_str = ", ".join(f"{k}={v}" for k, v in sorted(colors.items()))
    print(f"            colors: {color_str}")

    # ── 2. Preprocess ──────────────────────────────────────────────────
    page_window = page_window_override or preprocess.PAGE_WINDOW_DEFAULT
    with Stopwatch("preprocess") as t_pp:
        sections = preprocess.preprocess(annotations, "auto", page_window)
    print(f"  preprocess{fmt_ms(t_pp.elapsed)}   → {len(sections)} sections (page_window={page_window})")
    for s in sections[:5]:
        pr = s.get("page_range", {})
        print(f"            {s['section_id']:<40} ann={s['annotation_count']:>3}"
              f"  p.{pr.get('start')}–{pr.get('end')}")
    if len(sections) > 5:
        print(f"            ... and {len(sections) - 5} more")

    # ── 3. LLM narrative (optional + capped) ──────────────────────────
    state = {"sections": {}, "section_order": [s["section_id"] for s in sections]}
    llm_timings = []

    if llm_calls > 0:
        cfg = generate.load_llm_config()
        print()
        print(f"  LLM provider: {cfg.get('provider')} / {cfg.get('model')}")
        print(f"  max_tokens:   {cfg.get('max_tokens')}")
        print(f"  cache:        no Anthropic ephemeral cache hit possible "
              f"(provider != anthropic)" if cfg.get("provider") != "anthropic"
              else "  cache:        first call is cold (no warm-up)")
        print()

        for i, section in enumerate(sections[:llm_calls]):
            sid = section["section_id"]
            prompt = generate.build_narrative_prompt(section)
            try:
                with Stopwatch(f"llm[{sid}]") as t_llm:
                    raw = generate.call_llm(prompt, cfg)
                from zsg.json_repair import attempt_repair
                parsed, repaired = attempt_repair(raw)
                state["sections"][sid] = {
                    "narrative": parsed,
                    "narrative_approved": True,  # auto-approve for the timing run
                    "source_annotations": section["source_annotations"],
                }
                llm_timings.append(t_llm.elapsed)
                key_points = len(parsed.get("key_points", [])) if isinstance(parsed, dict) else 0
                print(f"  llm[{i+1}/{llm_calls}] {fmt_ms(t_llm.elapsed)}   {sid}"
                      f"   key_points={key_points}, repaired={repaired}")
            except generate.TruncationError as e:
                print(f"  llm[{i+1}/{llm_calls}] TRUNCATED   {sid}: {e}")
                llm_timings.append(None)
            except Exception as e:
                print(f"  llm[{i+1}/{llm_calls}] FAILED      {sid}: {type(e).__name__}: {e}")
                llm_timings.append(None)
    else:
        print()
        print("  (skipping LLM stage — pass --llm-calls N to enable)")
        # Hydrate with gold-standard state so build_guide has something to render
        gold_state_path = ROOT / "projects" / "civil_rights_m7" / "state.json"
        if gold_state_path.exists():
            print(f"  build:    using gold-standard state {gold_state_path.name}")
            state = json.loads(gold_state_path.read_text())

    # ── 4. Build ───────────────────────────────────────────────────────
    title = "Performance Benchmark"
    with Stopwatch("build") as t_build:
        html = build_guide.build(state, title, "light")
    approved = sum(1 for s in state.get("sections", {}).values() if s.get("narrative_approved"))
    print()
    print(f"  build     {fmt_ms(t_build.elapsed)}   → {len(html):,} bytes HTML"
          f", {approved} approved sections")

    # ── Totals ─────────────────────────────────────────────────────────
    print()
    print("─" * 78)
    print("  Wall clock")
    print(f"    parse      {fmt_ms(t_parse.elapsed)}")
    print(f"    preprocess {fmt_ms(t_pp.elapsed)}")
    if llm_timings:
        good = [t for t in llm_timings if t is not None]
        if good:
            print(f"    llm/call   mean={fmt_ms(statistics.mean(good))}"
                  f"  min={fmt_ms(min(good))}  max={fmt_ms(max(good))}  n={len(good)}")
            extrap = statistics.mean(good) * len(sections)
            print(f"    llm/full   extrapolated for {len(sections)} sections: {fmt_ms(extrap)}"
                  f" (1×narrative). ~2× including quiz.")
    print(f"    build      {fmt_ms(t_build.elapsed)}")
    deterministic_total = (t_parse.elapsed or 0) + (t_pp.elapsed or 0) + (t_build.elapsed or 0)
    print(f"    determ.    {fmt_ms(deterministic_total)}  (parse+preprocess+build)")


# ────────────────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Benchmark + edge-case probe.")
    parser.add_argument("--llm-calls", type=int, default=0,
                        help="Number of real LLM narrative calls to run (0 = skip).")
    parser.add_argument("--probe-only", action="store_true",
                        help="Skip pipeline run, only report edge-case findings.")
    parser.add_argument("--page-window", type=int, default=None,
                        help="Override page-window (smaller → more sections).")
    parser.add_argument("--concurrency", type=int, default=None,
                        help="If set, run a serial-vs-parallel LLM throughput "
                             "comparison on --llm-calls sections at this "
                             "concurrency. Requires API key.")
    args = parser.parse_args()

    header("EDGE-CASE PROBES")
    findings = probe_edge_cases()
    if not findings:
        print("  (no findings)")
    else:
        for sev, area, msg in findings:
            tag = {"BUG": "❌ BUG ", "WARN": "⚠ WARN", "INFO": "ℹ INFO"}.get(sev, sev)
            print(f"  {tag}  [{area:<11}] {msg}")
    bugs = sum(1 for f in findings if f[0] == "BUG")
    warns = sum(1 for f in findings if f[0] == "WARN")
    infos = sum(1 for f in findings if f[0] == "INFO")
    print()
    print(f"  Summary: {bugs} bug(s), {warns} warning(s), {infos} note(s)")

    if not args.probe_only:
        header("END-TO-END RUN")
        run_pipeline(args.llm_calls, page_window_override=args.page_window)

    if args.concurrency:
        n = max(args.llm_calls, args.concurrency)
        header("LLM SERIAL vs CONCURRENT")
        run_concurrency_bench(n=n, concurrency=args.concurrency,
                              page_window_override=args.page_window)


if __name__ == "__main__":
    main()
