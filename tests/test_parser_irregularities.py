"""tests/test_parser_irregularities.py
Tests for all parser irregularities, edge cases, and coverage guarantees (Phase 1).
"""

from pathlib import Path

import pytest

from app.parsing.extract import extract_spans
from app.parsing.normalize import normalize_for_hash, normalize_text
from app.parsing.report import generate_report
from app.parsing.tree import ParsedNode, build_tree


@pytest.fixture(scope="module")
def v1_data():
    pdf_path = Path(__file__).parent.parent / "data" / "ct200_manual.pdf"
    pages = extract_spans(pdf_path)
    nodes = build_tree(pages)
    report = generate_report("ct200_manual", 1, pages, nodes)
    return {"pages": pages, "nodes": nodes, "report": report}


@pytest.fixture(scope="module")
def v2_data():
    pdf_path = Path(__file__).parent.parent / "data" / "ct200_manual_v2.pdf"
    pages = extract_spans(pdf_path)
    nodes = build_tree(pages)
    report = generate_report("ct200_manual_v2", 2, pages, nodes)
    return {"pages": pages, "nodes": nodes, "report": report}


class TestParserIrregularities:
    """Test suite verifying all documented irregularities in CT-200 PDFs."""

    def test_title_merged_as_root(self, v1_data):
        """Title spanning 3 lines must be merged into a single root node (level 0)."""
        nodes = v1_data["nodes"]
        root = nodes[0]
        assert root.level == 0
        assert root.number_path == ""
        assert "CardioTrack CT-200" in root.heading
        assert "Technical &" in root.heading
        assert "User Manual" in root.heading
        assert root.parent_number_path is None

    def test_duplicate_heading_error_codes(self, v1_data):
        """'Error Codes' appears twice (4.2 and 7.1) under different parents (4 and 7)."""
        nodes = v1_data["nodes"]
        error_nodes = [n for n in nodes if "Error Codes" in n.heading]
        assert len(error_nodes) == 2

        n42 = next(n for n in error_nodes if n.number_path == "4.2")
        n71 = next(n for n in error_nodes if n.number_path == "7.1")

        assert n42.parent_number_path == "4"
        assert n71.parent_number_path == "7"
        assert n42.level == 2
        assert n71.level == 2

    def test_level_gap_2111(self, v1_data):
        """'2.1.1.1' appears directly under 2.1 with no 2.1.1.
        Parent must be 2.1, tree level 3, numbering_depth 4, anomaly flag 'level_gap'.
        """
        nodes = v1_data["nodes"]
        n2111 = next(n for n in nodes if n.number_path == "2.1.1.1")
        assert n2111.parent_number_path == "2.1"
        assert n2111.level == 3
        assert n2111.numbering_depth == 4
        assert "level_gap" in n2111.anomalies

    def test_out_of_order_34_before_33(self, v1_data):
        """'3.4 Auto Shutoff' appears before '3.3 Result Display' in document order.
        Document order must be preserved, anomaly 'out_of_order' flagged on 3.3.
        """
        nodes = v1_data["nodes"]
        n34 = next(n for n in nodes if n.number_path == "3.4")
        n33 = next(n for n in nodes if n.number_path == "3.3")

        assert n34.document_order < n33.document_order
        assert n34.parent_number_path == "3"
        assert n33.parent_number_path == "3"
        assert "out_of_order" in n33.anomalies
        assert "out_of_order" not in n34.anomalies

    def test_list_items_not_headings(self, v1_data):
        """Section 3.3 numbered items (1. Normal ... 5. Hypertensive Crisis) must NOT be headings."""
        nodes = v1_data["nodes"]
        number_paths = {n.number_path for n in nodes}
        # None of these should exist as independent section nodes
        for num in ["1.0", "2.0", "3.0", "4.0", "5.0"]:
            assert num not in number_paths

        n33 = next(n for n in nodes if n.number_path == "3.3")
        assert len(n33.lists) >= 1
        # The list items should be captured inside node.lists or body
        assert any("Normal" in item for items in n33.lists for item in items)
        assert any("Hypertensive Crisis" in item for items in n33.lists for item in items)

    def test_v2_fused_heading_split_correctly(self, v2_data):
        """Heading and body are cleanly separated (e.g., 3.1)."""
        nodes = v2_data["nodes"]
        n31 = next(n for n in nodes if n.number_path == "3.1")
        assert n31.heading == "3.1 Powering On and Profile Selection"
        assert n31.body_text.startswith("Press and hold the power button")

    def test_table_extraction_v1_and_v2(self, v1_data, v2_data):
        """2.1 General Specs has 7 rows. 4.2 Error Codes has 5 rows in v1, 6 rows in v2 (new E6)."""
        v1_nodes = v1_data["nodes"]
        v2_nodes = v2_data["nodes"]

        # 2.1 specs table
        v1_21 = next(n for n in v1_nodes if n.number_path == "2.1")
        assert len(v1_21.tables) == 1
        assert len(v1_21.tables[0].rows) == 7
        assert v1_21.tables[0].headers == ["Parameter", "Value"]

        # 4.2 error codes table v1 vs v2
        v1_42 = next(n for n in v1_nodes if n.number_path == "4.2")
        assert len(v1_42.tables) == 1
        assert len(v1_42.tables[0].rows) == 5
        assert v1_42.tables[0].headers == ["Code", "Meaning", "Device Behavior"]

        v2_42 = next(n for n in v2_nodes if n.number_path == "4.2")
        assert len(v2_42.tables) == 1
        assert len(v2_42.tables[0].rows) == 6
        assert any("E6" in row.cells[0] for row in v2_42.tables[0].rows)

    def test_cross_references_extracted(self, v1_data):
        """Cross references are correctly extracted into node.cross_refs."""
        nodes = v1_data["nodes"]
        n33 = next(n for n in nodes if n.number_path == "3.3")
        assert "2.1" in n33.cross_refs
        assert "4.3" in n33.cross_refs

        n71 = next(n for n in nodes if n.number_path == "7.1")
        assert "4.2" in n71.cross_refs

    def test_hash_stability_across_variants(self):
        """Hash must be stable across non-breaking hyphens (U+2011), extra whitespace, etc."""
        text1 = "CardioTrack CT\u2011200   Blood  Pressure  Monitor"
        text2 = "CardioTrack CT-200 Blood Pressure Monitor"

        assert normalize_for_hash(text1) == normalize_for_hash(text2)

        node1 = ParsedNode(
            heading="Test Heading",
            number_path="9.9",
            level=1,
            numbering_depth=2,
            document_order=1,
            body_text=text1,
            normalized_text=normalize_text(text1),
        )
        node2 = ParsedNode(
            heading="Test Heading",
            number_path="9.9",
            level=1,
            numbering_depth=2,
            document_order=1,
            body_text=text2,
            normalized_text=normalize_text(text2),
        )

        assert node1.compute_hash() == node2.compute_hash()

    def test_coverage_reports_no_gaps(self, v1_data, v2_data):
        """Both v1 and v2 reports must have 100% text coverage with 0 unassigned spans."""
        v1_report = v1_data["report"]
        v2_report = v2_data["report"]

        assert v1_report.coverage.passed is True
        assert v1_report.coverage.coverage_percentage == 100.0
        assert len(v1_report.coverage.unassigned_spans) == 0

        assert v2_report.coverage.passed is True
        assert v2_report.coverage.coverage_percentage == 100.0
        assert len(v2_report.coverage.unassigned_spans) == 0

    def test_v2_has_section_53_data_export(self, v1_data, v2_data):
        """v2 must include newly added section 5.3 Data Export."""
        v1_paths = {n.number_path for n in v1_data["nodes"]}
        v2_paths = {n.number_path for n in v2_data["nodes"]}

        assert "5.3" not in v1_paths
        assert "5.3" in v2_paths

        n53 = next(n for n in v2_data["nodes"] if n.number_path == "5.3")
        assert n53.heading == "5.3 Data Export"
        assert n53.parent_number_path == "5"
        assert "CSV" in n53.body_text
