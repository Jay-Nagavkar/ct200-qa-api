"""tests/test_api_browse_selection.py
Tests for Browse and Selection API (Phase 3).
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.models.sql import Node
from app.services.ingest import ingest_document


@pytest.fixture
def test_app_and_db():
    """Create test client with in-memory SQLite database sharing a StaticPool."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        echo=False,
    )
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(bind=engine)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)

    db = TestingSessionLocal()
    yield client, db

    db.close()
    app.dependency_overrides.clear()


@pytest.fixture
def pdf_paths():
    data_dir = Path(__file__).parent.parent / "data"
    return {
        "v1": data_dir / "ct200_manual.pdf",
        "v2": data_dir / "ct200_manual_v2.pdf",
    }


class TestBrowseAndSelectionAPI:
    """Test suite verifying Phase 3 Browse, Search, Diff, and Selection APIs."""

    def test_browse_tree_and_node_detail(self, test_app_and_db, pdf_paths):
        client, db = test_app_and_db
        doc, v1, _, _ = ingest_document(db, pdf_paths["v1"], "CT-200")

        # 1. GET /documents
        resp = client.get("/documents")
        assert resp.status_code == 200
        docs = resp.json()
        assert len(docs) == 1
        assert docs[0]["id"] == doc.id

        # 2. GET /documents/{id}/sections
        resp = client.get(f"/documents/{doc.id}/sections")
        assert resp.status_code == 200
        sections = resp.json()
        assert len(sections) >= 1  # root node
        root = sections[0]
        assert "CardioTrack" in root["heading"]
        assert len(root["children"]) >= 8  # top-level sections 1..8

        # 3. GET /nodes/{node_version_id}
        n2111 = db.query(Node).filter(Node.number_path == "2.1.1.1").first()
        resp = client.get(f"/nodes/{n2111.node_version_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["number_path"] == "2.1.1.1"
        assert "level_gap" in data["anomalies"]
        assert "300 measurement cycles" in data["body_text"]

        # 4. GET /documents/{id}/search
        resp = client.get(f"/documents/{doc.id}/search?q=cuff&scope=all")
        assert resp.status_code == 200
        results = resp.json()
        assert len(results) >= 2
        paths = [r["number_path"] for r in results]
        assert "2.2" in paths or "3.2" in paths

    def test_ingestion_report_endpoint(self, test_app_and_db, pdf_paths):
        client, db = test_app_and_db
        doc, v1, _, _ = ingest_document(db, pdf_paths["v1"], "CT-200")

        resp = client.get(f"/documents/{doc.id}/ingestion-report?version=1")
        assert resp.status_code == 200
        report = resp.json()
        assert report["coverage"]["passed"] is True
        assert report["coverage"]["coverage_percentage"] == 100.0

    def test_node_changes_endpoint(self, test_app_and_db, pdf_paths):
        client, db = test_app_and_db
        doc, v1, _, _ = ingest_document(db, pdf_paths["v1"], "CT-200")
        _, v2, _, _ = ingest_document(db, pdf_paths["v2"], "CT-200", document_id=doc.id)

        n32 = db.query(Node).filter(Node.version_id == v2.id, Node.number_path == "3.2").first()
        resp = client.get(f"/nodes/{n32.node_version_id}/changes?from=1&to=2")
        assert resp.status_code == 200
        diff = resp.json()
        assert diff["changed"] is True
        assert diff["change_class"] == "numeric"
        assert "[-40-] [+30+]" in diff["word_diff_summary"]

    def test_selection_pinning_and_deduplication_gate(self, test_app_and_db, pdf_paths):
        """GATE: Selection created on v1 pins text and returns v1 text even after v2 exists."""
        client, db = test_app_and_db
        doc, v1, _, _ = ingest_document(db, pdf_paths["v1"], "CT-200")

        # Pick nodes: 2.1.1.1 (modified in v2), 3.2 (modified in v2), 6.1 (unchanged)
        nodes_v1 = {n.number_path: n for n in db.query(Node).filter(Node.version_id == v1.id).all()}
        selected_ids = [
            nodes_v1["2.1.1.1"].node_version_id,
            nodes_v1["3.2"].node_version_id,
            nodes_v1["6.1"].node_version_id,
        ]

        # 1. Create selection on v1
        resp = client.post("/selections", json={"name": "Battery & Operation QA", "node_ids": selected_ids})
        assert resp.status_code == 201
        sel_data = resp.json()
        selection_id = sel_data["id"]
        assert len(sel_data["items"]) == 3

        # Verify initial text snapshot contains v1 values
        item_2111 = next(item for item in sel_data["items"] if item["number_path"] == "2.1.1.1")
        assert "300 measurement cycles" in item_2111["text_snapshot"]

        # 2. Test duplicate selection returns existing selection
        resp_dup = client.post("/selections", json={"name": "Duplicate Selection", "node_ids": selected_ids})
        assert resp_dup.status_code == 201 or resp_dup.status_code == 200
        assert resp_dup.json()["id"] == selection_id

        # 3. Now ingest v2 (where 2.1.1.1 changes to 250 cycles!)
        _, v2, _, _ = ingest_document(db, pdf_paths["v2"], "CT-200", document_id=doc.id)

        # 4. Fetch selection after v2 exists - MUST still resolve original v1 text!
        resp_fetch = client.get(f"/selections/{selection_id}")
        assert resp_fetch.status_code == 200
        fetched = resp_fetch.json()

        item_2111_after = next(item for item in fetched["items"] if item["number_path"] == "2.1.1.1")
        # Pinned text MUST still say 300 cycles, NOT 250 cycles!
        assert "300 measurement cycles" in item_2111_after["text_snapshot"]
        assert "250 measurement cycles" not in item_2111_after["text_snapshot"]
