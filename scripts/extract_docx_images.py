"""Extract embedded images from PRD.docx into docs/screenshots/.

Why: the user-edited docx has 30 inline screenshots but the source-of-truth
PRD.md does not reference them. Round-tripping the docx through markdown
would lose the images. This script pulls them out so:
  1. Markdown can reference them with a stable path
  2. build_prd_docx.py can re-embed them mechanically on every build
  3. They become version-controllable assets

Output naming:
  - s-01.png, s-02.png, ... s-NN.<ext>  (numbered by document-order appearance)
  - The first text paragraph BEFORE each image is captured into mapping.json
    so we can later wire each placeholder to the correct caption / chapter.

Why we don't reuse python-docx for the binary extraction:
  python-docx exposes images via the relationship table (`part.related_parts`),
  but it doesn't iterate them in *document order* — and order is what we
  need for sane numbering. Walking the body XML ourselves is the cleanest
  way to preserve order.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

try:
    from docx import Document
    from docx.oxml.ns import qn
except ImportError:
    print("ERROR: python-docx not installed. Run: pip install python-docx", file=sys.stderr)
    sys.exit(1)


# --- Constants (no magic numbers) -------------------------------------------
NAMESPACE_DRAWING = qn("w:drawing")
NAMESPACE_BLIP = ".//" + qn("a:blip")
ATTR_EMBED = qn("r:embed")


def iter_block_items(parent):
    """Iterate paragraphs in document order (we don't care about tables here)."""
    from docx.document import Document as _Document
    from docx.oxml.text.paragraph import CT_P
    from docx.text.paragraph import Paragraph

    parent_elm = parent.element.body if isinstance(parent, _Document) else parent._tc
    for child in parent_elm.iterchildren():
        if isinstance(child, CT_P):
            yield Paragraph(child, parent)


def extract_images_from_para(p):
    """Return list of (rId, runIndex) tuples for images embedded in this para."""
    found = []
    for r_idx, r in enumerate(p.runs):
        for drawing in r._element.findall(NAMESPACE_DRAWING):
            blip = drawing.find(NAMESPACE_BLIP)
            if blip is not None:
                rid = blip.get(ATTR_EMBED)
                if rid:
                    found.append((rid, r_idx))
    return found


def main(in_path: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    doc = Document(str(in_path))
    main_part = doc.part

    # --- Walk paragraphs, remember the most recent non-image text paragraph ---
    mapping = []  # one record per image: {seq, file, rid, near_text, chapter}
    last_text = ""
    last_chapter = ""
    seq = 0

    for p in iter_block_items(doc):
        text = (p.text or "").strip()
        # Track chapter heading using markdown-ish heuristics on the docx text:
        # any paragraph that starts with "##" / "###" or with a top-level
        # number like "一、" / "二、" or "1.1 / 2.3" we treat as a chapter mark.
        if text and (
            text.startswith("##") or text.startswith("###")
            or any(text.startswith(p) for p in ("一、", "二、", "三、", "四、", "五、", "六、", "七、", "八、"))
        ):
            last_chapter = text

        images = extract_images_from_para(p)
        if not images:
            if text:
                last_text = text
            continue

        for rid, _ in images:
            seq += 1
            try:
                image_part = main_part.related_parts[rid]
            except KeyError:
                print(f"WARN: rId={rid} not found in relationships", file=sys.stderr)
                continue

            ext = (image_part.partname.rpartition(".")[-1] or "png").lower()
            out_name = f"s-{seq:02d}.{ext}"
            (out_dir / out_name).write_bytes(image_part.blob)

            mapping.append({
                "seq": seq,
                "file": out_name,
                "rid": rid,
                "chapter": last_chapter,
                "near_text": last_text or text or "",
                "size_bytes": len(image_part.blob),
            })
            # Avoid console-encoding issues on Windows (GBK) by printing without fancy quotes.
            try:
                print(f"  s-{seq:02d}  ({len(image_part.blob)//1024:>4} KB)  chapter={last_chapter[:40]}  near={(last_text or text or '')[:50]}")
            except UnicodeEncodeError:
                print(f"  s-{seq:02d}  ({len(image_part.blob)//1024:>4} KB)")

        # If a paragraph had both text and images, the text is what comes
        # AFTER the image semantically — but our heuristic uses last_text
        # (text BEFORE), so don't overwrite last_text with this paragraph's
        # text unless it's image-only.
        if text and not images:
            last_text = text

    # --- Save mapping for the markdown author (us) and build script ---
    mapping_path = out_dir / "mapping.json"
    mapping_path.write_text(
        json.dumps(mapping, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\nOK -> {seq} images extracted to {out_dir}")
    print(f"     mapping saved to {mapping_path}")


if __name__ == "__main__":
    here = Path(__file__).resolve().parent.parent
    main(
        in_path=here / "docs" / "PRD.docx",
        out_dir=here / "docs" / "screenshots",
    )
