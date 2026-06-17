/* storage.js — Client-side persistence (IndexedDB + localStorage fallback)
 *
 * The hosted, BYOK mode of the app keeps all per-project data in the
 * browser: annotations, sections, narratives, quizzes, approval flags,
 * and section order. The server is a stateless transformer.
 *
 * Public API (loaded as window.ZSGStore):
 *   ZSGStore.init()                            -> Promise<void>
 *   ZSGStore.isEnabled()                       -> boolean
 *   ZSGStore.listProjects()                    -> Promise<Project[]>
 *   ZSGStore.createProject(name)               -> Promise<Project>
 *   ZSGStore.deleteProject(id)                 -> Promise<void>
 *   ZSGStore.getActive()                       -> Promise<Project|null>
 *   ZSGStore.setActive(id)                     -> Promise<void>
 *   ZSGStore.loadState(id)                     -> Promise<State>
 *   ZSGStore.saveState(id, state)              -> Promise<void>
 *   ZSGStore.getConfig()                       -> Promise<Config>
 *   ZSGStore.saveConfig(cfg)                   -> Promise<void>
 *
 * A "project" record:
 *   { id: string, name: string, created: ISOString, updated: ISOString }
 *
 * A "state" record mirrors the server-side state.json shape:
 *   { sections: { [id]: SectionState }, section_order: string[],
 *     annotations: AnnotationDoc, global_settings: {...},
 *     schema_version: 1 }
 *
 * Config is stored separately so it survives project switches:
 *   { llm: { provider, model, api_key, base_url, max_tokens, temperature },
 *     colors: {...} }
 */

"use strict";

(function () {
  const DB_NAME = "zsg";
  const DB_VERSION = 1;
  const STORE_PROJECTS = "projects";    // key: id
  const STORE_STATES   = "states";      // key: project id
  const STORE_META     = "meta";        // key: "active" | "config"
  const SCHEMA_VERSION = 1;

  let _db = null;
  let _enabled = typeof indexedDB !== "undefined";

  // ────────────────────────────────────────────────────────────────────────
  // IndexedDB helpers
  // ────────────────────────────────────────────────────────────────────────

  function openDb() {
    return new Promise((resolve, reject) => {
      if (!_enabled) return reject(new Error("IndexedDB not available"));
      if (_db) return resolve(_db);
      const req = indexedDB.open(DB_NAME, DB_VERSION);
      req.onupgradeneeded = (e) => {
        const db = req.result;
        if (!db.objectStoreNames.contains(STORE_PROJECTS)) {
          db.createObjectStore(STORE_PROJECTS, { keyPath: "id" });
        }
        if (!db.objectStoreNames.contains(STORE_STATES)) {
          db.createObjectStore(STORE_STATES);
        }
        if (!db.objectStoreNames.contains(STORE_META)) {
          db.createObjectStore(STORE_META);
        }
      };
      req.onsuccess = () => { _db = req.result; resolve(_db); };
      req.onerror   = () => reject(req.error);
    });
  }

  function tx(storeNames, mode) {
    return openDb().then((db) => db.transaction(storeNames, mode));
  }

  function asPromise(req) {
    return new Promise((resolve, reject) => {
      req.onsuccess = () => resolve(req.result);
      req.onerror   = () => reject(req.error);
    });
  }

  // ────────────────────────────────────────────────────────────────────────
  // ID + helpers
  // ────────────────────────────────────────────────────────────────────────

  function newId() {
    // 12-char base36 ID — collision risk negligible for personal use
    return (
      Date.now().toString(36) +
      Math.random().toString(36).slice(2, 10)
    );
  }

  function defaultState() {
    return {
      schema_version: SCHEMA_VERSION,
      sections: {},
      section_order: [],
      annotations: null,        // raw {annotations: [...], color_config: {...}}
      sections_raw: null,       // raw output of /api/v2/sections
      global_settings: {},
    };
  }

  function defaultConfig() {
    return {
      llm: {
        provider: "purdue_genai",
        model: "llama3.1:latest",
        api_key: "",
        base_url: "https://genai.rcac.purdue.edu/api/chat/completions",
        temperature: 0.1,
        max_tokens: 8192,
      },
      // Mirrors color_config.yaml so client-mode (which has no server-side
      // config file) shows the same default annotation-color meanings in the
      // Setup tab. Keep in sync with color_config.yaml.
      colors: {
        yellow: { label: "Key concepts", description: "Definitions, core ideas, important terms" },
        red: { label: "Quiz-worthy facts", description: "Dates, events, specific claims to test" },
        green: { label: "People & organizations", description: "Biographical information, institutional roles" },
        blue: { label: "Themes & arguments", description: "Analytical threads, overarching narratives" },
        purple: { label: "Connections", description: "Cross-references, links between topics" },
        orange: { label: "Examples", description: "Case studies, illustrative instances" },
      },
    };
  }

  // ────────────────────────────────────────────────────────────────────────
  // Public API
  // ────────────────────────────────────────────────────────────────────────

  async function init() {
    if (!_enabled) return;
    try { await openDb(); } catch (e) {
      console.warn("IndexedDB unavailable; ZSGStore disabled:", e);
      _enabled = false;
    }
  }

  function isEnabled() { return _enabled; }

  async function listProjects() {
    const t = await tx([STORE_PROJECTS], "readonly");
    const store = t.objectStore(STORE_PROJECTS);
    const all = await asPromise(store.getAll());
    return all.sort((a, b) => (b.updated || "").localeCompare(a.updated || ""));
  }

  async function createProject(name) {
    const id = newId();
    const now = new Date().toISOString();
    const proj = { id, name: String(name || "Untitled").trim(), created: now, updated: now };
    const t = await tx([STORE_PROJECTS, STORE_STATES], "readwrite");
    await Promise.all([
      asPromise(t.objectStore(STORE_PROJECTS).put(proj)),
      asPromise(t.objectStore(STORE_STATES).put(defaultState(), id)),
    ]);
    return proj;
  }

  async function deleteProject(id) {
    const t = await tx([STORE_PROJECTS, STORE_STATES, STORE_META], "readwrite");
    await Promise.all([
      asPromise(t.objectStore(STORE_PROJECTS).delete(id)),
      asPromise(t.objectStore(STORE_STATES).delete(id)),
    ]);
    const meta = t.objectStore(STORE_META);
    const active = await asPromise(meta.get("active"));
    if (active === id) await asPromise(meta.delete("active"));
  }

  async function getActive() {
    const t = await tx([STORE_META, STORE_PROJECTS], "readonly");
    const id = await asPromise(t.objectStore(STORE_META).get("active"));
    if (!id) return null;
    const proj = await asPromise(t.objectStore(STORE_PROJECTS).get(id));
    return proj || null;
  }

  async function setActive(id) {
    const t = await tx([STORE_META], "readwrite");
    await asPromise(t.objectStore(STORE_META).put(id, "active"));
  }

  async function loadState(id) {
    const t = await tx([STORE_STATES], "readonly");
    const state = await asPromise(t.objectStore(STORE_STATES).get(id));
    return migrateState(state || defaultState());
  }

  async function saveState(id, state) {
    const merged = { ...defaultState(), ...state, schema_version: SCHEMA_VERSION };
    const t = await tx([STORE_STATES, STORE_PROJECTS], "readwrite");
    await asPromise(t.objectStore(STORE_STATES).put(merged, id));
    const proj = await asPromise(t.objectStore(STORE_PROJECTS).get(id));
    if (proj) {
      proj.updated = new Date().toISOString();
      await asPromise(t.objectStore(STORE_PROJECTS).put(proj));
    }
  }

  async function getConfig() {
    const t = await tx([STORE_META], "readonly");
    const cfg = await asPromise(t.objectStore(STORE_META).get("config"));
    return { ...defaultConfig(), ...(cfg || {}), llm: { ...defaultConfig().llm, ...((cfg || {}).llm || {}) } };
  }

  async function saveConfig(cfg) {
    const t = await tx([STORE_META], "readwrite");
    await asPromise(t.objectStore(STORE_META).put(cfg, "config"));
  }

  // ────────────────────────────────────────────────────────────────────────
  // Migrations (no-op at schema_version 1; placeholder for future bumps)
  // ────────────────────────────────────────────────────────────────────────

  function migrateState(state) {
    if (!state.schema_version) state.schema_version = SCHEMA_VERSION;
    if (!state.sections) state.sections = {};
    if (!Array.isArray(state.section_order)) state.section_order = [];
    return state;
  }

  // ────────────────────────────────────────────────────────────────────────
  // Export to global
  // ────────────────────────────────────────────────────────────────────────

  window.ZSGStore = {
    init, isEnabled,
    listProjects, createProject, deleteProject,
    getActive, setActive,
    loadState, saveState,
    getConfig, saveConfig,
    SCHEMA_VERSION,
  };
})();
