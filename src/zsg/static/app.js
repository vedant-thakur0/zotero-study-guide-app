/* app.js — Setup and Pipeline tabs for the GUI app */

"use strict";

// ── State ──────────────────────────────────────────────────────────────────

let appConfig = {};
let allProjects = [];
let currentRunId = null;

// ── Helpers ────────────────────────────────────────────────────────────────

function toast(msg, duration = 2000) {
  const el = document.getElementById("toast");
  if (!el) return;
  el.textContent = msg;
  el.classList.add("show");
  setTimeout(() => el.classList.remove("show"), duration);
}

function escapeHtml(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

// ── API calls ──────────────────────────────────────────────────────────────

async function fetchConfig() {
  const res = await fetch("/api/config");
  return res.json();
}

async function saveConfig(cfg) {
  const res = await fetch("/api/config", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(cfg),
  });
  return res.json();
}

async function testLLMConnection() {
  const res = await fetch("/api/config/test", { method: "POST" });
  return res.json();
}

async function fetchProjects() {
  const res = await fetch("/api/projects");
  return res.json();
}

async function createProject(name) {
  const res = await fetch("/api/projects/new", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
  return res.json();
}

async function openProject(slug) {
  const res = await fetch("/api/projects/open", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ slug }),
  });
  return res.json();
}

async function runPipelineStage(stage, options = {}) {
  const res = await fetch("/api/pipeline/run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ stage, ...options }),
  });
  return res.json();
}

async function getPipelineStatus(runId) {
  const res = await fetch(`/api/pipeline/status/${runId}`);
  return res.json();
}

async function uploadZoteroExport(file) {
  const fd = new FormData();
  fd.append("file", file);
  const res = await fetch("/api/upload/zotero_export", {
    method: "POST",
    body: fd,
  });
  return res.json();
}

// ── Setup Tab ──────────────────────────────────────────────────────────────

async function initSetupTab() {
  try {
    appConfig = await fetchConfig();
  } catch (e) {
    console.error("Error loading config:", e);
    toast("Error loading config from server");
    return;
  }
  renderSetupTab();
}

function renderSetupTab() {
  const tab = document.getElementById("tab-setup");
  if (!tab) return;

  const llm = appConfig.llm || {};
  const colors = appConfig.colors || {};

  let html = `
    <div class="setup-container">
      <h2>Setup</h2>
      <p class="setup-help">Configure your AI model and customize annotation colors.</p>

      <h3>AI Model Configuration</h3>

      <div class="form-row">
        <label class="form-label" for="setup-provider">Provider</label>
        <select class="form-input" id="setup-provider">
          <option value="purdue_genai" ${llm.provider === "purdue_genai" ? "selected" : ""}>Purdue GenAI Studio</option>
          <option value="anthropic" ${llm.provider === "anthropic" ? "selected" : ""}>Anthropic (Claude)</option>
          <option value="ollama" ${llm.provider === "ollama" ? "selected" : ""}>Ollama (Local)</option>
          <option value="lmstudio" ${llm.provider === "lmstudio" ? "selected" : ""}>LM Studio (Local)</option>
          <option value="openai" ${llm.provider === "openai" ? "selected" : ""}>OpenAI-compatible</option>
        </select>
      </div>

      <div class="form-row" id="model-row">
        <label class="form-label" for="setup-model">Model</label>
        <input class="form-input" id="setup-model" type="text" value="${llm.model || ""}" placeholder="e.g., llama3.1:latest">
        <small id="model-hint" class="form-hint"></small>
      </div>

      <div class="form-row" id="api-key-row" style="${["ollama", "lmstudio"].includes(llm.provider) ? "display:none" : ""}">
        <label class="form-label" for="setup-api-key">API Key</label>
        <input class="form-input" id="setup-api-key" type="password" value="${llm.api_key || ""}" placeholder="Paste your API key here">
        <small id="api-key-hint" class="form-hint"></small>
      </div>

      <div class="form-row" id="base-url-row" style="${["ollama", "lmstudio", "openai", "purdue_genai"].includes(llm.provider) ? "" : "display:none"}">
        <label class="form-label" for="setup-base-url">Base URL</label>
        <input class="form-input" id="setup-base-url" type="text" value="${llm.base_url || ""}" placeholder="https://genai.rcac.purdue.edu/api/chat/completions">
      </div>

      <div class="form-row">
        <label class="form-label" for="setup-temperature">Temperature</label>
        <input class="form-input" id="setup-temperature" type="number" step="0.01" min="0" max="2" value="${llm.temperature ?? 0.1}">
      </div>

      <div class="form-row">
        <label class="form-label" for="setup-max-tokens">Max Tokens</label>
        <input class="form-input" id="setup-max-tokens" type="number" min="100" value="${llm.max_tokens || 8192}">
      </div>

      <div class="button-group">
        <button id="test-connection-btn" class="btn btn-secondary">Test Connection</button>
        <button id="save-config-btn" class="btn btn-primary">Save Settings</button>
      </div>
      <div id="test-result" style="margin-top: 1rem; font-size: 0.9rem;"></div>

      <h3 style="margin-top: 2rem;">Annotation Colors</h3>
      <p class="setup-help">Customize what each highlight color means for your course.</p>

      <div id="colors-form">
  `;

  const colorOrder = ["yellow", "red", "green", "blue", "purple", "orange"];
  for (const color of colorOrder) {
    const cfg = colors[color] || {};
    html += `
      <div class="color-row">
        <div class="color-swatch" style="background-color: ${color};" title="${color}"></div>
        <div class="color-inputs">
          <input type="text" class="color-label" data-color="${color}" value="${cfg.label || ""}" placeholder="Label">
          <input type="text" class="color-desc" data-color="${color}" value="${cfg.description || ""}" placeholder="Description">
        </div>
      </div>
    `;
  }

  html += `
      </div>
    </div>
  `;

  tab.innerHTML = html;

  // Wire up handlers
  document.getElementById("setup-provider").addEventListener("change", updateProviderUI);
  document.getElementById("test-connection-btn").addEventListener("click", testConnection);
  document.getElementById("save-config-btn").addEventListener("click", saveSetupConfig);

  updateProviderUI();
}

function updateProviderUI() {
  const provider = document.getElementById("setup-provider")?.value || "purdue_genai";

  // Model hint
  const modelHints = {
    anthropic:    "e.g., claude-haiku-4-5-20251001",
    purdue_genai: "e.g., llama3.1:latest",
    ollama:       "e.g., llama3.1:70b",
    lmstudio:     "Check your LM Studio model name",
    openai:       "e.g., gpt-4-turbo",
  };
  const modelHint = document.getElementById("model-hint");
  if (modelHint) modelHint.textContent = modelHints[provider] || "";

  // API key hint
  const apiKeyHints = {
    anthropic:    "From console.anthropic.com → API Keys",
    purdue_genai: "From genai.rcac.purdue.edu → avatar → Settings → Account → API Keys",
    openai:       "From platform.openai.com → API Keys",
  };
  const apiKeyHint = document.getElementById("api-key-hint");
  if (apiKeyHint) apiKeyHint.textContent = apiKeyHints[provider] || "";

  // Show/hide API key (hidden for local providers only)
  const apiKeyRow = document.getElementById("api-key-row");
  if (apiKeyRow) {
    apiKeyRow.style.display = ["ollama", "lmstudio"].includes(provider) ? "none" : "";
  }

  // Show/hide base URL
  const baseUrlRow = document.getElementById("base-url-row");
  if (baseUrlRow) {
    baseUrlRow.style.display = ["ollama", "lmstudio", "openai", "purdue_genai"].includes(provider) ? "" : "none";
  }

  // Auto-fill base URL for Purdue
  const baseUrlInput = document.getElementById("setup-base-url");
  if (baseUrlInput && provider === "purdue_genai" && !baseUrlInput.value) {
    baseUrlInput.value = "https://genai.rcac.purdue.edu/api/chat/completions";
  }

  // Auto-fill model for Purdue
  const modelInput = document.getElementById("setup-model");
  if (modelInput && provider === "purdue_genai" && !modelInput.value) {
    modelInput.value = "llama3.1:latest";
  }
}

async function testConnection() {
  const btn = document.getElementById("test-connection-btn");
  const result = document.getElementById("test-result");

  btn.disabled = true;
  btn.textContent = "Testing...";
  result.textContent = "";

  try {
    const res = await testLLMConnection();
    if (res.ok) {
      result.innerHTML = `<span style="color: green;">✓ Connected to LLM</span>`;
    } else {
      result.innerHTML = `<span style="color: red;">✗ Error: ${escapeHtml(res.error)}</span>`;
    }
  } catch (e) {
    result.innerHTML = `<span style="color: red;">✗ Error: ${escapeHtml(e.message)}</span>`;
  } finally {
    btn.disabled = false;
    btn.textContent = "Test Connection";
  }
}

async function saveSetupConfig() {
  const cfg = { ...appConfig };

  cfg.llm = {
    provider: document.getElementById("setup-provider").value,
    model: document.getElementById("setup-model").value,
    api_key: document.getElementById("setup-api-key").value,
    base_url: document.getElementById("setup-base-url").value,
    temperature: parseFloat(document.getElementById("setup-temperature").value) || 0.1,
    max_tokens: parseInt(document.getElementById("setup-max-tokens").value) || 8192,
  };

  cfg.colors = {};
  document.querySelectorAll(".color-label").forEach((el) => {
    const color = el.dataset.color;
    cfg.colors[color] = {
      label: el.value || color,
      description: document.querySelector(`.color-desc[data-color="${color}"]`)?.value || "",
    };
  });

  await saveConfig(cfg);
  appConfig = cfg;
  toast("Settings saved!");
}

// ── Pipeline Tab ──────────────────────────────────────────────────────────

async function initPipelineTab() {
  try {
    await loadProjects();
  } catch (e) {
    console.error("Error loading projects:", e);
    toast("Error loading projects from server");
    return;
  }
  renderPipelineTab();
}

async function loadProjects() {
  const data = await fetchProjects();
  allProjects = data.projects || [];
  appConfig.active_project = data.active;
}

function renderPipelineTab() {
  const tab = document.getElementById("tab-pipeline");
  if (!tab) return;

  const active = appConfig.active_project;
  const activeProject = allProjects.find((p) => p.slug === active);

  let html = `
    <div class="pipeline-container">
      <div class="pipeline-header">
        <h2>Pipeline</h2>
        <div class="project-selector">
          <label>Project:</label>
          <select id="project-select">
            <option value="">-- New Project --</option>
  `;

  for (const proj of allProjects) {
    html += `<option value="${proj.slug}" ${proj.slug === active ? "selected" : ""}>${escapeHtml(proj.name)}</option>`;
  }

  html += `
          </select>
          <button id="new-project-btn" class="btn btn-small">+ New</button>
        </div>
      </div>

      ${
        !activeProject
          ? `
      <div class="pipeline-empty">
        <p>Create or select a project to begin.</p>
      </div>
      `
          : `
      <div class="pipeline-stages">
        <div class="stage ${activeProject.stages["1"] ? "done" : ""}">
          <h4>Stage 1: Parse Annotations</h4>
          <p>Upload your Zotero export.</p>
          <div class="stage-content">
            <input type="file" id="zotero-file" accept=".html,.htm,.md,.csv,.json" style="margin-bottom: 0.5rem;">
            <button id="upload-btn" class="btn btn-primary">Upload & Parse</button>
            ${activeProject.stages["1"] ? '<span class="stage-status">✓ Done</span>' : ""}
          </div>
          <pre id="log-stage-1" class="log" style="display:none;"></pre>
        </div>

        <div class="stage ${activeProject.stages["2"] ? "done" : ""}">
          <h4>Stage 2: Group into Sections</h4>
          <p>Cluster annotations by page proximity.</p>
          <div class="stage-content">
            <button id="run-stage-2-btn" class="btn btn-primary" ${!activeProject.stages["1"] ? "disabled" : ""}>Run</button>
            ${activeProject.stages["2"] ? '<span class="stage-status">✓ Done</span>' : ""}
          </div>
          <pre id="log-stage-2" class="log" style="display:none;"></pre>
        </div>

        <div class="stage ${activeProject.stages["3"] ? "done" : ""}">
          <h4>Stage 3: Generate with AI</h4>
          <p>Call LLM to write narratives and quizzes.</p>
          <div class="stage-content">
            <button id="run-stage-3-btn" class="btn btn-primary" ${!activeProject.stages["2"] ? "disabled" : ""}>Run</button>
            ${activeProject.stages["3"] ? '<span class="stage-status">✓ Done</span>' : ""}
          </div>
          <pre id="log-stage-3" class="log" style="display:none;"></pre>
        </div>

        <div class="stage">
          <h4>Stage 4: Review & Edit</h4>
          <p>Review LLM output side-by-side with sources.</p>
          <div class="stage-content">
            <button id="open-review-btn" class="btn btn-primary" ${!activeProject.stages["3"] ? "disabled" : ""}>Open Review</button>
          </div>
        </div>

        <div class="stage ${activeProject.stages["5"] ? "done" : ""}">
          <h4>Stage 5: Build & Export</h4>
          <p>Done in the Export tab after review.</p>
          <div class="stage-content">
            ${activeProject.stages["5"] ? '<span class="stage-status">✓ Done</span>' : '<em>Complete review first</em>'}
          </div>
        </div>
      </div>
      `
      }
    </div>
  `;

  tab.innerHTML = html;

  // Wire up handlers
  const projectSelect = document.getElementById("project-select");
  if (projectSelect) {
    projectSelect.addEventListener("change", (e) => {
      if (e.target.value) {
        switchProject(e.target.value);
      }
    });
  }

  document.getElementById("new-project-btn")?.addEventListener("click", promptNewProject);
  document.getElementById("upload-btn")?.addEventListener("click", uploadExport);
  document.getElementById("zotero-file")?.addEventListener("change", (e) => {
    // Optionally auto-upload on file select
    if (e.target.files[0]) {
      uploadExport();
    }
  });
  document.getElementById("run-stage-2-btn")?.addEventListener("click", () => runStage("preprocess"));
  document.getElementById("run-stage-3-btn")?.addEventListener("click", () => runStage("generate_narrative"));
  document.getElementById("open-review-btn")?.addEventListener("click", async () => {
    // Re-open project so Flask globals are pointing at the right files,
    // then re-run review.js init so it fetches fresh state before switching tabs.
    await openProject(appConfig.active_project);
    if (typeof init === "function") await init();
    document.querySelector('[data-tab="tab-narrative"]')?.click();
  });
}

async function switchProject(slug) {
  const res = await openProject(slug);
  if (res.ok) {
    appConfig.active_project = slug;  // keep local state in sync
    await loadProjects();
    renderPipelineTab();
    toast(`Switched to: ${slug}`);
  } else {
    toast(`Error: ${res.error}`);
  }
}

async function promptNewProject() {
  const name = prompt("Project name:");
  if (!name) return;

  const res = await createProject(name);
  if (res.ok) {
    await loadProjects();
    await switchProject(res.slug);
  } else {
    toast(`Error: ${res.error}`);
  }
}

async function uploadExport() {
  const file = document.getElementById("zotero-file")?.files[0];
  if (!file) {
    toast("Please select a file.");
    return;
  }

  if (!appConfig.active_project) {
    toast("No project selected.");
    return;
  }

  const res = await uploadZoteroExport(file);
  if (res.ok) {
    toast("File uploaded. Running Stage 1...");
    await runStage("export", { input_path: res.path });
  } else {
    toast(`Upload error: ${res.error}`);
  }
}

async function runStage(stage, options = {}) {
  if (!appConfig.active_project) {
    toast("No project selected.");
    return;
  }

  const res = await runPipelineStage(stage, options);
  if (!res.run_id) {
    toast(`Error: ${res.error}`);
    return;
  }

  currentRunId = res.run_id;
  const logId = `log-stage-${stage.includes("preprocess") ? "2" : stage.includes("generate") ? "3" : "1"}`;
  const logEl = document.getElementById(logId);
  if (logEl) {
    logEl.style.display = "block";
    logEl.textContent = "Running...";
  }

  // Poll for status
  const pollInterval = setInterval(async () => {
    const status = await getPipelineStatus(res.run_id);
    if (logEl && status.lines) {
      logEl.textContent = status.lines.join("\n");
      logEl.scrollTop = logEl.scrollHeight;
    }

    if (status.status === "done") {
      clearInterval(pollInterval);
      await loadProjects();
      renderPipelineTab();
      if (status.returncode === 0) {
        toast(`Stage complete!`);
      } else {
        toast(`Stage failed. Check log above.`);
      }
    }
  }, 500);
}

// ── Initialization ────────────────────────────────────────

document.addEventListener("DOMContentLoaded", async () => {
  // Load and render both tabs
  await initSetupTab();
  await initPipelineTab();

  // Watch for tab changes
  document.addEventListener("click", (e) => {
    if (e.target.classList.contains("tab-btn")) {
      const tab = e.target.dataset.tab;
      if (tab === "tab-setup") {
        initSetupTab();
      } else if (tab === "tab-pipeline") {
        initPipelineTab();
      }
    }
  });
});
