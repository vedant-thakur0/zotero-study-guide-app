/* guides.js — In-app guidance: intro modal + Zotero color guide in Setup tab */

"use strict";

// ── Constants ──────────────────────────────────────────────────────────────

const INTRO_SEEN_KEY = "zsg_intro_seen";

// Default Zotero color meanings (matches color_config.yaml defaults and GUIDE.md).
// These are shown in the color guide panel in the Setup tab so instructors
// understand how their Zotero highlight colors map to AI roles — without
// needing to read the README or edit any YAML.
const DEFAULT_COLOR_GUIDE = [
  {
    color: "yellow",
    swatch: "#ffd400",
    role: "Key concepts",
    use: "Definitions, important terms, core ideas — these become key points in the narrative",
  },
  {
    color: "red",
    swatch: "#ff6666",
    role: "Quiz-worthy facts",
    use: "Specific dates, names, claims worth testing — used to generate quiz questions",
  },
  {
    color: "green",
    swatch: "#5fb236",
    role: "People & organizations",
    use: "Biographical info, institutional roles — appear as figure cards in the narrative",
  },
  {
    color: "blue",
    swatch: "#2ea8e5",
    role: "Themes & arguments",
    use: "Analytical threads, overarching narratives — used to write the section introduction",
  },
  {
    color: "purple",
    swatch: "#a28ae5",
    role: "Connections",
    use: "Cross-references, links between topics — surfaced as key points or intro context",
  },
  {
    color: "orange",
    swatch: "#f19837",
    role: "Examples",
    use: "Case studies, illustrative instances — included as supporting key points",
  },
];

// ── Intro modal ────────────────────────────────────────────────────────────

function openIntroModal() {
  const modal = document.getElementById("intro-modal");
  if (modal) {
    modal.hidden = false;
    document.getElementById("intro-modal-close")?.focus();
  }
}

function closeIntroModal() {
  const modal = document.getElementById("intro-modal");
  if (modal) modal.hidden = true;
}

function dismissIntroModal() {
  try {
    localStorage.setItem(INTRO_SEEN_KEY, "1");
  } catch (_) {}
  closeIntroModal();
}

function initIntroModal() {
  const modal = document.getElementById("intro-modal");
  if (!modal) return;

  // Close on ×
  document.getElementById("intro-modal-close")?.addEventListener("click", closeIntroModal);

  // Dismiss (sets flag, doesn't reappear on reload)
  document.getElementById("intro-modal-dismiss")?.addEventListener("click", dismissIntroModal);

  // Close on backdrop click
  modal.querySelector(".intro-modal-backdrop")?.addEventListener("click", closeIntroModal);

  // Close on Escape
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !modal.hidden) closeIntroModal();
  });

  // Re-open affordance: "How this works" button in the header
  document.getElementById("how-it-works-btn")?.addEventListener("click", openIntroModal);

  // Show once on first visit (no zsg_intro_seen flag)
  let seen = false;
  try { seen = localStorage.getItem(INTRO_SEEN_KEY) === "1"; } catch (_) {}
  if (!seen) openIntroModal();
}

// ── Zotero color guide (injected into Setup tab after it renders) ──────────

function buildColorGuideHtml() {
  const rows = DEFAULT_COLOR_GUIDE.map((entry) => `
    <tr>
      <td>
        <div class="color-guide-color-cell">
          <span class="color-guide-swatch" style="background:${entry.swatch};"></span>
          ${entry.color}
        </div>
      </td>
      <td>${entry.role}</td>
      <td>${entry.use}</td>
    </tr>`).join("");

  return `
    <div class="color-guide">
      <div class="color-guide-title">What each color means</div>
      <table class="color-guide-table">
        <thead>
          <tr>
            <th>Color</th>
            <th>Role</th>
            <th>How the AI uses it</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
      <p class="color-guide-note">
        <strong>Tip:</strong> The text you type as a note on a highlight in Zotero becomes an
        <strong>instructor note</strong> visible during review. Use it to leave reminders like
        &ldquo;Use as section intro&rdquo; or &ldquo;Good quiz question — specific dates&rdquo;
        — the AI sees these notes and can use them to write better drafts.
        You can rename the label and description for any color above; the colors listed here
        are the defaults your project starts with.
      </p>
    </div>`;
}

// Inject the color guide into the Setup tab after renderSetupTab() runs.
// We use a MutationObserver on #tab-setup so we don't have to patch app.js.
function initColorGuide() {
  const setupTab = document.getElementById("tab-setup");
  if (!setupTab) return;

  function injectIfNeeded() {
    // Only inject once per render (guard against multiple mutations)
    if (setupTab.querySelector(".color-guide")) return;
    // The colors section heading is "Annotation Colors" — find the colors form
    const colorsForm = setupTab.querySelector("#colors-form");
    if (!colorsForm) return;
    // Insert the guide right after the colors form
    colorsForm.insertAdjacentHTML("afterend", buildColorGuideHtml());
  }

  // Observe the tab for subtree changes (renderSetupTab replaces innerHTML)
  const observer = new MutationObserver(() => injectIfNeeded());
  observer.observe(setupTab, { childList: true, subtree: false });

  // Also try immediately in case Setup tab is already rendered on load
  injectIfNeeded();
}

// ── Setup tab help hint ────────────────────────────────────────────────────
// The Setup tab content is dynamically rendered by app.js into #tab-setup.
// We inject a help hint there too, using the same MutationObserver approach.

function initSetupHint() {
  const setupTab = document.getElementById("tab-setup");
  if (!setupTab) return;

  const HINT_ID = "setup-help-hint";

  function injectSetupHint() {
    if (setupTab.querySelector(`#${HINT_ID}`)) return;
    // Wait until the setup container is present
    const container = setupTab.querySelector(".setup-container");
    if (!container) return;
    const hint = document.createElement("div");
    hint.id = HINT_ID;
    hint.className = "tab-help-hint";
    hint.innerHTML = `
      <span class="tab-help-hint-icon">&#9432;</span>
      <span>Paste your API key and choose an AI provider. The color rows below let you rename
      what each Zotero highlight color means for your course — the defaults work well out of
      the box. See the color guide at the bottom of this page for what each color does.</span>`;
    container.insertAdjacentElement("afterbegin", hint);
  }

  const observer = new MutationObserver(() => injectSetupHint());
  observer.observe(setupTab, { childList: true, subtree: false });
  injectSetupHint();
}

// ── Pipeline tab help hint ─────────────────────────────────────────────────
// renderPipelineTab() in app.js replaces #tab-pipeline's innerHTML entirely,
// wiping the static hint in the HTML. We re-inject it via MutationObserver.

function initPipelineHint() {
  const pipelineTab = document.getElementById("tab-pipeline");
  if (!pipelineTab) return;

  const HINT_ID = "pipeline-help-hint";

  function injectPipelineHint() {
    if (pipelineTab.querySelector(`#${HINT_ID}`)) return;
    // Wait until the pipeline container is present (renderPipelineTab renders .pipeline-container)
    const container = pipelineTab.querySelector(".pipeline-container");
    if (!container) return;
    const hint = document.createElement("div");
    hint.id = HINT_ID;
    hint.className = "tab-help-hint";
    hint.innerHTML = `
      <span class="tab-help-hint-icon">&#9432;</span>
      <span>Upload your Zotero annotation export here. The pipeline parses your highlights,
      groups them into sections, and calls the AI to write first drafts — then head to
      <strong>Narrative Review</strong> to check the output.</span>`;
    container.insertAdjacentElement("afterbegin", hint);
  }

  const observer = new MutationObserver(() => injectPipelineHint());
  observer.observe(pipelineTab, { childList: true, subtree: false });
  injectPipelineHint();
}

// ── Boot ───────────────────────────────────────────────────────────────────

document.addEventListener("DOMContentLoaded", () => {
  initIntroModal();
  initColorGuide();
  initSetupHint();
  initPipelineHint();
});
