"""app/parsing/headings.py
Heading detection from PDF spans based on font properties.

Rules derived from inspect_pdf.py analysis:
- Title: NimbusSans-Bold, size 22.0 (multi-line, merged into single node)
- Level 1 headings (1-8): NimbusSans-Bold, size 16.5
- Level 2 headings (x.y): NimbusSans-Bold, size 12.87
- Level 3+ headings (2.1.1.1, 3.2): NimbusSans-Bold, size 11.0
  BUT must distinguish from table headers (also Bold 11.0)
- Body text: NimbusSans-Regular, size 11.0
- Non-breaking hyphens: NotoSerif-Regular, size 11.0
- Numbered list items: NimbusSans-Regular, size 11.0, left indent (x0 > 40)

Key irregularities handled:
- 2.1.1.1 appears under 2.1 with no 2.1.1 (level gap)
- 3.4 appears before 3.3 (out of order)
- "Error Codes" appears twice (4.2 and 7.1)
- Numbered list (1. Normal, ...) must not become headings
- Table headers (Parameter, Value, Code, Meaning, Device Behavior) must not become headings
"""

import logging
import re
from dataclasses import dataclass, field

from app.parsing.extract import PageSpans, SpanInfo

logger = logging.getLogger(__name__)

# Heading number pattern: "1." or "2.1" or "2.1.1.1"
HEADING_NUMBER_PATTERN = re.compile(r"^(\d+(?:\.\d+)*)\.?\s+(.+)$")

# Table header keywords (Bold 11.0, positioned in table columns)
TABLE_HEADER_KEYWORDS = {
    "parameter",
    "value",
    "code",
    "meaning",
    "device behavior",
}

# Known numbered list prefixes that should NOT be treated as headings
LIST_ITEM_PATTERN = re.compile(r"^\d+\.\s+(?:Normal|Elevated|Hypertension|Hypertensive)")


@dataclass
class HeadingSpan:
    """A detected heading with its source spans."""

    text: str
    number_path: str  # e.g., "2.1.1.1"
    heading_text: str  # text after the number
    level: int  # heading level based on font size
    page_number: int
    y_position: float  # vertical position on page
    spans: list[SpanInfo] = field(default_factory=list)

    @property
    def is_title(self) -> bool:
        return self.level == 0

    @property
    def numbering_depth(self) -> int:
        """Depth based on the number path (e.g., '2.1.1.1' → 4)."""
        if not self.number_path:
            return 0
        return len(self.number_path.split("."))


def _font_level(span: SpanInfo) -> int | None:
    """Map font properties to heading level.

    Returns None if not a heading font.
    Level 0 = title (22.0)
    Level 1 = top sections (16.5)
    Level 2 = subsections (12.87)
    Level 3 = sub-subsections (11.0 bold)
    """
    if not span.bold:
        return None

    size = span.size
    if abs(size - 22.0) < 0.5:
        return 0  # title
    elif abs(size - 16.5) < 0.5:
        return 1  # top-level heading
    elif abs(size - 12.87) < 0.5:
        return 2  # sub-heading
    elif abs(size - 11.0) < 0.5:
        return 3  # sub-sub-heading (need further classification)
    return None


def _is_table_header(span: SpanInfo) -> bool:
    """Check if a bold 11.0 span is a table header, not a section heading."""
    text = span.text.strip().lower()
    if text in TABLE_HEADER_KEYWORDS:
        return True
    # Table headers are typically indented (x0 > 35)
    # and within a table region (check column alignment)
    return False


def _is_list_item(text: str) -> bool:
    """Check if text is a numbered list item, not a heading."""
    return bool(LIST_ITEM_PATTERN.match(text.strip()))


def _merge_title_spans(spans: list[SpanInfo]) -> str:
    """Merge multi-line title spans into a single text."""
    texts = [s.text.strip() for s in spans if s.text.strip()]
    return " ".join(texts)


def detect_headings(pages: list[PageSpans]) -> list[HeadingSpan]:
    """Detect all headings from extracted page spans.

    Returns headings in document order.
    """
    headings: list[HeadingSpan] = []
    title_spans: list[SpanInfo] = []
    all_spans: list[SpanInfo] = []

    # Flatten all spans in document order
    for page in pages:
        all_spans.extend(page.spans)

    i = 0
    while i < len(all_spans):
        span = all_spans[i]
        level = _font_level(span)

        if level is None:
            i += 1
            continue

        if level == 0:
            # Title span - collect all title-level spans
            title_spans.append(span)
            i += 1
            continue

        if level == 3:
            # Bold 11.0 - could be table header, list item, or actual heading
            text = span.text.strip()

            if _is_table_header(span):
                i += 1
                continue

            # Check if it has a heading number pattern
            match = HEADING_NUMBER_PATTERN.match(text)
            if not match:
                # Bold 11.0 without number - likely table header
                i += 1
                continue

        # This is a real heading
        text = span.text.strip()
        match = HEADING_NUMBER_PATTERN.match(text)

        if match:
            number_path = match.group(1)
            heading_text = match.group(2).strip()
        elif level == 1:
            # Top-level headings should have numbers
            match = HEADING_NUMBER_PATTERN.match(text)
            if match:
                number_path = match.group(1)
                heading_text = match.group(2).strip()
            else:
                number_path = ""
                heading_text = text
        else:
            number_path = ""
            heading_text = text

        heading = HeadingSpan(
            text=text,
            number_path=number_path,
            heading_text=heading_text,
            level=level,
            page_number=span.page_number,
            y_position=span.y0,
            spans=[span],
        )
        headings.append(heading)
        i += 1

    # Process title if found
    if title_spans:
        title_text = _merge_title_spans(title_spans)
        title = HeadingSpan(
            text=title_text,
            number_path="",
            heading_text=title_text,
            level=0,
            page_number=title_spans[0].page_number,
            y_position=title_spans[0].y0,
            spans=title_spans,
        )
        headings.insert(0, title)

    logger.info("Detected %d headings (including title)", len(headings))
    return headings
