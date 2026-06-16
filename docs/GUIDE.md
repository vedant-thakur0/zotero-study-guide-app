# Instructor Guide — Zotero Study Guide Builder

This guide walks you through using the pipeline from inside Zotero to a finished, distributable HTML file. It assumes you have Python 3.9+ installed and a local LLM running (or that you want to use `--dry-run` mode first).

---

## Part 1 — Annotating in Zotero

The pipeline reads color-coded highlights and the notes you attach to them. Your color choices are the primary signal the LLM uses to understand what role each excerpt plays.

### Default color meanings

| Color | Role | Use for |
|---|---|---|
| 🟡 Yellow | Key concept | Definitions, important terms, core ideas |
| 🔴 Red | Quiz-worthy | Specific dates, names, claims worth testing |
| 🟢 Green | People & orgs | Biographical info, institutional roles |
| 🔵 Blue | Theme / argument | Analytical threads, overarching narratives |
| 🟣 Purple | Connection | Cross-references, links between topics |
| 🟠 Orange | Example | Case studies, illustrative instances |

You can reassign these in `color_config.yaml` before running the pipeline.

### Annotation notes

The text you add as a note on a highlight appears in the verification app as an **instructor note**. Use these to leave reminders for yourself during review:

- `"Use as section intro"` — remind yourself why you highlighted this
- `"Good quiz question — specific dates"` — flags red annotations you especially want tested
- `"Contrast with Washington"` — connections to surface in the quiz or key points

### Section tagging (optional)

If you want to control exactly where section breaks fall, start a note with `#TagName`:

```
#reconstruction Use as section intro
#leadership Key figure card
```

Everything after a `#Tag` annotation belongs to that section until the next tag. If you don't use tags, the preprocessor groups by page proximity automatically.

---

## Part 2 — Exporting from Zotero

1. Select the PDF item in your Zotero library.
2. In the right panel, find the **Annotations** section and click **Create Note from Annotations**.
3. A new note appears. Right-click it → **Export Note**.
4. Save as **Zotero RDF** or use File → Export Note → save as HTML.

The HTML export (`.html`) is the most reliable format — `export.py` parses it directly from Zotero's native output structure.

---

## Part 3 — Running the pipeline

### Step 1 — Parse the export

```bash
python -m zsg.export --input Annotations.html --output projects/my_course/annotations.json
```

The script prints a color breakdown so you can confirm it read the highlights correctly:

```
Loaded 47 annotations from Annotations.html.
  blue     (Themes & arguments): 18
  green    (People & organizations): 12
  red      (Quiz-worthy facts): 8
  yellow   (Key concepts): 9
```

If a color count looks wrong, check `color_config.yaml` — your Zotero color may need remapping.

### Step 2 — Group into sections

```bash
python -m zsg.preprocess --input  projects/my_course/annotations.json \
                     --output projects/my_course/sections.json
```

Output shows you what sections were found and their page ranges:

```
Section ID                    Annotations         Pages  Role
----------------------------------------------------------------------
  reconstruction                        14       p.42–62  theme
  leadership                             9       p.63–78  people
  civil_rights_movement                 18       p.79–110 theme
  legislation                            6       p.111–125 concept
```

If the sections look wrong, you have two options:
- Add `#tags` to your Zotero annotations and re-export, or
- Adjust `--page-window` (default 6 pages) to be tighter or looser

### Step 3 — Generate narrative content

```bash
python -m zsg.generate narrative \
  --sections projects/my_course/sections.json \
  --state    projects/my_course/state.json
```

This calls your configured LLM once per section (Purdue GenAI Studio by default). Expect
roughly 20–40 seconds per section depending on the model and service load. The state is
saved after each section so you can interrupt and resume.

**First time using the pipeline?** Run with `--dry-run` to see the exact prompts without calling the LLM:

```bash
python -m zsg.generate narrative --sections ... --state ... --dry-run
```

### Step 4 — Open the verification app

```bash
python -m zsg.verify --state    projects/my_course/state.json \
                 --sections projects/my_course/sections.json
```

Opens at [http://localhost:5000](http://localhost:5000). Keep this running throughout review.

---

## Part 4 — Reviewing in the verification app

### Narrative Review tab

Each section card shows two panes side by side:

**Left pane — Source Annotations:** Your original highlighted text, color-coded and grouped. Each annotation shows its `ann_id`, the highlight, any note you attached, and the page number. Click an annotation to highlight which generated fields reference it.

**Right pane — Generated Content:** The LLM's output as editable fields. Click anywhere to type. Changes auto-save every 500ms.

**What to check:**
- Is the **heading** accurate and appropriately concise?
- Does the **intro** faithfully synthesize the blue annotations — without inventing claims?
- Does each **key point** match its source annotation exactly? The LLM sometimes paraphrases in ways that subtly change meaning.
- Are **figures** (people and organizations) described correctly?
- Is there an **"unused annotations" warning**? If so, drag the content into a key point or figure manually using the `+ Add` buttons.

**Controls:**
- `✓ Approve` — marks the section ready for quiz generation. You can unapprove and re-edit at any time.
- `↑ / ↓` — reorder sections. This order is what students will see.
- `↻ Regenerate` — re-runs the LLM for this section if you want a fresh draft.
- `+ Add Key Point` / `+ Add Figure` — add content the LLM missed.
- `×` — remove a key point or figure.

### Quiz Review tab

Only sections with approved narratives appear here. The left side shows the approved narrative (read-only). The right side shows generated questions.

**What to check:**
- Is the **question** clear and unambiguous?
- Is the **correct answer** accurate?
- Are the **distractors** plausible but clearly wrong? Replace anything that could confuse students with a genuine alternative answer.
- Do both **explanations** (correct and incorrect) add value, or are they redundant?

**Controls:**
- `↻ Regenerate` per question — re-runs just that question through the LLM.
- `+ Add distractor` / `✕` — add or remove answer options.
- `+ Add Question` — write your own question from scratch.
- `✓ Approve Quiz` — marks the section's quiz as ready for export.

---

## Part 5 — Building and distributing

### Generate quiz content

After approving narratives, run quiz generation:

```bash
python -m zsg.generate quiz \
  --sections projects/my_course/sections.json \
  --state    projects/my_course/state.json
```

Or use `↻ Regenerate All` per section in the Quiz Review tab to generate from the app directly.

### Export tab

Switch to the **Export** tab in the verification app:

1. Set the **title** (appears in the sidebar and browser tab).
2. Choose a **theme**: Light (default), Dark, or High Contrast.
3. Toggle **progress bar** on or off.
4. The **preview panel** on the right shows exactly what will be included — sections, key point counts, figure counts, question counts.
5. Click **Build Guide** — writes `output.html` to your project folder.
6. Click **Open in Browser** to preview before distributing.

Or build directly from the command line:

```bash
python -m zsg.build_guide \
  --state  projects/my_course/state.json \
  --output projects/my_course/output.html \
  --title  "The Quest for Equality — Module 7" \
  --theme  light
```

### Distributing to students

`output.html` is fully self-contained. Share it however works for your course:

- Upload to your LMS as a file attachment
- Share via Google Drive, Dropbox, or any file share
- Embed in a course page (most LMS platforms allow iframe embeds of HTML files)
- Email directly — it opens in any browser with no installation required

---

## Troubleshooting

**The LLM returns malformed JSON.**
The pipeline auto-repairs common failures (missing quotes, trailing commas, markdown fencing). If a section still fails, the error appears in the terminal. Run `generate.py --dry-run` to inspect the prompt, then try a different model or adjust `temperature` in `llm_config.yaml`.

**A section has an "unused annotations" warning.**
The LLM didn't reference every annotation. This is normal — not every highlight needs to become a key point. If the unused annotation contains important content, use `+ Add Key Point` to add it manually.

**The page-proximity grouping created sections that are too large or too small.**
Adjust `--page-window` in the `preprocess.py` call. A smaller value (e.g. `3`) creates more sections; a larger value (e.g. `12`) creates fewer. Alternatively, add `#tags` to your Zotero annotations to take direct control.

**Annotations are showing the wrong color.**
Check the `rgba` values in your Zotero HTML export against the palette in `export.py`. Zotero's exact color codes are fixed — if your export uses a non-standard color, add it to `ZOTERO_COLOR_MAP` in `export.py`.

**The verification app won't start.**
Make sure `sections.json` exists (run `preprocess.py` first). Check that port 5000 is free — use `--port 5001` if needed.

**The built guide looks wrong in the browser.**
Try opening in a different browser. The guide uses standard HTML5 with no external dependencies. If quiz buttons don't respond, check the browser console for JavaScript errors.
