"""
generate.py — Stage 3: LLM generation of narrative and quiz JSON

Reads sections.json, calls the configured LLM for each section, writes results
to state.json (the verification app's working file).

Two sub-commands:
  narrative   Run narrative generation for all (or named) sections
  quiz        Run quiz generation for approved sections (reads state.json)

Usage:
    python -m zsg.generate narrative --sections projects/my_project/sections.json \
                                     --state    projects/my_project/state.json

    python -m zsg.generate quiz      --sections projects/my_project/sections.json \
                                     --state    projects/my_project/state.json

    # Generate quizzes from narrative (for zero-red sections):
    python -m zsg.generate quiz      --sections projects/my_project/sections.json \
                                     --state    projects/my_project/state.json \
                                     --from-narrative

    # Single section only:
    python -m zsg.generate narrative --sections projects/my_project/sections.json \
                                     --state    projects/my_project/state.json \
                                     --only reconstruction

    # Dry-run: print prompt without calling LLM
    python -m zsg.generate narrative --sections projects/my_project/sections.json \
                                     --state    projects/my_project/state.json \
                                     --dry-run
"""

import argparse
import concurrent.futures
import json
import os
import re
import sys
import threading
import yaml
from pathlib import Path

from zsg import PKG_DIR, PROJECT_ROOT
from zsg.json_repair import attempt_repair

NARRATIVE_PROMPT         = (PKG_DIR / "prompts" / "narrative.txt").read_text(encoding="utf-8")
QUIZ_PROMPT              = (PKG_DIR / "prompts" / "quiz.txt").read_text(encoding="utf-8")
QUIZ_FROM_NARRATIVE_PROMPT = (PKG_DIR / "prompts" / "quiz_from_narrative.txt").read_text(encoding="utf-8")


class TruncationError(RuntimeError):
    """Raised when an LLM response stops at the max_tokens limit before completing JSON."""


# ---------------------------------------------------------------------------
# LLM client
# ---------------------------------------------------------------------------

def _llm_config_path() -> Path:
    """Resolve config path at call time so env var changes are picked up."""
    override = os.environ.get("ZSG_CONFIG_PATH")
    return Path(override) if override else PROJECT_ROOT / "llm_config.yaml"


def load_llm_config() -> dict:
    path = _llm_config_path()
    with open(path) as f:
        if path.suffix == ".json":
            cfg = json.load(f)
            return cfg.get("llm", cfg)
        else:
            return yaml.safe_load(f)


def call_llm(prompt: str, cfg: dict, dry_run: bool = False) -> str:
    if dry_run:
        print("\n--- PROMPT (dry-run) ---")
        print(prompt[:2000])
        print("--- END PROMPT ---\n")
        return '{"dry_run": true}'

    import requests

    provider = cfg.get("provider", "purdue_genai")
    base_url = cfg.get("base_url", "https://genai.rcac.purdue.edu/api/chat/completions")
    model    = cfg.get("model", "llama3.1:latest")
    temperature = cfg.get("temperature", 0.1)
    max_tokens  = cfg.get("max_tokens", 2048)

    if provider == "ollama":
        payload = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
                "top_p": cfg.get("top_p", 0.85),
                "repeat_penalty": cfg.get("repeat_penalty", 1.05),
            },
        }
        resp = requests.post(f"{base_url}/api/generate", json=payload, timeout=120)
        resp.raise_for_status()
        return resp.json()["response"]

    if provider in ("vllm", "lmstudio", "openai"):
        # OpenAI-compatible endpoint
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        # Bearer auth is optional here: hosted OpenAI-compatible endpoints need a
        # key; local runners (vLLM, LM Studio) usually don't. Source it from cfg
        # or the env so keys never have to live in tracked config.
        headers = {"Content-Type": "application/json"}
        api_key = cfg.get("api_key") or os.environ.get("OPENAI_API_KEY")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        resp = requests.post(
            f"{base_url}/v1/chat/completions",
            json=payload,
            headers=headers,
            timeout=120,
        )
        resp.raise_for_status()
        body = resp.json()
        choice = body["choices"][0]
        finish_reason = choice.get("finish_reason")
        if finish_reason == "length":
            raise TruncationError(
                f"LLM response was truncated at max_tokens={max_tokens}. "
                "Increase max_tokens and retry."
            )
        return choice["message"]["content"]

    if provider == "purdue_genai":
        api_key = cfg.get("api_key") or os.environ.get("PURDUE_GENAI_API_KEY")
        if not api_key:
            raise ValueError("Purdue GenAI provider requires an API key in config or PURDUE_GENAI_API_KEY env var")
        endpoint = base_url or "https://genai.rcac.purdue.edu/api/chat/completions"
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        resp = requests.post(
            endpoint,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=120,
        )
        resp.raise_for_status()
        body = resp.json()
        choice = body["choices"][0]
        if choice.get("finish_reason") == "length":
            raise TruncationError(
                f"LLM response was truncated at max_tokens={max_tokens}. "
                "Increase max_tokens and retry."
            )
        return choice["message"]["content"]

    if provider == "anthropic":
        import anthropic as _anthropic
        api_key = cfg.get("api_key") or os.environ.get("ANTHROPIC_API_KEY")
        if not model.startswith("claude-"):
            raise ValueError(f"Anthropic provider requires a claude-* model, got {model!r}")

        client = _anthropic.Anthropic(api_key=api_key, timeout=120.0)

        # Extract leading <system>...</system> block; rest is the user message.
        # The system block is sent with cache_control=ephemeral; Anthropic only
        # returns a cache hit when the cached text is byte-identical across
        # calls (≤5 min TTL). Keep per-section placeholders (section_id,
        # annotations, narrative_json, red_annotations) OUTSIDE this block —
        # the prompt templates put them in a trailing <input> region after
        # </system> for exactly this reason.
        # Allow a leading HTML comment (used by prompt files to document the
        # cache-reuse convention) before <system>.
        sys_match = re.match(
            r"\s*(?:<!--.*?-->\s*)?<system>(.*?)</system>", prompt, re.DOTALL
        )
        if sys_match:
            system_text = sys_match.group(1).strip()
            user_text = prompt[sys_match.end():].strip()
        else:
            system_text = None
            user_text = prompt.strip()

        system_param = (
            [{"type": "text", "text": system_text,
              "cache_control": {"type": "ephemeral"}}]
            if system_text else _anthropic.NOT_GIVEN
        )

        msg = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system_param,
            messages=[
                {"role": "user",      "content": user_text},
                {"role": "assistant", "content": "{"},
            ],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
        if getattr(msg, "stop_reason", None) == "max_tokens":
            raise TruncationError(
                f"Claude response was truncated at max_tokens={max_tokens}. "
                "Increase max_tokens in your LLM config and retry."
            )
        return "{" + text

    raise ValueError(f"Unknown LLM provider: {provider}")


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------

def format_annotations(annotations: list[dict]) -> str:
    lines = []
    for i, a in enumerate(annotations, 1):
        note_part = f' | note: "{a.get("instructor_note", "")}"' if a.get("instructor_note") else ""
        lines.append(
            f'[{i}] {a["id"]} ({a.get("color","?")}): "{a["text"]}"{note_part}'
        )
    return "\n".join(lines)


def build_narrative_prompt(section: dict) -> str:
    return NARRATIVE_PROMPT.replace("{section_id}", section["section_id"]).replace(
        "{annotations}", format_annotations(section["source_annotations"])
    )


def select_quiz_prompt(red_anns: list[dict]) -> str:
    """Return the appropriate quiz prompt template based on red annotation count.

    When ``red_anns`` is empty the artifact-sourced template is used so the LLM
    derives questions from the approved narrative (intro, key_points, figures).
    When at least one red annotation exists the standard template is used so
    existing behaviour is unchanged.

    B3 (CLI parity) should call this function directly rather than hard-coding
    the template selection.

    SYNC NOTE: This rule (zero red → quiz_from_narrative.txt; one+ red → quiz.txt)
    is mirrored client-side in static/client-mode.js generate_quiz handler.
    Any change here must be reflected there, and vice-versa.

    Args:
        red_anns: The list of red (quiz-worthy) annotations for the section.

    Returns:
        The full prompt template string — either ``QUIZ_PROMPT`` or
        ``QUIZ_FROM_NARRATIVE_PROMPT``.
    """
    if red_anns:
        return QUIZ_PROMPT
    return QUIZ_FROM_NARRATIVE_PROMPT


def build_quiz_prompt(section: dict, approved_narrative: dict) -> str:
    red_anns = [a for a in section["source_annotations"] if a.get("color") == "red"]
    template = select_quiz_prompt(red_anns)
    return (
        template
        .replace("{section_id}", section["section_id"])
        .replace("{narrative_json}", json.dumps(approved_narrative, indent=2))
        .replace("{red_annotations}", format_annotations(red_anns) or "(none)")
    )


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def load_state(state_path: Path) -> dict:
    if state_path.exists():
        with open(state_path) as f:
            return json.load(f)
    return {"sections": {}}


def save_state(state: dict, state_path: Path):
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with open(state_path, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Sub-commands
# ---------------------------------------------------------------------------

def run_narrative(sections: list[dict], state: dict, cfg: dict,
                  only, dry_run: bool, force: bool = False,
                  concurrency: int = 1):
    """Generate narratives for all (or named) sections.

    Per-section work runs on a ThreadPoolExecutor when concurrency > 1. The
    LLM call (the only slow step) happens outside the lock; state writes and
    prints are serialized so output stays readable and state is consistent.
    Per-section failures are isolated — siblings continue.
    """
    state_lock = threading.Lock()
    io_lock = threading.Lock()

    def _safe_print(msg: str):
        with io_lock:
            print(msg, flush=True)

    def _worker(section):
        sid = section["section_id"]
        # Check existing state under the lock (don't read a dict another thread is mutating).
        with state_lock:
            existing = state["sections"].get(sid, {})
        if existing.get("narrative_approved") and not force:
            _safe_print(f"  {sid}: already approved — skipping (use --force to re-run)")
            return

        _safe_print(f"  {sid}: generating narrative...")
        prompt = build_narrative_prompt(section)

        raw = ""
        try:
            raw = call_llm(prompt, cfg, dry_run)
            parsed, repaired = attempt_repair(raw)
        except Exception as e:
            _safe_print(f"  {sid}: FAILED: {e}")
            with state_lock:
                state["sections"].setdefault(sid, {})["narrative_error"] = str(e)
                state["sections"][sid]["narrative_raw"] = raw
            return

        with state_lock:
            state["sections"].setdefault(sid, {})
            state["sections"][sid]["narrative"] = parsed
            state["sections"][sid]["narrative_approved"] = False
            state["sections"][sid]["source_annotations"] = section["source_annotations"]
        _safe_print(f"  {sid}: done{' (repaired)' if repaired else ''}")

    # Filter to sections we will actually act on.
    targets = [s for s in sections if (not only or s["section_id"] == only)]

    if concurrency <= 1 or len(targets) <= 1:
        for s in targets:
            _worker(s)
        return

    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(_worker, s) for s in targets]
        for f in concurrent.futures.as_completed(futures):
            # Re-raise unexpected errors (the worker already catches LLM errors).
            f.result()


def run_quiz(sections: list[dict], state: dict, cfg: dict,
             only, dry_run: bool, force: bool = False,
             concurrency: int = 1, from_narrative: bool = False):
    """Generate quizzes for approved sections. Mirrors run_narrative's
    parallelism + locking pattern.

    When from_narrative=True, also generate quizzes for zero-red sections using
    the artifact-sourced template (quiz_from_narrative.txt). Otherwise, skip
    zero-red sections for backward compatibility.
    """
    state_lock = threading.Lock()
    io_lock = threading.Lock()

    def _safe_print(msg: str):
        with io_lock:
            print(msg, flush=True)

    def _worker(section):
        sid = section["section_id"]
        with state_lock:
            sec_state = state["sections"].get(sid, {})
        if not sec_state.get("narrative_approved"):
            _safe_print(f"  {sid}: narrative not approved — skipping quiz generation")
            return

        if sec_state.get("quiz_approved") and not force:
            _safe_print(f"  {sid}: quiz already approved — skipping (use --force to re-run)")
            return

        approved_narrative = sec_state.get("narrative", {})
        red_anns = [a for a in section["source_annotations"] if a.get("color") == "red"]
        if not red_anns and not from_narrative:
            _safe_print(f"  {sid}: no red annotations — skipping quiz generation")
            return

        _safe_print(f"  {sid}: generating quiz...")
        prompt = build_quiz_prompt(section, approved_narrative)

        try:
            raw = call_llm(prompt, cfg, dry_run)
            parsed, repaired = attempt_repair(raw)
        except Exception as e:
            _safe_print(f"  {sid}: FAILED: {e}")
            with state_lock:
                state["sections"].setdefault(sid, {})["quiz_error"] = str(e)
            return

        with state_lock:
            state["sections"][sid]["quiz"] = parsed
            state["sections"][sid]["quiz_approved"] = False
        _safe_print(f"  {sid}: done{' (repaired)' if repaired else ''}")

    targets = [s for s in sections if (not only or s["section_id"] == only)]

    if concurrency <= 1 or len(targets) <= 1:
        for s in targets:
            _worker(s)
        return

    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(_worker, s) for s in targets]
        for f in concurrent.futures.as_completed(futures):
            f.result()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="LLM narrative and quiz generation.")
    sub = parser.add_subparsers(dest="command", required=True)

    # Narrative subcommand
    p_narrative = sub.add_parser("narrative")
    p_narrative.add_argument("--sections", "-s", required=True, help="sections.json path")
    p_narrative.add_argument("--state",    "-t", required=True, help="state.json path")
    p_narrative.add_argument("--only",     help="Run only this section_id")
    p_narrative.add_argument("--dry-run",  action="store_true", help="Print prompt, skip LLM call")
    p_narrative.add_argument("--force",    action="store_true", help="Re-generate even if already approved")
    p_narrative.add_argument("--concurrency", "-c", type=int, default=1,
                             help="Number of sections to generate in parallel (default 1 = serial)")

    # Quiz subcommand
    p_quiz = sub.add_parser("quiz")
    p_quiz.add_argument("--sections", "-s", required=True, help="sections.json path")
    p_quiz.add_argument("--state",    "-t", required=True, help="state.json path")
    p_quiz.add_argument("--only",     help="Run only this section_id")
    p_quiz.add_argument("--dry-run",  action="store_true", help="Print prompt, skip LLM call")
    p_quiz.add_argument("--force",    action="store_true", help="Re-generate even if already approved")
    p_quiz.add_argument("--concurrency", "-c", type=int, default=1,
                        help="Number of sections to generate in parallel (default 1 = serial)")
    p_quiz.add_argument("--from-narrative", action="store_true",
                       help="Generate quizzes from approved narrative for zero-red sections")

    args = parser.parse_args()

    sections_path = Path(args.sections)
    if not sections_path.exists():
        print(f"Error: {sections_path} not found.", file=sys.stderr)
        sys.exit(1)

    with open(sections_path) as f:
        data = json.load(f)
    sections = data.get("sections", data) if isinstance(data, dict) else data

    state_path = Path(args.state)
    state = load_state(state_path)

    cfg = load_llm_config()
    print(f"LLM: {cfg['provider']} / {cfg['model']}")
    print(f"Command: {args.command} | sections: {len(sections)}\n")

    if args.concurrency > 1:
        print(f"Concurrency: {args.concurrency} parallel workers\n")

    if args.command == "narrative":
        run_narrative(sections, state, cfg, args.only, args.dry_run, args.force,
                      concurrency=args.concurrency)
    else:
        run_quiz(sections, state, cfg, args.only, args.dry_run, args.force,
                 concurrency=args.concurrency, from_narrative=getattr(args, "from_narrative", False))

    save_state(state, state_path)
    print(f"\nState saved to {state_path}")


if __name__ == "__main__":
    main()
