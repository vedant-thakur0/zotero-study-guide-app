/* guide.js — Student-facing study guide interactivity
   Inlined into output.html by build_guide.py */

"use strict";

(function () {

  // ── State ────────────────────────────────────────────────────────────────
  const sections   = document.querySelectorAll(".guide-section");
  const navLinks   = document.querySelectorAll(".nav-link");
  const navChecks  = document.querySelectorAll(".nav-check");
  const progress   = document.getElementById("progress-bar");
  const curLabel   = document.getElementById("current-section-label");

  const quizDone = new Set();  // section indices that have been fully answered

  // ── Sidebar toggle ───────────────────────────────────────────────────────
  const sidebar     = document.getElementById("sidebar");
  const mainWrap    = document.getElementById("main-wrap");
  const sidebarBtn  = document.getElementById("sidebar-toggle");

  function toggleSidebar() {
    sidebar.classList.toggle("collapsed");
    mainWrap.classList.toggle("sidebar-collapsed");
  }

  sidebarBtn?.addEventListener("click", toggleSidebar);
  document.getElementById("sidebar-close")?.addEventListener("click", toggleSidebar);

  // ── Scroll spy — highlight active nav link ───────────────────────────────
  const observer = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) {
          const idx = entry.target.dataset.idx;
          navLinks.forEach((l) => l.classList.remove("active"));
          const activeLink = document.querySelector(`.nav-link[data-idx="${idx}"]`);
          activeLink?.classList.add("active");
          curLabel.textContent = activeLink?.textContent.replace("✓", "").trim() ?? "";
          updateProgress();
        }
      });
    },
    { rootMargin: "-30% 0px -60% 0px" }
  );

  sections.forEach((s) => observer.observe(s));

  // ── Progress bar ─────────────────────────────────────────────────────────
  const progressText = document.getElementById("progress-text");

  function updateProgress() {
    const done  = document.querySelectorAll(".nav-check.done").length;
    const total = navChecks.length;
    if (!total) return;
    const pct = Math.round((done / total) * 100);
    progress?.style.setProperty("--progress", pct + "%");
    if (progressText) progressText.textContent = `${pct}%`;
  }

  function markSectionDone(idx) {
    const check = document.getElementById(`nav-check-${idx}`);
    if (check && !check.classList.contains("done")) {
      check.classList.add("done");
      const link = document.querySelector(`.nav-link[data-idx="${idx}"]`);
      link?.classList.add("completed");
    }
    updateProgress();
    checkAllDone();
  }
  window.markSectionDone = markSectionDone;

  // ── Quiz logic ───────────────────────────────────────────────────────────
  const allQuizBlocks = document.querySelectorAll(".block-quiz");

  allQuizBlocks.forEach((block) => {
    const sectionIdx = parseInt(block.dataset.section, 10);
    const questions  = block.querySelectorAll(".question-block");

    block.querySelectorAll(".option-btn").forEach((btn) => {
      btn.addEventListener("click", function () {
        const qid      = this.dataset.qid;
        const qBlock   = document.getElementById(qid);
        const feedback = document.getElementById(`${qid}-feedback`);
        const buttons  = qBlock.querySelectorAll(".option-btn");

        // Disable all options for this question
        buttons.forEach((b) => (b.disabled = true));

        const isCorrect = this.dataset.correct === "true";

        if (isCorrect) {
          this.classList.add("correct");
          feedback.textContent  = this.dataset.expOk || "Correct!";
          feedback.className    = "feedback correct-fb";
        } else {
          this.classList.add("wrong");
          feedback.textContent  = this.dataset.expBad || "Not quite.";
          feedback.className    = "feedback incorrect-fb";
          // Reveal correct answer
          buttons.forEach((b) => {
            if (b.dataset.correct === "true") b.classList.add("correct");
          });
        }

        // Check if all questions in this section are answered
        const answered = [...questions].every((q) =>
          q.querySelector(".option-btn:disabled")
        );
        if (answered && !quizDone.has(sectionIdx)) {
          quizDone.add(sectionIdx);
          markSectionDone(sectionIdx);
        }
      });
    });
  });


  // ── Summary panel ─────────────────────────────────────────────────────────
  function checkAllDone() {
    if (document.querySelectorAll(".nav-check.done").length === navChecks.length) {
      showSummary();
    }
  }

  function showSummary() {
    const panel   = document.getElementById("summary-panel");
    const text    = document.getElementById("summary-text");
    const content = document.getElementById("content");
    if (!panel) return;
    panel.hidden  = false;
    content.classList.add("summary-visible");
    if (text) {
      text.textContent = "You've completed all sections.";
    }
  }

  document.getElementById("restart-btn")?.addEventListener("click", () => {
    location.reload();
  });

  // ── Smooth scroll for nav links ──────────────────────────────────────────
  navLinks.forEach((link) => {
    link.addEventListener("click", (e) => {
      e.preventDefault();
      const target = document.querySelector(link.getAttribute("href"));
      target?.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  });

  // ── Init ─────────────────────────────────────────────────────────────────
  updateProgress();

})();
