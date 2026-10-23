"""app/llm/validation.py
Validation for LLM responses: markdown stripping, JSON parsing, schema checks, and quote verification.
"""

import json
import logging
import re
from typing import Any

from app.models.schemas import TestCaseSchema
from app.parsing.normalize import normalize_text

logger = logging.getLogger(__name__)


def strip_markdown_fences(raw: str) -> str:
    """Remove ```json and ``` markdown code fences."""
    text = raw.strip()
    if text.startswith("```"):
        # Remove opening fence
        text = re.sub(r"^```(?:json)?\s*\n?", "", text, flags=re.IGNORECASE)
        # Remove closing fence
        text = re.sub(r"\n?```\s*$", "", text)
    return text.strip()


def validate_test_cases(
    raw_response: str,
    selection_nodes: dict[str, str],  # logical_id -> normalized source text
) -> tuple[list[TestCaseSchema], list[dict[str, Any]], str]:
    """Validate LLM output.

    Returns:
    - valid_cases: list of validated TestCaseSchema objects
    - dropped_cases: list of dicts with title, raw data, and reason
    - error_message: overall validation error message (empty if all valid)
    """
    cleaned_json_str = strip_markdown_fences(raw_response)

    try:
        data = json.loads(cleaned_json_str)
    except json.JSONDecodeError as e:
        return [], [], f"Failed to parse JSON response: {e}"

    if not isinstance(data, dict) or "test_cases" not in data or not isinstance(data["test_cases"], list):
        return [], [], "Response JSON must be an object with a 'test_cases' list."

    raw_cases = data["test_cases"]

    valid_cases: list[TestCaseSchema] = []
    dropped_cases: list[dict[str, Any]] = []

    for idx, case_raw in enumerate(raw_cases):
        try:
            # 1. Pydantic schema validation
            test_case = TestCaseSchema.model_validate(case_raw)

            # 2. Verify steps non-empty
            if not test_case.steps or not any(s.strip() for s in test_case.steps):
                dropped_cases.append(
                    {
                        "index": idx,
                        "title": getattr(test_case, "title", f"Case {idx + 1}"),
                        "reason": "Test case steps must be non-empty.",
                    }
                )
                continue

            # 3. Verify source_refs and verbatim quotes
            invalid_quote_reason = None
            for ref in test_case.source_refs:
                if ref.logical_id not in selection_nodes:
                    invalid_quote_reason = f"Source ref logical_id '{ref.logical_id}' not found in current selection."
                    break

                source_text = selection_nodes[ref.logical_id]
                norm_quote = normalize_text(ref.verbatim_quote)
                norm_source = normalize_text(source_text)

                if norm_quote not in norm_source:
                    invalid_quote_reason = (
                        f"Verbatim quote '{ref.verbatim_quote[:60]}...' is not present in section "
                        f"'{ref.logical_id}' source text (fabricated quote rejected)."
                    )
                    break

            if invalid_quote_reason:
                dropped_cases.append(
                    {
                        "index": idx,
                        "title": test_case.title,
                        "reason": invalid_quote_reason,
                    }
                )
                continue

            valid_cases.append(test_case)

        except Exception as e:
            dropped_cases.append(
                {
                    "index": idx,
                    "title": case_raw.get("title", f"Case {idx + 1}")
                    if isinstance(case_raw, dict)
                    else f"Case {idx + 1}",
                    "reason": f"Schema validation error: {e}",
                }
            )

    # Check total count constraint (3 to 5 cases)
    error_msg = ""
    if len(valid_cases) < 3:
        reasons = "; ".join(f"{d['title']}: {d['reason']}" for d in dropped_cases)
        error_msg = (
            f"Expected between 3 and 5 valid test cases, but only {len(valid_cases)} passed validation. "
            f"Reasons for dropped cases: {reasons}"
        )
    elif len(valid_cases) > 5:
        # Trim to 5 valid cases
        extra = valid_cases[5:]
        valid_cases = valid_cases[:5]
        for c in extra:
            dropped_cases.append(
                {
                    "index": -1,
                    "title": c.title,
                    "reason": "Trimmed to stay within maximum 5 test cases limit.",
                }
            )

    return valid_cases, dropped_cases, error_msg
