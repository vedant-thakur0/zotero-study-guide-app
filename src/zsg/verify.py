"""
verify.py — Stage 4: Local verification web app

Serves a side-by-side review interface where the instructor can compare
source annotations against LLM-generated content, edit inline, approve
sections, and reorder sections before quiz generation.

Usage:
    python -m zsg.verify --state projects/my_project/state.json \
                         --sections projects/my_project/sections.json

    # Or just run the dev server (uses active_project from app_config.json):
    python -m zsg.app
"""

import argparse
import json
import os
import re
import subprocess
import sys as _sys
import threading
from contextlib import contextmanager
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

from zsg import PKG_DIR, PROJECT_ROOT

try:
    from filelock import FileLock, Timeout as FileLockTimeout
    _HAS_FILELOCK = True
except ImportError:
    FileLock = None
    FileLockTimeout = Exception
    _HAS_FILELOCK = False

_HERE = Path(__file__).parent
app = Flask(__name__,
            template_folder=str(_HERE / "templates"))

# Global config — set in main()
STATE_PATH      = None
SECTIONS_PATH   = None
APP_CONFIG_PATH = None
_save_lock      = threading.Lock()

# Cap request body to 32 MB so a stray large upload can't OOM the worker.
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024


def _atomic_write_text(path: Path, content: str) -> None:
    """Write to a sibling tmp file then os.replace — never leaves a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


@contextmanager
def _file_lock_for(path: Path):
    """Cross-process file lock around `path`. No-op when filelock isn't installed."""
    if not _HAS_FILELOCK or path is None:
        yield
        return
    lock = FileLock(str(path) + ".lock", timeout=10)
    try:
        with lock:
            yield
    except FileLockTimeout:
        # Best-effort: proceed without the lock rather than 500
        yield


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            with open(STATE_PATH) as f:
                return json.load(f)
        except json.JSONDecodeError:
            # Corrupt state — fall back to empty rather than crashing the UI.
            return {"sections": {}, "section_order": [], "_recovered_from_corrupt": True}
    return {"sections": {}, "section_order": []}


def save_state(state: dict):
    with _save_lock, _file_lock_for(STATE_PATH):
        _atomic_write_text(
            STATE_PATH,
            json.dumps(state, indent=2, ensure_ascii=False),
        )


@contextmanager
def locked_state():
    with _save_lock, _file_lock_for(STATE_PATH):
        state = load_state()
        yield state
        _atomic_write_text(
            STATE_PATH,
            json.dumps(state, indent=2, ensure_ascii=False),
        )


def load_sections() -> list:
    if not SECTIONS_PATH or not SECTIONS_PATH.exists():
        return []
    with open(SECTIONS_PATH) as f:
        data = json.load(f)
    return data.get("sections", data) if isinstance(data, dict) else data


def sections_index() -> dict:
    """section_id → section object"""
    return {s["section_id"]: s for s in load_sections()}


def _save_sections(sections: list) -> None:
    """Rewrite sections.json with a new section list, preserving the file's
    surrounding shape (the top-level dict carrying color_config etc.) when it
    has one. sections.json is the source of truth the UI rebuilds order from,
    so a durable delete must update it here — not just state.json."""
    if not SECTIONS_PATH:
        return
    data: dict = {}
    if SECTIONS_PATH.exists():
        with open(SECTIONS_PATH) as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            data = loaded
    if isinstance(data, dict):
        data["sections"] = sections
        payload = data
    else:
        payload = sections
    with _file_lock_for(SECTIONS_PATH):
        _atomic_write_text(
            SECTIONS_PATH,
            json.dumps(payload, indent=2, ensure_ascii=False),
        )


def load_color_config_from_sections() -> dict:
    """Return the per-project color→role map embedded in sections.json.

    The block is ``{ color: { label, description } }`` (e.g. red → "Quiz-worthy
    facts"). It travels with the project data and matches the labels the
    annotations were tagged against, so the UI can resolve which color carries
    the "Quiz-worthy" role rather than hard-coding the literal "red". Returns
    {} when sections.json is absent or carries no color_config (callers fall
    back to the default red mapping)."""
    if not SECTIONS_PATH or not SECTIONS_PATH.exists():
        return {}
    with open(SECTIONS_PATH) as f:
        data = json.load(f)
    return data.get("color_config", {}) if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------
# Routes — app shell
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return send_from_directory(str(_HERE / "templates"), "review.html")


@app.route("/static/<path:filename>")
def static_files(filename):
    try:
        return send_from_directory(str(_HERE / "static"), filename)
    except Exception as e:
        import sys as _sys2
        print(f"Error serving static file {filename}: {e}", file=_sys2.stderr)
        return jsonify({"error": str(e)}), 404


@app.route("/static/prompts/<path:filename>")
def static_prompts(filename):
    """Serve prompt templates so client-mode can load them via fetch()."""
    # Reject any traversal — send_from_directory already does, but be explicit.
    if ".." in filename or filename.startswith("/"):
        return jsonify({"error": "invalid prompt name"}), 400
    try:
        return send_from_directory(str(_HERE / "prompts"), filename, mimetype="text/plain")
    except Exception as e:
        return jsonify({"error": str(e)}), 404


# ---------------------------------------------------------------------------
# Routes — state API
# ---------------------------------------------------------------------------

@app.route("/api/state", methods=["GET"])
def get_state():
    sections = load_sections()
    idx = sections_index()
    known_ids = [s["section_id"] for s in sections]

    with locked_state() as state:
        # Ensure section_order exists and is complete
        order = state.get("section_order", [])
        original_order = list(order)
        for sid in known_ids:
            if sid not in order:
                order.append(sid)
        state["section_order"] = order

        # Attach source annotation metadata for each section.
        # source_annotations and page_range are ALWAYS refreshed from sections.json
        # (the file is rewritten on every re-upload/re-preprocess), so the UI
        # always reflects the current annotation set — including any new red
        # annotations added since the last export.  Approval flags and generated
        # content (narrative, quiz, …) are preserved from the existing state.
        for sid in order:
            sec_state = state["sections"].get(sid, {})
            source_section = idx.get(sid, {})
            if source_section:
                # Always overwrite from the freshly-loaded sections.json so a
                # re-upload that adds/removes annotations is immediately visible.
                sec_state["source_annotations"] = source_section.get("source_annotations", [])
                sec_state["page_range"] = source_section.get("page_range", {})
            else:
                # Section no longer exists in sections.json (orphaned by re-upload).
                # Keep whatever was cached; callers can detect orphans by the
                # absence of source_section.
                sec_state.setdefault("source_annotations", [])
                sec_state.setdefault("page_range", {})
            sec_state.setdefault("narrative_approved", False)
            state["sections"][sid] = sec_state

        # Always persist so section_order and defaults are written
        result = dict(state)

    # Expose the per-project color→role map so the UI can resolve the
    # "Quiz-worthy" role (rather than assuming the literal "red") when counting
    # quiz-worthy annotations for the gate prompt.
    result["color_config"] = load_color_config_from_sections()

    return jsonify(result)


@app.route("/api/state", methods=["POST"])
def post_state():
    """Full state replace (used by export tab)."""
    with locked_state() as state:
        state.clear()
        state.update(request.get_json())
    return jsonify({"ok": True})


@app.route("/api/section/<section_id>/narrative", methods=["PUT"])
def update_narrative(section_id):
    """Save inline edits to narrative fields."""
    payload = request.get_json()
    with locked_state() as state:
        state.setdefault("sections", {}).setdefault(section_id, {})
        state["sections"][section_id]["narrative"] = payload
    return jsonify({"ok": True})


@app.route("/api/section/<section_id>/approve", methods=["POST"])
def approve_section(section_id):
    approved = request.get_json().get("approved", True)
    with locked_state() as state:
        state.setdefault("sections", {}).setdefault(section_id, {})
        state["sections"][section_id]["narrative_approved"] = approved
    return jsonify({"ok": True, "approved": approved})


@app.route("/api/section_order", methods=["PUT"])
def update_section_order():
    order = request.get_json().get("order", [])
    with locked_state() as state:
        state["section_order"] = order
    return jsonify({"ok": True})


@app.route("/api/section/<section_id>/delete", methods=["DELETE"])
def delete_section(section_id):
    """Remove a section entirely: from sections.json (the source of truth the
    UI rebuilds order from), and from state.json's sections map + section_order.
    Re-running preprocess regenerates the full list, so this is reversible."""
    sections = load_sections()
    if section_id not in {s["section_id"] for s in sections}:
        return jsonify({"error": f"Section {section_id} not found"}), 404

    _save_sections([s for s in sections if s["section_id"] != section_id])

    with locked_state() as state:
        state.get("sections", {}).pop(section_id, None)
        state["section_order"] = [
            sid for sid in state.get("section_order", []) if sid != section_id
        ]
    return jsonify({"ok": True})


@app.route("/api/section/<section_id>/generate_narrative", methods=["POST"])
def generate_narrative(section_id):
    """Trigger LLM narrative generation for a single section."""
    try:
        import zsg.generate as gen

        cfg = load_llm_config()
        idx = sections_index()
        section = idx.get(section_id)
        if not section:
            return jsonify({"error": f"Section {section_id} not found"}), 404

        prompt = gen.build_narrative_prompt(section)
        raw = gen.call_llm(prompt, cfg)
        from zsg.json_repair import attempt_repair
        parsed, _ = attempt_repair(raw)

        with locked_state() as state:
            state.setdefault("sections", {}).setdefault(section_id, {})
            # Clear any prior error now that this run succeeded
            state["sections"][section_id].pop("narrative_error", None)
            state["sections"][section_id]["narrative"] = parsed
            state["sections"][section_id]["narrative_approved"] = False
            state["sections"][section_id]["source_annotations"] = section["source_annotations"]
        return jsonify({"ok": True, "narrative": parsed})
    except Exception as e:
        # Record the failure on the section so the UI can surface it
        try:
            import zsg.generate as gen
            is_truncation = isinstance(e, gen.TruncationError)
        except Exception:
            is_truncation = False
        try:
            with locked_state() as state:
                state.setdefault("sections", {}).setdefault(section_id, {})
                state["sections"][section_id]["narrative_error"] = str(e)
        except Exception:
            pass
        if is_truncation:
            return jsonify({"error": str(e), "code": "truncated"}), 422
        return jsonify({"error": str(e)}), 500


@app.route("/api/section/<section_id>/generate_quiz", methods=["POST"])
def generate_quiz(section_id):
    """Trigger LLM quiz generation for an approved section."""
    try:
        import zsg.generate as gen

        # Read approved_narrative before taking the write lock
        current_state = load_state()
        sec_state = current_state.get("sections", {}).get(section_id, {})
        if not sec_state.get("narrative_approved"):
            return jsonify({"error": "Narrative must be approved before generating quiz"}), 400

        cfg = load_llm_config()
        idx = sections_index()
        section = idx.get(section_id)
        if not section:
            return jsonify({"error": f"Section {section_id} not found"}), 404

        approved_narrative = sec_state.get("narrative", {})
        prompt = gen.build_quiz_prompt(section, approved_narrative)
        raw = gen.call_llm(prompt, cfg)
        from zsg.json_repair import attempt_repair
        parsed, _ = attempt_repair(raw)

        with locked_state() as state:
            state.setdefault("sections", {}).setdefault(section_id, {})
            # Clear any prior error now that this run succeeded
            state["sections"][section_id].pop("quiz_error", None)
            state["sections"][section_id]["quiz"] = parsed
            state["sections"][section_id]["quiz_approved"] = False
        return jsonify({"ok": True, "quiz": parsed})
    except Exception as e:
        # Record the failure on the section so the UI can surface it
        try:
            import zsg.generate as gen
            is_truncation = isinstance(e, gen.TruncationError)
        except Exception:
            is_truncation = False
        try:
            with locked_state() as state:
                state.setdefault("sections", {}).setdefault(section_id, {})
                state["sections"][section_id]["quiz_error"] = str(e)
        except Exception:
            pass
        if is_truncation:
            return jsonify({"error": str(e), "code": "truncated"}), 422
        return jsonify({"error": str(e)}), 500


@app.route("/api/section/<section_id>/quiz", methods=["PUT"])
def update_quiz(section_id):
    """Save inline edits to quiz fields."""
    payload = request.get_json()
    with locked_state() as state:
        state.setdefault("sections", {}).setdefault(section_id, {})
        state["sections"][section_id]["quiz"] = payload
    return jsonify({"ok": True})


@app.route("/api/section/<section_id>/quiz_approve", methods=["POST"])
def approve_quiz(section_id):
    approved = request.get_json().get("approved", True)
    with locked_state() as state:
        state.setdefault("sections", {}).setdefault(section_id, {})
        state["sections"][section_id]["quiz_approved"] = approved
    return jsonify({"ok": True, "approved": approved})


@app.route("/api/section/<section_id>/regenerate_question", methods=["POST"])
def regenerate_question(section_id):
    """Re-generate a single quiz question given its source annotation IDs."""
    try:
        import zsg.generate as gen

        state = load_state()
        sec_state = state.get("sections", {}).get(section_id, {})
        if not sec_state.get("narrative_approved"):
            return jsonify({"error": "Narrative must be approved"}), 400

        body = request.get_json()
        ann_ids = set(body.get("source_annotation_ids", []))

        idx = sections_index()
        section = idx.get(section_id)
        if not section:
            return jsonify({"error": f"Section {section_id} not found"}), 404

        # Build a mini-section with only the requested annotations
        if ann_ids:
            target_anns = [a for a in section["source_annotations"] if a["id"] in ann_ids]
        else:
            target_anns = [a for a in section["source_annotations"] if a.get("color") == "red"]
        mini_section = {**section, "source_annotations": target_anns}

        cfg = load_llm_config()
        approved_narrative = sec_state.get("narrative", {})
        prompt = gen.build_quiz_prompt(mini_section, approved_narrative)
        raw = gen.call_llm(prompt, cfg)
        from zsg.json_repair import attempt_repair
        parsed, _ = attempt_repair(raw)

        # Successful regenerate — clear any quiz-level error
        with locked_state() as wstate:
            wstate.setdefault("sections", {}).setdefault(section_id, {})
            wstate["sections"][section_id].pop("quiz_error", None)

        questions = parsed.get("questions", [])
        return jsonify({"ok": True, "question": questions[0] if questions else None})
    except Exception as e:
        try:
            import zsg.generate as gen
            is_truncation = isinstance(e, gen.TruncationError)
        except Exception:
            is_truncation = False
        try:
            with locked_state() as state:
                state.setdefault("sections", {}).setdefault(section_id, {})
                state["sections"][section_id]["quiz_error"] = str(e)
        except Exception:
            pass
        if is_truncation:
            return jsonify({"error": str(e), "code": "truncated"}), 422
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Export routes
# ---------------------------------------------------------------------------

@app.route("/api/export/preview", methods=["POST"])
def export_preview():
    """Return a summary of what will be in the built guide."""
    state   = load_state()
    body    = request.get_json() or {}
    title   = body.get("title", "Study Guide")
    theme   = body.get("theme", "light")
    order   = state.get("section_order") or list(state.get("sections", {}).keys())

    sections_summary = []
    for sid in order:
        sec = state.get("sections", {}).get(sid, {})
        if not sec.get("narrative_approved"):
            continue
        narrative  = sec.get("narrative", {})
        quiz_qs    = sec.get("quiz", {}).get("questions", [])
        kp_count   = len(narrative.get("key_points", []))
        fig_count  = len(narrative.get("figures", []))
        sections_summary.append({
            "section_id": sid,
            "heading":    narrative.get("heading", sid),
            "key_points": kp_count,
            "figures":    fig_count,
            "questions":  len(quiz_qs),
        })

    return jsonify({
        "title":    title,
        "theme":    theme,
        "sections": sections_summary,
        "total_questions": sum(s["questions"] for s in sections_summary),
    })


@app.route("/api/export/build", methods=["POST"])
def export_build():
    """Build the final HTML and write it to disk; return the output path."""
    import zsg.build_guide as bg

    state  = load_state()
    body   = request.get_json() or {}
    title  = body.get("title", "Study Guide")
    theme  = body.get("theme", "light")

    # Apply any global_settings overrides from the export form
    state.setdefault("global_settings", {})
    state["global_settings"]["title"] = title
    state["global_settings"]["theme"] = theme
    if "show_progress" in body:
        state["global_settings"]["show_progress"] = body["show_progress"]
    if "navigation" in body:
        state["global_settings"]["navigation"] = body["navigation"]

    output_path = STATE_PATH.parent / "output.html"
    html = bg.build(state, title, theme)
    save_state(state)
    _atomic_write_text(output_path, html)

    approved = [
        sid for sid, sec in state.get("sections", {}).items()
        if sec.get("narrative_approved")
    ]
    return jsonify({
        "ok":       True,
        "path":     str(output_path),
        "sections": len(approved),
        "size_kb":  round(len(html) / 1024, 1),
    })


@app.route("/api/export/open", methods=["POST"])
def export_open():
    """Open the built HTML file in the system browser."""
    import subprocess, sys as _sys
    output_path = STATE_PATH.parent / "output.html"
    if not output_path.exists():
        return jsonify({"error": "No output.html found — build first."}), 404
    if _sys.platform == "darwin":
        subprocess.Popen(["open", str(output_path)])
    elif _sys.platform == "win32":
        subprocess.Popen(["start", str(output_path)], shell=True)
    else:
        subprocess.Popen(["xdg-open", str(output_path)])
    return jsonify({"ok": True})


@app.route("/api/global_settings", methods=["PUT"])
def update_global_settings():
    payload = request.get_json() or {}
    with locked_state() as state:
        state.setdefault("global_settings", {}).update(payload)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Config API routes
# ---------------------------------------------------------------------------

def load_app_config() -> dict:
    if not APP_CONFIG_PATH or not APP_CONFIG_PATH.exists():
        return {}
    with open(APP_CONFIG_PATH) as f:
        return json.load(f)


def load_llm_config() -> dict:
    """Resolve the LLM config for generation routes.

    app_config.json is the GUI's source of truth — the Setup tab writes the
    provider, model, and API key there. Prefer its ``llm`` block so the review
    app works whether it was launched via ``python -m zsg.app`` (which exports
    ZSG_CONFIG_PATH) or ``python -m zsg.verify`` directly. Fall back to
    generate.load_llm_config() (llm_config.yaml / ZSG_CONFIG_PATH) only when
    app_config has no llm block, so power-user YAML workflows still work."""
    app_cfg = load_app_config()
    llm = app_cfg.get("llm")
    if isinstance(llm, dict) and llm.get("provider"):
        return llm
    import zsg.generate as gen
    return gen.load_llm_config()


def save_app_config(cfg: dict):
    if not APP_CONFIG_PATH:
        return
    with _file_lock_for(APP_CONFIG_PATH):
        _atomic_write_text(
            APP_CONFIG_PATH,
            json.dumps(cfg, indent=2, ensure_ascii=False),
        )


@app.route("/api/config", methods=["GET"])
def get_config():
    cfg = load_app_config()
    return jsonify(cfg)


@app.route("/api/config", methods=["PUT"])
def put_config():
    cfg = request.get_json() or {}
    save_app_config(cfg)
    return jsonify({"ok": True})


@app.route("/api/config/test", methods=["POST"])
def test_llm_config():
    """Test LLM connection with a minimal call."""
    try:
        import zsg.generate as gen
        cfg = load_app_config().get("llm", {})
        if not cfg:
            return jsonify({"ok": False, "error": "No LLM config found"}), 400
        result = gen.call_llm("Reply with the word OK.", cfg)
        return jsonify({"ok": True, "response": result[:100]})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ---------------------------------------------------------------------------
# Project management routes
# ---------------------------------------------------------------------------

def slugify(name: str) -> str:
    """Convert name to a filesystem-safe slug."""
    return re.sub(r'[^a-z0-9_-]', '', name.lower().replace(" ", "_"))


@app.route("/api/projects", methods=["GET"])
def get_projects():
    """List all projects with stage status."""
    cfg = load_app_config()
    projects = cfg.get("projects", [])

    base_dir = PROJECT_ROOT / "projects"
    result = []
    for proj in projects:
        slug = proj.get("slug")
        proj_dir = base_dir / slug

        stage1_done = (proj_dir / "annotations.json").exists()
        stage2_done = (proj_dir / "sections.json").exists()

        state_done = False
        if (proj_dir / "state.json").exists():
            try:
                with open(proj_dir / "state.json") as f:
                    state = json.load(f)
                    has_narrative = any(
                        s.get("narrative") for s in state.get("sections", {}).values()
                    )
                    state_done = has_narrative
            except:
                pass

        stage5_done = (proj_dir / "output.html").exists()

        result.append({
            "slug": slug,
            "name": proj.get("name", slug),
            "created": proj.get("created"),
            "stages": {
                "1": stage1_done,
                "2": stage2_done,
                "3": state_done,
                "5": stage5_done,
            }
        })

    return jsonify({"projects": result, "active": cfg.get("active_project")})


@app.route("/api/projects/new", methods=["POST"])
def create_project():
    """Create a new project."""
    body = request.get_json() or {}
    name = body.get("name", "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400

    slug = slugify(name)
    if not slug:
        return jsonify({"error": "invalid name"}), 400

    cfg = load_app_config()

    # Check if exists
    if any(p["slug"] == slug for p in cfg.get("projects", [])):
        return jsonify({"error": f"project '{slug}' already exists"}), 409

    # Create project dir
    proj_dir = PROJECT_ROOT / "projects" / slug
    proj_dir.mkdir(parents=True, exist_ok=True)

    # Add to config
    from datetime import datetime
    cfg.setdefault("projects", []).append({
        "slug": slug,
        "name": name,
        "created": datetime.now().isoformat(),
    })
    save_app_config(cfg)

    return jsonify({"slug": slug, "name": name, "ok": True})


@app.route("/api/projects/open", methods=["POST"])
def open_project():
    """Open a project and set it as active."""
    global STATE_PATH, SECTIONS_PATH

    body = request.get_json() or {}
    slug = body.get("slug", "").strip()
    if not slug:
        return jsonify({"error": "slug required"}), 400

    cfg = load_app_config()

    # Verify project exists
    if not any(p["slug"] == slug for p in cfg.get("projects", [])):
        return jsonify({"error": f"project '{slug}' not found"}), 404

    # Update globals
    proj_dir = PROJECT_ROOT / "projects" / slug
    STATE_PATH = proj_dir / "state.json"
    SECTIONS_PATH = proj_dir / "sections.json"

    # Update config
    cfg["active_project"] = slug
    save_app_config(cfg)

    # Return stage status
    stages = {
        "1": (proj_dir / "annotations.json").exists(),
        "2": (proj_dir / "sections.json").exists(),
        "3": False,
        "5": (proj_dir / "output.html").exists(),
    }
    state_path = proj_dir / "state.json"
    if state_path.exists():
        try:
            with open(state_path) as f:
                s = json.load(f)
            stages["3"] = any(
                sec.get("narrative") for sec in s.get("sections", {}).values()
            )
        except:
            pass

    return jsonify({"ok": True, "slug": slug, "stages": stages})


@app.route("/api/upload/zotero_export", methods=["POST"])
def upload_zotero_export():
    """Upload a Zotero export file."""
    if "file" not in request.files:
        return jsonify({"error": "no file provided"}), 400

    file = request.files["file"]
    if not file.filename:
        return jsonify({"error": "empty filename"}), 400

    cfg = load_app_config()
    active_slug = cfg.get("active_project")
    if not active_slug:
        return jsonify({"error": "no active project"}), 400

    proj_dir = PROJECT_ROOT / "projects" / active_slug
    proj_dir.mkdir(parents=True, exist_ok=True)

    # Determine extension and validate against allowlist
    ext = Path(file.filename).suffix.lower() or ".html"
    allowed = {".html", ".htm", ".md", ".csv", ".json"}
    if ext not in allowed:
        return jsonify({"error": f"unsupported file type '{ext}'"}), 400
    upload_path = proj_dir / f"source_export{ext}"
    file.save(str(upload_path))

    return jsonify({"ok": True, "path": str(upload_path)})


# ---------------------------------------------------------------------------
# Pipeline orchestration routes
# ---------------------------------------------------------------------------

@app.route("/api/pipeline/run", methods=["POST"])
def run_pipeline_stage():
    """Trigger a pipeline stage."""
    import zsg.pipeline_runner as runner

    body = request.get_json() or {}
    stage = body.get("stage")
    if not stage:
        return jsonify({"error": "stage required"}), 400

    cfg = load_app_config()
    active_slug = cfg.get("active_project")
    if not active_slug:
        return jsonify({"error": "no active project"}), 400

    proj_dir = PROJECT_ROOT / "projects" / active_slug

    # Set up env with config path
    env = os.environ.copy()
    env["ZSG_CONFIG_PATH"] = str(APP_CONFIG_PATH)

    try:
        if stage == "export":
            input_file = body.get("input_path")
            if not input_file:
                return jsonify({"error": "input_path required for export"}), 400
            cmd = [
                _sys.executable, "-m", "zsg.export",
                "--input", input_file,
                "--output", str(proj_dir / "annotations.json"),
            ]

        elif stage == "preprocess":
            options = body.get("options", {})
            strategy = options.get("strategy", "auto")
            page_window = options.get("page_window", 6)
            cmd = [
                _sys.executable, "-m", "zsg.preprocess",
                "--input", str(proj_dir / "annotations.json"),
                "--output", str(proj_dir / "sections.json"),
                "--strategy", strategy,
                "--page-window", str(page_window),
            ]

        elif stage == "generate_narrative":
            only_section = body.get("only_section")
            cmd = [
                _sys.executable, "-m", "zsg.generate",
                "narrative",
                "--sections", str(proj_dir / "sections.json"),
                "--state", str(proj_dir / "state.json"),
            ]
            if only_section:
                cmd.extend(["--only", only_section])

        elif stage == "generate_quiz":
            only_section = body.get("only_section")
            cmd = [
                _sys.executable, "-m", "zsg.generate",
                "quiz",
                "--sections", str(proj_dir / "sections.json"),
                "--state", str(proj_dir / "state.json"),
            ]
            if only_section:
                cmd.extend(["--only", only_section])

        elif stage == "build":
            # Just run build_guide synchronously; it's fast
            import zsg.build_guide as bg
            state = load_state()
            title = body.get("title", "Study Guide")
            theme = body.get("theme", "light")
            output_path = proj_dir / "output.html"
            html = bg.build(state, title, theme)
            _atomic_write_text(output_path, html)
            return jsonify({"ok": True, "path": str(output_path), "size_kb": round(len(html) / 1024, 1)})

        else:
            return jsonify({"error": f"unknown stage: {stage}"}), 400

        run_id = runner.start_stage(cmd, env=env)
        return jsonify({"run_id": run_id})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/pipeline/status/<run_id>", methods=["GET"])
def get_pipeline_status(run_id):
    """Get status of a running pipeline stage."""
    import zsg.pipeline_runner as runner
    status = runner.get_status(run_id)
    return jsonify(status)


@app.route("/api/pipeline/cancel/<run_id>", methods=["POST"])
def cancel_pipeline_stage(run_id):
    """Cancel a running pipeline stage."""
    import zsg.pipeline_runner as runner
    return jsonify(runner.cancel_stage(run_id))


# ---------------------------------------------------------------------------
# Stateless v2 API
# ---------------------------------------------------------------------------
# These endpoints hold no server-side state. Clients pass all inputs in the
# request body and persist results themselves (browser IndexedDB). API keys
# arrive per-request and are never logged or persisted.

@app.route("/api/v2/parse", methods=["POST"])
def v2_parse():
    """
    Parse a Zotero export from request body content. Stateless.

    Body: { "format": "html"|"md"|"csv"|"json", "content": "<file text>" }
    Returns: { "annotations": [...], "color_config": {...} }
    """
    try:
        import zsg.export as exp
        body = request.get_json(silent=True) or {}
        fmt = body.get("format", "html")
        content = body.get("content", "")
        if not content:
            return jsonify({"error": "content required"}), 400

        annotations = exp.parse_export_str(fmt, content)
        color_config = exp.load_color_config()
        return jsonify({
            "annotations": annotations,
            "color_config": color_config,
        })
    except exp.EmptyExportError as e:
        return jsonify({"error": str(e), "code": "empty_export"}), 422
    except (ValueError, json.JSONDecodeError) as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/v2/parse_upload", methods=["POST"])
def v2_parse_upload():
    """
    Parse an uploaded Zotero export file directly (no disk persistence).
    Stateless multipart variant of /api/v2/parse for the file-picker UI.
    """
    try:
        import zsg.export as exp
        if "file" not in request.files:
            return jsonify({"error": "no file provided"}), 400
        f = request.files["file"]
        ext = Path(f.filename or "").suffix.lower().lstrip(".") or "html"
        allowed = {"html", "htm", "md", "csv", "json"}
        if ext not in allowed:
            return jsonify({"error": f"unsupported file type '.{ext}'"}), 400

        content = f.read().decode("utf-8", errors="replace")
        annotations = exp.parse_export_str(ext, content)
        color_config = exp.load_color_config()
        return jsonify({
            "annotations": annotations,
            "color_config": color_config,
            "filename": f.filename,
        })
    except exp.EmptyExportError as e:
        return jsonify({"error": str(e), "code": "empty_export"}), 422
    except (ValueError, json.JSONDecodeError) as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/v2/sections", methods=["POST"])
def v2_sections():
    """
    Group annotations into sections. Stateless.

    Body: {
        "annotations": [...],
        "strategy": "auto"|"tags"|"proximity",
        "page_window": int
    }
    Returns: { "sections": [...] }
    """
    try:
        import zsg.preprocess as pp
        body = request.get_json(silent=True) or {}
        annotations = body.get("annotations") or []
        if not annotations:
            return jsonify({"error": "annotations required"}), 400

        strategy = body.get("strategy", "auto")
        page_window = int(body.get("page_window", pp.PAGE_WINDOW_DEFAULT))
        sections = pp.preprocess(annotations, strategy, page_window)
        return jsonify({"sections": sections})
    except (ValueError, TypeError) as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/v2/llm", methods=["POST"])
def v2_llm():
    """
    Proxy a prompt to the user's chosen LLM provider. Stateless and key-free.

    The api_key from the request body is used for this single call and is
    never logged or persisted. The response is returned verbatim plus any
    repaired JSON view.

    Body: {
        "provider": "purdue_genai"|"anthropic"|"openai"|"ollama"|...,
        "api_key":  "...",            # required for hosted providers
        "model":    "...",
        "base_url": "...",            # optional
        "max_tokens": int,
        "temperature": float,
        "prompt":   "<system>...</system>...user text..."
    }
    Returns: {
        "text":   "<raw LLM response>",
        "parsed": <object>|null,      # JSON view after repair, if applicable
    }
    """
    body = request.get_json(silent=True) or {}
    prompt = body.get("prompt", "")
    if not prompt:
        return jsonify({"error": "prompt required"}), 400

    cfg = {
        "provider":    body.get("provider", "purdue_genai"),
        "api_key":     body.get("api_key", ""),
        "model":       body.get("model", ""),
        "base_url":    body.get("base_url", ""),
        "temperature": float(body.get("temperature", 0.1)),
        "max_tokens":  int(body.get("max_tokens", 8192)),
        "top_p":       body.get("top_p"),
        "repeat_penalty": body.get("repeat_penalty"),
    }
    # Drop optional keys the underlying call_llm doesn't expect when unset
    cfg = {k: v for k, v in cfg.items() if v is not None}

    try:
        import zsg.generate as gen
        from zsg.json_repair import attempt_repair

        text = gen.call_llm(prompt, cfg)
        parsed = None
        try:
            parsed, _ = attempt_repair(text)
        except Exception:
            parsed = None

        resp = jsonify({"text": text, "parsed": parsed})
    except gen.TruncationError as e:
        resp = jsonify({"error": str(e), "code": "truncated"})
        resp.status_code = 422
    except Exception as e:
        resp = jsonify({"error": str(e)})
        resp.status_code = 500

    # Never let intermediaries cache an LLM response that may contain user data
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/v2/build", methods=["POST"])
def v2_build():
    """
    Build the final HTML guide from a complete state object. Stateless.

    Body: { "state": {...}, "title": "...", "theme": "light" }
    Returns: { "html": "...", "size_kb": float, "sections": int }
    """
    try:
        import zsg.build_guide as bg
        body = request.get_json(silent=True) or {}
        state = body.get("state") or {}
        title = body.get("title", "Study Guide")
        theme = body.get("theme", "light")
        html = bg.build(state, title, theme)
        approved = sum(
            1 for s in state.get("sections", {}).values()
            if s.get("narrative_approved")
        )
        resp = jsonify({
            "html": html,
            "size_kb": round(len(html) / 1024, 1),
            "sections": approved,
        })
        resp.headers["Cache-Control"] = "no-store"
        return resp
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/v2/build_download", methods=["POST"])
def v2_build_download():
    """
    Same as /api/v2/build but returns the HTML directly as a downloadable file
    rather than wrapped in JSON. Convenient for "Save guide" buttons.
    """
    from flask import Response
    try:
        import zsg.build_guide as bg
        body = request.get_json(silent=True) or {}
        state = body.get("state") or {}
        title = body.get("title", "Study Guide")
        theme = body.get("theme", "light")
        html = bg.build(state, title, theme)
        safe_title = re.sub(r"[^a-zA-Z0-9_-]+", "_", title).strip("_") or "study_guide"
        return Response(
            html,
            mimetype="text/html; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{safe_title}.html"',
                "Cache-Control": "no-store",
            },
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global STATE_PATH, SECTIONS_PATH, APP_CONFIG_PATH

    parser = argparse.ArgumentParser(description="Zotero Study Guide verification app.")
    parser.add_argument("--state",    "-t", required=False, help="Path to state.json")
    parser.add_argument("--sections", "-s", required=False, help="Path to sections.json")
    parser.add_argument("--port",     type=int, default=5000)
    args = parser.parse_args()

    APP_CONFIG_PATH = PROJECT_ROOT / "app_config.json"

    # If --state and --sections are provided, use them (backward compatibility)
    if args.state and args.sections:
        STATE_PATH    = Path(args.state)
        SECTIONS_PATH = Path(args.sections)
    else:
        # Otherwise, try to use active_project from config
        cfg = load_app_config()
        active_slug = cfg.get("active_project")
        if active_slug:
            proj_dir = PROJECT_ROOT / "projects" / active_slug
            STATE_PATH = proj_dir / "state.json"
            SECTIONS_PATH = proj_dir / "sections.json"
        else:
            # Fallback to a placeholder (app.py will set this)
            STATE_PATH = PROJECT_ROOT / "state.json"
            SECTIONS_PATH = PROJECT_ROOT / "sections.json"

    if args.state and args.sections and not SECTIONS_PATH.exists():
        print(f"Error: {SECTIONS_PATH} not found. Run preprocess.py first.")
        raise SystemExit(1)

    print(f"State file : {STATE_PATH}")
    print(f"Sections   : {SECTIONS_PATH}")
    print(f"Config     : {APP_CONFIG_PATH}")
    print(f"Open       : http://localhost:{args.port}")

    app.run(port=args.port, debug=False)


if __name__ == "__main__":
    main()
