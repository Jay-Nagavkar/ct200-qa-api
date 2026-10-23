"""app/parsing/tree.py
Builds a hierarchical document tree from detected headings and body text.

Handles:
- Title as root node
- Section numbering hierarchy (1, 1.1, 2.1.1.1)
- Level gaps (2.1.1.1 under 2.1 with no 2.1.1)
- Out-of-order sections (3.4 before 3.3)
- Table extraction (2.1 specs table, 4.2 error codes table)
- Numbered list detection (not headings)
- Cross-reference extraction
- Non-breaking hyphen normalization
- Page break handling
"""

import hashlib
import logging
import re
from dataclasses import dataclass, field

from app.parsing.extract import PageSpans, SpanInfo
from app.parsing.headings import HeadingSpan, detect_headings
from app.parsing.normalize import (
    extract_cross_refs,
    normalize_for_hash,
    normalize_text,
)

logger = logging.getLogger(__name__)

# Table column boundaries based on inspection
# 2.1 General Specifications: 2 columns
#   col1 (Parameter): x ~ 39.5
#   col2 (Value): x ~ 158.49
SPECS_TABLE_COL_BOUNDARY = 140.0

# 4.2 Error Codes: 3 columns
#   col1 (Code): x ~ 39.5
#   col2 (Meaning): x ~ 79.0
#   col3 (Device Behavior): x ~ 215.73
ERROR_TABLE_COL_BOUNDARIES = [70.0, 200.0]


@dataclass
class TableRow:
    """A single row in a table."""

    cells: list[str]

    def to_dict(self) -> dict:
        return {"cells": self.cells}


@dataclass
class ParsedTable:
    """A table extracted from the document."""

    headers: list[str]
    rows: list[TableRow]

    def to_dict(self) -> dict:
        return {
            "headers": self.headers,
            "rows": [r.to_dict() for r in self.rows],
        }


@dataclass
class ParsedNode:
    """A node in the document tree."""

    heading: str
    number_path: str
    level: int  # tree depth (0=root, 1=top sections, etc.)
    numbering_depth: int  # depth of number path (e.g., "2.1.1.1" = 4)
    document_order: int
    body_text: str  # original text
    normalized_text: str  # normalized for comparison
    tables: list[ParsedTable] = field(default_factory=list)
    lists: list[list[str]] = field(default_factory=list)
    cross_refs: list[str] = field(default_factory=list)
    page_start: int = 0
    page_end: int = 0
    anomalies: list[str] = field(default_factory=list)
    content_hash: str = ""
    parent_number_path: str | None = None
    children_number_paths: list[str] = field(default_factory=list)
    _heading_span: HeadingSpan | None = field(default=None, repr=False)

    def compute_hash(self) -> str:
        """Compute SHA-256 of normalized heading + body + table content ONLY (not children)."""
        hasher = hashlib.sha256()
        hasher.update(normalize_for_hash(self.heading).encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(normalize_for_hash(self.body_text).encode("utf-8"))
        hasher.update(b"\x00")
        # Include table content
        for table in self.tables:
            for row in table.rows:
                for cell in row.cells:
                    hasher.update(normalize_for_hash(cell).encode("utf-8"))
                    hasher.update(b"\x01")
        self.content_hash = hasher.hexdigest()
        return self.content_hash

    def to_dict(self) -> dict:
        return {
            "heading": self.heading,
            "number_path": self.number_path,
            "level": self.level,
            "numbering_depth": self.numbering_depth,
            "document_order": self.document_order,
            "body_text": self.body_text,
            "normalized_text": self.normalized_text,
            "tables": [t.to_dict() for t in self.tables],
            "lists": self.lists,
            "cross_refs": self.cross_refs,
            "page_start": self.page_start,
            "page_end": self.page_end,
            "anomalies": self.anomalies,
            "content_hash": self.content_hash,
            "parent_number_path": self.parent_number_path,
            "children": self.children_number_paths,
        }


def _collect_body_spans(
    all_spans: list[SpanInfo],
    start_idx: int,
    end_idx: int,
) -> tuple[str, int, int, list[SpanInfo]]:
    """Collect body text spans between two heading positions.

    Returns (body_text, page_start, page_end, body_spans).
    """
    body_parts: list[str] = []
    body_spans: list[SpanInfo] = []
    page_start = 0
    page_end = 0

    for i in range(start_idx, end_idx):
        span = all_spans[i]
        text = span.text

        if not page_start:
            page_start = span.page_number
        page_end = span.page_number

        body_parts.append(text)
        body_spans.append(span)

    body_text = " ".join(body_parts)
    # Clean up double spaces
    body_text = re.sub(r" {2,}", " ", body_text).strip()

    return body_text, page_start, page_end, body_spans


def _extract_table_from_spans(
    spans: list[SpanInfo],
    table_type: str,
) -> ParsedTable | None:
    """Extract a table from spans based on column alignment.

    table_type: 'specs' for 2-column or 'errors' for 3-column
    """
    if table_type == "specs":
        return _extract_specs_table(spans)
    elif table_type == "errors":
        return _extract_error_table(spans)
    return None


def _extract_specs_table(spans: list[SpanInfo]) -> ParsedTable | None:
    """Extract 2-column specification table from spans.

    Columns: Parameter | Value
    Identified by x-coordinate alignment.
    """
    headers = ["Parameter", "Value"]
    rows: list[TableRow] = []
    current_row: list[str] = ["", ""]

    for span in spans:
        text = span.text.strip()
        if not text:
            continue

        # Skip header spans
        if text.lower() in ("parameter", "value"):
            continue

        x0 = span.x0

        if x0 < SPECS_TABLE_COL_BOUNDARY:
            # Column 1 (Parameter)
            if current_row[0] and current_row[1]:
                rows.append(TableRow(cells=[c.strip() for c in current_row]))
                current_row = ["", ""]
            if current_row[0]:
                current_row[0] += " " + text
            else:
                current_row[0] = text
        else:
            # Column 2 (Value)
            if current_row[1]:
                current_row[1] += " " + text
            else:
                current_row[1] = text

    # Don't forget last row
    if current_row[0] or current_row[1]:
        rows.append(TableRow(cells=[c.strip() for c in current_row]))

    if rows:
        return ParsedTable(headers=headers, rows=rows)
    return None


def _extract_error_table(spans: list[SpanInfo]) -> ParsedTable | None:
    """Extract 3-column error codes table from spans.

    Columns: Code | Meaning | Device Behavior
    Multi-line cells are merged based on y-coordinate proximity.
    """
    headers = ["Code", "Meaning", "Device Behavior"]
    rows: list[TableRow] = []
    current_row: list[str] = ["", "", ""]

    for span in spans:
        text = span.text.strip()
        if not text:
            continue

        # Skip header spans
        if text.lower() in ("code", "meaning", "device behavior"):
            continue

        x0 = span.x0

        # Detect new row by checking if we've moved to a new error code
        # Error codes start with "E" followed by a digit in column 1
        is_new_code = x0 < ERROR_TABLE_COL_BOUNDARIES[0] and re.match(r"^E\d+$", text)

        if is_new_code and current_row[0]:
            # Save previous row
            rows.append(TableRow(cells=[c.strip() for c in current_row]))
            current_row = ["", "", ""]

        # Classify column
        if x0 < ERROR_TABLE_COL_BOUNDARIES[0]:
            current_row[0] = text
        elif x0 < ERROR_TABLE_COL_BOUNDARIES[1]:
            if current_row[1]:
                current_row[1] += " " + text
            else:
                current_row[1] = text
        else:
            if current_row[2]:
                current_row[2] += " " + text
            else:
                current_row[2] = text

    # Don't forget last row
    if any(current_row):
        rows.append(TableRow(cells=[c.strip() for c in current_row]))

    if rows:
        return ParsedTable(headers=headers, rows=rows)
    return None


def _extract_list_items(text: str) -> list[str]:
    """Extract numbered list items from body text."""
    items: list[str] = []
    # Match "1. Text" or "2. Text" etc.
    pattern = re.compile(r"(\d+)\.\s+(.+?)(?=\d+\.\s+|$)", re.DOTALL)
    for match in pattern.finditer(text):
        items.append(match.group(0).strip())
    return items


def _determine_parent(
    node: ParsedNode,
    nodes: list[ParsedNode],
) -> str | None:
    """Determine parent node for a given node based on number path hierarchy.

    For "2.1", parent is "2"
    For "2.1.1.1", parent is "2.1" (skipping 2.1.1 if it doesn't exist)
    """
    if not node.number_path:
        return None  # Root node has no parent

    parts = node.number_path.split(".")
    existing_paths = {n.number_path for n in nodes if n.number_path}

    # Try progressively shorter parent paths
    for depth in range(len(parts) - 1, 0, -1):
        candidate = ".".join(parts[:depth])
        if candidate in existing_paths:
            return candidate

    # If no parent found by number path (e.g. top sections 1, 2, ...), parent is root ("")
    return ""


def _detect_anomalies(
    node: ParsedNode,
    prev_node: ParsedNode | None,
    parent_node: ParsedNode | None,
    all_nodes: list[ParsedNode],
) -> list[str]:
    """Detect anomalies for a node."""
    anomalies = []

    if not node.number_path:
        return anomalies

    parts = node.number_path.split(".")
    numbering_depth = len(parts)

    # Check for level gap
    if parent_node and parent_node.number_path:
        parent_depth = len(parent_node.number_path.split("."))
        if numbering_depth > parent_depth + 1:
            anomalies.append("level_gap")
            logger.info(
                "Anomaly: level_gap for %s (parent=%s, gap=%d levels)",
                node.number_path,
                parent_node.number_path,
                numbering_depth - parent_depth - 1,
            )

    # Check for out-of-order numbering
    if prev_node and prev_node.number_path:
        prev_parts = prev_node.number_path.split(".")
        # Only compare siblings (same parent prefix)
        if len(parts) == len(prev_parts) and parts[:-1] == prev_parts[:-1]:
            try:
                prev_num = int(prev_parts[-1])
                curr_num = int(parts[-1])
                if curr_num < prev_num:
                    anomalies.append("out_of_order")
                    logger.info(
                        "Anomaly: out_of_order %s appears after %s",
                        node.number_path,
                        prev_node.number_path,
                    )
            except ValueError:
                pass

    return anomalies


def _is_body_span(span: SpanInfo) -> bool:
    """Check if a span is body text (not a heading)."""
    if span.bold and "Bold" in span.font:
        level = None
        size = span.size
        if abs(size - 22.0) < 0.5:
            level = 0
        elif abs(size - 16.5) < 0.5:
            level = 1
        elif abs(size - 12.87) < 0.5:
            level = 2
        elif abs(size - 11.0) < 0.5:
            # Could be heading or table header
            text = span.text.strip()
            if re.match(r"^\d+(?:\.\d+)*\s+", text):
                level = 3
        if level is not None:
            return False
    return True


def _get_span_index_after_heading(
    heading: HeadingSpan,
    all_spans: list[SpanInfo],
) -> int:
    """Find the index of the first span after a heading's spans."""
    if not heading.spans:
        return 0

    last_heading_span = heading.spans[-1]

    for i, span in enumerate(all_spans):
        if (
            span.page_number == last_heading_span.page_number
            and span.y0 == last_heading_span.y0
            and span.text == last_heading_span.text
        ):
            return i + 1

    return 0


def _find_next_heading_span_index(
    headings: list[HeadingSpan],
    heading_idx: int,
    all_spans: list[SpanInfo],
) -> int:
    """Find the span index where the next heading starts."""
    if heading_idx + 1 >= len(headings):
        return len(all_spans)

    next_heading = headings[heading_idx + 1]
    if not next_heading.spans:
        return len(all_spans)

    first_next_span = next_heading.spans[0]

    for i, span in enumerate(all_spans):
        if (
            span.page_number == first_next_span.page_number
            and span.y0 == first_next_span.y0
            and span.text == first_next_span.text
        ):
            return i

    return len(all_spans)


def build_tree(pages: list[PageSpans]) -> list[ParsedNode]:
    """Build document tree from extracted page spans.

    Returns a flat list of ParsedNode objects with parent links.
    """
    # Detect headings
    headings = detect_headings(pages)

    if not headings:
        logger.warning("No headings detected!")
        return []

    # Flatten all spans
    all_spans: list[SpanInfo] = []
    for page in pages:
        all_spans.extend(page.spans)

    # Build nodes
    nodes: list[ParsedNode] = []

    for h_idx, heading in enumerate(headings):
        # Determine body text range
        body_start_idx = _get_span_index_after_heading(heading, all_spans)
        body_end_idx = _find_next_heading_span_index(headings, h_idx, all_spans)

        # Collect body spans
        body_spans = all_spans[body_start_idx:body_end_idx]

        # Filter out heading spans from body and non-breaking hyphen spans
        # (they get merged into surrounding text)
        body_text_parts: list[str] = []
        table_spans: list[SpanInfo] = []
        is_in_table = False
        page_start = heading.page_number
        page_end = heading.page_number

        # Detect tables for 2.1 and 4.2
        section_has_table = heading.number_path in ("2.1", "4.2")

        for span in body_spans:
            page_end = max(page_end, span.page_number)

            # Check if this span is in a table region
            if section_has_table:
                # Table headers at x0 > 35 (indented)
                if (
                    span.bold
                    and span.size == 11.0
                    and span.text.strip().lower() in ("parameter", "value", "code", "meaning", "device behavior")
                ):
                    is_in_table = True
                    table_spans.append(span)
                    continue
                elif is_in_table:
                    # Check if we've left the table region
                    # Tables end when we hit a non-table bold span or subsection
                    if (span.bold and span.size > 11.0) or (
                        span.bold and span.size == 11.0 and re.match(r"^\d+(?:\.\d+)*\s+", span.text.strip())
                    ):
                        is_in_table = False
                    else:
                        table_spans.append(span)
                        continue

            body_text_parts.append(span.text)

        # Build body text
        body_text = " ".join(body_text_parts)
        body_text = re.sub(r" {2,}", " ", body_text).strip()

        # Normalize text
        normalized = normalize_text(body_text)

        # Extract cross-references
        cross_refs = extract_cross_refs(body_text)

        # Build table if applicable
        tables: list[ParsedTable] = []
        if section_has_table and table_spans:
            table_type = "specs" if heading.number_path == "2.1" else "errors"
            table = _extract_table_from_spans(table_spans, table_type)
            if table:
                tables.append(table)

        # Extract numbered lists
        lists: list[list[str]] = []
        list_pattern = re.compile(
            r"(?:^|\s)(\d+\.\s+(?:Normal|Elevated|Hypertension|Hypertensive).*?)(?=\s+\d+\.\s+(?:Normal|Elevated|Hypertension|Hypertensive)|$)",
            re.DOTALL,
        )
        list_matches = list_pattern.findall(body_text)
        if list_matches:
            lists.append(list_matches)

        node = ParsedNode(
            heading=heading.text,
            number_path=heading.number_path,
            level=heading.level,
            numbering_depth=heading.numbering_depth,
            document_order=h_idx,
            body_text=body_text,
            normalized_text=normalized,
            tables=tables,
            lists=lists,
            cross_refs=cross_refs,
            page_start=page_start,
            page_end=page_end,
            _heading_span=heading,
        )
        nodes.append(node)

    # Second pass: determine parents, tree levels, and anomalies
    path_to_node: dict[str, ParsedNode] = {n.number_path: n for n in nodes}
    prev_by_parent: dict[str | None, ParsedNode] = {}

    for node in nodes:
        # Find parent
        parent_path = _determine_parent(node, nodes)
        node.parent_number_path = parent_path

        # Set tree level strictly based on tree depth
        if parent_path is None:
            node.level = 0
        elif parent_path == "":
            node.level = 1
        elif parent_path in path_to_node:
            node.level = path_to_node[parent_path].level + 1
        else:
            node.level = 1

        # Find parent node for anomaly detection
        parent_node = path_to_node.get(parent_path) if parent_path else None

        # Find previous sibling (under same parent) for out-of-order detection
        prev_sibling = prev_by_parent.get(parent_path)

        # Detect anomalies
        anomalies = _detect_anomalies(node, prev_sibling, parent_node, nodes)
        node.anomalies = anomalies

        prev_by_parent[parent_path] = node

    # Third pass: compute hashes and build children lists
    for node in nodes:
        node.compute_hash()
        if node.parent_number_path is not None and node.parent_number_path in path_to_node:
            parent = path_to_node[node.parent_number_path]
            parent.children_number_paths.append(node.number_path)

    logger.info(
        "Built tree with %d nodes, %d anomalies",
        len(nodes),
        sum(len(n.anomalies) for n in nodes),
    )
    return nodes
