"""tests/test_versioning.py
Tests for persistence, versioning, matcher, and diffing (Phase 2).
"""

from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models.schemas import ChangeClass
from app.models.sql import DocumentVersion, LogicalNode, MatchReport, Node
from app.services.ingest import ingest_document
from app.versioning.diff import compute_node_diff


@pytest.fixture
def db_session():
    """In-memory SQLite database session for tests."""
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def pdf_paths():
    data_dir = Path(__file__).parent.parent / "data"
    return {
        "v1": data_dir / "ct200_manual.pdf",
        "v2": data_dir / "ct200_manual_v2.pdf",
    }


class TestVersioningAndPersistence:
    """Test suite verifying document persistence, versioning, matching, and diffing."""

    def test_ingest_v1_creates_nodes_and_logical_ids(self, db_session, pdf_paths):
        """v1 ingestion creates Document, DocumentVersion 1, nodes, and logical IDs."""
        doc, v1, report, match = ingest_document(
            db_session,
            pdf_paths["v1"],
            "CardioTrack CT-200 Manual",
        )

        assert doc.id is not None
        assert v1.version_number == 1
        assert v1.pdf_sha256 is not None

        nodes = db_session.query(Node).filter(Node.version_id == v1.id).all()
        assert len(nodes) == 28

        logical_nodes = db_session.query(LogicalNode).filter(LogicalNode.document_id == doc.id).all()
        assert len(logical_nodes) == 28

        # Verify parent relationships
        n21 = next(n for n in nodes if n.number_path == "2.1")
        n2111 = next(n for n in nodes if n.number_path == "2.1.1.1")
        assert n2111.parent_id == n21.node_version_id

    def test_reupload_identical_pdf_is_idempotent(self, db_session, pdf_paths):
        """Re-uploading the exact same PDF returns the existing version without duplicates."""
        doc, v1, _, _ = ingest_document(
            db_session,
            pdf_paths["v1"],
            "CardioTrack CT-200 Manual",
        )

        # Upload again
        doc2, v1_again, _, _ = ingest_document(
            db_session,
            pdf_paths["v1"],
            "CardioTrack CT-200 Manual",
            document_id=doc.id,
        )

        assert v1_again.id == v1.id
        assert v1_again.version_number == 1

        # Check versions count
        versions = db_session.query(DocumentVersion).filter(DocumentVersion.document_id == doc.id).all()
        assert len(versions) == 1

    def test_ingest_v2_matches_nodes_and_preserves_logical_ids(self, db_session, pdf_paths):
        """v2 ingestion matches v1 nodes, reuses logical IDs, and identifies modifications."""
        # 1. Ingest v1
        doc, v1, _, _ = ingest_document(
            db_session,
            pdf_paths["v1"],
            "CardioTrack CT-200 Manual",
        )

        # Snapshot v1 nodes before v2
        v1_nodes_before = {
            n.number_path: (n.logical_id, n.content_hash, n.body_text)
            for n in db_session.query(Node).filter(Node.version_id == v1.id).all()
        }

        # 2. Ingest v2
        _, v2, report_v2, match_result = ingest_document(
            db_session,
            pdf_paths["v2"],
            "CardioTrack CT-200 Manual",
            document_id=doc.id,
        )

        assert v2.version_number == 2
        assert match_result is not None

        # Verify expected changes:
        # modified: 2.1.1.1, 3.2, 4.2, 4.3
        # added: 5.3
        # unchanged: 24 nodes
        assert match_result.added_count == 1
        assert match_result.modified_count == 4
        assert match_result.unchanged_count == 24
        assert match_result.removed_count == 0

        # Check match report stored in DB
        db_match_report = db_session.query(MatchReport).filter(MatchReport.to_version_id == v2.id).first()
        assert db_match_report is not None
        assert db_match_report.report_data["summary"]["added"] == 1
        assert db_match_report.report_data["summary"]["modified"] == 4

        # Verify GATE: v1 nodes remain completely identical after v2 ingestion
        v1_nodes_after = {
            n.number_path: (n.logical_id, n.content_hash, n.body_text)
            for n in db_session.query(Node).filter(Node.version_id == v1.id).all()
        }
        assert v1_nodes_before == v1_nodes_after

        # Verify v2 node logical IDs match v1
        v2_nodes = {n.number_path: n for n in db_session.query(Node).filter(Node.version_id == v2.id).all()}
        for path in v1_nodes_before:
            assert v2_nodes[path].logical_id == v1_nodes_before[path][0]

        # Verify 5.3 has a distinct new logical_id
        assert v2_nodes["5.3"].logical_id not in [v[0] for v in v1_nodes_before.values()]

    def test_diff_classification_and_numeric_changes(self, db_session, pdf_paths):
        """Test diffing classifies numeric changes and word diffs accurately."""
        doc, v1, _, _ = ingest_document(db_session, pdf_paths["v1"], "CT-200")
        _, v2, _, _ = ingest_document(db_session, pdf_paths["v2"], "CT-200", document_id=doc.id)

        v1_nodes = {n.number_path: n for n in db_session.query(Node).filter(Node.version_id == v1.id).all()}
        v2_nodes = {n.number_path: n for n in db_session.query(Node).filter(Node.version_id == v2.id).all()}

        # 2.1.1.1: Battery life cycles 300 -> 250, 15% -> 10%
        diff_2111 = compute_node_diff(v1_nodes["2.1.1.1"], v2_nodes["2.1.1.1"], 1, 2)
        assert diff_2111.changed is True
        assert diff_2111.change_class == ChangeClass.NUMERIC
        assert any(d.old_value == 300 and d.new_value == 250 for d in diff_2111.numeric_changes)
        assert any(d.old_value == 15 and d.new_value == 10 for d in diff_2111.numeric_changes)

        # 3.2: Cuff inflation 40 -> 30 mmHg (note: v2 adds explanatory text mentioning 'reduced from original 40 mmHg')
        diff_32 = compute_node_diff(v1_nodes["3.2"], v2_nodes["3.2"], 1, 2)
        assert diff_32.changed is True
        assert diff_32.change_class == ChangeClass.NUMERIC
        assert any(d.new_value == 30 and d.unit == "mmHg" for d in diff_32.numeric_changes)
        assert "[-40-] [+30+]" in diff_32.word_diff_summary

        # 4.2: Error Codes table: E3 2 s -> 1.5 s, and new E6 row
        diff_42 = compute_node_diff(v1_nodes["4.2"], v2_nodes["4.2"], 1, 2)
        assert diff_42.changed is True
        assert diff_42.change_class == ChangeClass.NUMERIC

        # 4.3: E1-E5 -> E1-E6
        diff_43 = compute_node_diff(v1_nodes["4.3"], v2_nodes["4.3"], 1, 2)
        assert diff_43.changed is True
        assert diff_43.change_class == ChangeClass.NUMERIC

        # 6.1: Cleaning Instructions - unchanged
        diff_61 = compute_node_diff(v1_nodes["6.1"], v2_nodes["6.1"], 1, 2)
        assert diff_61.changed is False
        assert diff_61.change_class == ChangeClass.UNCHANGED

        # 5.3: Added
        diff_53 = compute_node_diff(None, v2_nodes["5.3"], 1, 2)
        assert diff_53.changed is True
        assert diff_53.change_class == ChangeClass.STRUCTURAL
