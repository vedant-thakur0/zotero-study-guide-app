# Pipeline Evaluation

Critical review of the zotero-study-guide pipeline.

## Stages

1. **`export.py`** — Zotero HTML/MD/CSV/JSON → `annotations.json` (normalize color, page, citation, instructor note).
2. **`preprocess.py`** — `annotations.json` → `sections.json` (group by `#Tag`, else page proximity).
3. **`generate.py narrative`** — per-section LLM call → `state.json` (awaiting human approval).
4. **Human review** via `verify.py` (Flask) — flips `narrative_approved`.
5. **`generate.py quiz`** — for approved sections with ≥1 red annotation → quiz JSON in `state.json`.
6. **`build_guide.py`** — assembles the final study guide.
7. **`pipeline_runner.py`** — subprocess wrapper used by the Flask UI to stream stage logs.

Architecture is sound: deterministic ETL front-end, single LLM hop per section, HTML approval gate, then assembly. JSON-on-disk is the right shared substrate at this size.

## What works well

- **Clean stage boundaries.** Each script is independently runnable with file-path I/O. Easy to debug and re-run.
- **Two human-in-the-loop checkpoints** (`narrative_approved`, `quiz_approved`). Quiz generation is gated on approved narrative — correct ordering for catching hallucinations early.
- **Robust color normalization** (`export.py:40-88`). Duplicate hex entries for Zotero's blended rgba alphas show this was hardened against real export quirks.
- **Layered JSON repair** (`json_repair.py`): fast path → cheap fixes → third-party library. The `"{" + text` prefill in the Anthropic call (`generate.py:164-168`) is a good belt-and-suspenders move.
- **Idempotency by default.** `--force` to re-run; otherwise approved sections skip.

## Real problems

### 1. Section grouping is the weakest link
- Proximity fallback (`preprocess.py:79-111`) only sorts by page number. Unrelated adjacent topics get fused; topics spanning a gap > `PAGE_WINDOW=6` get split. No semantic signal.
- `MIN_SECTION_SIZE=2` unconditionally merges into the *previous* run — a stray annotation at the start of a real new topic glues silently to the prior section.
- Non-numeric page labels ("vii", "Intro") all become `0` (`preprocess.py:70-77`), piling into one giant pseudo-section at the front.
- `group_by_tags` starts untagged annotations in a `general` bucket nobody asked for.

### 2. `generate.py` LLM layer bugs
- ~~`call_llm` defaults to `ollama`; `app.py` writes `anthropic`. They disagree.~~ **Resolved (2026-06-16):** all entry points now default to `purdue_genai` / `llama3.1:latest`.
- Anthropic branch passes `system_param = NOT_GIVEN` for missing `<system>` block — verify SDK-version behavior; some versions want the kwarg omitted entirely.
- `max_tokens=8192` default but no retry on truncation. Mid-JSON truncation sometimes gets "repaired" into structurally-valid-but-incomplete output.
- No token/cost accounting. The `ephemeral` cache on the system block sees little reuse because `{annotations}` substitution lands in the user message, not the cached prefix.

### 3. `pipeline_runner.py` issues
- `_runs` dict grows unbounded — memory leak proportional to number of runs.
- No way to cancel a running stage (Popen handle isn't stored).
- `daemon=True` thread orphans the subprocess if Flask exits mid-run.

### 4. `state.json` is a single mutable blob
- **No file locking.** Flask UI editing approvals concurrently with a CLI `generate.py` write = last-write-wins, silent approval loss.
- No schema migration story — adding a field means manually patching every project file.

### 5. Quiz generation is too narrow
- Requires `color == "red"` (`generate.py:275-278`). Zero red annotations → no quiz at all, silently. Instructors who don't follow the color convention get a broken pipeline with no error.

### 6. `export.py` HTML parser is regex-on-HTML
- `re.findall(r"<p\b[^>]*>(.*?)</p>", ...)` (`export.py:103`) breaks the moment Zotero nests `<p>` or restructures output. No version detection — just silent zero-annotation output.
- Markdown export defaults all colors to yellow with a `print` warning that gets buried in subprocess pipes the UI may not surface.

### 7. Cross-cutting
- No visible tests for `preprocess` or `generate`. Two test directories (`tests/`, `test/`) — itself a smell.
- `app.py:setup_flask` mutates module-level globals on `verify` (`app.py:122-134`). Project switching at runtime won't rebind reliably unless `verify` re-reads per request.
- `print(...)` everywhere instead of `logging`. Painful under subprocess capture.

## Priority fixes (one-day budget)

1. **Lock `state.json`** (filelock). Silent approval loss is the highest-severity bug.
2. **Replace `export.py`'s HTML regex** with a real parser (BeautifulSoup). Eliminates a class of silent-failure modes.
3. **Cap `_runs`** in `pipeline_runner` (LRU ~50) and store the Popen handle to enable cancel.
4. **Drop the red-only quiz gate** — make it configurable (e.g., any color set, or `instructor_note` containing "quiz").
5. **Detect truncation in `generate.py`** — check Anthropic `stop_reason == "max_tokens"` and surface as a real error instead of letting `attempt_repair` paper over it.

## Bottom line

The pipeline is well-shaped. Input-parsing edges, the concurrency story around `state.json`, and LLM-truncation handling are what would bite first in real use.
