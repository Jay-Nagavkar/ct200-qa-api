"""app/services/generation.py
Orchestrates test case generation with LLM retries, validation, Mongo storage, and idempotency.
"""

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.config import get_settings
from app.llm.client import get_llm_client
from app.llm.prompts import PROMPT_VERSION, SYSTEM_PROMPT, build_user_prompt
from app.llm.validation import validate_test_cases
from app.models.schemas import GenerationResponse, TestCaseSchema
from app.models.sql import Selection, SelectionItem
from app.mongo import get_mongo_db

logger = logging.getLogger(__name__)
settings = get_settings()


def generate_test_cases(
    db: Session,
    selection_id: str,
    force: bool = False,
) -> GenerationResponse:
    """Generate 3 to 5 QA test cases for an immutable selection.

    Idempotency:
    - If force=False and a generation for (selection_fingerprint, prompt_version, model) exists with status 'ok'/'partial', returns it.
    - If force=True, creates a brand new generation record (append-only history).
    """
    # 1. Resolve selection and items
    selection = db.query(Selection).filter(Selection.id == selection_id).first()
    if not selection:
        raise HTTPException(status_code=404, detail=f"Selection '{selection_id}' not found.")

    items = db.query(SelectionItem).filter(SelectionItem.selection_id == selection.id).all()
    if not items:
        raise HTTPException(status_code=400, detail="Selection has no pinned items.")

    mongo_db = get_mongo_db()
    generations_col = mongo_db["generations"]

    model_name = settings.LLM_MODEL
    fingerprint = selection.fingerprint

    # 2. Idempotency check
    if not force:
        cached = generations_col.find_one(
            {
                "source_fingerprint": fingerprint,
                "prompt_version": PROMPT_VERSION,
                "model": model_name,
                "status": {"$in": ["ok", "partial"]},
            },
            sort=[("created_at", -1)],
        )
        if cached:
            logger.info("Returning cached generation '%s' for selection '%s'", cached["generation_id"], selection_id)
            return GenerationResponse(
                generation_id=cached["generation_id"],
                selection_id=selection_id,
                status=cached["status"],
                model=cached["model"],
                prompt_version=cached["prompt_version"],
                created_at=cached["created_at"],
                test_cases=[TestCaseSchema.model_validate(c) for c in cached.get("test_cases", [])],
                dropped_cases=cached.get("dropped_cases", []),
                error=cached.get("error"),
            )

    # 3. Prepare source nodes payload
    source_nodes = [
        {
            "logical_id": item.logical_id,
            "node_version_id": item.node_version_id,
            "number_path": item.number_path,
            "heading": item.heading,
            "content_hash": item.content_hash,
            "text_snapshot": item.text_snapshot,
        }
        for item in items
    ]
    node_text_map = {item.logical_id: item.text_snapshot for item in items}

    # 4. Generate with up to 2 retries on validation failure
    llm = get_llm_client()
    attempts: list[dict[str, Any]] = []
    current_feedback = ""
    valid_cases: list[TestCaseSchema] = []
    dropped_cases: list[dict[str, Any]] = []
    final_error = ""

    MAX_RETRIES = 2
    for attempt_idx in range(MAX_RETRIES + 1):
        user_prompt = build_user_prompt(source_nodes, feedback_error=current_feedback)

        try:
            raw_response = llm.generate(SYSTEM_PROMPT, user_prompt)
        except HTTPException:
            raise
        except Exception as e:
            logger.error("LLM generation error on attempt %d: %s", attempt_idx + 1, e)
            attempts.append(
                {
                    "attempt": attempt_idx + 1,
                    "error": str(e),
                    "raw_response": None,
                }
            )
            final_error = f"LLM error: {e}"
            break

        valid_cases, dropped_cases, validation_error = validate_test_cases(raw_response, node_text_map)

        attempts.append(
            {
                "attempt": attempt_idx + 1,
                "raw_response": raw_response,
                "valid_count": len(valid_cases),
                "dropped_count": len(dropped_cases),
                "validation_error": validation_error,
            }
        )

        if not validation_error and 3 <= len(valid_cases) <= 5:
            # Full success
            final_error = ""
            break
        elif len(valid_cases) >= 3:
            # Partial success (some dropped, but at least 3 valid remain)
            final_error = ""
            break
        else:
            current_feedback = validation_error
            final_error = validation_error
            logger.warning("Attempt %d failed validation: %s. Retrying...", attempt_idx + 1, validation_error)

    # 5. Determine status and record Mongo document
    generation_id = str(uuid.uuid4())
    now = datetime.now(UTC)

    if len(valid_cases) >= 3:
        status = "ok" if not dropped_cases else "partial"
    else:
        status = "failed"

    mongo_doc = {
        "generation_id": generation_id,
        "selection_id": selection_id,
        "status": status,
        "model": model_name,
        "prompt_version": PROMPT_VERSION,
        "attempts": attempts,
        "source_nodes": source_nodes,
        "source_fingerprint": fingerprint,
        "test_cases": [c.model_dump() for c in valid_cases],
        "dropped_cases": dropped_cases,
        "error": final_error if status == "failed" else None,
        "created_at": now,
    }
    generations_col.insert_one(mongo_doc)

    if status == "failed":
        logger.error("Generation failed after retries: %s", final_error)
        raise HTTPException(
            status_code=502,
            detail=f"QA test case generation failed validation after retries: {final_error}",
        )

    logger.info("Generated %d valid test cases (status=%s) for selection '%s'", len(valid_cases), status, selection_id)

    return GenerationResponse(
        generation_id=generation_id,
        selection_id=selection_id,
        status=status,
        model=model_name,
        prompt_version=PROMPT_VERSION,
        created_at=now,
        test_cases=valid_cases,
        dropped_cases=dropped_cases,
    )
