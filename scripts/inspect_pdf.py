"""scripts/inspect_pdf.py
Dumps per-page text spans with font name, size, bold flag, bbox,
plus images/drawings and detected tables for both v1 and v2 PDFs.
"""

import json
import logging
import sys
from pathlib import Path

try:
    import fitz  # PyMuPDF
except ImportError:
    print("ERROR: PyMuPDF not installed. Run: pip install PyMuPDF", file=sys.stderr)
    sys.exit(1)

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def is_bold(font_name: str, flags: int) -> bool:
    """Determine if a font is bold from name or flags."""
    # flags bit 0x10 = bold in fitz
    name_lower = font_name.lower()
    return bool(flags & (1 << 4)) or "bold" in name_lower or "heavy" in name_lower


def inspect_page(page: fitz.Page) -> dict:
    """Extract detailed info from a single page."""
    page_data = {
        "page_number": page.number + 1,
        "width": page.rect.width,
        "height": page.rect.height,
        "spans": [],
        "images": [],
        "drawings": [],
        "tables_detected": False,
    }

    # Extract text spans with full detail
    blocks = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)["blocks"]
    for block in blocks:
        if block["type"] == 0:  # text block
            for line in block["lines"]:
                for span in line["spans"]:
                    span_info = {
                        "text": span["text"],
                        "font": span["font"],
                        "size": round(span["size"], 2),
                        "bold": is_bold(span["font"], span["flags"]),
                        "flags": span["flags"],
                        "color": span["color"],
                        "bbox": [round(x, 2) for x in span["bbox"]],
                        "block_bbox": [round(x, 2) for x in block["bbox"]],
                        "origin": [round(x, 2) for x in span["origin"]],
                    }
                    page_data["spans"].append(span_info)
        elif block["type"] == 1:  # image block
            page_data["images"].append(
                {
                    "bbox": [round(x, 2) for x in block["bbox"]],
                    "width": block.get("width", 0),
                    "height": block.get("height", 0),
                }
            )

    # Check for drawings (vector graphics)
    drawings = page.get_drawings()
    if drawings:
        page_data["drawings"] = [
            {
                "rect": [round(x, 2) for x in d["rect"]],
                "type": d.get("type", "unknown"),
            }
            for d in drawings[:20]  # limit
        ]

    # Detect potential tables by looking for grid-like line patterns
    lines_h = []
    lines_v = []
    for d in drawings:
        for item in d.get("items", []):
            if item[0] == "l":  # line
                p1, p2 = item[1], item[2]
                if abs(p1.y - p2.y) < 2:  # horizontal
                    lines_h.append((p1, p2))
                elif abs(p1.x - p2.x) < 2:  # vertical
                    lines_v.append((p1, p2))
    if len(lines_h) >= 3 and len(lines_v) >= 2:
        page_data["tables_detected"] = True

    return page_data


def render_page_to_png(page: fitz.Page, output_dir: Path, pdf_name: str) -> str:
    """Render a page to PNG at 150 dpi for visual inspection."""
    pix = page.get_pixmap(dpi=150)
    out_path = output_dir / f"{pdf_name}_page_{page.number + 1}.png"
    pix.save(str(out_path))
    return str(out_path)


def inspect_pdf(pdf_path: str, output_dir: Path) -> dict:
    """Full inspection of a PDF."""
    doc = fitz.open(pdf_path)
    pdf_name = Path(pdf_path).stem

    result = {
        "file": pdf_path,
        "page_count": len(doc),
        "pages": [],
        "summary": {
            "total_spans": 0,
            "unique_fonts": set(),
            "unique_sizes": set(),
            "total_images": 0,
            "pages_with_tables": [],
            "pages_with_drawings": [],
            "bold_spans": 0,
        },
    }

    png_dir = output_dir / "pages"
    png_dir.mkdir(parents=True, exist_ok=True)

    for page in doc:
        page_data = inspect_page(page)
        result["pages"].append(page_data)

        # Update summary
        result["summary"]["total_spans"] += len(page_data["spans"])
        result["summary"]["total_images"] += len(page_data["images"])
        for s in page_data["spans"]:
            result["summary"]["unique_fonts"].add(s["font"])
            result["summary"]["unique_sizes"].add(s["size"])
            if s["bold"]:
                result["summary"]["bold_spans"] += 1
        if page_data["tables_detected"]:
            result["summary"]["pages_with_tables"].append(page_data["page_number"])
        if page_data["drawings"]:
            result["summary"]["pages_with_drawings"].append(page_data["page_number"])

        # Render to PNG
        render_page_to_png(page, png_dir, pdf_name)

    # Convert sets to sorted lists for JSON serialization
    result["summary"]["unique_fonts"] = sorted(result["summary"]["unique_fonts"])
    result["summary"]["unique_sizes"] = sorted(result["summary"]["unique_sizes"])

    doc.close()
    return result


def print_heading_candidates(result: dict) -> None:
    """Print spans that might be headings based on font properties."""
    logger.info("=== HEADING CANDIDATES ===")
    seen = set()
    for page in result["pages"]:
        for span in page["spans"]:
            text = span["text"].strip()
            if not text:
                continue
            key = (span["font"], span["size"], span["bold"])
            if key not in seen:
                seen.add(key)
                logger.info(f"  Font={span['font']}, Size={span['size']}, Bold={span['bold']} | Sample: {text[:80]!r}")


def print_font_size_distribution(result: dict) -> None:
    """Print font/size distribution to understand document structure."""
    from collections import Counter

    dist: Counter = Counter()
    for page in result["pages"]:
        for span in page["spans"]:
            text = span["text"].strip()
            if text:
                dist[(span["font"], span["size"], span["bold"])] += 1

    logger.info("=== FONT/SIZE DISTRIBUTION ===")
    for (font, size, bold), count in dist.most_common():
        logger.info(f"  {count:4d}x  Font={font}, Size={size}, Bold={bold}")


def main() -> None:
    project_root = Path(__file__).parent.parent
    data_dir = project_root / "data"
    output_dir = project_root / "docs" / "inspection"
    output_dir.mkdir(parents=True, exist_ok=True)

    pdfs = [
        data_dir / "ct200_manual.pdf",
        data_dir / "ct200_manual_v2.pdf",
    ]

    for pdf_path in pdfs:
        if not pdf_path.exists():
            logger.error(f"PDF not found: {pdf_path}")
            continue

        logger.info(f"\n{'=' * 60}")
        logger.info(f"INSPECTING: {pdf_path.name}")
        logger.info(f"{'=' * 60}")

        result = inspect_pdf(str(pdf_path), output_dir)

        # Print summary
        s = result["summary"]
        logger.info(f"Pages: {result['page_count']}")
        logger.info(f"Total spans: {s['total_spans']}")
        logger.info(f"Bold spans: {s['bold_spans']}")
        logger.info(f"Unique fonts: {s['unique_fonts']}")
        logger.info(f"Unique sizes: {s['unique_sizes']}")
        logger.info(f"Total images: {s['total_images']}")
        logger.info(f"Pages with tables: {s['pages_with_tables']}")
        logger.info(f"Pages with drawings: {s['pages_with_drawings']}")

        print_font_size_distribution(result)
        print_heading_candidates(result)

        # Save full JSON
        json_path = output_dir / f"{pdf_path.stem}_inspection.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        logger.info(f"Full inspection saved to: {json_path}")

        # Print all spans per page for detailed analysis
        detail_path = output_dir / f"{pdf_path.stem}_spans.txt"
        with open(detail_path, "w", encoding="utf-8") as f:
            for page in result["pages"]:
                f.write(f"\n--- PAGE {page['page_number']} ---\n")
                for span in page["spans"]:
                    text = span["text"]
                    f.write(
                        f"  [{span['font']} {span['size']} "
                        f"{'B' if span['bold'] else ' '} "
                        f"bbox={span['bbox']}] "
                        f"{text!r}\n"
                    )
        logger.info(f"Span details saved to: {detail_path}")


if __name__ == "__main__":
    main()
