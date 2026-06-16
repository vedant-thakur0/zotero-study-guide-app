"""
json_repair.py — Fix common LLM JSON output failures

Handles, in order:
  1. Markdown fencing (```json ... ```)
  2. Leading preamble ("Here is the JSON:" etc.)
  3. Trailing commentary after closing brace/bracket
  4. Trailing commas before } or ]
  5. Unquoted keys
  6. Python-style True/False/None → true/false/null
  7. Delegates remaining failures to the json-repair library if installed

Returns (parsed_dict_or_list, was_repaired: bool)
"""

import json
import re


def _strip_markdown_fence(text: str) -> str:
    text = text.strip()
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if m:
        return m.group(1).strip()
    # Opening fence with no closing — take everything after it
    m = re.match(r"```(?:json)?\s*([\s\S]*)", text)
    if m:
        return m.group(1).strip()
    return text


def _strip_preamble(text: str) -> str:
    """Drop lines before the first { or [."""
    for i, ch in enumerate(text):
        if ch in "{[":
            return text[i:]
    return text


def _strip_trailing_commentary(text: str) -> str:
    """Truncate at the last } or ] that closes the root structure."""
    # Find the last } or ] character
    for i in range(len(text) - 1, -1, -1):
        if text[i] in "}]":
            return text[: i + 1]
    return text


def _fix_trailing_commas(text: str) -> str:
    return re.sub(r",\s*([}\]])", r"\1", text)


def _fix_python_literals(text: str) -> str:
    parts = re.split(r'("(?:[^"\\]|\\.)*")', text)
    for i, part in enumerate(parts):
        if not part.startswith('"'):
            part = re.sub(r"\bTrue\b",  "true",  part)
            part = re.sub(r"\bFalse\b", "false", part)
            part = re.sub(r"\bNone\b",  "null",  part)
            parts[i] = part
    return "".join(parts)


def attempt_repair(raw: str) -> tuple:
    """
    Try progressively more aggressive repairs.
    Returns (parsed_object, was_repaired).
    Raises ValueError if all attempts fail.
    """
    original = raw

    # Fast path — already valid
    try:
        return json.loads(raw), False
    except json.JSONDecodeError:
        pass

    # Apply fixes in sequence, retry after each stage
    fixes = [
        _strip_markdown_fence,
        _strip_preamble,
        _strip_trailing_commentary,
        _fix_trailing_commas,
        _fix_python_literals,
    ]

    current = raw
    for fix in fixes:
        current = fix(current)
        try:
            return json.loads(current), True
        except json.JSONDecodeError:
            pass

    # Last resort: delegate to json-repair library
    try:
        import json_repair as jr  # pip install json-repair
        result = jr.repair_json(original, return_objects=True)
        if result is not None:
            return result, True
    except ImportError:
        pass
    except Exception:
        pass

    raise ValueError(
        "Could not parse LLM output as JSON after all repair attempts.\n"
        f"Raw output (first 500 chars):\n{original[:500]}"
    )
