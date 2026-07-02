# Zotero Study Guide Repository Documentation

> **Note.** The code lives in a `src/zsg/` package — invoke stages via
> `python -m zsg.<stage>` and the dev server via `python -m zsg.app`. The
> generation step is powered by **Purdue GenAI Studio** by default (other providers
> available). An earlier desktop-app packaging (`run.command`, `run.bat`, a `.spec`
> bundle) was **removed**; `app.py` is a plain local Flask dev server. A browser-first
> **client mode** (IndexedDB + stateless `/api/v2/*` API; see `static/client-mode.js`
> and `static/storage.js`) was added. The structure tree, run commands, schemas, and
> API reference below reflect the current layout. For the quickstart, see the top-level
> **[README](../README.md)**.
>
> **On "stateless."** Client mode stores no user content or API keys server-side (content →
> browser IndexedDB; keys → sent per-request, never written or logged). The one exception, on
> the *hosted* deployment only, is **anonymous per-call telemetry** persisted to S3 — latency,
> provider/model, success, and a salted one-way key fingerprint (never the raw key/prompt/
> response). See `src/zsg/metrics.py` and `deploy/README.md`.

## Overview

This repository contains a **complete pipeline for converting Zotero PDF annotation exports into interactive HTML study guides**. The tool is designed for academic instructors who use color-coded PDF annotations (via Zotero) and want to transform them into self-contained, student-facing study materials.

The application now includes a **local GUI app** that guides non-technical users through the entire pipeline without requiring terminal access.

## Repository Structure

```
zotero-study-guide/
├── pyproject.toml             Package metadata, deps, pytest config
├── requirements.txt           Python dependencies
├── app_config.json            Runtime config (LLM, colors, projects) — gitignored
├── llm_config.yaml            Power-user / CLI LLM config (defaults to Purdue GenAI Studio)
├── color_config.yaml          Annotation color meanings
│
├── src/zsg/                   The pipeline package — run via `python -m zsg.<stage>`
│   ├── __init__.py            Defines PKG_DIR (assets) and PROJECT_ROOT (data)
│   ├── app.py                 Local dev-server launcher (seeds app_config.json)
│   ├── export.py              Stage 1: Parse Zotero export → annotations.json
│   ├── preprocess.py          Stage 2: Group annotations → sections.json
│   ├── generate.py            Stage 3: Call LLM (call_llm = provider seam) → state.json
│   ├── verify.py              Stage 4: Flask app — /api/* (server state) + /api/v2/* (stateless)
│   ├── build_guide.py         Stage 5: Assemble approved content → output.html
│   ├── json_repair.py         Handle malformed LLM JSON output
│   ├── pipeline_runner.py     Subprocess runner for pipeline stages (LRU-bound, cancellable)
│   ├── prompts/               narrative.txt, quiz.txt, quiz_from_narrative.txt
│   ├── templates/
│   │   └── review.html        Single Page App shell (Setup, Pipeline, Review, Export tabs)
│   └── static/
│       ├── review.css/.js     SPA (Narrative, Quiz, Export tabs)
│       ├── app.css/.js        Setup & Pipeline tabs
│       ├── client-mode.js     Browser-first client mode
│       ├── storage.js         Client-mode (IndexedDB) state
│       └── guide.css/.js      Output-guide assets (inlined into output.html)
│
├── projects/                  Per-course working data (artifacts gitignored)
│   └── <course>/              annotations.json, sections.json, state.json, output.html
│
├── sample-data/               Example Zotero export + the source PDF
├── interactive_practice_exam/ Separate sub-tool: DOCX exams → interactive HTML
│   ├── build_exam.py / parse_exam.py
│   └── exam_toolkit/          exam_config.py, question_types.py, renderers.py
│
├── docs/                      GUIDE, REPOSITORY, GETTING_STARTED
└── tests/                     pytest suite (+ tests/e2e Playwright, tests/js Node, tests/fixtures)
```

---

## The Pipeline: 5-Stage Workflow

### Stage 1: `export.py` — Parse Zotero Export

**Input:** Zotero HTML export file (`.html`, `.md`, `.csv`, or `.json`)
**Output:** `annotations.json`

Parses color-coded highlights and instructor notes from a Zotero export. Each annotation is normalized with:
- **id** — unique identifier
- **text** — the highlighted passage
- **color** — normalized to `yellow|red|green|blue|purple|orange`
- **page** — source page number
- **instructor_note** — may begin with `#SectionTag`
- **source_document** — author/title string

**CLI Usage:**
```bash
python -m zsg.export --input zotero_export.html --output annotations.json
```

---

### Stage 2: `preprocess.py` — Cluster into Sections

**Input:** `annotations.json`
**Output:** `sections.json`

Groups annotations into named sections using one of three strategies:
1. **tags** — Instructor notes beginning with `#SectionName`
2. **proximity** — Annotations within N pages (default: 6) are grouped
3. **auto** — Try tags first; fall back to proximity

**CLI Usage:**
```bash
python -m zsg.preprocess --input annotations.json --output sections.json --strategy auto
```

---

### Stage 3: `generate.py` — LLM Generation

**Input:** `sections.json` + `state.json` (created if absent)
**Output:** Updated `state.json` with narrative and quiz data per section

Calls the configured LLM (Purdue GenAI Studio by default; also Anthropic Claude, Ollama, LM Studio, OpenAI-compatible) to generate:
- **Narrative** — section heading, intro, key points (term/explanation pairs), figures (biographical cards)
- **Quiz** — multiple-choice questions with correct answer, distractors, and explanations

Two sub-commands:
- `narrative` — Generate narratives for all sections (or `--only section_id`)
- `quiz` — Generate quiz questions for approved sections

**CLI Usage:**
```bash
python -m zsg.generate narrative --sections sections.json --state state.json
python -m zsg.generate quiz --sections sections.json --state state.json
```

**Environment Variable:**
- `ZSG_CONFIG_PATH` — Override config file path (used by the GUI app)

---

### Stage 4: `verify.py` — Flask Review Web App

**Input:** `state.json` + `sections.json`
**Output:** Auto-saves to `state.json` as instructor reviews/edits

A Flask-based Single Page App that runs at `http://localhost:5000`. Provides three tabs:

1. **Narrative Review** — Side-by-side view of source annotations and LLM-generated narrative. Inline editing for heading, intro, key points, figures. Approve/reject. Regenerate with LLM.
2. **Quiz Review** — View and edit quiz questions per section. Inline editing. Regenerate single questions with LLM.
3. **Export** — Set guide title and theme. Build the final HTML. Open in browser.

**REST API Endpoints:**
- `GET /api/state` — Load current state
- `POST /api/state` — Full state replace
- `PUT /api/section/<id>/narrative` — Save narrative edits
- `POST /api/section/<id>/approve` — Approve/unapprove narrative
- `PUT /api/section_order` — Reorder sections
- `POST /api/section/<id>/generate_narrative` — Regenerate narrative via LLM
- `POST /api/section/<id>/generate_quiz` — Regenerate quiz via LLM
- `PUT /api/section/<id>/quiz` — Save quiz edits
- `POST /api/section/<id>/quiz_approve` — Approve/unapprove quiz
- `POST /api/export/preview` — Preview what will be built
- `POST /api/export/build` — Build output.html
- `POST /api/export/open` — Open output.html in browser

**CLI Usage:**
```bash
python -m zsg.verify --state projects/course1/state.json --sections projects/course1/sections.json
```

---

### Stage 5: `build_guide.py` — Assemble into HTML

**Input:** `state.json`
**Output:** `output.html` (self-contained, no external dependencies)

Renders the approved narrative and quiz into a single portable HTML file. Features:
- Collapsible sidebar navigation
- Progress bar
- Interactive quiz with instant feedback
- Responsive design (mobile-friendly)
- Optional dark/high-contrast themes
- All CSS and JS inlined (no external requests)

**CLI Usage:**
```bash
python -m zsg.build_guide --state projects/course1/state.json --output study_guide.html
```

---

## GUI App Architecture

### Entry Point: `zsg.app`

A single command launches the local web app:

```bash
python -m zsg.app
```

**Startup Sequence:**

1. **Config seeding** — If `app_config.json` doesn't exist, seed it (migrating from
   `llm_config.yaml` / `color_config.yaml` if present, else writing defaults — the LLM
   provider defaults to **Purdue GenAI Studio**).
2. **Export `ZSG_CONFIG_PATH`** — so generation stages spawned as subprocesses read the
   same config.
3. **Initialize Flask** — Import `verify`, which sets up all routes; set Flask globals
   (`STATE_PATH`, `SECTIONS_PATH`, `APP_CONFIG_PATH`).
4. **Run server** — Start the Flask dev server on port 5000 (server-state mode at `/`,
   client mode at `/?mode=client`).

### Configuration: `app_config.json`

Persistent JSON file (in `.gitignore` for security). Contains:

```json
{
  "llm": {
    "provider": "purdue_genai|anthropic|ollama|lmstudio|openai",
    "api_key": "",
    "model": "llama3.1:latest",
    "base_url": "https://genai.rcac.purdue.edu/api/chat/completions",
    "temperature": 0.1,
    "max_tokens": 8192
  },
  "colors": {
    "yellow": { "label": "Key concepts", "description": "..." },
    "red": { "label": "Quiz-worthy facts", "description": "..." },
    ...
  },
  "projects": [
    { "slug": "course_1", "name": "Course 1", "created": "2024-05-12T..." }
  ],
  "active_project": "course_1"
}
```

### New Routes in `verify.py`

**Config Management:**
- `GET /api/config` — Load config
- `PUT /api/config` — Save config
- `POST /api/config/test` — Test LLM connection

**Project Management:**
- `GET /api/projects` — List all projects + stage status
- `POST /api/projects/new` — Create new project
- `POST /api/projects/open` — Switch to a project

**File Upload:**
- `POST /api/upload/zotero_export` — Upload and save Zotero export

**Pipeline Orchestration:**
- `POST /api/pipeline/run` — Trigger a pipeline stage (subprocess)
- `GET /api/pipeline/status/<run_id>` — Poll subprocess status and logs

### GUI Tabs (in `templates/review.html`)

**Setup Tab** — Configure LLM and annotation colors
- Provider dropdown (Purdue GenAI Studio [default], Anthropic, Ollama, LM Studio, OpenAI)
- Model, API key, base URL (with conditional display based on provider)
- Temperature and max tokens sliders
- Test Connection button
- Color customization rows (6 colors, label + description per color)
- Save Settings button

**Pipeline Tab** — Run the 5-stage pipeline
- Project selector (dropdown + "New Project" button)
- 5 stage cards:
  - Stage 1: File upload + Parse button
  - Stage 2: Run button (disabled until Stage 1 done)
  - Stage 3: Run button (disabled until Stage 2 done)
  - Stage 4: Open Review button (switches to Narrative Review tab)
  - Stage 5: Status indicator (done after Export)
- Live log display per stage (scrollable, green-on-black terminal style)

**Narrative Review, Quiz Review, Export Tabs** — Unchanged from the original

### Frontend: `static/app.js` and `static/app.css`

**app.js** — JavaScript for Setup and Pipeline tabs
- Fetches config via `GET /api/config`
- Renders Setup form with conditional fields
- Saves settings via `PUT /api/config`
- Tests LLM connection via `POST /api/config/test`
- Lists projects via `GET /api/projects`
- Creates projects via `POST /api/projects/new`
- Switches projects via `POST /api/projects/open`
- Uploads files via `POST /api/upload/zotero_export` (HTML5 `<input type="file">`)
- Triggers stages via `POST /api/pipeline/run`
- Polls stage status via `GET /api/pipeline/status/<run_id>` (every 500ms)
- Renders live log output

**app.css** — Styling for Setup and Pipeline tabs
- Form elements (inputs, selects, labels)
- Color swatch visualization
- Button styling
- Log output (monospace, dark background)
- Responsive layout for mobile
- Status indicators (✓ Done, ○ Pending)

### Subprocess Runner: `pipeline_runner.py`

Spawns Python scripts as background subprocesses and exposes their output via polling.

```python
def start_stage(cmd: list, env: dict = None) -> str:
    """Spawn cmd, capture stdout+stderr line-by-line. Returns run_id."""
    ...

def get_status(run_id: str) -> dict:
    """Get {status: "running|done|error", lines: [...], returncode: ...}"""
    ...
```

Thread-safe dictionary of runs. Each run is a background thread that:
1. Spawns the subprocess
2. Reads stdout/stderr line-by-line
3. Accumulates lines in memory
4. Records returncode and final status
5. Returns when process exits

The frontend polls `/api/pipeline/status/<run_id>` to display live output and detect completion.

---

## Data Flow & JSON Schemas

### `annotations.json` (Stage 1 Output)

```json
{
  "color_config": {
    "yellow": { "label": "Key concepts", "description": "..." },
    ...
  },
  "annotations": [
    {
      "id": "ann_001",
      "text": "The First Amendment protects...",
      "color": "yellow",
      "page": 42,
      "instructor_note": "#Constitutional_Rights Additional context",
      "source_document": "Author — Title"
    }
  ]
}
```

### `sections.json` (Stage 2 Output)

```json
{
  "color_config": {...},
  "sections": [
    {
      "section_id": "constitutional_rights",
      "page_range": { "start": 40, "end": 65 },
      "annotation_count": 14,
      "source_annotations": [
        { "id": "ann_001", "text": "...", "color": "yellow", ... }
      ]
    }
  ]
}
```

### `state.json` (Stage 3-4 Output, Stage 5 Input)

```json
{
  "section_order": ["constitutional_rights", "due_process", ...],
  "global_settings": {
    "title": "Civil Rights Study Guide",
    "theme": "light",
    "show_progress": true
  },
  "sections": {
    "constitutional_rights": {
      "narrative": {
        "heading": "Constitutional Rights",
        "intro": "The First Amendment of the U.S. Constitution...",
        "key_points": [
          {
            "term": "Amendment",
            "explanation": "A formal change to a legal document...",
            "source_annotation_ids": ["ann_001"]
          }
        ],
        "figures": [
          {
            "name": "James Madison",
            "description": "Primary author of the Bill of Rights...",
            "source_annotation_ids": ["ann_005", "ann_007"]
          }
        ],
        "source_annotation_ids_used": ["ann_001", "ann_002", ...]
      },
      "narrative_approved": true,
      "quiz": {
        "questions": [
          {
            "question_text": "Which amendment protects freedom of speech?",
            "correct_answer": "The First Amendment",
            "distractors": ["The Second Amendment", "The Tenth Amendment"],
            "explanation_if_correct": "Correct! The First Amendment...",
            "explanation_if_incorrect": "Actually, the First Amendment protects...",
            "source_annotation_ids": ["ann_001", "ann_003"]
          }
        ]
      },
      "quiz_approved": true,
      "source_annotations": [...],
      "page_range": {...}
    }
  }
}
```

---

## Configuration

### Modern: `app_config.json` (GUI App)

Managed entirely through the **Setup tab** in the GUI. Contains LLM settings, color meanings, project registry, and active project.

### `llm_config.yaml`

Power-user / CLI YAML config. Defaults to Purdue GenAI Studio; other providers are listed
(commented) in the file:

```yaml
provider: purdue_genai
base_url: https://genai.rcac.purdue.edu/api/chat/completions
model: llama3.1:latest
temperature: 0.1
max_tokens: 8192
# api_key: ...                      # or set PURDUE_GENAI_API_KEY in the environment

# Alternative: Anthropic (Claude)
# provider: anthropic
# model: claude-haiku-4-5-20251001
```

The `zsg.app` startup seeds `app_config.json` from this (and `color_config.yaml`) on first
run; the GUI Setup tab manages `app_config.json` thereafter.

### Legacy: `color_config.yaml`

Hand-edited YAML that maps Zotero colors to semantic meanings:

```yaml
colors:
  yellow:
    label: "Key concepts"
    description: "Definitions, core ideas, important terms"
  red:
    label: "Quiz-worthy facts"
    description: "Dates, events, specific claims to test"
  ...
```

Also migrated to `app_config.json` on first run.

---

## LLM Integration

The tool is **LLM-agnostic** and supports multiple providers. Configuration determines which is used.

### Supported Providers

| Provider | Endpoint | Auth | Use Case |
|---|---|---|---|
| **Purdue GenAI Studio** (default) | `https://genai.rcac.purdue.edu/api/chat/completions` | API key (Bearer) | Cloud, OpenAI-compatible; Purdue's LLM service |
| **Anthropic** | `https://api.anthropic.com` | API key required | Cloud-based Claude; prompt-cache aware |
| **Ollama** | `http://localhost:11434` | None (local) | Run LLaMA locally, privacy-focused |
| **LM Studio** | `http://localhost:1234` | None (local) | User-friendly local model runner |
| **OpenAI-compatible** | Custom URL | API key optional | vLLM, text-generation-webui, etc. |

### LLM Prompts

Two templates in `prompts/`:

**narrative.txt** — Instructs LLM to analyze a section of annotations and generate:
- A descriptive heading
- A short introductory paragraph
- Key points with term/explanation pairs
- Notable figures or organizations mentioned
- JSON format with proper escaping

**quiz.txt** — Instructs LLM to generate multiple-choice questions from red-highlighted (quiz-worthy) facts:
- Question text
- Correct answer
- Three distractors
- Feedback for correct and incorrect responses
- JSON format

---

## Extending the Tool

### Adding a New LLM Provider

1. Edit `generate.py:call_llm()` to handle the provider's API.
2. Document the provider in `llm_config.yaml` comments.
3. Add a UI section in `app.js` for provider-specific fields (base URL, API key, etc.).

### Adding a New Pipeline Stage

1. Create a new Python script (e.g., `analyze.py`).
2. Document input/output JSON schemas.
3. Add a button in the Pipeline tab to trigger it.
4. Add the subprocess invocation to `verify.py:/api/pipeline/run`.

### Customizing the Study Guide Output

Edit `build_guide.py` to change HTML structure, or modify `static/guide.css` and `static/guide.js` (which are inlined into the final output).

---

## Testing

### Quick Test (No Installation)

Use the Flask test client to verify routes without a running server:

```python
from app import setup_flask
app = setup_flask()
client = app.test_client()

# Test config API
resp = client.get("/api/config")
assert resp.status_code == 200
```

### Test Fixtures

In `test/`:
- **Annotations11.html** — Sample Zotero export
- **annotations.json**, **sections.json**, **state.json**, **output.html** — Expected outputs at each stage

Compare your outputs to these to verify correctness.

### End-to-End Test

```bash
# 1. Create project via GUI
# 2. Upload test/Annotations11.html
# 3. Run all 5 stages
# 4. Review and approve content
# 5. Export guide
# 6. Verify output.html is valid HTML and contains expected sections
```

---

## Known Limitations

1. **No user authentication** — The tool is meant for a single instructor per installation. Add authentication if hosting multi-user.
2. **LLM latency** — Narrative and quiz generation can take 30–120 seconds depending on section count and model. No cancel button (can force-kill Flask).
3. **JSON repair is lossy** — If the LLM outputs malformed JSON, `json_repair.py` attempts recovery but may drop data. Instructor review is critical.
4. **File size limit** — Flask's default file upload limit is 16 MB. Increase `max_content_length` if needed.
5. **No draft auto-save** — If the browser tab is closed mid-edit, unsaved changes are lost. Add local storage if desired.

---

## Security Considerations

1. **API keys stored in plaintext** — `app_config.json` is gitignored but stored in plaintext on disk. Only run this tool on trusted computers. Consider OS keychain integration for production.
2. **No request authentication** — The Flask app assumes localhost access only. Do not expose over the internet without adding auth.
3. **User-generated HTML** — The final study guide is student-facing HTML. Ensure the LLM output doesn't contain XSS or malicious code (json_repair.py guards against some cases, but instructor review is the safest).

---

## Performance

- **Stage 1 (export)** — < 1 second for typical Zotero export
- **Stage 2 (preprocess)** — < 5 seconds for ~100 annotations
- **Stage 3 (generate narrative)** — 30–120 seconds (depends on LLM and section count; typically ~15s per section)
- **Stage 3 (generate quiz)** — 30–120 seconds (similar)
- **Stage 4 (verify UI)** — Real-time; load time ~2 seconds
- **Stage 5 (build HTML)** — < 1 second

Most time is spent in LLM calls (Stage 3). Using a faster model (e.g., Claude Haiku) or running locally (Ollama) can improve speed.

---

## Troubleshooting

### "ModuleNotFoundError: No module named 'flask'"

Run `pip install -r requirements.txt`. The app.py startup script tries to do this automatically.

### "Port 5000 already in use"

Another service is using port 5000. Kill it, or edit app.py to use a different port.

### "LLM not responding" / "Connection timeout"

Check that:
1. Your API key is valid (if using Anthropic)
2. The LLM endpoint is reachable (Ollama: `curl http://localhost:11434/api/tags`)
3. Network connectivity is OK

### "JSON parsing error in LLM output"

The LLM generated malformed JSON. Review the raw output in the Flask logs. Try re-running generation or changing the LLM model/temperature.

---

## Future Enhancements

- [ ] Multi-user support (instructor authentication, per-user project folders)
- [ ] Collaborative editing (WebSocket-based live sync)
- [ ] Custom prompt templates (UI editor for editing prompts before generation)
- [ ] Batch processing (upload multiple exports, run pipeline in parallel)
- [ ] PDF output (alternative to HTML)
- [ ] Mobile app (React Native version)
- [ ] Database backend (replace JSON file storage)
- [ ] Scheduling (run pipeline on a cron schedule)
- [ ] Slack/email notifications (notify instructor when stage completes)
- [ ] A/B testing UI (preview multiple LLM outputs for the same section)

---

## Contributing

To improve the tool:

1. Create a branch: `git checkout -b feature/my-feature`
2. Make changes to the Python scripts, HTML/CSS/JS, or documentation.
3. Test with `python -m zsg.app` and verify the GUI works end-to-end.
4. Commit with a clear message: `git commit -m "Add feature XYZ"`
5. Push and create a pull request.

---

## License

[Specify license — MIT, Apache 2.0, etc.]

---

## Contact & Support

For questions or issues:
- Check the README.md and GUIDE.md
- Review this documentation
- Examine the docstrings in the Python files
- Test with the fixture in `test/`
