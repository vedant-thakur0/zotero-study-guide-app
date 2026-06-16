/* client-mode.js — Route the legacy UI through IndexedDB + v2 endpoints.
 *
 * This module installs a fetch() interceptor that recognizes the legacy
 * `/api/state`, `/api/section/...`, `/api/section_order`, `/api/config`,
 * `/api/projects`, `/api/export/<x>`, and `/api/section/<id>/generate_<kind>` URLs
 * and serves them from ZSGStore (IndexedDB) + the stateless `/api/v2/*`
 * endpoints. The existing review.js and app.js are untouched and remain
 * usable in the legacy server-state mode.
 *
 * Activation: client mode is on whenever `localStorage.zsg_client_mode === "1"`
 * or the URL contains `?mode=client`. Once set, it persists across reloads.
 * To exit client mode: `localStorage.removeItem("zsg_client_mode")`.
 *
 * Why an interceptor rather than route-by-route changes? It localizes the
 * migration to one file and makes the legacy/client toggle a single switch.
 */

"use strict";

(function () {
  const URL_FLAG = new URLSearchParams(window.location.search).get("mode");
  if (URL_FLAG === "client") {
    try { localStorage.setItem("zsg_client_mode", "1"); } catch (e) {}
  } else if (URL_FLAG === "server") {
    try { localStorage.removeItem("zsg_client_mode"); } catch (e) {}
  }

  function isEnabled() {
    try { return localStorage.getItem("zsg_client_mode") === "1"; } catch (e) { return false; }
  }

  if (!isEnabled() || !window.ZSGStore) return;

  // ────────────────────────────────────────────────────────────────────────
  // JSON response helpers
  // ────────────────────────────────────────────────────────────────────────

  function jsonResponse(obj, status = 200) {
    return new Response(JSON.stringify(obj), {
      status,
      headers: { "Content-Type": "application/json" },
    });
  }

  function err(msg, status = 500) {
    return jsonResponse({ error: msg }, status);
  }

  // ────────────────────────────────────────────────────────────────────────
  // Active-project state cache (keeps the current project loaded in memory)
  // ────────────────────────────────────────────────────────────────────────

  let _activeId = null;
  let _state = null;     // cached state for the active project
  let _config = null;    // cached config

  async function ensureActive() {
    if (_activeId && _state) return;
    let proj = await ZSGStore.getActive();
    if (!proj) {
      proj = await ZSGStore.createProject("Untitled project");
      await ZSGStore.setActive(proj.id);
    }
    _activeId = proj.id;
    _state = await ZSGStore.loadState(_activeId);
  }

  async function persist() {
    if (_activeId && _state) await ZSGStore.saveState(_activeId, _state);
  }

  async function ensureConfig() {
    if (_config) return _config;
    _config = await ZSGStore.getConfig();
    return _config;
  }

  // ────────────────────────────────────────────────────────────────────────
  // Route handlers — mirror the legacy server responses
  // ────────────────────────────────────────────────────────────────────────

  const routes = [];

  function route(method, pattern, handler) {
    routes.push({ method, pattern, handler });
  }

  // Match URL paths. Patterns may include :param segments.
  function matchRoute(method, urlPath) {
    for (const r of routes) {
      if (r.method !== method) continue;
      const pat = r.pattern.split("/").filter(Boolean);
      const segs = urlPath.split("/").filter(Boolean);
      if (pat.length !== segs.length) continue;
      const params = {};
      let ok = true;
      for (let i = 0; i < pat.length; i++) {
        if (pat[i].startsWith(":")) {
          params[pat[i].slice(1)] = decodeURIComponent(segs[i]);
        } else if (pat[i] !== segs[i]) {
          ok = false;
          break;
        }
      }
      if (ok) return { handler: r.handler, params };
    }
    return null;
  }

  // ── State ──────────────────────────────────────────────────────────────

  route("GET", "/api/state", async () => {
    await ensureActive();

    // Hydrate from cached sections_raw if needed
    const sectionsRaw = _state.sections_raw || [];
    const knownIds = sectionsRaw.map((s) => s.section_id);

    // Ensure section_order is complete
    if (!Array.isArray(_state.section_order)) _state.section_order = [];
    for (const sid of knownIds) {
      if (!_state.section_order.includes(sid)) _state.section_order.push(sid);
    }

    // Hydrate per-section defaults
    const idx = Object.fromEntries(sectionsRaw.map((s) => [s.section_id, s]));
    for (const sid of _state.section_order) {
      const sec = _state.sections[sid] || {};
      const src = idx[sid] || {};
      if (sec.source_annotations === undefined) sec.source_annotations = src.source_annotations || [];
      if (sec.page_range === undefined) sec.page_range = src.page_range || {};
      if (sec.narrative_approved === undefined) sec.narrative_approved = false;
      _state.sections[sid] = sec;
    }
    await persist();
    return jsonResponse(_state);
  });

  route("POST", "/api/state", async (_, req) => {
    await ensureActive();
    const body = await req.json();
    _state = { ..._state, ...body };
    await persist();
    return jsonResponse({ ok: true });
  });

  route("PUT", "/api/section/:id/narrative", async (params, req) => {
    await ensureActive();
    const body = await req.json();
    const sid = params.id;
    _state.sections[sid] = _state.sections[sid] || {};
    _state.sections[sid].narrative = body;
    await persist();
    return jsonResponse({ ok: true });
  });

  route("POST", "/api/section/:id/approve", async (params, req) => {
    await ensureActive();
    const { approved = true } = await req.json();
    _state.sections[params.id] = _state.sections[params.id] || {};
    _state.sections[params.id].narrative_approved = approved;
    await persist();
    return jsonResponse({ ok: true, approved });
  });

  route("PUT", "/api/section_order", async (_, req) => {
    await ensureActive();
    const { order = [] } = await req.json();
    _state.section_order = order;
    await persist();
    return jsonResponse({ ok: true });
  });

  route("PUT", "/api/section/:id/quiz", async (params, req) => {
    await ensureActive();
    const body = await req.json();
    _state.sections[params.id] = _state.sections[params.id] || {};
    _state.sections[params.id].quiz = body;
    await persist();
    return jsonResponse({ ok: true });
  });

  route("POST", "/api/section/:id/quiz_approve", async (params, req) => {
    await ensureActive();
    const { approved = true } = await req.json();
    _state.sections[params.id] = _state.sections[params.id] || {};
    _state.sections[params.id].quiz_approved = approved;
    await persist();
    return jsonResponse({ ok: true, approved });
  });

  route("PUT", "/api/global_settings", async (_, req) => {
    await ensureActive();
    const payload = await req.json();
    _state.global_settings = { ..._state.global_settings, ...payload };
    await persist();
    return jsonResponse({ ok: true });
  });

  // ── LLM generation (proxied to /api/v2/llm with the user's stored key) ──

  function loadPromptTemplate(name) {
    return fetch(`/static/prompts/${name}.txt`).then((r) =>
      r.ok ? r.text() : null
    );
  }

  function formatAnnotations(anns) {
    return anns
      .map((a, i) => {
        const note = a.instructor_note ? ` | note: "${a.instructor_note}"` : "";
        return `[${i + 1}] ${a.id} (${a.color || "?"}): "${a.text}"${note}`;
      })
      .join("\n");
  }

  async function callLLM(prompt) {
    await ensureConfig();
    const llm = _config.llm || {};
    const res = await fetch("/api/v2/llm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...llm, prompt }),
    });
    if (!res.ok) {
      const e = await res.json().catch(() => ({ error: res.statusText }));
      throw new Error(e.error || "LLM call failed");
    }
    return res.json();
  }

  // Embedded prompts. These are minimal stand-ins; the server-side
  // prompts/{narrative,quiz}.txt versions are richer. In client mode we
  // call /api/v2/llm directly so the prompts live alongside this file.
  // The /static/prompts/* files are served by Flask's static handler.
  const NARRATIVE_PROMPT_TPL_URL          = "/static/prompts/narrative.txt";
  const QUIZ_PROMPT_TPL_URL               = "/static/prompts/quiz.txt";
  // Template-selection rule (mirror of generate.py:select_quiz_prompt):
  //   zero red annotations → quiz_from_narrative.txt
  //   one or more red annotations → quiz.txt
  // Keep in sync with generate.py:select_quiz_prompt; changes to either file
  // must be reflected in the other.
  const QUIZ_FROM_NARRATIVE_PROMPT_TPL_URL = "/static/prompts/quiz_from_narrative.txt";

  async function getTpl(url) {
    const r = await fetch(url);
    if (!r.ok) throw new Error(`Prompt template not available at ${url}`);
    return r.text();
  }

  function findSection(sid) {
    return (_state.sections_raw || []).find((s) => s.section_id === sid);
  }

  route("POST", "/api/section/:id/generate_narrative", async (params) => {
    await ensureActive();
    const section = findSection(params.id);
    if (!section) return err(`Section ${params.id} not found`, 404);
    try {
      const tpl = await getTpl(NARRATIVE_PROMPT_TPL_URL);
      const prompt = tpl
        .replaceAll("{section_id}", params.id)
        .replaceAll("{annotations}", formatAnnotations(section.source_annotations || []));
      const result = await callLLM(prompt);
      const narrative = result.parsed || {};
      _state.sections[params.id] = {
        ..._state.sections[params.id],
        narrative,
        narrative_approved: false,
        source_annotations: section.source_annotations || [],
      };
      await persist();
      return jsonResponse({ ok: true, narrative });
    } catch (e) {
      return err(e.message, 500);
    }
  });

  route("POST", "/api/section/:id/generate_quiz", async (params) => {
    await ensureActive();
    const sec = _state.sections[params.id] || {};
    if (!sec.narrative_approved) return err("Narrative must be approved before generating quiz", 400);
    const section = findSection(params.id);
    if (!section) return err(`Section ${params.id} not found`, 404);

    const redAnns = (section.source_annotations || []).filter((a) => a.color === "red");
    try {
      // Template selection mirrors generate.py:select_quiz_prompt (keep in sync):
      //   zero red → quiz_from_narrative.txt  |  one+ red → quiz.txt
      const tplUrl = redAnns.length === 0 ? QUIZ_FROM_NARRATIVE_PROMPT_TPL_URL : QUIZ_PROMPT_TPL_URL;
      const tpl = await getTpl(tplUrl);
      const prompt = tpl
        .replaceAll("{section_id}", params.id)
        .replaceAll("{narrative_json}", JSON.stringify(sec.narrative || {}, null, 2))
        .replaceAll("{red_annotations}", formatAnnotations(redAnns) || "(none)");
      const result = await callLLM(prompt);
      const quiz = result.parsed || {};
      _state.sections[params.id] = { ...sec, quiz, quiz_approved: false };
      await persist();
      return jsonResponse({ ok: true, quiz });
    } catch (e) {
      return err(e.message, 500);
    }
  });

  route("POST", "/api/section/:id/regenerate_question", async (params, req) => {
    await ensureActive();
    const sec = _state.sections[params.id] || {};
    if (!sec.narrative_approved) return err("Narrative must be approved", 400);
    const section = findSection(params.id);
    if (!section) return err(`Section ${params.id} not found`, 404);

    const body = await req.json();
    const annIds = new Set(body.source_annotation_ids || []);
    const target = annIds.size
      ? (section.source_annotations || []).filter((a) => annIds.has(a.id))
      : (section.source_annotations || []).filter((a) => a.color === "red");

    try {
      const tpl = await getTpl(QUIZ_PROMPT_TPL_URL);
      const prompt = tpl
        .replaceAll("{section_id}", params.id)
        .replaceAll("{narrative_json}", JSON.stringify(sec.narrative || {}, null, 2))
        .replaceAll("{red_annotations}", formatAnnotations(target) || "(none)");
      const result = await callLLM(prompt);
      const questions = (result.parsed || {}).questions || [];
      return jsonResponse({ ok: true, question: questions[0] || null });
    } catch (e) {
      return err(e.message, 500);
    }
  });

  // ── Config ─────────────────────────────────────────────────────────────

  route("GET", "/api/config", async () => {
    return jsonResponse(await ensureConfig());
  });

  route("PUT", "/api/config", async (_, req) => {
    const body = await req.json();
    _config = body;
    await ZSGStore.saveConfig(body);
    return jsonResponse({ ok: true });
  });

  route("POST", "/api/config/test", async () => {
    try {
      const result = await callLLM("Reply with the word OK.");
      return jsonResponse({ ok: true, response: (result.text || "").slice(0, 100) });
    } catch (e) {
      return jsonResponse({ ok: false, error: e.message }, 500);
    }
  });

  // ── Projects ───────────────────────────────────────────────────────────

  function projectsResponse(list, activeId) {
    return {
      projects: list.map((p) => ({
        slug: p.id,
        name: p.name,
        created: p.created,
        stages: {
          "1": false, "2": false, "3": false, "5": false,
        },
      })),
      active: activeId,
    };
  }

  route("GET", "/api/projects", async () => {
    const list = await ZSGStore.listProjects();
    const active = await ZSGStore.getActive();

    // Compute stage status from each project's stored state
    const result = await Promise.all(list.map(async (p) => {
      const s = await ZSGStore.loadState(p.id);
      const hasAnn = !!(s.annotations && (s.annotations.annotations || []).length);
      const hasSec = Array.isArray(s.sections_raw) && s.sections_raw.length > 0;
      const hasNarr = Object.values(s.sections || {}).some((sec) => sec.narrative);
      return {
        slug: p.id,
        name: p.name,
        created: p.created,
        stages: { "1": hasAnn, "2": hasSec, "3": hasNarr, "5": false },
      };
    }));
    return jsonResponse({ projects: result, active: active ? active.id : null });
  });

  route("POST", "/api/projects/new", async (_, req) => {
    const body = await req.json();
    if (!body.name || !body.name.trim()) return err("name required", 400);
    const proj = await ZSGStore.createProject(body.name.trim());
    await ZSGStore.setActive(proj.id);
    _activeId = proj.id;
    _state = await ZSGStore.loadState(_activeId);
    return jsonResponse({ slug: proj.id, name: proj.name, ok: true });
  });

  route("POST", "/api/projects/open", async (_, req) => {
    const body = await req.json();
    const id = (body.slug || "").trim();
    if (!id) return err("slug required", 400);
    await ZSGStore.setActive(id);
    _activeId = id;
    _state = await ZSGStore.loadState(id);
    const hasAnn = !!(_state.annotations && (_state.annotations.annotations || []).length);
    const hasSec = Array.isArray(_state.sections_raw) && _state.sections_raw.length > 0;
    const hasNarr = Object.values(_state.sections || {}).some((sec) => sec.narrative);
    return jsonResponse({
      ok: true,
      slug: id,
      stages: { "1": hasAnn, "2": hasSec, "3": hasNarr, "5": false },
    });
  });

  // ── Upload + pipeline stages (call /api/v2/* equivalents) ───────────────

  route("POST", "/api/upload/zotero_export", async (_, req) => {
    const fd = await req.formData();
    const file = fd.get("file");
    if (!file) return err("no file provided", 400);

    const ext = (file.name.split(".").pop() || "html").toLowerCase();
    const content = await file.text();
    const r = await fetch("/api/v2/parse", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ format: ext, content }),
    });
    if (!r.ok) {
      const e = await r.json().catch(() => ({ error: r.statusText }));
      return err(e.error || "parse failed", r.status);
    }
    const parsed = await r.json();
    await ensureActive();
    _state.annotations = parsed;
    await persist();
    return jsonResponse({ ok: true, path: `(in-browser): ${file.name}` });
  });

  route("POST", "/api/pipeline/run", async (_, req) => {
    await ensureActive();
    const body = await req.json();
    const stage = body.stage;

    if (stage === "export") {
      // The legacy "export" stage reparses a file from disk. In client mode
      // the file was already parsed by /api/upload/zotero_export, so this
      // is a no-op — return a synthetic completed run_id.
      const runId = "client_" + Date.now().toString(36);
      _clientRuns[runId] = { status: "done", returncode: 0, lines: ["(client mode: annotations already loaded)"] };
      return jsonResponse({ run_id: runId });
    }

    if (stage === "preprocess") {
      const opts = body.options || {};
      const anns = (_state.annotations || {}).annotations || [];
      const r = await fetch("/api/v2/sections", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          annotations: anns,
          strategy: opts.strategy || "auto",
          page_window: opts.page_window || 6,
        }),
      });
      if (!r.ok) {
        const e = await r.json().catch(() => ({ error: r.statusText }));
        return err(e.error || "preprocess failed", r.status);
      }
      const result = await r.json();
      _state.sections_raw = result.sections;
      // Initialize section_order if empty
      if (!_state.section_order || !_state.section_order.length) {
        _state.section_order = result.sections.map((s) => s.section_id);
      }
      await persist();
      const runId = "client_" + Date.now().toString(36);
      _clientRuns[runId] = {
        status: "done",
        returncode: 0,
        lines: [`Grouped into ${result.sections.length} sections`],
      };
      return jsonResponse({ run_id: runId });
    }

    if (stage === "build") {
      const r = await fetch("/api/v2/build", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          state: _state,
          title: body.title || "Study Guide",
          theme: body.theme || "light",
        }),
      });
      if (!r.ok) {
        const e = await r.json().catch(() => ({ error: r.statusText }));
        return err(e.error || "build failed", r.status);
      }
      const data = await r.json();
      _state._last_build = { html: data.html, ts: Date.now() };
      await persist();
      return jsonResponse({ ok: true, path: "(in-browser)", size_kb: data.size_kb });
    }

    if (stage === "generate_narrative" || stage === "generate_quiz") {
      // Loop client-side: call generate for each section. For the MVP,
      // this is intentionally serial and synchronous from the runner's
      // perspective — the returned run_id is "done" immediately and the
      // UI re-renders from _state.
      const runId = "client_" + Date.now().toString(36);
      _clientRuns[runId] = { status: "running", returncode: null, lines: [] };
      (async () => {
        const sectionsRaw = _state.sections_raw || [];
        for (const s of sectionsRaw) {
          const sid = s.section_id;
          if (body.only_section && body.only_section !== sid) continue;
          const existing = _state.sections[sid] || {};
          if (stage === "generate_narrative" && existing.narrative_approved) continue;
          if (stage === "generate_quiz" && !existing.narrative_approved) continue;
          if (stage === "generate_quiz" && existing.quiz_approved) continue;
          try {
            const path = `/api/section/${encodeURIComponent(sid)}/` +
              (stage === "generate_narrative" ? "generate_narrative" : "generate_quiz");
            await window.fetch(path, { method: "POST" });
            _clientRuns[runId].lines.push(`${sid}: done`);
          } catch (e) {
            _clientRuns[runId].lines.push(`${sid}: FAILED ${e.message}`);
          }
        }
        _clientRuns[runId].status = "done";
        _clientRuns[runId].returncode = 0;
      })();
      return jsonResponse({ run_id: runId });
    }

    return err(`unknown stage: ${stage}`, 400);
  });

  const _clientRuns = {};
  route("GET", "/api/pipeline/status/:run_id", async (params) => {
    const rec = _clientRuns[params.run_id];
    if (!rec) return jsonResponse({ status: "not_found" });
    return jsonResponse(rec);
  });

  route("POST", "/api/pipeline/cancel/:run_id", async (params) => {
    const rec = _clientRuns[params.run_id];
    if (!rec) return jsonResponse({ status: "not_found" });
    rec.status = "cancelled";
    rec.last_error = "Cancelled by user";
    return jsonResponse(rec);
  });

  // ── Export endpoints (build) ───────────────────────────────────────────

  route("POST", "/api/export/preview", async (_, req) => {
    await ensureActive();
    const body = await req.json().catch(() => ({}));
    const order = _state.section_order || Object.keys(_state.sections || {});
    const sections = [];
    for (const sid of order) {
      const sec = _state.sections[sid] || {};
      if (!sec.narrative_approved) continue;
      const narr = sec.narrative || {};
      sections.push({
        section_id: sid,
        heading: narr.heading || sid,
        key_points: (narr.key_points || []).length,
        figures: (narr.figures || []).length,
        questions: ((sec.quiz || {}).questions || []).length,
      });
    }
    return jsonResponse({
      title: body.title || "Study Guide",
      theme: body.theme || "light",
      sections,
      total_questions: sections.reduce((a, s) => a + s.questions, 0),
    });
  });

  route("POST", "/api/export/build", async (_, req) => {
    await ensureActive();
    const body = await req.json().catch(() => ({}));
    const title = body.title || "Study Guide";
    const theme = body.theme || "light";

    _state.global_settings = {
      ..._state.global_settings,
      title, theme,
      show_progress: body.show_progress ?? _state.global_settings?.show_progress ?? true,
      navigation: body.navigation || _state.global_settings?.navigation || "sidebar",
    };

    const r = await fetch("/api/v2/build", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ state: _state, title, theme }),
    });
    if (!r.ok) {
      const e = await r.json().catch(() => ({ error: r.statusText }));
      return err(e.error || "build failed", r.status);
    }
    const data = await r.json();
    _state._last_build = { html: data.html, ts: Date.now() };
    await persist();
    const approved = Object.values(_state.sections || {}).filter((s) => s.narrative_approved).length;
    return jsonResponse({
      ok: true,
      path: "(in-browser — use Open in Browser to download)",
      sections: approved,
      size_kb: data.size_kb,
    });
  });

  route("POST", "/api/export/open", async () => {
    if (!_state || !_state._last_build) return err("No build available — click Build Guide first.", 404);
    const blob = new Blob([_state._last_build.html], { type: "text/html;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    window.open(url, "_blank", "noopener");
    setTimeout(() => URL.revokeObjectURL(url), 60_000);
    return jsonResponse({ ok: true });
  });

  // ────────────────────────────────────────────────────────────────────────
  // Fetch interceptor
  // ────────────────────────────────────────────────────────────────────────

  const _origFetch = window.fetch.bind(window);

  window.fetch = function (input, init = {}) {
    try {
      const url = typeof input === "string" ? input : input.url;
      const method = (init.method || (input && input.method) || "GET").toUpperCase();
      const parsed = new URL(url, window.location.origin);
      // Only intercept same-origin /api/ paths (not /api/v2/ — those go to the real server)
      if (parsed.origin === window.location.origin
          && parsed.pathname.startsWith("/api/")
          && !parsed.pathname.startsWith("/api/v2/")) {
        const match = matchRoute(method, parsed.pathname);
        if (match) {
          const req = (typeof input === "string")
            ? new Request(input, init)
            : input;
          return Promise.resolve().then(() => match.handler(match.params, req)).catch((e) => err(e.message, 500));
        }
      }
    } catch (e) {
      // Fall through to native fetch on any parse error
    }
    return _origFetch(input, init);
  };

  // ────────────────────────────────────────────────────────────────────────
  // UI banner
  // ────────────────────────────────────────────────────────────────────────

  function showBanner() {
    const el = document.createElement("div");
    el.id = "client-mode-banner";
    el.textContent = "Client mode — data stored in your browser";
    Object.assign(el.style, {
      position: "fixed", bottom: "8px", right: "8px",
      background: "#222", color: "#fff",
      padding: "4px 10px", borderRadius: "12px",
      fontSize: "11px", fontFamily: "system-ui, sans-serif",
      zIndex: "9999", opacity: "0.7", pointerEvents: "none",
    });
    document.body && document.body.appendChild(el);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", showBanner);
  } else {
    showBanner();
  }

  // Expose for console debugging
  window.ZSGClientMode = {
    state: () => _state,
    config: () => _config,
    persist,
    routes,
  };

  console.info("ZSG client mode active — legacy /api/* calls routed through IndexedDB");
})();
