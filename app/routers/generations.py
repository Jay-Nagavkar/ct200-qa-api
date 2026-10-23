"""app/routers/generations.py
Endpoints for QA test-case generation and retrieval with read-time staleness blocks.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.schemas import GenerationResponse, TestCaseSchema
from app.models.sql import Node, Selection
from app.mongo import get_mongo_db
from app.services.generation import generate_test_cases
from app.services.staleness import compute_generation_staleness

router = APIRouter(tags=["generations"])


@router.post("/selections/{selection_id}/generate", response_model=GenerationResponse)
def post_generate(
    selection_id: str,
    force: bool = Query(False, description="Force new generation (bypasses idempotency cache)"),
    db: Session = Depends(get_db),
):
    """Generate 3 to 5 QA test cases for an immutable node selection."""
    return generate_test_cases(db, selection_id=selection_id, force=force)


@router.get("/selections/{selection_id}/generations", response_model=list[GenerationResponse])
def get_selection_generations(
    selection_id: str,
    db: Session = Depends(get_db),
):
    """Retrieve all generations for a selection with read-time computed staleness."""
    selection = db.query(Selection).filter(Selection.id == selection_id).first()
    if not selection:
        raise HTTPException(status_code=404, detail="Selection not found.")

    mongo_db = get_mongo_db()
    docs = list(mongo_db["generations"].find({"selection_id": selection_id}).sort("created_at", -1))

    responses = []
    for doc in docs:
        test_cases = [TestCaseSchema.model_validate(c) for c in doc.get("test_cases", [])]
        source_nodes = doc.get("source_nodes", [])

        # Compute staleness at read time
        staleness = compute_generation_staleness(
            db=db,
            document_id=selection.document_id,
            source_nodes=source_nodes,
            test_cases=test_cases,
        )

        responses.append(
            GenerationResponse(
                generation_id=doc["generation_id"],
                selection_id=selection_id,
                status=doc["status"],
                model=doc["model"],
                prompt_version=doc["prompt_version"],
                created_at=doc["created_at"],
                test_cases=test_cases,
                staleness=staleness,
                dropped_cases=doc.get("dropped_cases", []),
                error=doc.get("error"),
            )
        )

    return responses


@router.get("/nodes/{node_id}/generations", response_model=list[GenerationResponse])
def get_node_generations(
    node_id: str,
    db: Session = Depends(get_db),
):
    """Retrieve generations citing a specific node (by logical_id or node_version_id)."""
    node = db.query(Node).filter((Node.node_version_id == node_id) | (Node.logical_id == node_id)).first()
    if not node:
        raise HTTPException(status_code=404, detail="Node not found.")

    logical_id = node.logical_id

    mongo_db = get_mongo_db()
    # Find generations where any source node or test case source ref matches logical_id
    docs = list(
        mongo_db["generations"]
        .find(
            {
                "$or": [
                    {"source_nodes.logical_id": logical_id},
                    {"test_cases.source_refs.logical_id": logical_id},
                ]
            }
        )
        .sort("created_at", -1)
    )

    responses = []
    for doc in docs:
        test_cases = [TestCaseSchema.model_validate(c) for c in doc.get("test_cases", [])]
        source_nodes = doc.get("source_nodes", [])

        # Find document_id from selection
        selection = db.query(Selection).filter(Selection.id == doc["selection_id"]).first()
        doc_id = selection.document_id if selection else ""

        staleness = compute_generation_staleness(
            db=db,
            document_id=doc_id,
            source_nodes=source_nodes,
            test_cases=test_cases,
        )

        responses.append(
            GenerationResponse(
                generation_id=doc["generation_id"],
                selection_id=doc["selection_id"],
                status=doc["status"],
                model=doc["model"],
                prompt_version=doc["prompt_version"],
                created_at=doc["created_at"],
                test_cases=test_cases,
                staleness=staleness,
                dropped_cases=doc.get("dropped_cases", []),
                error=doc.get("error"),
            )
        )

    return responses
