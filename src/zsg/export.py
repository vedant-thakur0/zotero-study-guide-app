"""
export.py — Stage 1: Zotero annotation export

Accepts a Zotero annotation export (HTML, CSV, or JSON) and normalizes it into
the pipeline's internal annotations.json format.

The HTML format is what Zotero produces via "Create Note from Annotations" —
each highlight is a <p> with an inline rgba background-color and a citation span.
Any text after the citation span is treated as the instructor's note.

Usage:
    python -m zsg.export --input Annotations.html --output projects/my_project/annotations.json
    python -m zsg.export --input Annotations.md   --output projects/my_project/annotations.json
    python -m zsg.export --input my_annotations.csv --output projects/my_project/annotations.json
    python -m zsg.export --input my_annotations.json --output projects/my_project/annotations.json
    python -m zsg.export --demo --output projects/civil_rights_m7/annotations.json

Supported formats:
    .html  Zotero "Create Note from Annotations" → Export Note (HTML) — RECOMMENDED
           Preserves highlight colors via inline rgba styles.
    .md    Zotero "Create Note from Annotations" → Export Note (Markdown)
           No color data — all annotations default to yellow.
    .csv   Zotero "Export Items" CSV or Better BibTeX CSV
    .json  Zotero RDF JSON export, or pipeline-native annotations.json
"""

import argparse
import csv
import html
import io
import json
import re
import sys
import yaml
from pathlib import Path

from zsg import PROJECT_ROOT

try:
    from bs4 import BeautifulSoup
    _HAS_BS4 = True
except ImportError:
    BeautifulSoup = None
    _HAS_BS4 = False


class EmptyExportError(ValueError):
    """Raised when an export file parses cleanly but yields no annotations."""


COLOR_CONFIG_PATH = PROJECT_ROOT / "color_config.yaml"

ZOTERO_COLOR_MAP = {
    "#ffd400": "yellow",
    "#ff6666": "red",
    "#5fb236": "green",
    "#2ea8e5": "blue",
    "#a28ae5": "purple",
    "#f19837": "orange",
    "#aaaaaa": "gray",
    "#e56eee": "pink",
    # Shades Zotero actually emits in HTML note exports (rgba with 0.5 alpha
    # blended toward a different base than the palette swatches above).
    "#facd5a": "yellow",
    "#7cc868": "green",
    "#c885da": "purple",
    "#fb5c89": "pink",
    "#ec2814": "red",
    "#f9b500": "orange",
    "#5ba0d0": "blue",
}


def load_color_config():
    with open(COLOR_CONFIG_PATH) as f:
        return yaml.safe_load(f)["colors"]

def _rgba_to_hex(raw: str):
    """Convert 'rgba(r, g, b, ...)' to '#rrggbb', or return None if not an rgba string."""
    m = re.match(r"rgba?\((\d+),\s*(\d+),\s*(\d+)", raw)
    if not m:
        return None
    return "#{:02x}{:02x}{:02x}".format(int(m.group(1)), int(m.group(2)), int(m.group(3)))


def normalize_color(raw_color) -> str:
    """Map a Zotero hex color, rgba string, or color name to a color name.

    Accepts None / empty / non-string inputs by defaulting to "yellow" — this
    keeps the pipeline-native JSON parser from crashing on `"color": null`.
    """
    if not raw_color or not isinstance(raw_color, str):
        return "yellow"
    raw = raw_color.strip().lower()
    if raw in ZOTERO_COLOR_MAP:
        return ZOTERO_COLOR_MAP[raw]
    if raw in {"yellow", "red", "green", "blue", "purple", "orange", "gray", "pink"}:
        return raw
    # rgba(r, g, b, a) — convert to hex and look up in the palette
    hex_val = _rgba_to_hex(raw)
    if hex_val is not None:
        color = ZOTERO_COLOR_MAP.get(hex_val)
        if color:
            return color
        import sys
        print(f"Warning: unrecognized rgba color {raw_color!r} (→ {hex_val}), defaulting to yellow", file=sys.stderr)
    return "yellow"


_CITATION_PAGE_RE = re.compile(r",\s*p\.\s*(\S+)")


def _parse_citation(text: str) -> tuple[str, str]:
    """Return (source_document, page) from a 'Lastname, p. N' citation string."""
    text = (text or "").strip()
    if not text:
        return "", ""
    m = _CITATION_PAGE_RE.search(text)
    if m:
        return text[: m.start()].strip(), m.group(1).rstrip(")")
    return text, ""


def _annotation_from_parts(
    counter: int,
    highlighted_text: str,
    raw_color: str,
    citation_raw: str,
    instructor_note: str,
) -> dict:
    source_document, page = _parse_citation(citation_raw)
    return {
        "id": f"ann_{counter:03d}",
        "text": highlighted_text.strip(),
        "color": normalize_color(raw_color),
        "page": int(page) if str(page).isdigit() else page,
        "instructor_note": html.unescape((instructor_note or "").strip()),
        "source_document": source_document,
    }


def _from_zotero_html_bs4(content: str) -> list[dict]:
    """Parse Zotero HTML export with BeautifulSoup."""
    soup = BeautifulSoup(content, "html.parser")
    annotations: list[dict] = []
    counter = 1

    for para in soup.find_all("p"):
        # Find the highlight/underline span carrying the color
        color_el = para.find(
            lambda t: t.name in ("span", "u")
            and t.get("style")
            and ("background-color" in t["style"] or "text-decoration-color" in t["style"])
        )
        if color_el is None:
            continue

        style = color_el["style"]
        m = re.search(r"(?:background-color|text-decoration-color):\s*(rgba?\([^)]+\)|#[0-9a-fA-F]+|\w+)", style)
        if not m:
            continue
        raw_color = m.group(1)
        highlighted_text = color_el.get_text(" ", strip=True)
        if not highlighted_text:
            continue

        # Citation: <span class="citation-item">Author, p. N</span>
        citation_el = para.find("span", class_="citation-item")
        citation_raw = citation_el.get_text(" ", strip=True) if citation_el else ""

        # Instructor note: text in the paragraph after the citation block
        instructor_note = ""
        citation_block = para.find("span", class_="citation")
        if citation_block is not None:
            tail_parts = []
            for sib in citation_block.next_siblings:
                if hasattr(sib, "get_text"):
                    tail_parts.append(sib.get_text(" ", strip=True))
                else:
                    tail_parts.append(str(sib).strip())
            instructor_note = " ".join(p for p in tail_parts if p).strip()
        else:
            # No citation block — take everything after the color element
            tail_parts = []
            for sib in color_el.next_siblings:
                if hasattr(sib, "get_text"):
                    tail_parts.append(sib.get_text(" ", strip=True))
                else:
                    tail_parts.append(str(sib).strip())
            instructor_note = " ".join(p for p in tail_parts if p).strip()

        annotations.append(_annotation_from_parts(
            counter, highlighted_text, raw_color, citation_raw, instructor_note,
        ))
        counter += 1

    return annotations


def _from_zotero_html_regex(content: str) -> list[dict]:
    """Legacy regex parser — fallback when bs4 is unavailable."""
    paragraphs = re.findall(r"<p\b[^>]*>(.*?)</p>", content, re.DOTALL)
    annotations: list[dict] = []
    counter = 1

    for para in paragraphs:
        highlight_match = re.search(
            r'background-color:\s*(rgba?\([^)]+\))[^>]*>(.*?)</span>',
            para, re.DOTALL,
        )
        underline_match = re.search(
            r'text-decoration-color:\s*(rgba?\([^)]+\))[^>]*>(.*?)</u>',
            para, re.DOTALL,
        )

        if highlight_match:
            raw_color = highlight_match.group(1)
            highlighted_text = re.sub(r"<[^>]+>", "", highlight_match.group(2)).strip()
        elif underline_match:
            raw_color = underline_match.group(1)
            highlighted_text = re.sub(r"<[^>]+>", "", underline_match.group(2)).strip()
        else:
            continue

        if not highlighted_text:
            continue

        citation_match = re.search(r'<span class="citation-item">(.*?)</span>', para)
        citation_raw = citation_match.group(1).strip() if citation_match else ""

        after_citation = re.sub(r"^.*?</span>\s*", "", para, count=1, flags=re.DOTALL)
        instructor_note = re.sub(r"<[^>]+>", "", after_citation).strip()

        annotations.append(_annotation_from_parts(
            counter, highlighted_text, raw_color, citation_raw, instructor_note,
        ))
        counter += 1

    return annotations


def from_zotero_html_str(content: str) -> list[dict]:
    """
    Pure-function variant: parse a Zotero HTML export from a string.

    Prefers BeautifulSoup when installed; falls back to a legacy regex parser.
    Raises EmptyExportError if the input is non-trivial (>1 KB) but no
    annotations were found — usually means it isn't a Zotero export.
    """
    if not content or not content.strip():
        return []

    if _HAS_BS4:
        annotations = _from_zotero_html_bs4(content)
    else:
        annotations = _from_zotero_html_regex(content)

    if not annotations and len(content) > 1024:
        raise EmptyExportError(
            "File parsed cleanly but contained no Zotero annotations. "
            "Make sure this is a 'Create Note from Annotations' HTML export."
        )

    return annotations


def from_zotero_html(path: Path) -> list[dict]:
    """
    Parse a Zotero "Create Note from Annotations" HTML export from disk.

    Each annotation is a <p> containing:
      - <span class="highlight"><span style="background-color: rgba(...);">TEXT</span></span>
      - <span class="citation">(<span class="citation-item">Author, p. N</span>)</span>
      - Optional trailing text = instructor note
    """
    return from_zotero_html_str(path.read_text(encoding="utf-8"))


def from_zotero_markdown(path: Path) -> list[dict]:
    """Parse a Zotero Markdown note export from disk."""
    return from_zotero_markdown_str(path.read_text(encoding="utf-8"))


def from_zotero_markdown_str(content: str) -> list[dict]:
    """
    Parse Zotero's Markdown note export (File → Export Note → Markdown).

    Each annotation is a paragraph like:
      "highlighted text" ([Author, p. N](zotero://...)) ([pdf](...)) optional note text

    Color information is NOT present in the Markdown format — all annotations
    default to "yellow". To get color data, use the HTML export instead.
    """
    annotations = []
    counter = 1

    # Annotations are blank-line-separated paragraphs; join soft-wrapped lines
    paragraphs = re.split(r"\n{2,}", content)
    for para in paragraphs:
        line = " ".join(para.splitlines()).strip()
        if not line or line.startswith("#") or line.startswith("("):
            continue

        # Normalise curly/smart quotes to straight ASCII so regex is quote-agnostic
        line_norm = line.replace("“", '"').replace("”", '"')

        # Extract quoted highlighted text
        text_match = re.match(r'^"([^"]+)"', line_norm)
        if not text_match:
            continue
        highlighted_text = text_match.group(1).strip()

        # Extract citation: ([Author, p. N](zotero://...))
        citation_match = re.search(r'\(\[([^\]]+)\]\(zotero://[^)]+\)\)', line_norm)
        author_page = citation_match.group(1) if citation_match else ""

        # Parse page from "Author, p. N"
        page = ""
        source_document = ""
        if author_page:
            page_match = re.search(r",\s*p\.\s*(\S+)", author_page)
            if page_match:
                page = page_match.group(1).rstrip(")")
                source_document = author_page[: page_match.start()].strip()
            else:
                source_document = author_page

        # Instructor note = text after the last Markdown link group
        after_links = re.sub(r'\[[^\]]*\]\([^\)]*\)', "", line_norm)
        after_links = re.sub(r'\([^\)]*\)', "", after_links)
        after_links = re.sub(r'^"[^"]+"', "", after_links).strip()
        instructor_note = after_links.strip()

        annotations.append({
            "id": f"ann_{counter:03d}",
            "text": highlighted_text,
            "color": "yellow",  # Markdown export has no color data
            "page": int(page) if str(page).isdigit() else page,
            "instructor_note": instructor_note,
            "source_document": source_document,
        })
        counter += 1

    return annotations


def from_zotero_csv(path: Path) -> list[dict]:
    """Parse a CSV exported from Zotero from disk."""
    with open(path, newline="", encoding="utf-8") as f:
        return _from_zotero_csv_reader(csv.DictReader(f))


def from_zotero_csv_str(content: str) -> list[dict]:
    """Parse a CSV exported from Zotero from an in-memory string."""
    return _from_zotero_csv_reader(csv.DictReader(io.StringIO(content)))


def _from_zotero_csv_reader(reader) -> list[dict]:
    """
    Shared CSV parser. Expected columns (Zotero Better BibTeX / Zotero RDF CSV):
        Annotation Text, Color, Page, Comment, Source Title, Author

    Column names are flexible — common variants are tried.
    """
    annotations = []

    def pick(row, *candidates):
        for c in candidates:
            for h in row:
                if h.strip().lower() == c.lower():
                    return row[h].strip()
        return ""

    for i, row in enumerate(reader):
        text = pick(row, "annotation text", "text", "highlighted text", "quote")
        color_raw = pick(row, "color", "highlight color", "annotation color")
        page = pick(row, "page", "page number", "location")
        note = pick(row, "comment", "note", "annotation comment", "instructor note")
        source = pick(row, "source title", "title", "document", "publication title")
        author = pick(row, "author", "authors", "creator")

        if not text:
            continue

        annotations.append({
            "id": f"ann_{i+1:03d}",
            "text": text,
            "color": normalize_color(color_raw) if color_raw else "yellow",
            "page": int(page) if page.isdigit() else page,
            "instructor_note": note,
            "source_document": f"{author} — {source}".strip(" —") if (author or source) else "",
        })

    return annotations


def from_zotero_json(path: Path) -> list[dict]:
    """Parse a JSON export from disk."""
    with open(path, encoding="utf-8") as f:
        return from_zotero_json_data(json.load(f))


def from_zotero_json_str(content: str) -> list[dict]:
    """Parse a JSON export from an in-memory string."""
    return from_zotero_json_data(json.loads(content))


def from_zotero_json_data(data) -> list[dict]:
    """
    Parse a JSON export (already-decoded).

    Accepts two formats:
      1. Zotero RDF/JSON export (array of items with 'annotations' key)
      2. Pipeline-native format (array of annotation objects) — passed through as-is
         after normalizing color fields.
    """
    annotations = []

    # Native pipeline format: top-level list of objects with 'id' and 'text'
    if isinstance(data, list) and data and "text" in data[0]:
        for i, ann in enumerate(data):
            ann.setdefault("id", f"ann_{i+1:03d}")
            ann["color"] = normalize_color(ann.get("color", "yellow"))
            annotations.append(ann)
        return annotations

    # Zotero JSON export format: list of library items each with an 'annotations' list
    if isinstance(data, list):
        counter = 1
        for item in data:
            source = item.get("title", item.get("shortTitle", ""))
            author = ""
            creators = item.get("creators", [])
            if creators:
                c = creators[0]
                author = c.get("lastName", c.get("name", ""))

            for ann in item.get("annotations", []):
                annotations.append({
                    "id": f"ann_{counter:03d}",
                    "text": ann.get("annotationText", ann.get("text", "")),
                    "color": normalize_color(ann.get("annotationColor", ann.get("color", "yellow"))),
                    "page": ann.get("annotationPageLabel", ann.get("page", "")),
                    "instructor_note": ann.get("annotationComment", ann.get("comment", "")),
                    "source_document": f"{author} — {source}".strip(" —"),
                })
                counter += 1

    return annotations


def parse_export_str(fmt: str, content: str) -> list[dict]:
    """
    Dispatch to the right parser based on a format hint.

    fmt: one of 'html', 'md', 'csv', 'json'.
    Raises ValueError for an unknown format and EmptyExportError when an HTML
    file parses cleanly but yielded no annotations.
    """
    fmt = (fmt or "").lower().lstrip(".")
    if fmt in ("html", "htm"):
        return from_zotero_html_str(content)
    if fmt in ("md", "markdown"):
        return from_zotero_markdown_str(content)
    if fmt == "csv":
        return from_zotero_csv_str(content)
    if fmt == "json":
        return from_zotero_json_str(content)
    raise ValueError(f"Unsupported export format: {fmt!r}")


def demo_annotations() -> list[dict]:
    """Return a small set of demo annotations for testing the pipeline."""
    return [
        {
            "id": "ann_001",
            "text": "After the Civil War, formerly enslaved people sought to exercise their newly won freedom through political participation, land ownership, and education.",
            "color": "blue",
            "page": 42,
            "instructor_note": "Use as section intro",
            "source_document": "Darling — The Quest for Equality",
        },
        {
            "id": "ann_002",
            "text": "The Reconstruction Acts of 1867 divided the South into five military districts and required states to ratify the 14th Amendment before readmission.",
            "color": "red",
            "page": 43,
            "instructor_note": "Good quiz question — specific dates and requirements",
            "source_document": "Darling — The Quest for Equality",
        },
        {
            "id": "ann_003",
            "text": "Forty acres and a mule — the promise of land redistribution that was never fulfilled for most freedpeople.",
            "color": "yellow",
            "page": 44,
            "instructor_note": "",
            "source_document": "Darling — The Quest for Equality",
        },
        {
            "id": "ann_004",
            "text": "The sharecropping system replaced slavery with a new form of economic bondage, trapping Black farmers in cycles of debt.",
            "color": "blue",
            "page": 45,
            "instructor_note": "",
            "source_document": "Darling — The Quest for Equality",
        },
        {
            "id": "ann_005",
            "text": "Booker T. Washington founded the Tuskegee Institute in 1881, emphasizing vocational training and economic self-sufficiency over immediate political equality.",
            "color": "green",
            "page": 47,
            "instructor_note": "Key figure card",
            "source_document": "Darling — The Quest for Equality",
        },
        {
            "id": "ann_006",
            "text": "W.E.B. Du Bois co-founded the NAACP in 1909 and argued for the full and immediate civil and political rights of Black Americans, directly challenging Washington's accommodationist approach.",
            "color": "green",
            "page": 49,
            "instructor_note": "Contrast with Washington",
            "source_document": "Darling — The Quest for Equality",
        },
        {
            "id": "ann_007",
            "text": "The Supreme Court's Plessy v. Ferguson decision (1896) enshrined 'separate but equal' as constitutional doctrine, providing legal cover for Jim Crow laws across the South.",
            "color": "red",
            "page": 52,
            "instructor_note": "Critical case — must quiz on this",
            "source_document": "Darling — The Quest for Equality",
        },
        {
            "id": "ann_008",
            "text": "Jim Crow laws enforced racial segregation in public life — schools, transportation, restaurants, restrooms — throughout the South from the 1870s through the 1960s.",
            "color": "yellow",
            "page": 53,
            "instructor_note": "",
            "source_document": "Darling — The Quest for Equality",
        },
        {
            "id": "ann_009",
            "text": "The NAACP's legal strategy culminated in Brown v. Board of Education (1954), which overturned Plessy and declared school segregation unconstitutional.",
            "color": "purple",
            "page": 78,
            "instructor_note": "Connect back to Plessy",
            "source_document": "Darling — The Quest for Equality",
        },
        {
            "id": "ann_010",
            "text": "Montgomery Bus Boycott (1955–1956): sparked by Rosa Parks's arrest, organized by the Montgomery Improvement Association under MLK, lasted 381 days.",
            "color": "orange",
            "page": 82,
            "instructor_note": "Good example of nonviolent direct action",
            "source_document": "Darling — The Quest for Equality",
        },
    ]


def main():
    parser = argparse.ArgumentParser(description="Export Zotero annotations to pipeline format.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--input", "-i", help="Path to Zotero export file (.csv or .json)")
    group.add_argument("--demo", action="store_true", help="Use built-in demo annotations")
    parser.add_argument("--output", "-o", required=True, help="Output path for annotations.json")
    args = parser.parse_args()

    if args.demo:
        annotations = demo_annotations()
        print(f"Loaded {len(annotations)} demo annotations.")
    else:
        input_path = Path(args.input)
        if not input_path.exists():
            print(f"Error: input file not found: {input_path}", file=sys.stderr)
            sys.exit(1)

        suffix = input_path.suffix.lower()
        if suffix in (".html", ".htm"):
            annotations = from_zotero_html(input_path)
        elif suffix == ".md":
            annotations = from_zotero_markdown(input_path)
            print("Note: Markdown exports have no color data — all annotations default to yellow.")
            print("For color information, use the HTML export format instead.")
        elif suffix == ".csv":
            annotations = from_zotero_csv(input_path)
        elif suffix == ".json":
            annotations = from_zotero_json(input_path)
        else:
            print(f"Error: unsupported file type '{suffix}'. Use .html, .md, .csv, or .json.", file=sys.stderr)
            sys.exit(1)

        print(f"Loaded {len(annotations)} annotations from {input_path.name}.")

    if not annotations:
        print("Warning: no annotations found. Check your export file.", file=sys.stderr)

    color_config = load_color_config()
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    output = {
        "color_config": color_config,
        "annotations": annotations,
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"Written to {output_path}")
    color_counts = {}
    for a in annotations:
        color_counts[a["color"]] = color_counts.get(a["color"], 0) + 1
    for color, count in sorted(color_counts.items()):
        label = color_config.get(color, {}).get("label", color)
        print(f"  {color:8s} ({label}): {count}")


if __name__ == "__main__":
    main()
