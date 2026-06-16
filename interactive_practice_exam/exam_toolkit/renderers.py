"""
renderers.py — HTML renderer functions for the new exam block types.

Each render_*() function accepts a data dict (as built by question_types.py)
and a section index, and returns an HTML string.

To activate these renderers, import EXAM_BLOCK_RENDERERS and merge it into
build_guide.BLOCK_RENDERERS before calling build_guide.build().

Also exports:
  state_to_sections_exam()  — replacement for build_guide.state_to_sections()
                               that honours the _blocks_override key.
  EXAM_CSS                  — additional CSS to append to guide.css
  EXAM_JS                   — additional JS to append to guide.js
"""

from __future__ import annotations

import json


# ---------------------------------------------------------------------------
# Escaping helpers (mirrored from build_guide to avoid circular import)
# ---------------------------------------------------------------------------

def _e(s) -> str:
    return (str(s)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))


def _ea(s) -> str:
    return _e(s).replace("'", "&#39;")


# ---------------------------------------------------------------------------
# CSS additions
# ---------------------------------------------------------------------------

EXAM_CSS = """
/* ── Exam toolkit additions ─────────────────────────────────────────────── */

/* Interactive area wrapper */
.interactive-area {
  background: var(--surface);
  border-radius: 1.2rem;
  margin: 1rem 0 1.2rem;
  border: 1px solid var(--border);
  padding: 1rem 1.2rem;
}

/* Vocab list box */
.vocab-list-box {
  background: var(--kp-bg);
  padding: 1rem 1.5rem;
  border-radius: 1.5rem;
  margin: 0.8rem 0 1.2rem;
  border: 1px solid var(--border);
}

.word-pair {
  display: flex;
  flex-wrap: wrap;
  gap: 1.8rem;
  justify-content: space-between;
}

.word-col {
  flex: 1;
  min-width: 160px;
}

.word-item {
  margin: 0.5rem 0;
  font-weight: 500;
  font-family: 'Courier New', monospace;
  font-size: 14px;
}

/* Matching cards */
.match-zone {
  display: flex;
  flex-wrap: wrap;
  gap: 1rem;
  margin: 1rem 0;
}

.match-card {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 2rem;
  padding: 0.3rem 0.9rem;
  cursor: pointer;
  transition: background 0.1s, box-shadow 0.1s;
  user-select: none;
  font-size: 14px;
  font-family: 'Courier New', monospace;
}

.match-card.selected {
  background: var(--accent-soft);
  border-color: var(--accent);
  box-shadow: 0 0 0 2px var(--accent);
}

.match-card.matched-correct {
  background: #dcfce7;
  border-color: var(--correct);
  color: #14532d;
  cursor: default;
}

.match-pair-status {
  margin: 0.5rem 0 0.3rem;
  font-style: italic;
  color: var(--muted);
  font-size: 13px;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}

/* Reveal / check buttons */
.exam-btn {
  background: var(--bg);
  border: 1px solid var(--border);
  font-weight: 600;
  padding: 0.45rem 1.1rem;
  border-radius: 2rem;
  font-size: 0.85rem;
  cursor: pointer;
  color: var(--text);
  transition: background 0.15s;
  margin-top: 0.5rem;
  margin-right: 0.5rem;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}

.exam-btn:hover, .exam-btn:focus {
  background: var(--accent-soft);
  border-color: var(--accent);
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

/* Hidden answer div */
.answer-hidden {
  background: var(--kp-bg);
  border-left: 5px solid var(--accent);
  padding: 0.8rem 1rem;
  border-radius: 0.8rem;
  margin-top: 0.8rem;
  display: none;
  font-size: 0.92rem;
  color: var(--text);
  line-height: 1.6;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}

.answer-hidden.visible {
  display: block;
}

/* Free-text textareas & text inputs */
.exam-label {
  display: block;
  font-size: 14px;
  font-weight: 600;
  margin: 0.7rem 0 0.2rem;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  color: var(--text);
}

.exam-textarea {
  width: 100%;
  border-radius: 0.7rem;
  border: 1px solid var(--border);
  background: var(--bg);
  padding: 0.6rem 0.9rem;
  font-family: Georgia, serif;
  font-size: 0.95rem;
  resize: vertical;
  margin: 0.2rem 0 0.5rem;
  color: var(--text);
}

.exam-textarea:focus {
  outline: 3px solid var(--accent);
  outline-offset: 2px;
  border-color: var(--accent);
}

.exam-text-input {
  border-radius: 0.6rem;
  border: 1px solid var(--border);
  background: var(--bg);
  padding: 0.4rem 0.7rem;
  font-family: Georgia, serif;
  font-size: 0.95rem;
  color: var(--text);
  width: 75%;
}

.exam-text-input:focus {
  outline: 3px solid var(--accent);
  outline-offset: 2px;
  border-color: var(--accent);
}

.exam-letter-input {
  border-radius: 0.5rem;
  border: 1px solid var(--border);
  background: var(--bg);
  padding: 0.3rem 0.5rem;
  font-family: 'Courier New', monospace;
  font-size: 0.95rem;
  color: var(--text);
  width: 3.5rem;
  text-align: center;
}

.exam-letter-input:focus {
  outline: 3px solid var(--accent);
  outline-offset: 2px;
}

.inline-code {
  font-family: 'Courier New', monospace;
  background: var(--border);
  padding: 0.1rem 0.35rem;
  border-radius: 6px;
  font-size: 0.88rem;
}

/* Reading passage box */
.text-passage-box {
  background: var(--kp-bg);
  border-radius: 1.2rem;
  padding: 1rem 1.2rem;
  margin: 1rem 0;
  border-left: 5px solid var(--muted);
  font-size: 15px;
  line-height: 1.7;
}

.text-passage-box p + p {
  margin-top: 0.8em;
}

/* Expression list */
.expression-list {
  font-size: 14px;
  color: var(--muted);
  margin-bottom: 0.6rem;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}

/* Preamble instruction line */
.exam-preamble {
  font-size: 14px;
  font-weight: 600;
  margin-bottom: 0.7rem;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  color: var(--text);
}

/* Stem row for text-input and letter-choice */
.stem-row {
  margin: 0.8rem 0;
  font-size: 15px;
  line-height: 1.6;
  font-family: Georgia, serif;
}

/* Exam footer */
.exam-footer {
  text-align: center;
  font-size: 0.9rem;
  color: var(--muted);
  margin-top: 2rem;
  font-style: italic;
  border-top: 1px solid var(--border);
  padding-top: 1.2rem;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}

/* Score toast */
#exam-score-toast {
  position: fixed;
  bottom: 1.4rem;
  right: 1.4rem;
  z-index: 9999;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 1rem;
  box-shadow: 0 4px 18px rgba(0,0,0,0.13);
  padding: 0.7rem 1.1rem 0.7rem 1rem;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  font-size: 0.9rem;
  display: flex;
  align-items: center;
  gap: 0.7rem;
  min-width: 180px;
  max-width: 260px;
  opacity: 0;
  transform: translateY(8px);
  transition: opacity 0.2s ease, transform 0.2s ease;
  pointer-events: none;
}

#exam-score-toast.visible {
  opacity: 1;
  transform: translateY(0);
  pointer-events: auto;
}

#exam-score-toast .toast-fraction {
  font-size: 1.3rem;
  font-weight: 700;
  color: var(--accent);
  flex-shrink: 0;
  line-height: 1;
}

#exam-score-toast .toast-label {
  color: var(--muted);
  font-size: 0.82rem;
  line-height: 1.3;
}

#exam-score-toast .toast-close {
  background: none;
  border: none;
  cursor: pointer;
  color: var(--muted);
  font-size: 1rem;
  padding: 0 0 0 0.3rem;
  margin-left: auto;
  flex-shrink: 0;
  line-height: 1;
}

#exam-score-toast .toast-close:hover { color: var(--text); }

@media (max-width: 680px) {
  .word-pair { flex-direction: column; }
  .exam-text-input { width: 100%; }
  #exam-score-toast { bottom: 0.8rem; right: 0.8rem; left: 0.8rem; max-width: none; }
}
"""


# ---------------------------------------------------------------------------
# JS additions — one self-contained IIFE appended after guide.js
# ---------------------------------------------------------------------------

EXAM_JS = r"""
/* ── Exam toolkit interactivity ─────────────────────────────────────────── */
(function () {
  "use strict";

  // ── Score toast ──────────────────────────────────────────────────────────
  const toast = document.createElement("div");
  toast.id = "exam-score-toast";
  toast.innerHTML =
    '<span class="toast-fraction"></span>' +
    '<span class="toast-label"></span>' +
    '<button class="toast-close" aria-label="Fermer">✕</button>';
  document.body.appendChild(toast);

  const toastFraction = toast.querySelector(".toast-fraction");
  const toastLabel    = toast.querySelector(".toast-label");
  const toastClose    = toast.querySelector(".toast-close");
  let toastTimer = null;

  toastClose.addEventListener("click", () => hideToast());

  function showScore(correct, total, label) {
    clearTimeout(toastTimer);
    toastFraction.textContent = `${correct} / ${total}`;
    toastLabel.textContent    = label || "correcte(s)";
    toast.classList.add("visible");
    toastTimer = setTimeout(hideToast, 5000);
  }

  function hideToast() {
    toast.classList.remove("visible");
  }

  // ── Utility: accent-tolerant normalisation ───────────────────────────────
  function normalise(s) {
    return s.trim().toLowerCase()
      .normalize("NFD").replace(/[̀-ͯ]/g, "")
      .replace(/\s+/g, " ");
  }

  // ── Matching widgets ─────────────────────────────────────────────────────
  document.querySelectorAll(".matching-widget").forEach(widget => {
    const leftWords  = JSON.parse(widget.dataset.left);
    const rightWords = JSON.parse(widget.dataset.right);
    const correctMap = JSON.parse(widget.dataset.correct);
    const zone       = widget.querySelector(".match-zone");
    const statusEl   = widget.querySelector(".match-pair-status");
    const checkBtn   = widget.querySelector(".match-check-btn");
    const revealBtn  = widget.querySelector(".match-reveal-btn");
    const answerDiv  = widget.querySelector(".answer-hidden");

    let selectedLeft = null;
    const pairs = new Array(leftWords.length).fill(null);

    function render() {
      zone.innerHTML = "";
      const leftCol  = document.createElement("div");
      leftCol.className = "word-col";
      const rightCol = document.createElement("div");
      rightCol.className = "word-col";

      leftWords.forEach((word, li) => {
        const card = document.createElement("div");
        card.className = "match-card";
        card.textContent = `${li + 1}. ${word}`;
        if (pairs[li] !== null) card.classList.add("matched-correct");
        card.addEventListener("click", () => {
          if (pairs[li] !== null) return;
          zone.querySelectorAll(".match-card").forEach(c => c.classList.remove("selected"));
          card.classList.add("selected");
          selectedLeft = li;
        });
        leftCol.appendChild(card);
      });

      rightWords.forEach((word, ri) => {
        const card = document.createElement("div");
        card.className = "match-card";
        card.textContent = `${String.fromCharCode(97 + ri)}. ${word}`;
        const alreadyUsed = pairs.includes(ri);
        if (alreadyUsed) card.classList.add("matched-correct");
        card.addEventListener("click", () => {
          if (selectedLeft === null || alreadyUsed) return;
          pairs[selectedLeft] = ri;
          selectedLeft = null;
          render();
          updateStatus();
        });
        rightCol.appendChild(card);
      });

      zone.appendChild(leftCol);
      zone.appendChild(rightCol);
    }

    function updateStatus() {
      const mapped = pairs.map((v, i) =>
        v !== null ? `${i + 1}→${String.fromCharCode(97 + v)}` : `${i + 1}→?`
      ).join(", ");
      statusEl.textContent = `📌 Associations : ${mapped}`;
    }

    if (checkBtn) {
      checkBtn.addEventListener("click", () => {
        let correct = 0;
        pairs.forEach((ri, li) => { if (ri !== null && correctMap[li] === ri) correct++; });
        showScore(correct, leftWords.length, "correspondance(s) correcte(s)");
        const sIdx = widget.closest(".guide-section")?.dataset.idx;
        if (sIdx !== undefined) window.markSectionDone(parseInt(sIdx, 10));
      });
    }

    if (revealBtn && answerDiv) {
      revealBtn.addEventListener("click", () => {
        answerDiv.classList.add("visible");
        revealBtn.disabled = true;
        const sIdx = widget.closest(".guide-section")?.dataset.idx;
        if (sIdx !== undefined) window.markSectionDone(parseInt(sIdx, 10));
      });
    }

    render();
    updateStatus();
  });

  // ── Text-input widgets (conditionnel passé etc.) ─────────────────────────
  document.querySelectorAll(".text-input-widget").forEach(widget => {
    const answers  = JSON.parse(widget.dataset.answers);
    const checkBtn = widget.querySelector(".ti-check-btn");
    const revealBtn = widget.querySelector(".ti-reveal-btn");
    const answerDiv = widget.querySelector(".answer-hidden");
    const inputs    = widget.querySelectorAll(".exam-text-input");

    if (checkBtn) {
      checkBtn.addEventListener("click", () => {
        let correct = 0;
        inputs.forEach((inp, i) => {
          if (normalise(inp.value) === normalise(answers[i] || "")) correct++;
        });
        showScore(correct, inputs.length, "correcte(s) — voir corrigé pour formes exactes");
        const sIdx = widget.closest(".guide-section")?.dataset.idx;
        if (sIdx !== undefined) window.markSectionDone(parseInt(sIdx, 10));
      });
    }

    if (revealBtn && answerDiv) {
      revealBtn.addEventListener("click", () => {
        answerDiv.classList.add("visible");
        revealBtn.disabled = true;
        const sIdx = widget.closest(".guide-section")?.dataset.idx;
        if (sIdx !== undefined) window.markSectionDone(parseInt(sIdx, 10));
      });
    }
  });

  // ── Letter-choice widgets (Si clauses etc.) ──────────────────────────────
  document.querySelectorAll(".letter-choice-widget").forEach(widget => {
    const correct   = JSON.parse(widget.dataset.correct);
    const checkBtn  = widget.querySelector(".lc-check-btn");
    const revealBtn = widget.querySelector(".lc-reveal-btn");
    const answerDiv = widget.querySelector(".answer-hidden");
    const inputs    = widget.querySelectorAll(".exam-letter-input");

    if (checkBtn) {
      checkBtn.addEventListener("click", () => {
        let ok = 0;
        inputs.forEach((inp, i) => {
          if (inp.value.trim().toLowerCase().replace(/[.\s]+$/, '') === (correct[i] || "").toLowerCase()) ok++;
        });
        showScore(ok, inputs.length, "réponse(s) correcte(s)");
        const sIdx = widget.closest(".guide-section")?.dataset.idx;
        if (sIdx !== undefined) window.markSectionDone(parseInt(sIdx, 10));
      });
    }

    if (revealBtn && answerDiv) {
      revealBtn.addEventListener("click", () => {
        answerDiv.classList.add("visible");
        revealBtn.disabled = true;
        const sIdx = widget.closest(".guide-section")?.dataset.idx;
        if (sIdx !== undefined) window.markSectionDone(parseInt(sIdx, 10));
      });
    }
  });

  // ── Reveal-QA widgets ────────────────────────────────────────────────────
  document.querySelectorAll(".reveal-qa-widget").forEach(widget => {
    const revealBtn = widget.querySelector(".rqa-reveal-btn");
    const answerDiv = widget.querySelector(".answer-hidden");
    if (revealBtn && answerDiv) {
      revealBtn.addEventListener("click", () => {
        answerDiv.classList.add("visible");
        revealBtn.disabled = true;
        const sIdx = widget.closest(".guide-section")?.dataset.idx;
        if (sIdx !== undefined) window.markSectionDone(parseInt(sIdx, 10));
      });
    }
  });

  // ── Free-text widgets ────────────────────────────────────────────────────
  document.querySelectorAll(".free-text-widget").forEach(widget => {
    const revealBtn = widget.querySelector(".ft-reveal-btn");
    const answerDiv = widget.querySelector(".answer-hidden");
    if (revealBtn && answerDiv) {
      revealBtn.addEventListener("click", () => {
        answerDiv.classList.add("visible");
        revealBtn.disabled = true;
        const sIdx = widget.closest(".guide-section")?.dataset.idx;
        if (sIdx !== undefined) window.markSectionDone(parseInt(sIdx, 10));
      });
    }
  });

})();
"""


# ---------------------------------------------------------------------------
# Block renderers
# ---------------------------------------------------------------------------

def _unique_id(prefix: str, section_idx: int, suffix: str = "") -> str:
    return f"{prefix}-s{section_idx}{('-' + suffix) if suffix else ''}"


def render_matching_block(data: dict, section_idx: int) -> str:
    left_words  = data.get("left_words", [])
    right_words = data.get("right_words", [])
    correct_map = data.get("correct_map", [])
    reveal_text = data.get("reveal_text", "")
    wid = f"match-s{section_idx}"
    answer_id = f"answer-{wid}"

    left_col_html = "".join(
        f'<div class="word-item">{i+1}. {_e(w)}</div>'
        for i, w in enumerate(left_words)
    )
    right_col_html = "".join(
        f'<div class="word-item">{chr(97+i)}. {_e(w)}</div>'
        for i, w in enumerate(right_words)
    )

    reveal_html = (
        f'<div id="{answer_id}" class="answer-hidden">✅ {_e(reveal_text)}</div>'
        if reveal_text else ""
    )
    reveal_btn = (
        f'<button class="exam-btn match-reveal-btn">🔍 Voir les bonnes réponses</button>'
        if reveal_text else ""
    )

    return f"""
<div class="block interactive-area matching-widget"
     data-wid="{wid}"
     data-left="{_ea(json.dumps(left_words, ensure_ascii=False))}"
     data-right="{_ea(json.dumps(right_words, ensure_ascii=False))}"
     data-correct="{_ea(json.dumps(correct_map))}">
  <div class="vocab-list-box">
    <p><strong>📋 Listes à associer :</strong></p>
    <div class="word-pair">
      <div class="word-col">{left_col_html}</div>
      <div class="word-col">{right_col_html}</div>
    </div>
  </div>
  <div class="match-zone"></div>
  <div class="match-pair-status"></div>
  <button class="exam-btn match-check-btn">✓ Vérifier mon association</button>
  {reveal_btn}
  {reveal_html}
</div>"""


def render_free_text_block(data: dict, section_idx: int) -> str:
    prompts       = data.get("prompts", [])
    placeholder   = data.get("placeholder", "Écrivez votre réponse...")
    reveal_label  = data.get("reveal_label", "💡 Exemples de réponses")
    sample_answers = data.get("sample_answers", [])
    expression_list = data.get("expression_list", "")
    wid_suffix    = data.get("wid_suffix", "")
    wid = f"ft-s{section_idx}{wid_suffix}"
    answer_id = f"answer-{wid}"

    expr_html = (
        f'<p class="expression-list"><strong>Expressions :</strong> {_e(expression_list)}</p>'
        if expression_list else ""
    )

    inputs_html = ""
    for i, prompt in enumerate(prompts):
        field_id = f"{wid}-p{i}"
        inputs_html += f"""
  <label class="exam-label" for="{field_id}">{_e(prompt)}</label>
  <textarea id="{field_id}" class="exam-textarea" rows="2" placeholder="{_ea(placeholder)}"></textarea>"""

    samples_html = "".join(
        f"<br>• {s}" for s in sample_answers
    )
    answer_html = (
        f'<div id="{answer_id}" class="answer-hidden">📘 Suggestions :{samples_html}</div>'
        if sample_answers else ""
    )
    reveal_btn = (
        f'<button class="exam-btn ft-reveal-btn">{_e(reveal_label)}</button>'
        if sample_answers else ""
    )

    return f"""
<div class="block interactive-area free-text-widget" data-wid="{wid}">
  {expr_html}
  {inputs_html}
  {reveal_btn}
  {answer_html}
</div>"""


def render_text_input_block(data: dict, section_idx: int) -> str:
    items        = data.get("items", [])
    check_label  = data.get("check_label", "✔️ Vérifier")
    reveal_label = data.get("reveal_label", "📖 Afficher corrigé")
    preamble     = data.get("preamble", "")
    wid = f"ti-s{section_idx}"
    answer_id = f"answer-{wid}"

    # Build JS answer array (normalised) and correction HTML simultaneously
    js_answers = []
    stems_html = ""
    correction_lines = []

    for i, item in enumerate(items):
        stem            = item.get("stem", "")
        hint            = item.get("hint", "")
        answer          = item.get("answer", "")
        answer_display  = item.get("answer_display", answer)
        field_id        = f"{wid}-i{i}"

        js_answers.append(answer)

        hint_html = f' <span class="inline-code">{_e(hint)}</span>' if hint else ""
        # Replace ____ in stem with the input field
        stem_with_input = _e(stem).replace(
            "____",
            f'<input type="text" id="{field_id}" class="exam-text-input" placeholder="Votre réponse">'
        )
        stems_html += f'<div class="stem-row">{i+1}. {stem_with_input}{hint_html}</div>\n'
        correction_lines.append(f"{i+1}. {answer_display}")

    correction_html = "<br>".join(correction_lines)
    preamble_html = (
        f'<p class="exam-preamble">{_e(preamble)}</p>' if preamble else ""
    )

    return f"""
<div class="block interactive-area text-input-widget"
     data-answers="{_ea(json.dumps(js_answers, ensure_ascii=False))}">
  {preamble_html}
  {stems_html}
  <button class="exam-btn ti-check-btn">{_e(check_label)}</button>
  <button class="exam-btn ti-reveal-btn">{_e(reveal_label)}</button>
  <div id="{answer_id}" class="answer-hidden">
    <strong>Correction :</strong><br>{correction_html}
  </div>
</div>"""


def render_letter_choice_block(data: dict, section_idx: int) -> str:
    items            = data.get("items", [])
    correct_answers  = data.get("correct_answers", [])
    check_label      = data.get("check_label", "🔎 Vérifier mes réponses")
    reveal_label     = data.get("reveal_label", "📝 Montrer le corrigé")
    wid = f"lc-s{section_idx}"
    answer_id = f"answer-{wid}"

    stems_html = ""
    for i, item in enumerate(items):
        stem    = item.get("stem", "")
        options = item.get("options", [])
        field_id = f"{wid}-i{i}"

        options_str = " / ".join(
            f"{chr(97+j)}: {_e(opt)}" for j, opt in enumerate(options)
        )
        stems_html += f"""
<div class="stem-row">
  {i+1}. {_e(stem)}<br>
  <input type="text" id="{field_id}" class="exam-letter-input" placeholder="a/b/c" maxlength="1">
  <span style="font-size:13px; color:var(--muted); font-family:monospace;">({options_str})</span>
</div>"""

    key_str = ", ".join(
        f"{i+1}-{ans}" for i, ans in enumerate(correct_answers)
    )

    return f"""
<div class="block interactive-area letter-choice-widget"
     data-correct="{_ea(json.dumps(correct_answers))}">
  {stems_html}
  <button class="exam-btn lc-check-btn">{_e(check_label)}</button>
  <button class="exam-btn lc-reveal-btn">{_e(reveal_label)}</button>
  <div id="{answer_id}" class="answer-hidden">
    ✅ Réponses : {_e(key_str)}
  </div>
</div>"""


def render_text_passage_block(data: dict, section_idx: int) -> str:
    title      = data.get("title", "")
    paragraphs = data.get("paragraphs", [])

    title_html = f"<p><strong>{_e(title)}</strong></p>" if title else ""
    paras_html = "".join(f"<p>{_e(p)}</p>" for p in paragraphs)

    return f"""
<div class="block text-passage-box">
  {title_html}
  {paras_html}
</div>"""


def render_reveal_qa_block(data: dict, section_idx: int) -> str:
    preamble       = data.get("preamble", "")
    questions      = data.get("questions", [])
    model_answers  = data.get("model_answers", [])
    check_label    = data.get("check_label", "📖 Vérifier compréhension")
    wid = f"rqa-s{section_idx}"
    answer_id = f"answer-{wid}"

    preamble_html = (
        f'<p class="exam-preamble">{_e(preamble)}</p>' if preamble else ""
    )

    inputs_html = ""
    for i, q in enumerate(questions):
        field_id = f"{wid}-q{i}"
        inputs_html += f"""
  <label class="exam-label" for="{field_id}">{_e(q)}</label>
  <textarea id="{field_id}" class="exam-textarea" rows="2"></textarea>"""

    answers_html = "".join(
        f"<br>{i+1}. {a}" for i, a in enumerate(model_answers)
    )

    return f"""
<div class="block interactive-area reveal-qa-widget">
  {preamble_html}
  {inputs_html}
  <button class="exam-btn rqa-reveal-btn">{_e(check_label)}</button>
  <div id="{answer_id}" class="answer-hidden">
    ✅ <strong>Réponses suggérées :</strong>{answers_html}
  </div>
</div>"""


# ---------------------------------------------------------------------------
# Renderer dispatch table (merge into build_guide.BLOCK_RENDERERS)
# ---------------------------------------------------------------------------

EXAM_BLOCK_RENDERERS: dict = {
    "matching":      render_matching_block,
    "free_text":     render_free_text_block,
    "text_input":    render_text_input_block,
    "letter_choice": render_letter_choice_block,
    "text_passage":  render_text_passage_block,
    "reveal_qa":     render_reveal_qa_block,
}


# ---------------------------------------------------------------------------
# Replacement state_to_sections that honours _blocks_override
# ---------------------------------------------------------------------------

def state_to_sections_exam(state: dict) -> list[dict]:
    """
    Drop-in replacement for build_guide.state_to_sections() that also handles
    sections produced by ExamConfig.to_state() (which store their block list
    in _blocks_override rather than the narrative/quiz/key_points fields).
    """
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
    from build_guide import state_to_sections as _original  # noqa: F401

    order    = state.get("section_order") or list(state.get("sections", {}).keys())
    sections = []

    for sid in order:
        sec = state.get("sections", {}).get(sid, {})
        if not sec.get("narrative_approved"):
            continue

        blocks_override = sec.get("_blocks_override")
        if blocks_override is not None:
            # Exam-toolkit path: blocks are already fully specified
            sections.append({"section_id": sid, "blocks": blocks_override})
        else:
            # Legacy study-guide path: reconstruct blocks from narrative/quiz
            narrative = sec.get("narrative", {})
            quiz_data = sec.get("quiz", {})
            sources   = sorted({
                a.get("source_document", "")
                for a in sec.get("source_annotations", [])
                if a.get("source_document")
            })
            blocks = [
                {"type": "narrative", "data": {
                    "heading": narrative.get("heading", sid),
                    "intro":   narrative.get("intro", ""),
                }},
                {"type": "key_points", "data": {
                    "points": narrative.get("key_points", []),
                }},
                {"type": "figures", "data": {
                    "figures": narrative.get("figures", []),
                }},
            ]
            if quiz_data.get("questions"):
                blocks.append({"type": "quiz", "data": quiz_data})
            if sources:
                blocks.append({"type": "source_panel", "data": {"sources": sources}})
            sections.append({"section_id": sid, "blocks": blocks})

    return sections
