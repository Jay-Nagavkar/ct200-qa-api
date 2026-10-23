# ruff: noqa: E402
"""scripts/demo.py
End-to-end runnable demo script for CardioTrack CT-200 QA API.
Runs the complete workflow:
1. Ingest v1
2. Show tree structure and coverage report
3. Create pinned selection (3.2, 4.2, 6.1)
4. Generate test cases
5. Ingest v2
6. Show node changes and diffs
7. Fetch generation with read-time staleness block
"""

import json
import sys
from pathlib import Path

# Add project root to sys.path
root_dir = Path(__file__).parent.parent
sys.path.insert(0, str(root_dir))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

import mongomock
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.llm.client import MockLLMClient, set_llm_client
from app.main import app
from app.models.sql import Node
from app.mongo import set_mongo_client


def run_demo():
    print("=" * 70)
    print("CARDIOTRACK CT-200 QA API - END-TO-END DEMONSTRATION")
    print("=" * 70)

    # Setup in-memory DB and mock Mongo for 100% reproducible demo execution
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(bind=engine)

    def override_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    set_mongo_client(mongomock.MongoClient())

    # Pre-configure mock LLM with valid responses based on manual text
    mock_llm = MockLLMClient()
    set_llm_client(mock_llm)

    client = TestClient(app)
    db = TestingSession()

    data_dir = root_dir / "data"
    pdf_v1 = data_dir / "ct200_manual.pdf"
    pdf_v2 = data_dir / "ct200_manual_v2.pdf"

    # Step 1: Ingest v1
    print("\n[STEP 1] Ingesting Document v1 (ct200_manual.pdf)...")
    with open(pdf_v1, "rb") as f:
        resp = client.post(
            "/documents",
            data={"name": "CardioTrack CT-200 Manual"},
            files={"file": ("ct200_manual.pdf", f, "application/pdf")},
        )
    assert resp.status_code == 201, f"Failed: {resp.text}"
    doc_data = resp.json()
    doc_id = doc_data["id"]
    print(f"  Document Created: ID={doc_id}, Name='{doc_data['name']}'")

    # Step 2: Show Tree and Ingestion Report
    print("\n[STEP 2] Fetching Document Sections Tree and Ingestion Report...")
    resp = client.get(f"/documents/{doc_id}/sections?version=1")
    assert resp.status_code == 200
    sections = resp.json()

    print("  Document Tree (Top Levels):")

    def print_tree(nodes, depth=0):
        for n in nodes:
            indent = "  " * (depth + 2)
            anom = f" [{', '.join(n['anomalies'])}]" if n.get("anomalies") else ""
            print(f"{indent}• {n['number_path'] or 'ROOT'}: {n['heading'][:45]}{anom}")
            if n.get("children"):
                print_tree(n["children"], depth + 1)

    print_tree(sections)

    resp_rep = client.get(f"/documents/{doc_id}/ingestion-report?version=1")
    rep = resp_rep.json()
    print(f"  Coverage: {rep['coverage']['coverage_percentage']}% (Passed: {rep['coverage']['passed']})")
    print(f"  Tables Found: {len(rep['tables_found'])}, Anomalies: {len(rep['anomalies'])}")

    # Step 3: Create Selection (Sections 3.2, 4.2, 6.1)
    print("\n[STEP 3] Creating Pinned Selection for Sections 3.2, 4.2, 6.1...")
    nodes_v1 = {n.number_path: n for n in db.query(Node).all()}
    selected_node_ids = [
        nodes_v1["3.2"].node_version_id,
        nodes_v1["4.2"].node_version_id,
        nodes_v1["6.1"].node_version_id,
    ]

    resp_sel = client.post(
        "/selections", json={"name": "Core Safety & Operation Selection", "node_ids": selected_node_ids}
    )
    assert resp_sel.status_code == 201
    sel = resp_sel.json()
    sel_id = sel["id"]
    print(f"  Selection Created: ID={sel_id}, Fingerprint={sel['fingerprint'][:16]}...")
    for item in sel["items"]:
        print(f"    - {item['number_path']}: {item['heading']} (hash: {item['content_hash'][:8]}...)")

    # Step 4: Generate QA Test Cases
    print("\n[STEP 4] Generating QA Test Cases from Selection via LLM...")
    items_map = {item["number_path"]: item["logical_id"] for item in sel["items"]}
    mock_payload = {
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
                        "logical_id": items_map["3.2"],
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
                        "logical_id": items_map["4.2"],
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
                        "logical_id": items_map["6.1"],
                        "verbatim_quote": "Wipe the device body and cuff exterior with a soft, dry cloth or one lightly dampened with water.",
                    }
                ],
            },
        ]
    }
    mock_llm.add_response(json.dumps(mock_payload))

    resp_gen = client.post(f"/selections/{sel_id}/generate")
    assert resp_gen.status_code == 200
    gen = resp_gen.json()
    print(f"  Generated {len(gen['test_cases'])} Test Cases (Status: {gen['status']}):")
    for idx, tc in enumerate(gen["test_cases"], 1):
        print(f"    {idx}. [{tc['category'].upper()}] {tc['title']}")
        print(f'       Quote: "{tc["source_refs"][0]["verbatim_quote"][:60]}..."')

    # Step 5: Ingest Document v2
    print("\n[STEP 5] Ingesting Document v2 (ct200_manual_v2.pdf)...")
    with open(pdf_v2, "rb") as f:
        resp_v2 = client.post(
            f"/documents/{doc_id}/versions",
            files={"file": ("ct200_manual_v2.pdf", f, "application/pdf")},
        )
    assert resp_v2.status_code == 201
    v2_data = resp_v2.json()
    print(f"  Ingested Version: v{v2_data['version_number']} (sha256: {v2_data['pdf_sha256'][:8]}...)")

    # Step 6: Show Node Changes & Diffs
    print("\n[STEP 6] Inspecting Version Changes (v1 -> v2)...")
    for sec_path in ["2.1.1.1", "3.2", "4.2", "4.3", "5.3", "6.1"]:
        node = db.query(Node).filter(Node.version_id == v2_data["id"], Node.number_path == sec_path).first()
        if node:
            resp_diff = client.get(f"/nodes/{node.node_version_id}/changes?from=1&to=2")
            d = resp_diff.json()
            num_summary = ""
            if d.get("numeric_changes"):
                formatted_nums = [
                    str(n["old_value"]) + "->" + str(n["new_value"]) + " " + n["unit"] for n in d["numeric_changes"]
                ]
                num_summary = f" (numeric: {formatted_nums})"
            print(f"  Section {sec_path}: changed={d['changed']}, class={d['change_class']}{num_summary}")

    # Step 7: Fetch Generations Showing Read-Time Staleness
    print("\n[STEP 7] Fetching Generation with Read-Time Computed Staleness...")
    resp_staleness = client.get(f"/selections/{sel_id}/generations")
    assert resp_staleness.status_code == 200
    gens = resp_staleness.json()
    latest_gen = gens[0]
    st = latest_gen["staleness"]

    print(f"  Generation Overall Staleness: {st['status'].upper()}")
    print(f"  Notice: {st['limits']}")
    print("  Per-Test-Case Staleness:")
    for idx, tc in enumerate(st["test_cases"], 1):
        print(f"    {idx}. {tc['test_case_title']}")
        print(f"       Status: {tc['status']}")
        for detail in tc["details"]:
            if detail.get("numeric_changes"):
                changes_str = ", ".join(
                    f"{c['old_value']} -> {c['new_value']} {c['unit']}" for c in detail["numeric_changes"]
                )
                print(f"       Details: {detail['number_path']} changed: {changes_str}")

    print("\n" + "=" * 70)
    print("DEMO RUN COMPLETE: ALL STEPS VERIFIED SUCCESSFULLY")
    print("=" * 70)


if __name__ == "__main__":
    run_demo()
