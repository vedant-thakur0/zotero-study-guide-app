# Getting Started with the Zotero Study Guide App

This document is for **colleagues who will work on or use this project**. It explains what
was built, how to run it, and how to extend it. For the user-facing quickstart, see the
top-level **[README](../README.md)**; for the deep architecture reference, see
**[REPOSITORY.md](REPOSITORY.md)**.

---

## What Was Built

A pipeline that converts color-coded Zotero PDF annotations into self-contained,
interactive HTML study guides — with a human-in-the-loop review step before anything
reaches students. It ships as the `zsg` package under `src/`, runnable both as individual
CLI stages (`python -m zsg.<stage>`) and as a **local web app** (`python -m zsg.app`) that
guides a non-technical instructor through the whole flow in the browser.

The generation step is powered by **[Purdue GenAI Studio](https://genai.rcac.purdue.edu)**
by default (an OpenAI-compatible LLM service), with other providers available as
alternatives.

**Before:** run 5 separate Python CLI commands in the right order.
**Now:** click through the stages in a web app, review the LLM output, and export.

> **Note on packaging:** earlier iterations shipped double-click launchers
> (`run.command` / `run.bat`) and a desktop bundle. Those were **removed**. The app is a
> plain local Flask dev server now — run it with `python -m zsg.app`.

---

## Install

```bash
pip install -e .          # installs the zsg package + dependencies
# or, without installing the package:
pip install -r requirements.txt && export PYTHONPATH=src
```

Requires Python 3.9+.

---

## How to Run

### The web app (recommended)

```bash
python -m zsg.app
```

Opens at [http://localhost:5000](http://localhost:5000) (server-state mode). Append
`/?mode=client` to keep all project state in the browser (IndexedDB), with the server
acting only as a stateless transformer.

> **macOS port note:** Control Center's AirPlay Receiver binds `*:5000` and returns `403`,
> which looks like an app failure. Either disable AirPlay Receiver
> (System Settings → General → AirDrop & Handoff) or run the review server on another port:
> `python -m zsg.verify --port 5050`.

### First-time setup (in the app)

1. The app opens to the **Setup** tab.
2. The AI model provider defaults to **Purdue GenAI Studio** (its endpoint and a default
   model are pre-filled). Paste your API key — from
   genai.rcac.purdue.edu → avatar → Settings → Account → API Keys.
3. Click **Test Connection** to verify it works.
4. Click **Save Settings**.

To use a different provider (Anthropic, Ollama, LM Studio, OpenAI-compatible), pick it from
the same dropdown.

### For each new course

1. Click **+ New Project** and type the course name.
2. Drag your Zotero export file (`.html`) into the upload box.
3. Run the pipeline stages (parse → preprocess → generate).
4. Open **Narrative Review** to edit/approve the LLM output, then **Quiz Review**.
5. Use the **Export** tab to build and download your study guide.

### Running individual stages from the CLI

Each stage is a module under `zsg`:

```bash
python -m zsg.export     --input Annotations.html --output projects/my_course/annotations.json
python -m zsg.preprocess --input projects/my_course/annotations.json --output projects/my_course/sections.json
python -m zsg.generate   narrative --sections projects/my_course/sections.json --state projects/my_course/state.json
python -m zsg.verify     --state projects/my_course/state.json --sections projects/my_course/sections.json
python -m zsg.build_guide --state projects/my_course/state.json --output projects/my_course/output.html
```

---

## For Developers: How It Works

### Architecture overview

```
python -m zsg.app
  │
  ├─→ [Flask dev server on :5000]   (src/zsg/verify.py)
  │     ├─→ /api/config      — LLM settings (Setup tab)
  │     ├─→ /api/projects/*  — project management
  │     ├─→ /api/pipeline/*  — run pipeline stages (subprocess)
  │     ├─→ /api/v2/*        — stateless transforms (client mode)
  │     └─→ /static/, /      — web UI (HTML/CSS/JS)
  │
  └─→ [Browser]
        ├─→ Setup tab     (configure LLM provider + colors)
        ├─→ Pipeline tab  (upload files, run stages, view logs)
        ├─→ Narrative Review tab
        ├─→ Quiz Review tab
        └─→ Export tab    (build HTML guide)
```

### Key files

| File | Purpose |
|---|---|
| `src/zsg/app.py` | Dev-server launcher; seeds `app_config.json`, exports `ZSG_CONFIG_PATH` |
| `src/zsg/verify.py` | Flask app — all routes (server-state `/api/*` + stateless `/api/v2/*`) |
| `src/zsg/generate.py` | LLM generation; `call_llm()` is the single provider seam |
| `src/zsg/pipeline_runner.py` | Spawns stages as subprocesses; streams output, LRU-bounded, cancellable |
| `app_config.json` | Runtime config (gitignored; holds the API key) |
| `src/zsg/static/app.js` | Setup & Pipeline tab logic |
| `src/zsg/static/storage.js` | Client-mode (IndexedDB) state |
| `src/zsg/templates/review.html` | HTML shell (5 tabs) |

### The 5-stage pipeline

1. **`zsg.export`** — Parse Zotero export → `annotations.json`
2. **`zsg.preprocess`** — Group annotations → `sections.json`
3. **`zsg.generate`** — Call the LLM → `state.json` with narratives & quizzes
4. **`zsg.verify`** — Flask app for review/editing
5. **`zsg.build_guide`** — Assemble into a self-contained `output.html`

The web app wraps all 5 and lets you run them with a click.

---

## Making Changes

### Editing the Setup tab
**File:** `src/zsg/static/app.js`, function `renderSetupTab()` (+ `updateProviderUI()` for
the per-provider field logic).

### Editing the Pipeline tab
**File:** `src/zsg/static/app.js`, function `renderPipelineTab()`.

### Adding a new API route
**File:** `src/zsg/verify.py`. Add a `@app.route(...)` handler, then call it from
`app.js` with `fetch(...)`.

### Adding a new LLM provider
The single seam is `call_llm()` in `src/zsg/generate.py` — add a branch there, then add
the provider's option + field hints in `app.js:renderSetupTab()` / `updateProviderUI()`.
Most providers are thin OpenAI-compatible HTTP calls; see the existing `purdue_genai` and
OpenAI-compatible branches as templates.

### Changing the study-guide output
Edit `src/zsg/build_guide.py`, or the `static/guide.*` assets that get inlined into the
final HTML.

### Running tests

```bash
python -m pytest            # unit + API suite (e2e is opt-in, see below)
python -m pytest -k purdue  # e.g. just the Purdue provider tests
```

End-to-end (Playwright, browser) and frontend unit tests:

```bash
pip install pytest-playwright && playwright install chromium
python -m pytest tests/e2e/ -v
node --test "tests/js/*.mjs"
```

---

## Configuration

- **`app_config.json`** — runtime config managed by the Setup tab (gitignored; never edit
  by hand). Holds the active LLM provider, model, API key, colors, and project list.
- **`llm_config.yaml`** — power-user / CLI config. Defaults to the Purdue GenAI Studio
  provider; alternatives are listed (commented) in the file.
- **`color_config.yaml`** — instructor-editable highlight-color meanings.

On first run, `app.py` seeds `app_config.json` (migrating from the YAML files if present).

### LLM providers

| Provider | Notes |
|---|---|
| **Purdue GenAI Studio** (default) | Cloud, OpenAI-compatible; endpoint pre-filled, requires an API key |
| Anthropic (Claude) | Cloud API, requires API key; prompt-cache aware |
| Ollama | Run a model locally, no API key |
| LM Studio | Local model runner |
| OpenAI-compatible | vLLM, text-generation-webui, etc. |

---

## Data Flow

### Input: Zotero export
A `.html` file exported from Zotero with color-coded highlights and notes.

### Stage outputs
- `annotations.json` — parsed annotations (each keeps a stable `id`)
- `sections.json` — annotations grouped into sections
- `state.json` — LLM-generated narratives + quizzes, plus review/approval state
- `output.html` — a single, self-contained file (CSS + JS inlined, no external requests),
  ready to share with students

See [REPOSITORY.md](REPOSITORY.md) for the full schemas.

---

## Troubleshooting

**"Port 5000 in use" / `403`.** On macOS this is usually AirPlay Receiver — disable it or
run `python -m zsg.verify --port 5050`.

**"ModuleNotFoundError".** Run `pip install -e .` (or
`pip install -r requirements.txt && export PYTHONPATH=src`).

**"LLM not responding."** Check the API key is correct and the endpoint is reachable
(Purdue GenAI Studio: `https://genai.rcac.purdue.edu`). For local providers, confirm the
server is running.

**"Pipeline stage failed / empty log."** Run the stage from the CLI to see the full error,
e.g. `python -m zsg.export --input ... --output ...`.

**"LLM JSON parsing error."** The output was malformed; the pipeline auto-repairs common
cases. Re-run generation, try a different model, or lower the temperature.

---

## Where Things Live

- **Zotero exports** → `projects/<course_slug>/source_export.html`
- **Pipeline artifacts** → `projects/<course_slug>/{annotations,sections,state}.json`, `output.html`
- **Config** → `app_config.json` (gitignored)
- **Web UI** → `src/zsg/templates/review.html`, `src/zsg/static/*`
- **API routes** → `src/zsg/verify.py`
- **Subprocess runner** → `src/zsg/pipeline_runner.py`
- **Dev-server entry** → `src/zsg/app.py`

---

## Questions?

- `README.md` — quickstart + overview
- `docs/GUIDE.md` — detailed instructor usage
- `docs/REPOSITORY.md` — architecture, schemas, API reference
- Docstrings and code comments for implementation detail
