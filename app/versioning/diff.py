"""app/versioning/diff.py
Word-level diff summary, numeric token diffing, and change classification.
"""

import difflib
import logging
from typing import Any

from app.models.schemas import ChangeClass, NodeDiffResponse, NumericDiff
from app.parsing.normalize import extract_numeric_tokens, normalize_text

logger = logging.getLogger(__name__)


def _extract_all_numeric_tokens(node: Any) -> list[dict]:
    """Extract numeric tokens from node heading, body, and tables."""
    text_parts = [getattr(node, "heading", ""), getattr(node, "body_text", "")]
    tables = getattr(node, "tables", []) or []
    for table in tables:
        if isinstance(table, dict):
            rows = table.get("rows", [])
            for r in rows:
                cells = r.get("cells", []) if isinstance(r, dict) else getattr(r, "cells", [])
                text_parts.extend(cells)
        else:
            rows = getattr(table, "rows", [])
            for r in rows:
                cells = getattr(r, "cells", [])
                text_parts.extend(cells)

    combined_text = " ".join(text_parts)
    return extract_numeric_tokens(combined_text)


def compare_numeric_tokens(old_tokens: list[dict], new_tokens: list[dict]) -> list[NumericDiff]:
    """Find differences between old and new numeric tokens."""
    diffs: list[NumericDiff] = []

    # Map by (unit, raw) or index
    old_raws = [t["raw"] for t in old_tokens]
    new_raws = [t["raw"] for t in new_tokens]

    # Find tokens in old not in new, and vice-versa
    removed = [t for t in old_tokens if t["raw"] not in new_raws]
    added = [t for t in new_tokens if t["raw"] not in old_raws]

    # Pair them up if possible (same unit)
    paired_added = set()
    for rem in removed:
        # Look for replacement with same unit
        match_add = None
        for i, add in enumerate(added):
            if i not in paired_added and (add["unit"] == rem["unit"] or not rem["unit"]):
                match_add = add
                paired_added.add(i)
                break

        if match_add:
            diffs.append(
                NumericDiff(
                    old_value=rem["value"],
                    new_value=match_add["value"],
                    unit=rem["unit"] or match_add["unit"],
                    context=f"{rem['raw']} -> {match_add['raw']}",
                )
            )
        else:
            diffs.append(
                NumericDiff(
                    old_value=rem["value"],
                    new_value=None,
                    unit=rem["unit"],
                    context=f"Removed: {rem['raw']}",
                )
            )

    for i, add in enumerate(added):
        if i not in paired_added:
            diffs.append(
                NumericDiff(
                    old_value=None,
                    new_value=add["value"],
                    unit=add["unit"],
                    context=f"Added: {add['raw']}",
                )
            )

    return diffs


def generate_word_diff(old_text: str, new_text: str) -> str:
    """Generate a clean, readable word-level diff summary."""
    old_words = normalize_text(old_text).split()
    new_words = normalize_text(new_text).split()

    matcher = difflib.SequenceMatcher(None, old_words, new_words)
    diff_parts = []

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "replace":
            old_chunk = " ".join(old_words[i1:i2])
            new_chunk = " ".join(new_words[j1:j2])
            diff_parts.append(f"[-{old_chunk}-] [+{new_chunk}+]")
        elif tag == "delete":
            old_chunk = " ".join(old_words[i1:i2])
            diff_parts.append(f"[-{old_chunk}-]")
        elif tag == "insert":
            new_chunk = " ".join(new_words[j1:j2])
            diff_parts.append(f"[+{new_chunk}+]")

    return " ".join(diff_parts) if diff_parts else "No textual differences"


def compute_node_diff(
    old_node: Any | None,
    new_node: Any | None,
    from_version: int = 1,
    to_version: int = 2,
) -> NodeDiffResponse:
    """Compute detailed diff between two versions of a node."""
    if old_node is None and new_node is None:
        raise ValueError("Both old_node and new_node cannot be None")

    if old_node is None:
        return NodeDiffResponse(
            node_id=getattr(new_node, "node_version_id", ""),
            logical_id=getattr(new_node, "logical_id", ""),
            from_version=from_version,
            to_version=to_version,
            changed=True,
            change_class=ChangeClass.STRUCTURAL,
            word_diff_summary="Node newly added in this version",
            numeric_changes=[],
            old_hash="",
            new_hash=getattr(new_node, "content_hash", ""),
        )

    if new_node is None:
        return NodeDiffResponse(
            node_id=getattr(old_node, "node_version_id", ""),
            logical_id=getattr(old_node, "logical_id", ""),
            from_version=from_version,
            to_version=to_version,
            changed=True,
            change_class=ChangeClass.REMOVED,
            word_diff_summary="Node removed in this version",
            numeric_changes=[],
            old_hash=getattr(old_node, "content_hash", ""),
            new_hash="",
        )

    old_hash = getattr(old_node, "content_hash", "")
    new_hash = getattr(new_node, "content_hash", "")
    logical_id = getattr(new_node, "logical_id", getattr(old_node, "logical_id", ""))
    node_id = getattr(new_node, "node_version_id", "")

    if old_hash == new_hash:
        return NodeDiffResponse(
            node_id=node_id,
            logical_id=logical_id,
            from_version=from_version,
            to_version=to_version,
            changed=False,
            change_class=ChangeClass.UNCHANGED,
            word_diff_summary="No changes",
            numeric_changes=[],
            old_hash=old_hash,
            new_hash=new_hash,
        )

    # Content changed - classify the change
    old_tokens = _extract_all_numeric_tokens(old_node)
    new_tokens = _extract_all_numeric_tokens(new_node)
    num_diffs = compare_numeric_tokens(old_tokens, new_tokens)

    old_body = getattr(old_node, "body_text", "")
    new_body = getattr(new_node, "body_text", "")
    word_diff = generate_word_diff(old_body, new_body)

    # Classify
    if num_diffs:
        change_class = ChangeClass.NUMERIC
    elif (
        len(getattr(old_node, "tables", []) or []) != len(getattr(new_node, "tables", []) or [])
        or getattr(old_node, "level", 0) != getattr(new_node, "level", 0)
        or getattr(old_node, "parent_id", None) != getattr(new_node, "parent_id", None)
    ):
        change_class = ChangeClass.STRUCTURAL
    else:
        change_class = ChangeClass.WORDING

    return NodeDiffResponse(
        node_id=node_id,
        logical_id=logical_id,
        from_version=from_version,
        to_version=to_version,
        changed=True,
        change_class=change_class,
        word_diff_summary=word_diff,
        numeric_changes=num_diffs,
        old_hash=old_hash,
        new_hash=new_hash,
    )
