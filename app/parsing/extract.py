"""app/parsing/extract.py
Low-level PDF text extraction using PyMuPDF.
Produces a list of SpanInfo objects per page, preserving font metadata.
"""

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

logger = logging.getLogger(__name__)


@dataclass
class SpanInfo:
    """A single text span with full font metadata."""

    text: str
    font: str
    size: float
    bold: bool
    flags: int
    color: int
    bbox: tuple[float, float, float, float]  # x0, y0, x1, y1
    origin: tuple[float, float]
    page_number: int  # 1-indexed
    block_index: int
    line_index: int
    span_index: int

    @property
    def is_heading_font(self) -> bool:
        """Check if this span uses a heading font style."""
        return self.bold and "Bold" in self.font

    @property
    def x0(self) -> float:
        return self.bbox[0]

    @property
    def y0(self) -> float:
        return self.bbox[1]

    @property
    def x1(self) -> float:
        return self.bbox[2]

    @property
    def y1(self) -> float:
        return self.bbox[3]


@dataclass
class PageSpans:
    """All spans from a single page."""

    page_number: int
    spans: list[SpanInfo] = field(default_factory=list)
    width: float = 0.0
    height: float = 0.0


def _is_bold(font_name: str, flags: int) -> bool:
    """Determine if font is bold from name or flags."""
    name_lower = font_name.lower()
    return bool(flags & (1 << 4)) or "bold" in name_lower or "heavy" in name_lower


def extract_spans(pdf_path: str | Path) -> list[PageSpans]:
    """Extract all text spans from a PDF, preserving font metadata.

    Returns a list of PageSpans, one per page in document order.
    """
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    doc = pymupdf.open(str(pdf_path))
    pages: list[PageSpans] = []

    for page_idx, page in enumerate(doc):
        page_spans = PageSpans(
            page_number=page_idx + 1,
            width=page.rect.width,
            height=page.rect.height,
        )

        blocks = page.get_text("dict", flags=pymupdf.TEXT_PRESERVE_WHITESPACE)["blocks"]

        for block_idx, block in enumerate(blocks):
            if block["type"] != 0:  # skip image blocks
                continue

            for line_idx, line in enumerate(block["lines"]):
                for span_idx, span in enumerate(line["spans"]):
                    text = span["text"]
                    if not text:
                        continue

                    span_info = SpanInfo(
                        text=text,
                        font=span["font"],
                        size=round(span["size"], 2),
                        bold=_is_bold(span["font"], span["flags"]),
                        flags=span["flags"],
                        color=span["color"],
                        bbox=tuple(round(x, 2) for x in span["bbox"]),
                        origin=tuple(round(x, 2) for x in span["origin"]),
                        page_number=page_idx + 1,
                        block_index=block_idx,
                        line_index=line_idx,
                        span_index=span_idx,
                    )
                    page_spans.spans.append(span_info)

        pages.append(page_spans)

    doc.close()
    logger.info(
        "Extracted %d pages, %d total spans from %s",
        len(pages),
        sum(len(p.spans) for p in pages),
        pdf_path.name,
    )
    return pages


def compute_pdf_sha256(pdf_path: str | Path) -> str:
    """Compute SHA-256 hash of a PDF file."""
    pdf_path = Path(pdf_path)
    sha256 = hashlib.sha256()
    with open(pdf_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha256.update(chunk)
    return sha256.hexdigest()
