"""app/versioning/matcher.py
Matches nodes across document versions to maintain stable logical IDs.

Matching Strategy (in strict order):
1. Same normalized number_path AND normalized title (exact match)
2. Same matched parent + title similarity >= 0.9 (using RapidFuzz)
3. Fuzzy body similarity >= 0.85 for orphans (using RapidFuzz)
4. Otherwise: added (new node) or removed (old node without successor)

Equal content_hash = unchanged, different content_hash = modified.
"""

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from rapidfuzz import fuzz

from app.models.schemas import MatchMethod
from app.parsing.normalize import normalize_for_hash
from app.parsing.tree import ParsedNode

logger = logging.getLogger(__name__)


@dataclass
class NodeMatch:
    """Record of a match between an old node and a new node."""

    logical_id: str
    number_path: str
    heading: str
    old_node_id: str | None
    new_node_index: int | None  # index in new_nodes list
    method: MatchMethod
    similarity: float
    status: str  # "unchanged", "modified", "added", "removed"
    old_hash: str | None = None
    new_hash: str | None = None


@dataclass
class MatchResult:
    """Complete result of version matching."""

    matches: list[NodeMatch] = field(default_factory=list)
    new_node_logical_ids: dict[int, str] = field(default_factory=dict)  # new_index -> logical_id
    added_count: int = 0
    modified_count: int = 0
    unchanged_count: int = 0
    removed_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": {
                "total_new_nodes": len(self.new_node_logical_ids),
                "added": self.added_count,
                "modified": self.modified_count,
                "unchanged": self.unchanged_count,
                "removed": self.removed_count,
            },
            "matches": [
                {
                    "logical_id": m.logical_id,
                    "number_path": m.number_path,
                    "heading": m.heading,
                    "old_node_id": m.old_node_id,
                    "method": m.method.value,
                    "similarity": round(m.similarity, 3),
                    "status": m.status,
                    "old_hash": m.old_hash,
                    "new_hash": m.new_hash,
                }
                for m in self.matches
            ],
        }


def match_nodes(
    old_nodes: list[
        Any
    ],  # List of Node DB models or objects with logical_id, number_path, heading, body_text, content_hash
    new_nodes: list[ParsedNode],
) -> MatchResult:
    """Match nodes from old version to new version."""
    result = MatchResult()

    unmatched_old = {i: n for i, n in enumerate(old_nodes)}
    unmatched_new = {i: n for i, n in enumerate(new_nodes)}
    matched_new_to_old: dict[int, int] = {}  # new_idx -> old_idx

    # Rule 1: Same normalized number_path AND normalized title
    for new_idx, new_node in list(unmatched_new.items()):
        new_norm_path = normalize_for_hash(new_node.number_path)
        new_norm_title = normalize_for_hash(new_node.heading)

        best_old_idx = None
        for old_idx, old_node in unmatched_old.items():
            old_norm_path = normalize_for_hash(old_node.number_path)
            old_norm_title = normalize_for_hash(old_node.heading)

            if new_norm_path == old_norm_path and new_norm_title == old_norm_title:
                best_old_idx = old_idx
                break

        if best_old_idx is not None:
            old_node = unmatched_old.pop(best_old_idx)
            unmatched_new.pop(new_idx)
            matched_new_to_old[new_idx] = best_old_idx

            status = "unchanged" if old_node.content_hash == new_node.content_hash else "modified"
            match = NodeMatch(
                logical_id=old_node.logical_id,
                number_path=new_node.number_path,
                heading=new_node.heading,
                old_node_id=getattr(old_node, "node_version_id", str(old_node.logical_id)),
                new_node_index=new_idx,
                method=MatchMethod.EXACT_PATH_TITLE,
                similarity=1.0,
                status=status,
                old_hash=old_node.content_hash,
                new_hash=new_node.content_hash,
            )
            result.matches.append(match)
            result.new_node_logical_ids[new_idx] = old_node.logical_id
            if status == "unchanged":
                result.unchanged_count += 1
            else:
                result.modified_count += 1

    # Rule 2: Same matched parent + title similarity >= 0.9
    for new_idx, new_node in list(unmatched_new.items()):
        new_parent_path = new_node.parent_number_path or ""
        # Find logical_id of new node's parent if already matched
        new_parent_logical = None
        for n_idx, o_idx in matched_new_to_old.items():
            if new_nodes[n_idx].number_path == new_parent_path:
                new_parent_logical = old_nodes[o_idx].logical_id
                break

        best_score = 0.0
        best_old_idx = None

        for old_idx, old_node in unmatched_old.items():
            old_parent_logical = getattr(old_node, "parent_logical_id", None)
            # If parents match or parent paths match
            parents_match = (new_parent_logical and old_parent_logical == new_parent_logical) or (
                new_parent_path and getattr(old_node, "parent_number_path", "") == new_parent_path
            )
            if parents_match:
                score = fuzz.ratio(normalize_for_hash(old_node.heading), normalize_for_hash(new_node.heading)) / 100.0
                if score >= 0.9 and score > best_score:
                    best_score = score
                    best_old_idx = old_idx

        if best_old_idx is not None:
            old_node = unmatched_old.pop(best_old_idx)
            unmatched_new.pop(new_idx)
            matched_new_to_old[new_idx] = best_old_idx

            status = "unchanged" if old_node.content_hash == new_node.content_hash else "modified"
            match = NodeMatch(
                logical_id=old_node.logical_id,
                number_path=new_node.number_path,
                heading=new_node.heading,
                old_node_id=getattr(old_node, "node_version_id", str(old_node.logical_id)),
                new_node_index=new_idx,
                method=MatchMethod.MATCHED_PARENT_TITLE,
                similarity=best_score,
                status=status,
                old_hash=old_node.content_hash,
                new_hash=new_node.content_hash,
            )
            result.matches.append(match)
            result.new_node_logical_ids[new_idx] = old_node.logical_id
            if status == "unchanged":
                result.unchanged_count += 1
            else:
                result.modified_count += 1

    # Rule 3: Fuzzy body similarity >= 0.85 for orphans
    for new_idx, new_node in list(unmatched_new.items()):
        if not new_node.body_text:
            continue

        best_score = 0.0
        best_old_idx = None

        for old_idx, old_node in unmatched_old.items():
            if not old_node.body_text:
                continue
            score = (
                fuzz.token_set_ratio(normalize_for_hash(old_node.body_text), normalize_for_hash(new_node.body_text))
                / 100.0
            )
            if score >= 0.85 and score > best_score:
                best_score = score
                best_old_idx = old_idx

        if best_old_idx is not None:
            old_node = unmatched_old.pop(best_old_idx)
            unmatched_new.pop(new_idx)
            matched_new_to_old[new_idx] = best_old_idx

            status = "unchanged" if old_node.content_hash == new_node.content_hash else "modified"
            match = NodeMatch(
                logical_id=old_node.logical_id,
                number_path=new_node.number_path,
                heading=new_node.heading,
                old_node_id=getattr(old_node, "node_version_id", str(old_node.logical_id)),
                new_node_index=new_idx,
                method=MatchMethod.FUZZY_BODY,
                similarity=best_score,
                status=status,
                old_hash=old_node.content_hash,
                new_hash=new_node.content_hash,
            )
            result.matches.append(match)
            result.new_node_logical_ids[new_idx] = old_node.logical_id
            if status == "unchanged":
                result.unchanged_count += 1
            else:
                result.modified_count += 1

    # Remaining new nodes are ADDED
    for new_idx, new_node in unmatched_new.items():
        new_logical_id = str(uuid.uuid4())
        match = NodeMatch(
            logical_id=new_logical_id,
            number_path=new_node.number_path,
            heading=new_node.heading,
            old_node_id=None,
            new_node_index=new_idx,
            method=MatchMethod.ADDED,
            similarity=0.0,
            status="added",
            old_hash=None,
            new_hash=new_node.content_hash,
        )
        result.matches.append(match)
        result.new_node_logical_ids[new_idx] = new_logical_id
        result.added_count += 1

    # Remaining old nodes are REMOVED
    for old_idx, old_node in unmatched_old.items():
        match = NodeMatch(
            logical_id=old_node.logical_id,
            number_path=old_node.number_path,
            heading=old_node.heading,
            old_node_id=getattr(old_node, "node_version_id", str(old_node.logical_id)),
            new_node_index=None,
            method=MatchMethod.REMOVED,
            similarity=0.0,
            status="removed",
            old_hash=old_node.content_hash,
            new_hash=None,
        )
        result.matches.append(match)
        result.removed_count += 1

    logger.info(
        "Version matching complete: %d unchanged, %d modified, %d added, %d removed",
        result.unchanged_count,
        result.modified_count,
        result.added_count,
        result.removed_count,
    )
    return result
