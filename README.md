# Zotero → Interactive Study Guide Pipeline

Turn Zotero PDF annotations into a self-contained, interactive HTML study guide — with a human-in-the-loop verification step before anything reaches students.

The content-generation step is powered by **[Purdue GenAI Studio](https://genai.rcac.purdue.edu)** by default (other LLM providers are supported behind a single seam).

---

## How it works

```
Zotero annotations (HTML export)
        │
        ▼
  zsg.export         Parse highlights + instructor notes → annotations.json
        │
        ▼
  zsg.preprocess     Group annotations into sections → sections.json
        │
        ▼
  zsg.generate       LLM generates narrative + quiz JSON (dry-run safe)
        │
        ▼
  zsg.verify         Local web app: instructor reviews, edits, approves
        │
        ▼
  zsg.build_guide    Assemble final HTML from approved JSON
        │
        ▼
  output.html        Single portable file — no dependencies
```

---

## Architecture & deployment

**LLM integration is a single seam.** All model calls go through `call_llm(prompt, cfg)` in
[`src/zsg/generate.py`](src/zsg/generate.py), which switches on a provider. The default is
Purdue GenAI Studio (a Bearer-token, OpenAI-compatible endpoint); adding a provider — or
pointing an OpenAI-compatible one at a different `base_url` — is a small, localized change.
API keys are read from config **or** environment variables and are never logged.

**Two runtime modes, differing in where state lives:**

| | Server-state mode (default, `/`) | Client mode (`/?mode=client`) |
|---|---|---|
| State | server's local disk (`projects/<slug>/*.json`) | browser IndexedDB |
| Server role | stateful | **stateless transformer** |
| Multi-user | single-user | safe (nothing shared server-side) |

The stateless path is the deployment-friendly one: the `/api/v2/*` routes in
[`src/zsg/verify.py`](src/zsg/verify.py) take data in the request body and return transformed
data, writing nothing to disk (`parse` → `sections` → `llm` → `build`). It's end-to-end
tested (Playwright) but currently opt-in via `?mode=client`.

**Status / not yet productionized:** the app runs on Flask's development server
([`src/zsg/app.py`](src/zsg/app.py)) and the default mode is single-user. A multi-user hosted
deployment would mean defaulting to the stateless client-mode path and running behind a
production WSGI server (e.g. gunicorn). The architecture supports this; it isn't switched on
yet.

---

## Quickstart

The pipeline ships as the `zsg` package under `src/`. Run each stage as a module
with `python -m zsg.<stage>`. (Set `PYTHONPATH=src`, or `pip install -e .` once,
so `zsg` is importable.)

### 1. Install dependencies

```bash
pip install -e .          # installs the package + its dependencies
# or, without installing the package:
pip install -r requirements.txt && export PYTHONPATH=src
```

### 2. Export annotations from Zotero

In Zotero: select a PDF item → right-click → **Create Note from Annotations** → File → **Export Note** → save as HTML.

Drop the file anywhere; you'll point to it in the next step. (See `sample-data/` for a real example export.)

### 3. Parse the export

```bash
python -m zsg.export --input Annotations.html --output projects/my_course/annotations.json
```

Use `--demo` to run the pipeline with built-in example data instead:

```bash
python -m zsg.export --demo --output projects/demo/annotations.json
```

### 4. Group into sections

```bash
python -m zsg.preprocess --input  projects/my_course/annotations.json \
                         --output projects/my_course/sections.json
```

To force your own section names, start any annotation's note with `#SectionName` in Zotero before exporting. The preprocessor will use those tags instead of page-proximity grouping.

### 5. Generate content (requires a running LLM)

Configure your model in `llm_config.yaml`, then:

```bash
# Narrative only (review these before generating quiz)
python -m zsg.generate narrative \
  --sections projects/my_course/sections.json \
  --state    projects/my_course/state.json

# Quiz (runs after you approve narratives in the verification app)
python -m zsg.generate quiz \
  --sections projects/my_course/sections.json \
  --state    projects/my_course/state.json
```

Append `--dry-run` to print the prompts without calling the LLM.

### 6. Review and approve

```bash
python -m zsg.verify --state    projects/my_course/state.json \
                     --sections projects/my_course/sections.json
```

Or just start the dev server, which uses the active project from `app_config.json`:

```bash
python -m zsg.app
```

Opens at [http://localhost:5000](http://localhost:5000). Use the **Narrative Review** tab to compare source annotations against generated content, edit inline, and approve each section. Then move to **Quiz Review**. Append `?mode=client` to the URL to keep all project state in the browser (IndexedDB) with the server acting only as a stateless transformer.

### 7. Build the guide

Use the **Export** tab in the verification app, or run directly:

```bash
python -m zsg.build_guide \
  --state  projects/my_course/state.json \
  --output projects/my_course/output.html \
  --title  "My Course — Module 7" \
  --theme  light
```

Open `output.html` in any browser. No server required.

---

## LLM configuration

Generation is powered by **[Purdue GenAI Studio](https://genai.rcac.purdue.edu)** by
default — an OpenAI-compatible LLM service. Set your key in the Setup tab of the web app,
or edit `llm_config.yaml` for CLI use:

```yaml
provider: purdue_genai
base_url: https://genai.rcac.purdue.edu/api/chat/completions
model: llama3.1:latest
temperature: 0.1
max_tokens: 8192
# api_key: ...                 # or set PURDUE_GENAI_API_KEY in the environment
```

(Get an API key from genai.rcac.purdue.edu → avatar → Settings → Account → API Keys.)

Other providers are supported as alternatives — uncomment the relevant block in
`llm_config.yaml`, or pick one in the Setup tab:

| Provider | Notes |
|---|---|
| **Purdue GenAI Studio** (default) | Cloud, OpenAI-compatible, Bearer-token auth |
| Anthropic (Claude) | Cloud API; prompt-cache aware (`claude-*` models) |
| Ollama | Local models (`format: json` auto), e.g. `llama3.1:70b` |
| vLLM / LM Studio / OpenAI-compatible | Local or hosted OpenAI-style `/v1/chat/completions` |

**Keeping keys out of config.** Each hosted provider falls back to an environment variable
when no `api_key` is set in config: `PURDUE_GENAI_API_KEY`, `ANTHROPIC_API_KEY`,
`OPENAI_API_KEY`. Copy `.env.example` → `.env` (gitignored) and fill it in; `python -m
zsg.app` loads it automatically if `python-dotenv` is installed. This is the recommended
posture for shared/hosted deployments so secrets never live in a tracked or on-disk config
file.

---

## Color configuration

Edit `color_config.yaml` to reassign the meaning of each Zotero highlight color for your course:

```yaml
colors:
  yellow:
    label: "Key concepts"
    description: "Definitions, core ideas, important terms"
  red:
    label: "Quiz-worthy facts"
    description: "Dates, events, specific claims to test"
  green:
    label: "People & organizations"
  blue:
    label: "Themes & arguments"
  purple:
    label: "Connections"
  orange:
    label: "Examples"
```

The LLM prompts use these role assignments: blue/purple → intro synthesis, yellow/orange → key points, green → figure cards, red → quiz questions.

---

## Project file structure

```
zotero-study-guide/
├── pyproject.toml            Package metadata, deps, pytest config
├── requirements.txt
├── color_config.yaml         Instructor-editable color meanings
├── llm_config.yaml           Model endpoint configuration
├── app_config.json           Runtime config (gitignored; holds API keys)
│
├── src/zsg/                  The pipeline package (run as `python -m zsg.<stage>`)
│   ├── __init__.py           Defines PKG_DIR (assets) and PROJECT_ROOT (data)
│   ├── export.py             Parse Zotero HTML/CSV/JSON → annotations.json
│   ├── preprocess.py         Group annotations → sections.json
│   ├── generate.py           LLM generation (narrative + quiz sub-commands)
│   ├── verify.py             Flask review app + stateless v2 API
│   ├── build_guide.py        Assemble approved JSON → output.html
│   ├── json_repair.py        Fix malformed LLM JSON output
│   ├── pipeline_runner.py    Subprocess runner used by the review UI
│   ├── app.py                Local dev-server launcher
│   ├── prompts/              narrative.txt, quiz.txt
│   ├── templates/            review.html (review app frontend)
│   └── static/               review.* (review UI), guide.* (inlined into output),
│                             client-mode.js / storage.js (browser client mode)
│
├── projects/                 Per-course working data
│   └── <project_name>/
│       ├── source_export.html  Tracked input (real exports are gitignored)
│       ├── annotations.json    Raw parsed annotations   (gitignored)
│       ├── sections.json       Preprocessed sections     (gitignored)
│       ├── state.json          Review app state          (gitignored)
│       └── output.html         Final study guide         (gitignored)
│
├── interactive_practice_exam/ Separate sub-tool: DOCX exams → interactive HTML
├── sample-data/              Example Zotero export
├── docs/                     GUIDE, REPOSITORY, GETTING_STARTED
└── tests/                    pytest suite (+ tests/e2e Playwright, tests/fixtures)
```

---

## Annotation format

`zsg.export` outputs `annotations.json`:

```json
{
  "color_config": { ... },
  "annotations": [
    {
      "id": "ann_001",
      "text": "After the Civil War, formerly enslaved people sought...",
      "color": "blue",
      "page": 42,
      "instructor_note": "Use as section intro",
      "source_document": "Darling — The Quest for Equality"
    }
  ]
}
```

Every annotation keeps its `id` throughout the pipeline. The verification app uses these IDs to link source annotations to generated fields.

---

## Section tagging

To control section boundaries, add `#TagName` at the start of an annotation's note in Zotero before exporting:

```
#reconstruction Use as section intro
```

All annotations after this one (up to the next tag) belong to the `reconstruction` section. Untagged annotations inherit the most recent tag.

If no tags are found, the preprocessor falls back to page-proximity grouping (configurable with `--page-window`).

---

## End-to-end tests

A Playwright-based browser test exercises the client-mode (IndexedDB) pipeline from upload through to a built HTML guide. It stubs `/api/v2/llm` at the browser level, so no API key is needed.

```bash
pip install pytest-playwright
playwright install chromium
pytest tests/e2e/ -v
```

The test boots `verify.py` on a free port in a background thread, opens a Chromium browser at `/?mode=client`, uploads a small fixture export, runs preprocess, generates + approves one narrative section against the stubbed LLM, and asserts the final `/api/v2/build` returns a complete HTML document containing the stubbed heading.

## Frontend unit tests

A handful of pure frontend helpers (e.g. the quiz-gate `quizWorthyRole()`
color-role resolver) are unit-tested with Node's built-in runner — no browser,
no extra dependencies:

```bash
node --test "tests/js/*.mjs"
```

Each test extracts the target function from the real `static/*.js` source and
evaluates it in isolation, so it pins shipped code rather than a copy.
