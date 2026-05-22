"""Extract plain text + tables + image positions from a docx file.

Used to recover the user-edited PRD.docx as readable markdown so we can diff
it against the repo's PRD.md and apply additional changes on top of the user's
edits (without losing them).

Why we wrote this instead of using pandoc:
- pandoc isn't guaranteed to be installed on the user's Windows box
- We already depend on python-docx for build_prd_docx.py, so reusing it is
  zero new dependency.

Output is intentionally lossy on the image side — we just emit a placeholder
`[image: <fileName>]` line. The point is *text recovery* for diff, not docx
round-tripping.
"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    from docx import Document
    from docx.oxml.ns import qn
except ImportError as e:  # pragma: no cover
    print("ERROR: python-docx not installed. Run: pip install python-docx", file=sys.stderr)
    sys.exit(1)


def iter_block_items(parent):
    """Yield paragraphs and tables in document order (the SDK separates them)."""
    from docx.document import Document as _Document
    from docx.oxml.table import CT_Tbl
    from docx.oxml.text.paragraph import CT_P
    from docx.table import _Cell, Table
    from docx.text.paragraph import Paragraph

    if isinstance(parent, _Document):
        parent_elm = parent.element.body
    elif isinstance(parent, _Cell):
        parent_elm = parent._tc
    else:
        raise ValueError(parent)

    for child in parent_elm.iterchildren():
        if isinstance(child, CT_P):
            yield Paragraph(child, parent)
        elif isinstance(child, CT_Tbl):
            yield Table(child, parent)


def render_paragraph(p) -> str:
    text = p.text or ""
    style = (p.style.name if p.style else "").lower()
    # Detect heading levels by style name: "Heading 1", "Heading 2", ...
    if style.startswith("heading"):
        try:
            level = int(style.replace("heading", "").strip() or "1")
        except ValueError:
            level = 1
        return f"{'#' * max(1, level)} {text}"
    # Detect images embedded in this paragraph (we just emit a marker)
    images = []
    for r in p.runs:
        for drawing in r._element.findall(qn("w:drawing")):
            blip = drawing.find(".//" + qn("a:blip"))
            if blip is not None:
                rid = blip.get(qn("r:embed"))
                images.append(rid or "img")
    if images and not text.strip():
        return "[image: " + ", ".join(images) + "]"
    if images:
        return text + "  " + " ".join(f"[image: {i}]" for i in images)
    return text


def render_table(t) -> str:
    """Render a docx table back to a github-flavored markdown table."""
    rows = []
    for row in t.rows:
        cells = [c.text.replace("\n", " ").strip() or " " for c in row.cells]
        rows.append("| " + " | ".join(cells) + " |")
    if not rows:
        return ""
    out = [rows[0], "|" + "|".join("---" for _ in t.rows[0].cells) + "|"]
    out.extend(rows[1:])
    return "\n".join(out)


def main(in_path: Path, out_path: Path) -> None:
    doc = Document(str(in_path))
    out_lines = []
    for block in iter_block_items(doc):
        if hasattr(block, "rows"):
            md = render_table(block)
        else:
            md = render_paragraph(block)
        if md:
            out_lines.append(md)
        else:
            out_lines.append("")
    out_path.write_text("\n".join(out_lines), encoding="utf-8")
    print(f"OK -> {out_path} ({out_path.stat().st_size} bytes, {len(out_lines)} lines)")


if __name__ == "__main__":
    here = Path(__file__).resolve().parent.parent
    in_file = here / "docs" / "PRD.docx"
    out_file = here / "docs" / "PRD-from-docx.md"
    if not in_file.exists():
        print(f"ERROR: {in_file} not found", file=sys.stderr)
        sys.exit(2)
    main(in_file, out_file)
