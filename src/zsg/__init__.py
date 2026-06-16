"""
zsg — Zotero → interactive study-guide pipeline.

Layout convention:
- PKG_DIR       = this package directory (src/zsg). Code-owned assets
                  (templates/, static/, prompts/) live here, beside the code.
- PROJECT_ROOT  = the repo root (two levels up). User/runtime data lives here:
                  projects/, app_config.json, llm_config.yaml, color_config.yaml,
                  and the interactive_practice_exam/ sub-tool.
"""

from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PKG_DIR.parent.parent
