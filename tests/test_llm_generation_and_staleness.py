"""tests/test_llm_generation_and_staleness.py
Tests for Phase 4 (LLM generation, validation, retries, idempotency) and
Phase 5 (Staleness detection at read time).
"""

import json
from pathlib import Path

import mongomock
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.llm.client import MockLLMClient, set_llm_client
from app.main import app
from app.models.sql import Node
from app.mongo import set_mongo_client
from app.services.ingest import ingest_document


@pytest.fixture
def test_env():
    """Sets up in-memory SQLite, mongomock, and MockLLMClient."""
    # 1. In-memory SQLite
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        echo=False,
    )
    Base.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(bind=engine)

    def override_get_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db

    # 2. In-memory Mongo mock
    mock_mongo = mongomock.MongoClient()
    set_mongo_client(mock_mongo)

    # 3. Mock LLM client
    mock_llm = MockLLMClient()
    set_llm_client(mock_llm)

    client = TestClient(app)
    db = TestingSession()

    yield {
        "client": client,
        "db": db,
        "mock_llm": mock_llm,
        "mock_mongo": mock_mongo,
    }

    db.close()
    app.dependency_overrides.clear()


@pytest.fixture
def sample_setup(test_env):
    """Ingests v1 and sets up a selection with sections 3.2, 4.2, and 6.1."""
    env = test_env
    data_dir = Path(__file__).parent.parent / "data"
    doc, v1, _, _ = ingest_document(env["db"], data_dir / "ct200_manual.pdf", "CT-200")

    nodes = {n.number_path: n for n in env["db"].query(Node).filter(Node.version_id == v1.id).all()}
    selected_node_ids = [
        nodes["3.2"].node_version_id,
        nodes["4.2"].node_version_id,
        nodes["6.1"].node_version_id,
    ]

    resp = env["client"].post("/selections", json={"name": "Multi-Section QA", "node_ids": selected_node_ids})
    assert resp.status_code == 201
    selection = resp.json()

    return {
        "doc": doc,
        "v1": v1,
        "nodes": nodes,
        "selection": selection,
        "v2_pdf": data_dir / "ct200_manual_v2.pdf",
    }


def _make_valid_response(n32_lid: str, n42_lid: str, n61_lid: str) -> dict:
    """Helper to build a valid LLM response using verbatim quotes from CT-200 manual v1."""
    return {
        "test_cases": [
            {
                "title": "Verify Initial Inflation Target and Increment Steps",
                "objective": "Verify the device inflates to 180 mmHg and increases in 40 mmHg steps if no pulse detected",
                "preconditions": ["Device is powered on", "Cuff connected properly"],
                "steps": ["Trigger measurement", "Monitor pressure progression"],
                "expected_result": "Initial target of 180 mmHg reached; inflates in 40 mmHg increments",
                "priority": "high",
                "category": "functional",
                "source_refs": [
                    {
                        "logical_id": n32_lid,
                        "verbatim_quote": "the device inflates the cuff to an initial target of 180 mmHg. If the user's pulse is not detected by 180 mmHg, the device inflates in 40 mmHg increments",
                    }
                ],
            },
            {
                "title": "Verify E3 Overpressure Auto-Deflation Response",
                "objective": "Verify overpressure condition triggers E3 error and deflates within 2 seconds",
                "preconditions": ["Device running measurement cycle"],
                "steps": ["Simulate overpressure fault", "Observe deflation time and display"],
                "expected_result": "Auto-deflates within 2 seconds and displays E3",
                "priority": "high",
                "category": "safety",
                "source_refs": [
                    {
                        "logical_id": n42_lid,
                        "verbatim_quote": "Auto-deflates within 2 seconds, displays E3",
                    }
                ],
            },
            {
                "title": "Verify Maintenance Cloth Cleaning Procedure",
                "objective": "Verify cleaning instructions allow dry or water-dampened soft cloth",
                "preconditions": ["Device powered off"],
                "steps": ["Wipe device exterior with lightly dampened cloth", "Verify display is not damaged"],
                "expected_result": "Device body cleaned without solvent or submersion",
                "priority": "medium",
                "category": "functional",
                "source_refs": [
                    {
                        "logical_id": n61_lid,
                        "verbatim_quote": "Wipe the device body and cuff exterior with a soft, dry cloth or one lightly dampened with water.",
                    }
                ],
            },
        ]
    }


class TestLLMGeneration:
    """Test suite verifying LLM generation, validation rules, retries, and idempotency."""

    def test_markdown_fenced_json_parsed_successfully(self, test_env, sample_setup):
        """LLM returning markdown code fences ```json is stripped and parsed cleanly."""
        client = test_env["client"]
        sel = sample_setup["selection"]
        items = {item["number_path"]: item["logical_id"] for item in sel["items"]}

        payload = _make_valid_response(items["3.2"], items["4.2"], items["6.1"])
        fenced_json = f"```json\n{json.dumps(payload)}\n```"

        test_env["mock_llm"].add_response(fenced_json)

        resp = client.post(f"/selections/{sel['id']}/generate")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert len(data["test_cases"]) == 3
        assert data["test_cases"][0]["title"] == "Verify Initial Inflation Target and Increment Steps"

    def test_invalid_json_retries_and_fails_502(self, test_env, sample_setup):
        """Unparseable text exhausts retries and returns 502 with error details."""
        client = test_env["client"]
        sel = sample_setup["selection"]

        # Queue 3 invalid responses (initial + 2 retries)
        for _ in range(3):
            test_env["mock_llm"].add_response("I am an AI and cannot generate JSON right now.")

        resp = client.post(f"/selections/{sel['id']}/generate")
        assert resp.status_code == 502
        assert (
            "validation error" in resp.json()["detail"].lower() or "failed validation" in resp.json()["detail"].lower()
        )
        assert test_env["mock_llm"].call_count == 3

    def test_too_few_cases_retries_and_succeeds(self, test_env, sample_setup):
        """When LLM returns only 2 cases, validation feedback triggers retry, which succeeds."""
        client = test_env["client"]
        sel = sample_setup["selection"]
        items = {item["number_path"]: item["logical_id"] for item in sel["items"]}

        valid_payload = _make_valid_response(items["3.2"], items["4.2"], items["6.1"])
        # Attempt 1: only 2 test cases
        too_few = {"test_cases": valid_payload["test_cases"][:2]}
        test_env["mock_llm"].add_response(json.dumps(too_few))

        # Attempt 2 (retry): full 3 valid test cases
        test_env["mock_llm"].add_response(json.dumps(valid_payload))

        resp = client.post(f"/selections/{sel['id']}/generate")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["test_cases"]) == 3
        assert test_env["mock_llm"].call_count == 2

    def test_too_many_cases_trimmed_to_five(self, test_env, sample_setup):
        """When LLM returns 7 cases, it is trimmed to 5 cases."""
        client = test_env["client"]
        sel = sample_setup["selection"]
        items = {item["number_path"]: item["logical_id"] for item in sel["items"]}

        base_case = _make_valid_response(items["3.2"], items["4.2"], items["6.1"])["test_cases"][0]
        seven_cases = {"test_cases": [dict(base_case, title=f"Test Case {i}") for i in range(1, 8)]}
        test_env["mock_llm"].add_response(json.dumps(seven_cases))

        resp = client.post(f"/selections/{sel['id']}/generate")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["test_cases"]) == 5
        assert len(data["dropped_cases"]) == 2

    def test_fabricated_quote_rejected(self, test_env, sample_setup):
        """Quotes not present verbatim in source text are rejected as fabricated."""
        client = test_env["client"]
        sel = sample_setup["selection"]
        items = {item["number_path"]: item["logical_id"] for item in sel["items"]}

        payload = _make_valid_response(items["3.2"], items["4.2"], items["6.1"])
        # Fabricate a quote
        payload["test_cases"][0]["source_refs"][0]["verbatim_quote"] = (
            "The device is made of titanium and flies to Mars."
        )

        # Pass 3 valid cases + 1 fabricated case (total 4 cases, 1 dropped, 3 remain valid -> status=partial)
        extra_case = dict(payload["test_cases"][1], title="Extra Case")
        payload["test_cases"].append(extra_case)

        test_env["mock_llm"].add_response(json.dumps(payload))

        resp = client.post(f"/selections/{sel['id']}/generate")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "partial"
        assert len(data["test_cases"]) == 3
        assert len(data["dropped_cases"]) == 1
        assert "fabricated quote" in data["dropped_cases"][0]["reason"].lower()

    def test_idempotency_cached_and_force_true(self, test_env, sample_setup):
        """Repeated generation call returns cached generation; force=true generates new record."""
        client = test_env["client"]
        sel = sample_setup["selection"]
        items = {item["number_path"]: item["logical_id"] for item in sel["items"]}

        payload = _make_valid_response(items["3.2"], items["4.2"], items["6.1"])
        test_env["mock_llm"].add_response(json.dumps(payload))

        # Call 1: fresh generation
        resp1 = client.post(f"/selections/{sel['id']}/generate")
        assert resp1.status_code == 200
        gen1 = resp1.json()
        assert test_env["mock_llm"].call_count == 1

        # Call 2: cached generation returned (mock_llm not called again!)
        resp2 = client.post(f"/selections/{sel['id']}/generate")
        assert resp2.status_code == 200
        gen2 = resp2.json()
        assert gen2["generation_id"] == gen1["generation_id"]
        assert test_env["mock_llm"].call_count == 1  # No LLM call made!

        # Call 3: force=True triggers new LLM call and returns a NEW generation_id
        test_env["mock_llm"].add_response(json.dumps(payload))
        resp3 = client.post(f"/selections/{sel['id']}/generate?force=true")
        assert resp3.status_code == 200
        gen3 = resp3.json()
        assert gen3["generation_id"] != gen1["generation_id"]
        assert test_env["mock_llm"].call_count == 2


class TestStalenessAndRetrieval:
    """Test suite verifying Phase 5 staleness detection and retrieval."""

    def test_staleness_computation_after_v2_ingestion(self, test_env, sample_setup):
        """Verify:
        - Before v2: test cases are fresh.
        - After v2:
          - 3.2 test case is stale_numeric (40 -> 30 mmHg)
          - 4.2 test case is stale_numeric (2 s -> 1.5 s)
          - 6.1 test case is fresh (cleaning unchanged)
          - Generation status is partially_stale
        """
        client = test_env["client"]
        db = test_env["db"]
        doc = sample_setup["doc"]
        sel = sample_setup["selection"]
        items = {item["number_path"]: item["logical_id"] for item in sel["items"]}

        # 1. Generate test cases on v1
        payload = _make_valid_response(items["3.2"], items["4.2"], items["6.1"])
        test_env["mock_llm"].add_response(json.dumps(payload))

        resp_gen = client.post(f"/selections/{sel['id']}/generate")
        assert resp_gen.status_code == 200

        # 2. Fetch generations before v2: all fresh!
        resp_before = client.get(f"/selections/{sel['id']}/generations")
        assert resp_before.status_code == 200
        gens_before = resp_before.json()
        assert len(gens_before) == 1
        st_before = gens_before[0]["staleness"]
        assert st_before["status"] == "fresh"
        for tc in st_before["test_cases"]:
            assert tc["status"] == "fresh"

        # 3. Now re-ingest manual as v2!
        ingest_document(db, sample_setup["v2_pdf"], doc.name, document_id=doc.id)

        # 4. Fetch generations after v2: staleness recomputed at read time!
        resp_after = client.get(f"/selections/{sel['id']}/generations")
        assert resp_after.status_code == 200
        gens_after = resp_after.json()
        assert len(gens_after) == 1

        st_after = gens_after[0]["staleness"]
        assert st_after["status"] == "partially_stale"
        assert "limits" in st_after
        assert "semantic" in st_after["limits"].lower()

        # Check each test case:
        tc_32 = next(tc for tc in st_after["test_cases"] if "Initial Inflation" in tc["test_case_title"])
        assert tc_32["status"] == "stale_numeric"

        tc_42 = next(tc for tc in st_after["test_cases"] if "Overpressure" in tc["test_case_title"])
        assert tc_42["status"] == "stale_numeric"

        tc_61 = next(tc for tc in st_after["test_cases"] if "Cleaning" in tc["test_case_title"])
        assert tc_61["status"] == "fresh"

        # 5. Also verify GET /nodes/{node_id}/generations returns generation with staleness block
        n32_lid = items["3.2"]
        resp_node_gen = client.get(f"/nodes/{n32_lid}/generations")
        assert resp_node_gen.status_code == 200
        node_gens = resp_node_gen.json()
        assert len(node_gens) == 1
        assert node_gens[0]["staleness"]["status"] == "partially_stale"
