/* review.js — Narrative Review tab logic */

"use strict";

// ── State ──────────────────────────────────────────────────────────────────

let appState = { sections: {}, section_order: [] };

// ── Helpers ────────────────────────────────────────────────────────────────

function toast(msg, duration = 2000) {
  const el = document.getElementById("toast");
  if (!el) return;
  el.textContent = msg;
  el.classList.add("show");
  setTimeout(() => el.classList.remove("show"), duration);
}

function debounce(fn, ms) {
  let timer = null;
  let pending = null;
  const wrapped = (...args) => {
    pending = args;
    clearTimeout(timer);
    timer = setTimeout(() => { timer = null; fn(...pending); pending = null; }, ms);
  };
  wrapped.flush = () => {
    if (timer !== null) {
      clearTimeout(timer);
      timer = null;
      if (pending) { fn(...pending); pending = null; }
    }
  };
  return wrapped;
}

function escapeHtml(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

// ── API calls ──────────────────────────────────────────────────────────────

async function fetchState() {
  const res = await fetch("/api/state");
  return res.json();
}

async function saveNarrative(sectionId, narrative) {
  await fetch(`/api/section/${sectionId}/narrative`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(narrative),
  });
}

async function approveSection(sectionId, approved) {
  await fetch(`/api/section/${sectionId}/approve`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ approved }),
  });
}

async function saveSectionOrder(order) {
  await fetch("/api/section_order", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ order }),
  });
}

async function deleteSection(sectionId) {
  const res = await fetch(`/api/section/${sectionId}/delete`, { method: "DELETE" });
  return res.ok;
}

async function generateNarrative(sectionId) {
  const res = await fetch(`/api/section/${sectionId}/generate_narrative`, {
    method: "POST",
  });
  if (!res.ok) {
    const err = await res.json();
    throw new Error(err.error || "Generation failed");
  }
  return res.json();
}

// ── Narrative extraction from DOM ──────────────────────────────────────────

function readNarrativeFromDom(sectionId) {
  const card = document.querySelector(`.section-card[data-id="${CSS.escape(sectionId)}"]`);
  if (!card) return null;

  const get = (sel) => card.querySelector(sel)?.innerText.trim() ?? "";

  const keyPoints = [...card.querySelectorAll(".kp-row")].map((row) => ({
    term: row.querySelector(".item-term")?.innerText.trim() ?? "",
    explanation: row.querySelector(".item-explanation")?.innerText.trim() ?? "",
    source_annotation_ids: (row.dataset.sourceIds ?? "").split(",").filter(Boolean),
  }));

  const figures = [...card.querySelectorAll(".fig-row")].map((row) => ({
    name: row.querySelector(".item-term")?.innerText.trim() ?? "",
    description: row.querySelector(".item-explanation")?.innerText.trim() ?? "",
    source_annotation_ids: (row.dataset.sourceIds ?? "").split(",").filter(Boolean),
  }));

  const usedIds = [
    ...keyPoints.flatMap((kp) => kp.source_annotation_ids),
    ...figures.flatMap((f) => f.source_annotation_ids),
  ];
  const introEl = card.querySelector(".narrative-intro");
  // also grab ids from intro data attr if present
  if (introEl?.dataset.sourceIds) {
    usedIds.push(...introEl.dataset.sourceIds.split(",").filter(Boolean));
  }

  return {
    section_id: sectionId,
    heading: get(".narrative-heading"),
    intro: get(".narrative-intro"),
    key_points: keyPoints,
    figures: figures,
    source_annotation_ids_used: [...new Set(usedIds)],
  };
}

// ── Save on edit ───────────────────────────────────────────────────────────

const debouncedSave = debounce(async (sectionId) => {
  const narrative = readNarrativeFromDom(sectionId);
  if (narrative) {
    await saveNarrative(sectionId, narrative);
    toast("Saved");
  }
}, 500);

// ── Render helpers ─────────────────────────────────────────────────────────

function colorDot(color) {
  return `<span class="color-dot ${color}"></span>`;
}

function renderAnnotation(ann) {
  const note = ann.instructor_note
    ? `<div class="annotation-note">📝 ${escapeHtml(ann.instructor_note)}</div>`
    : "";
  const page = ann.page ? `<div class="annotation-page">p. ${ann.page}</div>` : "";
  return `
    <div class="annotation ${ann.color}" data-id="${ann.id}"
         title="Click to highlight linked fields">
      <div class="annotation-id">${colorDot(ann.color)}${ann.id}</div>
      <div class="annotation-text">"${escapeHtml(ann.text)}"</div>
      ${note}${page}
    </div>`;
}

function renderKeyPoint(kp, sectionId) {
  const ids = (kp.source_annotation_ids ?? []).join(",");
  return `
    <div class="item-row kp-row" data-source-ids="${ids}">
      <div class="item-fields">
        <div class="item-term" contenteditable="true"
             data-section="${sectionId}">${escapeHtml(kp.term)}</div>
        <div class="item-explanation" contenteditable="true"
             data-section="${sectionId}">${escapeHtml(kp.explanation)}</div>
      </div>
      <button class="remove-btn" title="Remove">×</button>
    </div>`;
}

function renderFigure(fig, sectionId) {
  const ids = (fig.source_annotation_ids ?? []).join(",");
  return `
    <div class="item-row fig-row" data-source-ids="${ids}">
      <div class="item-fields">
        <div class="item-term" contenteditable="true"
             data-section="${sectionId}">${escapeHtml(fig.name)}</div>
        <div class="item-explanation" contenteditable="true"
             data-section="${sectionId}">${escapeHtml(fig.description)}</div>
      </div>
      <button class="remove-btn" title="Remove">×</button>
    </div>`;
}

function unusedAnnotations(section, narrative) {
  const usedIds = new Set(narrative?.source_annotation_ids_used ?? []);
  return (section.source_annotations ?? []).filter((a) => !usedIds.has(a.id));
}

function renderSectionCard(sectionId, idx) {
  const secState  = appState.sections[sectionId] ?? {};
  const narrative = secState.narrative ?? {};
  const approved  = secState.narrative_approved ?? false;
  const hasNarr   = !!narrative.heading;
  const anns      = secState.source_annotations ?? [];
  const order     = appState.section_order ?? [];
  const isFirst   = idx === 0;
  const isLast    = idx === order.length - 1;
  const narrError = secState.narrative_error ?? "";

  const badgeClass = approved ? "approved" : hasNarr ? "pending" : "error";
  const badgeText  = approved ? "✓ Approved" : hasNarr ? "Pending review" : "No content";

  // Red error badge surfaced when the previous LLM generation failed
  const errorBadge = narrError
    ? `<span class="badge error-llm" title="${escapeHtml(narrError)}">⚠ ${escapeHtml(
        narrError.length > 140 ? narrError.slice(0, 140) + "…" : narrError
      )}</span>`
    : "";

  const unused = hasNarr ? unusedAnnotations({ source_annotations: anns }, narrative) : [];
  const unusedWarning = unused.length
    ? `<span class="unused-warning">⚠ ${unused.length} annotation(s) unused</span>`
    : "";

  const kpHtml  = (narrative.key_points ?? []).map((kp) => renderKeyPoint(kp, sectionId)).join("");
  const figHtml = (narrative.figures ?? []).map((f) => renderFigure(f, sectionId)).join("");
  const annHtml = anns.map(renderAnnotation).join("");

  const introSourceIds = (narrative.source_annotation_ids_used ?? []).join(",");

  return `
    <div class="section-card ${approved ? "approved" : ""}" data-id="${sectionId}">
      <div class="section-header">
        <button class="reorder-btn" data-move="-1" data-id="${sectionId}" ${isFirst ? "disabled" : ""}>↑</button>
        <button class="reorder-btn" data-move="1"  data-id="${sectionId}" ${isLast  ? "disabled" : ""}>↓</button>
        <h2>${escapeHtml(sectionId)}</h2>
        <span class="badge ${badgeClass}">${badgeText}</span>
        <span class="badge">${anns.length} annotations</span>
        ${errorBadge}
        <button class="delete-section-btn" data-id="${sectionId}" title="Delete section">🗑</button>
      </div>

      <div class="section-body">
        <div class="split-pane">

          <!-- Left: source annotations -->
          <div class="pane">
            <div class="pane-label">Source Annotations</div>
            ${annHtml || '<p class="pane-empty-note">No annotations loaded.</p>'}
          </div>

          <!-- Right: generated content -->
          <div class="pane">
            <div class="pane-label">Generated Content</div>

            ${hasNarr ? `
            <div class="field-group">
              <div class="field-label">Heading</div>
              <div class="narrative-heading" contenteditable="true"
                   data-section="${sectionId}">${escapeHtml(narrative.heading ?? "")}</div>
            </div>

            <div class="field-group">
              <div class="field-label">Introduction</div>
              <div class="narrative-intro" contenteditable="true"
                   data-section="${sectionId}"
                   data-source-ids="${introSourceIds}">${escapeHtml(narrative.intro ?? "")}</div>
            </div>

            <div class="field-group">
              <div class="field-label">Key Points</div>
              <div class="kp-list">${kpHtml}</div>
              <button class="add-btn add-kp" data-section="${sectionId}">+ Add Key Point</button>
            </div>

            <div class="field-group">
              <div class="field-label">Figures</div>
              <div class="fig-list">${figHtml}</div>
              <button class="add-btn add-fig" data-section="${sectionId}">+ Add Figure</button>
            </div>
            ` : `
            <div class="empty-state">
              <p>${narrError ? "Generation failed." : "No content generated yet."}</p>
              <button class="generate-btn inline-gen empty-state-btn" data-section="${sectionId}">Generate with LLM</button>
              ${narrError ? `<button class="generate-btn retry-narr-btn empty-state-btn" data-section="${sectionId}">↻ Retry</button>` : ""}
            </div>
            `}
          </div>
        </div>

        <div class="section-footer">
          ${hasNarr ? `
            <button class="approve-btn ${approved ? "unapprove" : ""}"
                    data-section="${sectionId}" data-approved="${approved}">
              ${approved ? "Unapprove" : "✓ Approve"}
            </button>
            <button class="generate-btn regen-btn" data-section="${sectionId}">↻ Regenerate</button>
          ` : ""}
          ${unusedWarning}
        </div>
      </div>
    </div>`;
}

// ── Full render ────────────────────────────────────────────────────────────

function renderNarrativeTab() {
  debouncedSave.flush();
  const container = document.getElementById("narrative-sections");
  const order = appState.section_order ?? [];

  if (!order.length) {
    container.innerHTML = `<div class="empty-state">
      <p>No sections found. Run <code>preprocess.py</code> first.</p>
    </div>`;
    return;
  }

  container.innerHTML = order.map((id, i) => renderSectionCard(id, i)).join("");

  const approvedCount = order.filter(
    (id) => appState.sections[id]?.narrative_approved
  ).length;
  document.getElementById("progress-label").textContent =
    `${approvedCount} / ${order.length} approved`;

  attachNarrativeListeners();
}

// ── Event listeners ────────────────────────────────────────────────────────

function attachNarrativeListeners() {
  const container = document.getElementById("narrative-sections");

  // Toggle section open/close
  container.querySelectorAll(".section-header").forEach((header) => {
    header.addEventListener("click", (e) => {
      if (e.target.closest("button")) return; // don't toggle on button clicks
      const body = header.nextElementSibling;
      body.classList.toggle("open");
    });
  });

  // Auto-open sections that aren't yet approved
  container.querySelectorAll(".section-card").forEach((card) => {
    const sid = card.dataset.id;
    if (!appState.sections[sid]?.narrative_approved) {
      card.querySelector(".section-body")?.classList.add("open");
    }
  });

  // Inline editing — debounced save
  container.querySelectorAll("[contenteditable]").forEach((el) => {
    el.addEventListener("input", () => {
      const sid = el.dataset.section;
      if (sid) debouncedSave(sid);
    });
  });

  // Annotation click → highlight linked fields
  container.querySelectorAll(".annotation").forEach((annEl) => {
    annEl.addEventListener("click", () => {
      const annId = annEl.dataset.id;
      const card  = annEl.closest(".section-card");

      // Clear previous highlights
      card.querySelectorAll(".annotation.highlighted").forEach((a) =>
        a.classList.remove("highlighted")
      );
      card.querySelectorAll("[data-source-ids]").forEach((el) =>
        el.classList.remove("highlighted")
      );

      annEl.classList.add("highlighted");

      // Highlight any generated fields that reference this annotation
      card.querySelectorAll("[data-source-ids]").forEach((el) => {
        if (el.dataset.sourceIds.split(",").includes(annId)) {
          el.classList.add("highlighted");
        }
      });
    });
  });

  // Approve / unapprove
  container.querySelectorAll(".approve-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const sid     = btn.dataset.section;
      const current = btn.dataset.approved === "true";
      const next    = !current;
      await approveSection(sid, next);
      appState.sections[sid].narrative_approved = next;
      toast(next ? "Section approved ✓" : "Approval removed");
      renderNarrativeTab();
      if (activeTabId() === "tab-export") loadExportPreview();
    });
  });

  // Reorder buttons
  container.querySelectorAll(".reorder-btn").forEach((btn) => {
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const sid   = btn.dataset.id;
      const delta = parseInt(btn.dataset.move, 10);
      const order = [...appState.section_order];
      const i     = order.indexOf(sid);
      if (i < 0) return;
      const j = i + delta;
      if (j < 0 || j >= order.length) return;
      [order[i], order[j]] = [order[j], order[i]];
      appState.section_order = order;
      await saveSectionOrder(order);
      renderNarrativeTab();
    });
  });

  // Delete section
  container.querySelectorAll(".delete-section-btn").forEach((btn) => {
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const sid = btn.dataset.id;
      if (!confirm(`Delete section "${sid}"? Its review state is removed. Re-run preprocess to regenerate.`)) return;
      const ok = await deleteSection(sid);
      if (!ok) { toast("Could not delete section"); return; }
      delete appState.sections[sid];
      appState.section_order = appState.section_order.filter((id) => id !== sid);
      toast("Section deleted");
      renderNarrativeTab();
      if (activeTabId() === "tab-export") loadExportPreview();
    });
  });

  // Remove key point / figure
  container.querySelectorAll(".remove-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const row = btn.closest(".item-row");
      const sid = row.querySelector("[data-section]")?.dataset.section;
      row.remove();
      if (sid) debouncedSave(sid);
    });
  });

  // Add key point
  container.querySelectorAll(".add-kp").forEach((btn) => {
    btn.addEventListener("click", () => {
      const sid  = btn.dataset.section;
      const list = btn.previousElementSibling;
      const row  = document.createElement("div");
      row.innerHTML = renderKeyPoint({ term: "", explanation: "", source_annotation_ids: [] }, sid);
      const newRow = row.firstElementChild;
      list.appendChild(newRow);
      newRow.querySelector(".item-term").focus();
      newRow.querySelectorAll("[contenteditable]").forEach((el) =>
        el.addEventListener("input", () => debouncedSave(sid))
      );
      newRow.querySelector(".remove-btn").addEventListener("click", () => {
        newRow.remove();
        debouncedSave(sid);
      });
    });
  });

  // Add figure
  container.querySelectorAll(".add-fig").forEach((btn) => {
    btn.addEventListener("click", () => {
      const sid  = btn.dataset.section;
      const list = btn.previousElementSibling;
      const row  = document.createElement("div");
      row.innerHTML = renderFigure({ name: "", description: "", source_annotation_ids: [] }, sid);
      const newRow = row.firstElementChild;
      list.appendChild(newRow);
      newRow.querySelector(".item-term").focus();
      newRow.querySelectorAll("[contenteditable]").forEach((el) =>
        el.addEventListener("input", () => debouncedSave(sid))
      );
      newRow.querySelector(".remove-btn").addEventListener("click", () => {
        newRow.remove();
        debouncedSave(sid);
      });
    });
  });

  // Inline generate (when no content yet) — also wires the Retry button
  container.querySelectorAll(".inline-gen, .regen-btn, .retry-narr-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const sid = btn.dataset.section;
      const isRetry = btn.classList.contains("retry-narr-btn");
      const originalText = btn.textContent;
      btn.disabled = true;
      btn.innerHTML = '<span class="spinner"></span> Generating…';
      try {
        const result = await generateNarrative(sid);
        appState.sections[sid] = {
          ...appState.sections[sid],
          narrative: result.narrative,
          narrative_approved: false,
        };
        // Successful retry: clear the local mirror of the error too
        delete appState.sections[sid].narrative_error;
        toast(isRetry ? "Retry succeeded" : "Narrative generated");
        renderNarrativeTab();
      } catch (err) {
        toast(`Error: ${err.message}`, 4000);
        // Keep the freshly observed error in local state so the badge sticks
        appState.sections[sid] = {
          ...(appState.sections[sid] ?? {}),
          narrative_error: err.message,
        };
        btn.disabled = false;
        btn.textContent = originalText || "Generate with LLM";
      }
    });
  });
}

// ── Tab switching ──────────────────────────────────────────────────────────

function activeTabId() {
  return document.querySelector(".tab-btn.active")?.dataset.tab ?? "";
}

function initTabs() {
  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
      document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
      btn.classList.add("active");
      const tabId = btn.dataset.tab;
      document.getElementById(tabId).classList.add("active");
      if (tabId === "tab-quiz")      renderQuizTab();
      if (tabId === "tab-narrative") renderNarrativeTab();
      if (tabId === "tab-export")    loadExportPreview();
    });
  });
}

// ── Boot ───────────────────────────────────────────────────────────────────

async function init() {
  initTabs();
  initExportTab();
  try {
    appState = await fetchState();
    renderNarrativeTab();
  } catch (err) {
    document.getElementById("narrative-sections").innerHTML = `
      <div class="empty-state">
        <p>Could not load state: ${err.message}</p>
        <p class="empty-state-hint">Make sure verify.py is running.</p>
      </div>`;
  }
}

document.addEventListener("DOMContentLoaded", init);

// ═══════════════════════════════════════════════════════════════════════════
// EXPORT TAB — Phase 7
// ═══════════════════════════════════════════════════════════════════════════

function exportSettings() {
  return {
    title:        document.getElementById("exp-title")?.value.trim() || "Study Guide",
    theme:        document.getElementById("exp-theme")?.value || "light",
    navigation:   document.getElementById("exp-nav")?.value || "sidebar",
    show_progress: document.getElementById("exp-progress")?.checked ?? true,
  };
}

async function loadExportPreview() {
  const preview = document.getElementById("export-preview-content");
  if (!preview) return;

  preview.innerHTML = '<div class="preview-empty"><span class="spinner"></span> Loading…</div>';

  try {
    const res = await fetch("/api/export/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(exportSettings()),
    });
    const data = await res.json();

    if (!data.sections?.length) {
      preview.innerHTML = `<div class="preview-empty">
        No approved sections yet. Approve sections in the Narrative Review tab first.
      </div>`;
      return;
    }

    const sectionRows = data.sections.map((s) => `
      <div class="preview-section-row">
        <span class="preview-section-heading">${escapeHtml(s.heading)}</span>
        ${s.key_points ? `<span class="preview-pill">${s.key_points} key point${s.key_points !== 1 ? "s" : ""}</span>` : ""}
        ${s.figures    ? `<span class="preview-pill">${s.figures} figure${s.figures !== 1 ? "s" : ""}</span>` : ""}
        ${s.questions  ? `<span class="preview-pill">${s.questions} Q</span>` : '<span class="preview-pill preview-pill--warn">no quiz</span>'}
      </div>`).join("");

    preview.innerHTML = `
      <div class="preview-meta">
        <span><strong>${data.sections.length}</strong> section${data.sections.length !== 1 ? "s" : ""}</span>
        <span><strong>${data.total_questions}</strong> quiz question${data.total_questions !== 1 ? "s" : ""}</span>
        <span>Theme: <strong>${escapeHtml(data.theme)}</strong></span>
      </div>
      <div class="preview-section-list">${sectionRows}</div>`;
  } catch (err) {
    preview.innerHTML = `<div class="preview-empty">Error: ${err.message}</div>`;
  }
}

function initExportTab() {
  document.getElementById("refresh-preview-btn")?.addEventListener("click", loadExportPreview);

  // Theme swatches
  document.querySelectorAll(".theme-swatch").forEach((swatch) => {
    swatch.addEventListener("click", () => {
      document.querySelectorAll(".theme-swatch").forEach((s) => s.classList.remove("selected"));
      swatch.classList.add("selected");
      const themeInput = document.getElementById("exp-theme");
      if (themeInput) themeInput.value = swatch.dataset.theme;
      loadExportPreview();
    });
  });

  // Live preview update on title/nav/progress change
  ["exp-title", "exp-nav", "exp-progress"].forEach((id) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.addEventListener("change", loadExportPreview);
    if (el.tagName === "INPUT" && el.type === "text") {
      el.addEventListener("input", debounce(loadExportPreview, 400));
    }
  });

  // Build button
  document.getElementById("build-btn")?.addEventListener("click", async () => {
    const btn    = document.getElementById("build-btn");
    const result = document.getElementById("build-result");
    const openBtn = document.getElementById("open-btn");

    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Building…';

    try {
      const res = await fetch("/api/export/build", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(exportSettings()),
      });
      const data = await res.json();

      if (!data.ok) throw new Error(data.error || "Build failed");

      document.getElementById("result-path").textContent = data.path;
      document.getElementById("result-meta").textContent =
        `${data.sections} section${data.sections !== 1 ? "s" : ""} · ${data.size_kb} KB`;
      result.classList.add("show");
      openBtn.disabled = false;
      toast("Guide built successfully ✓");
    } catch (err) {
      toast(`Build error: ${err.message}`, 4000);
    } finally {
      btn.disabled = false;
      btn.textContent = "Build Guide";
    }
  });

  // Open in browser button
  document.getElementById("open-btn")?.addEventListener("click", async () => {
    try {
      await fetch("/api/export/open", { method: "POST" });
    } catch (err) {
      toast(`Error: ${err.message}`, 3000);
    }
  });
}

// ═══════════════════════════════════════════════════════════════════════════
// QUIZ REVIEW TAB — Phase 5
// ═══════════════════════════════════════════════════════════════════════════

// ── API ────────────────────────────────────────────────────────────────────

async function generateQuiz(sectionId) {
  const res = await fetch(`/api/section/${sectionId}/generate_quiz`, { method: "POST" });
  if (!res.ok) { const e = await res.json(); throw new Error(e.error || "Generation failed"); }
  return res.json();
}

async function saveQuiz(sectionId, quiz) {
  await fetch(`/api/section/${sectionId}/quiz`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(quiz),
  });
}

async function approveQuiz(sectionId, approved) {
  await fetch(`/api/section/${sectionId}/quiz_approve`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ approved }),
  });
}

async function regenerateQuestion(sectionId, sourceIds) {
  const res = await fetch(`/api/section/${sectionId}/regenerate_question`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ source_annotation_ids: sourceIds }),
  });
  if (!res.ok) { const e = await res.json(); throw new Error(e.error || "Regeneration failed"); }
  return res.json();
}

// ── Quiz state read from DOM ───────────────────────────────────────────────

function readQuizFromDom(sectionId) {
  const card = document.querySelector(`.quiz-section-card[data-id="${CSS.escape(sectionId)}"]`);
  if (!card) return null;

  const questions = [...card.querySelectorAll(".question-card")].map((qcard) => {
    const get = (sel) => qcard.querySelector(sel)?.innerText.trim() ?? "";
    const distractors = [...qcard.querySelectorAll(".distractor-row [contenteditable]")]
      .map((el) => el.innerText.trim())
      .filter(Boolean);
    return {
      question_text:            get(".q-question [contenteditable]"),
      correct_answer:           get(".q-correct [contenteditable]"),
      distractors,
      explanation_if_correct:   get(".q-exp-correct [contenteditable]"),
      explanation_if_incorrect: get(".q-exp-incorrect [contenteditable]"),
      source_annotation_ids: (qcard.dataset.sourceIds ?? "").split(",").filter(Boolean),
    };
  });

  return { section_id: sectionId, questions };
}

const debouncedSaveQuiz = debounce(async (sectionId) => {
  const quiz = readQuizFromDom(sectionId);
  if (quiz) { await saveQuiz(sectionId, quiz); toast("Saved"); }
}, 500);

// ── Render helpers ─────────────────────────────────────────────────────────

function renderDistractorRow(text, sectionId) {
  return `
    <div class="distractor-row">
      <div contenteditable="true" data-section="${sectionId}">${escapeHtml(text)}</div>
      <button class="swap-btn remove-distractor">✕</button>
    </div>`;
}

function renderQuestionCard(q, idx, sectionId) {
  const ids = (q.source_annotation_ids ?? []).join(",");
  const distractorHtml = (q.distractors ?? [])
    .map((d) => renderDistractorRow(d, sectionId))
    .join("");

  return `
    <div class="question-card" data-source-ids="${ids}">
      <div class="question-card-header">
        <span class="question-num">Q${idx + 1}</span>
        <span class="badge">${(q.source_annotation_ids ?? []).join(", ") || "—"}</span>
      </div>
      <div class="question-card-body">
        <div class="q-field q-question">
          <div class="q-label">Question</div>
          <div contenteditable="true" data-section="${sectionId}">${escapeHtml(q.question_text ?? "")}</div>
        </div>
        <div class="q-field q-correct">
          <div class="q-label">Correct Answer</div>
          <div contenteditable="true" data-section="${sectionId}">${escapeHtml(q.correct_answer ?? "")}</div>
        </div>
        <div class="q-field">
          <div class="q-label">Distractors</div>
          <div class="distractor-list">${distractorHtml}</div>
          <button class="add-distractor-btn" data-section="${sectionId}">+ Add distractor</button>
        </div>
        <div class="q-field q-exp-correct">
          <div class="q-label">Explanation (correct)</div>
          <div contenteditable="true" data-section="${sectionId}">${escapeHtml(q.explanation_if_correct ?? "")}</div>
        </div>
        <div class="q-field q-exp-incorrect">
          <div class="q-label">Explanation (incorrect)</div>
          <div contenteditable="true" data-section="${sectionId}">${escapeHtml(q.explanation_if_incorrect ?? "")}</div>
        </div>
      </div>
      <div class="question-footer">
        <button class="regen-q-btn" data-section="${sectionId}" data-ids="${ids}">↻ Regenerate</button>
        <button class="remove-q-btn">Remove question</button>
      </div>
    </div>`;
}

// Resolve which color carries the "Quiz-worthy" role from a per-project
// color_config (the block served by /api/state). Matches on the label
// generate.py / preprocess.py assign ("Quiz-worthy facts"); falls back to the
// default red mapping when color_config is absent or doesn't name the role.
// cfg is a parameter (defaulting to the live appState) so it's unit-testable.
function quizWorthyRole(cfg = (typeof appState !== "undefined" ? appState.color_config : null) ?? {}) {
  for (const [color, meta] of Object.entries(cfg)) {
    if ((meta?.label ?? "").toLowerCase().startsWith("quiz-worthy")) {
      return { color, label: meta.label };
    }
  }
  return { color: "red", label: "Quiz-worthy facts" };
}

function renderQuizSection(sectionId) {
  const secState  = appState.sections[sectionId] ?? {};
  const narrative = secState.narrative ?? {};
  const approved  = secState.narrative_approved ?? false;
  const quiz      = secState.quiz ?? {};
  const quizApproved = secState.quiz_approved ?? false;
  const questions = quiz.questions ?? [];
  const hasQuiz   = questions.length > 0;
  const quizError = secState.quiz_error ?? "";

  if (!approved) return ""; // only show approved narrative sections

  // Count quiz-worthy annotations for the gate prompt, keyed off the
  // configurable "Quiz-worthy" role (color_config is now served by /api/state)
  // rather than the literal "red" — so a remapped color config is honored.
  const { color: quizColor, label: quizLabel } = quizWorthyRole();
  const sourceAnnotations = secState.source_annotations ?? [];
  const quizWorthyCount = sourceAnnotations.filter((a) => a.color === quizColor).length;

  const badgeClass = quizApproved ? "approved" : hasQuiz ? "pending" : "error";
  const badgeText  = quizApproved ? "✓ Approved" : hasQuiz ? "Pending review" : "No quiz yet";

  // Red error badge surfaced when the previous quiz LLM call failed
  const errorBadge = quizError
    ? `<span class="badge error-llm" title="${escapeHtml(quizError)}">⚠ ${escapeHtml(
        quizError.length > 140 ? quizError.slice(0, 140) + "…" : quizError
      )}</span>`
    : "";

  const questionsHtml = questions
    .map((q, i) => renderQuestionCard(q, i, sectionId))
    .join("");

  return `
    <div class="quiz-section-card ${quizApproved ? "approved" : ""}" data-id="${sectionId}">
      <div class="quiz-section-header">
        <h2>${escapeHtml(narrative.heading || sectionId)}</h2>
        <span class="badge ${badgeClass}">${badgeText}</span>
        <span class="badge">${questions.length} question${questions.length !== 1 ? "s" : ""}</span>
        ${errorBadge}
      </div>

      <div class="quiz-body">
        <!-- Narrative summary (read-only) -->
        <div class="narrative-summary">
          <strong>${escapeHtml(narrative.heading ?? sectionId)}</strong>
          ${narrative.intro ? `<p class="narrative-summary-intro">${escapeHtml(narrative.intro)}</p>` : ""}
        </div>

        <!-- Questions -->
        <div class="question-list">
          ${hasQuiz ? questionsHtml : `
            <div class="empty-state empty-state--padded">
              ${quizError ? `
                <p>Quiz generation failed.</p>
                <button class="generate-btn gen-quiz-btn empty-state-btn" data-section="${sectionId}">Generate with LLM</button>
                <button class="generate-btn retry-quiz-btn empty-state-btn" data-section="${sectionId}">↻ Retry quiz</button>
              ` : `
                <p>This section has <strong>${quizWorthyCount}</strong> &#8220;${escapeHtml(quizLabel)}&#8221; annotation${quizWorthyCount !== 1 ? "s" : ""}.</p>
                <button class="generate-btn gen-quiz-btn empty-state-btn" data-section="${sectionId}">Generate with LLM</button>
                <p class="empty-state-hint gate-prompt-hint">
                  Or, add more &#8220;${escapeHtml(quizLabel)}&#8221; (${escapeHtml(quizColor)}) highlights in Zotero, re-export,
                  and <button class="link-btn go-to-pipeline-btn" type="button">re-upload your export</button>
                  to give the LLM more to work with.
                </p>
              `}
            </div>`}
        </div>

        <div class="quiz-footer">
          ${hasQuiz ? `
            <button class="approve-btn ${quizApproved ? "unapprove" : ""}"
                    data-section="${sectionId}" data-approved="${quizApproved}">
              ${quizApproved ? "Unapprove" : "✓ Approve Quiz"}
            </button>
            <button class="generate-btn gen-quiz-btn" data-section="${sectionId}">↻ Regenerate All</button>
            <button class="add-btn add-question-btn" data-section="${sectionId}">+ Add Question</button>
          ` : ""}
        </div>
      </div>
    </div>`;
}

// ── Full quiz tab render ───────────────────────────────────────────────────

function renderQuizTab() {
  debouncedSaveQuiz.flush();
  const container = document.getElementById("quiz-sections");
  const order     = appState.section_order ?? [];
  const approvedSections = order.filter((id) => appState.sections[id]?.narrative_approved);

  if (!approvedSections.length) {
    container.innerHTML = `<div class="empty-state">
      <p>No approved narrative sections yet.</p>
      <p class="empty-state-hint">
        Approve sections in the Narrative Review tab first.
      </p>
    </div>`;
    return;
  }

  container.innerHTML = approvedSections.map(renderQuizSection).join("");

  const quizApprovedCount = approvedSections.filter(
    (id) => appState.sections[id]?.quiz_approved
  ).length;
  document.getElementById("progress-label").textContent =
    `${quizApprovedCount} / ${approvedSections.length} quiz sections approved`;

  attachQuizListeners();
}

// ── Quiz event listeners ───────────────────────────────────────────────────

function attachQuizCardListeners(card, sid) {
  card.querySelectorAll("[contenteditable]").forEach((el) =>
    el.addEventListener("input", () => debouncedSaveQuiz(sid))
  );
  card.querySelector(".remove-q-btn")?.addEventListener("click", () => {
    const container = document.getElementById("quiz-sections");
    card.remove();
    container.querySelectorAll(".question-num").forEach((el, i) => {
      el.textContent = `Q${i + 1}`;
    });
    debouncedSaveQuiz(sid);
  });
  card.querySelector(".regen-q-btn")?.addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    const ids = btn.dataset.ids.split(",").filter(Boolean);
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span>';
    try {
      const result = await regenerateQuestion(sid, ids);
      if (!result.question) throw new Error("No question returned");
      const idx = [...card.parentElement.children].indexOf(card);
      const tmp = document.createElement("div");
      tmp.innerHTML = renderQuestionCard(result.question, idx, sid);
      const newCard = tmp.firstElementChild;
      card.replaceWith(newCard);
      attachQuizCardListeners(newCard, sid);
      debouncedSaveQuiz(sid);
      toast("Question regenerated");
    } catch (err) {
      toast(`Error: ${err.message}`, 4000);
      btn.disabled = false;
      btn.textContent = "↻ Regenerate";
    }
  });
  card.querySelectorAll(".remove-distractor").forEach((btn) => {
    btn.addEventListener("click", () => {
      btn.closest(".distractor-row").remove();
      debouncedSaveQuiz(sid);
    });
  });
  card.querySelector(".add-distractor-btn")?.addEventListener("click", () => {
    const list = card.querySelector(".distractor-list");
    const row  = document.createElement("div");
    row.innerHTML = renderDistractorRow("", sid);
    const newRow = row.firstElementChild;
    list.appendChild(newRow);
    const editable = newRow.querySelector("[contenteditable]");
    editable.focus();
    editable.addEventListener("input", () => debouncedSaveQuiz(sid));
    newRow.querySelector(".remove-distractor").addEventListener("click", () => {
      newRow.remove();
      debouncedSaveQuiz(sid);
    });
  });
}

function attachQuizListeners() {
  const container = document.getElementById("quiz-sections");

  // Toggle open/close
  container.querySelectorAll(".quiz-section-header").forEach((header) => {
    header.addEventListener("click", (e) => {
      if (e.target.closest("button")) return;
      header.nextElementSibling.classList.toggle("open");
    });
  });

  // Auto-open unapproved quiz sections
  container.querySelectorAll(".quiz-section-card").forEach((sectionCard) => {
    if (!appState.sections[sectionCard.dataset.id]?.quiz_approved) {
      sectionCard.querySelector(".quiz-body")?.classList.add("open");
    }
  });

  // Attach per-card listeners to all rendered question cards
  container.querySelectorAll(".quiz-section-card").forEach((sectionCard) => {
    const sid = sectionCard.dataset.id;
    sectionCard.querySelectorAll(".question-card").forEach((card) => {
      attachQuizCardListeners(card, sid);
    });
  });

  // Generate / regenerate all quiz questions for a section (and Retry button)
  container.querySelectorAll(".gen-quiz-btn, .retry-quiz-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const sid = btn.dataset.section;
      const isRetry = btn.classList.contains("retry-quiz-btn");
      const originalText = btn.textContent;
      btn.disabled = true;
      btn.innerHTML = '<span class="spinner"></span> Generating…';
      try {
        const result = await generateQuiz(sid);
        appState.sections[sid] = {
          ...appState.sections[sid],
          quiz: result.quiz,
          quiz_approved: false,
        };
        // Successful retry: clear the local mirror of the error
        delete appState.sections[sid].quiz_error;
        toast(isRetry ? "Retry succeeded" : "Quiz generated");
        renderQuizTab();
      } catch (err) {
        toast(`Error: ${err.message}`, 4000);
        appState.sections[sid] = {
          ...(appState.sections[sid] ?? {}),
          quiz_error: err.message,
        };
        btn.disabled = false;
        btn.textContent = originalText || "Generate with LLM";
      }
    });
  });

  // Approve / unapprove quiz
  container.querySelectorAll(".approve-btn[data-section]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const sid     = btn.dataset.section;
      const current = btn.dataset.approved === "true";
      const next    = !current;
      await approveQuiz(sid, next);
      appState.sections[sid].quiz_approved = next;
      toast(next ? "Quiz approved ✓" : "Approval removed");
      renderQuizTab();
      if (activeTabId() === "tab-export") loadExportPreview();
    });
  });

  // Add blank question
  container.querySelectorAll(".add-question-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const sid   = btn.dataset.section;
      const list  = document.querySelector(`.quiz-section-card[data-id="${CSS.escape(sid)}"] .question-list`);
      const blank = {
        question_text: "",
        correct_answer: "",
        distractors: ["", ""],
        explanation_if_correct: "",
        explanation_if_incorrect: "",
        source_annotation_ids: [],
      };
      const idx = list.querySelectorAll(".question-card").length;
      const tmp = document.createElement("div");
      tmp.innerHTML = renderQuestionCard(blank, idx, sid);
      const newCard = tmp.firstElementChild;
      list.appendChild(newCard);
      attachQuizCardListeners(newCard, sid);
      newCard.querySelector("[contenteditable]")?.focus();
      debouncedSaveQuiz(sid);
    });
  });

  // Re-upload hint — navigate to the Pipeline tab so the instructor can
  // upload a new Zotero export without leaving the app.
  container.querySelectorAll(".go-to-pipeline-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelector('[data-tab="tab-pipeline"]')?.click();
    });
  });
}
