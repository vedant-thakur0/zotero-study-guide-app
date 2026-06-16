#!/usr/bin/env python3
"""
app.py — local dev server for the Zotero Study Guide review/verification UI.

Run: python -m zsg.app

This starts the Flask app that serves the review UI and the stateless v2 API.
The browser is the source of truth in client mode (see static/client-mode.js);
this server is a stateless transformer for parse / sections / llm / build.

Setup (one-time):  pip install -r requirements.txt
Then open http://localhost:5000  (append ?mode=client for client-side storage).
"""

import json
import os

from zsg import PROJECT_ROOT

APP_CONFIG_PATH = PROJECT_ROOT / "app_config.json"

DEFAULT_COLORS = {
    "yellow": {"label": "Key concepts", "description": "Definitions, core ideas, important terms"},
    "red": {"label": "Quiz-worthy facts", "description": "Dates, events, specific claims to test"},
    "green": {"label": "People & organizations", "description": "Biographical information, institutional roles"},
    "blue": {"label": "Themes & arguments", "description": "Analytical threads, overarching narratives"},
    "purple": {"label": "Connections", "description": "Cross-references, links between topics"},
    "orange": {"label": "Examples", "description": "Case studies, illustrative instances"},
}


def ensure_config():
    """
    Make sure app_config.json exists. If it doesn't, migrate from the legacy
    llm_config.yaml / color_config.yaml if present, otherwise write defaults.
    """
    if APP_CONFIG_PATH.exists():
        return

    print("Initializing app_config.json...")

    llm_cfg, colors_cfg = {}, {}

    yaml_path = PROJECT_ROOT / "llm_config.yaml"
    colors_path = PROJECT_ROOT / "color_config.yaml"
    if yaml_path.exists() or colors_path.exists():
        import yaml
        if yaml_path.exists():
            data = yaml.safe_load(yaml_path.read_text()) or {}
            llm_cfg = {k: v for k, v in data.items() if k != "colors"}
        if colors_path.exists():
            data = yaml.safe_load(colors_path.read_text()) or {}
            colors_cfg = data.get("colors", {})

    app_config = {
        "llm": {
            "provider": llm_cfg.get("provider", "purdue_genai"),
            "api_key": llm_cfg.get("api_key", ""),
            "model": llm_cfg.get("model", "llama3.1:latest"),
            "base_url": llm_cfg.get("base_url", "https://genai.rcac.purdue.edu/api/chat/completions"),
            "temperature": llm_cfg.get("temperature", 0.1),
            "max_tokens": llm_cfg.get("max_tokens", 8192),
        },
        "colors": colors_cfg or DEFAULT_COLORS,
        "projects": [],
        "active_project": None,
    }
    APP_CONFIG_PATH.write_text(json.dumps(app_config, indent=2, ensure_ascii=False))
    print(f"Created {APP_CONFIG_PATH}")


def setup_flask():
    """Import verify and wire up the Flask app with config + project paths."""
    import zsg.verify as verify

    verify.APP_CONFIG_PATH = APP_CONFIG_PATH

    app_config = json.loads(APP_CONFIG_PATH.read_text()) if APP_CONFIG_PATH.exists() else {}
    active_slug = app_config.get("active_project")

    if active_slug:
        proj_dir = PROJECT_ROOT / "projects" / active_slug
        verify.STATE_PATH = proj_dir / "state.json"
        verify.SECTIONS_PATH = proj_dir / "sections.json"
    else:
        verify.STATE_PATH = PROJECT_ROOT / "state.json"
        verify.SECTIONS_PATH = PROJECT_ROOT / "sections.json"

    return verify.app


def _load_dotenv():
    """Best-effort load of a local .env (e.g. PURDUE_GENAI_API_KEY) so secrets
    can stay out of tracked/on-disk config. No-op if python-dotenv isn't
    installed or there's no .env — keeps it a zero-dependency convenience."""
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    try:
        from dotenv import load_dotenv
        load_dotenv(env_path)
    except ImportError:
        pass


def main():
    _load_dotenv()
    ensure_config()

    # generate.py reads its LLM config from this path when invoked as a subprocess.
    os.environ["ZSG_CONFIG_PATH"] = str(APP_CONFIG_PATH)

    app = setup_flask()

    print("\n" + "=" * 60)
    print("Zotero Study Guide — dev server")
    print("=" * 60)
    print("  http://localhost:5000               (server-state mode)")
    print("  http://localhost:5000/?mode=client  (client-side storage)")
    print("  Ctrl+C to stop\n")

    app.run(port=5000, debug=False, use_reloader=False, threaded=True)


if __name__ == "__main__":
    main()
