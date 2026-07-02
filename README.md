# Zotero → Interactive Study Guide Pipeline

Turn Zotero PDF annotations into a self-contained, interactive HTML study guide — with a human-in-the-loop verification step before anything reaches students.

The content-generation step is powered by **[Purdue GenAI Studio](https://genai.rcac.purdue.edu)** by default (other LLM providers are supported behind a single seam).

The app runs two ways: a **hosted web app** (live on AWS ECS Express Mode — open the URL, paste your Purdue GenAI key in Setup, and go), or the **local CLI/dev-server** walkthrough below. Both share the same pipeline and the same human-in-the-loop review step.

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

| | Client mode (default on the hosted app) | Server-state mode (`/?mode=server`) |
|---|---|---|
| State | browser IndexedDB | server's local disk (`projects/<slug>/*.json`) |
| Server role | **stateless transformer** | stateful |
| Multi-user | safe (nothing shared server-side) | single-user |

The stateless path is the deployment-friendly one: the `/api/v2/*` routes in
[`src/zsg/verify.py`](src/zsg/verify.py) take data in the request body and return transformed
data, writing nothing to disk (`parse` → `sections` → `llm` → `build`). It's end-to-end
tested (Playwright). A fresh web session defaults to client mode; `?mode=server` opts into the
local-disk pipeline used by the CLI walkthrough below.

**Hosted deployment (live).** The app is containerized (gunicorn behind a Docker image — see
the [`Dockerfile`](Dockerfile)) and deployed on **AWS ECS Express Mode**. In the hosted app
each user pastes their own Purdue GenAI key in the Setup tab (sent per request, never stored
server-side). The full build-push-deploy steps live in the [deploy runbook](deploy/README.md).
The image **must** be built for `linux/amd64` (ECS Fargate is x86_64). To run it locally
instead, use Flask's dev server via `python -m zsg.app`.

**What the server stores.** No server-side storage of your annotations or your API key — your
content lives in your browser (IndexedDB), and the key is sent per request and never written to
disk or logged. The server *does* record **anonymous per-call telemetry** to S3 for operational
monitoring: timestamp, provider, model, success/failure, round-trip latency, and a **salted,
one-way fingerprint** of the API key. The fingerprint lets us count distinct users and correlate
calls from the same key, but it is irreversible — the key itself cannot be recovered from it
(see the security invariant in [`src/zsg/metrics.py`](src/zsg/metrics.py)).

**In-app onboarding.** The web app guides a first-time user end to end: it lands on the
Pipeline tab, shows a 5-stage flow indicator, frames the human-in-the-loop review step ("AI
drafts, you review & approve — nothing is exported until you do"), teaches the Zotero color
conventions in-app, and offers a re-openable "How this works" overview — so an instructor
never has to read this README or touch a terminal to use it.

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

### Metrics log

Every LLM call is logged (read-only) to `metrics.jsonl` in the project root (gitignored).
Each line is a JSON record with: `ts` (ISO-8601 UTC timestamp), `provider` (e.g.
`"purdue_genai"`), `model`, `ok` (bool), `round_trip_ms` (wall-clock time for the HTTP call),
and `user` (16-char hex fingerprint of the API key, or `"none"` for keyless providers).

**Security guarantee:** The log stores only a non-reversible fingerprint of the API key,
never the raw key, Bearer headers, prompt text, or response body.

To summarize the log:

```bash
python -m zsg.metrics
```

Prints: total calls, successful vs. failed split, latency min/median/p95/max (over
successful calls), and unique users (distinct API key fingerprints).

For a visual analytics dashboard, export a **self-contained HTML file** (no server, no
external scripts — open it in any browser, like the study guides themselves):

```bash
python -m zsg.metrics --html metrics_dashboard.html
```

The dashboard charts latency over time, the latency distribution, calls and success rate
broken down by provider/model, and calls per user. It inlines only the metrics-schema
fields, so the same security guarantee holds — no raw keys, prompts, or responses.

**Storage backend (file or S3).** The log location is set by `ZSG_METRICS_PATH`. With no value
(or a plain path) it writes the local `metrics.jsonl` file — the default, unchanged. Set it to
an `s3://bucket/prefix` URI to ship each record to **Amazon S3** instead (one object per
record, date-partitioned), so metrics survive container restarts on the ephemeral hosted
filesystem. The report and dashboard read back from whichever backend is active. S3 access uses
ambient AWS credentials (on the hosted app, the ECS **task role** — no keys in the image);
`boto3` is imported only on the S3 path, so local file-mode installs don't need it. Writes are
best-effort — a metrics failure never breaks or slows an LLM call.

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
├── Dockerfile                Production image (gunicorn; build for linux/amd64)
├── wsgi.py                   gunicorn entrypoint for the container
│
├── src/zsg/                  The pipeline package (run as `python -m zsg.<stage>`)
│   ├── __init__.py           Defines PKG_DIR (assets) and PROJECT_ROOT (data)
│   ├── export.py             Parse Zotero HTML/CSV/JSON → annotations.json
│   ├── preprocess.py         Group annotations → sections.json
│   ├── generate.py           LLM generation (narrative + quiz sub-commands)
│   ├── verify.py             Flask review app + stateless v2 API
│   ├── build_guide.py        Assemble approved JSON → output.html
│   ├── json_repair.py        Fix malformed LLM JSON output
│   ├── metrics.py            Per-call metrics log (file or S3) + report
│   ├── metrics_dashboard.py  Self-contained HTML analytics dashboard
│   ├── pipeline_runner.py    Subprocess runner used by the review UI
│   ├── app.py                Local dev-server launcher
│   ├── prompts/              narrative.txt, quiz.txt
│   ├── templates/            review.html (review app frontend)
│   └── static/               review.* (review UI), guide.* (inlined into output),
│                             guides.js (in-app onboarding/help),
│                             client-mode.js / storage.js (browser client mode)
│
├── deploy/                   AWS ECS Express deploy runbook + push_and_deploy.sh
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
