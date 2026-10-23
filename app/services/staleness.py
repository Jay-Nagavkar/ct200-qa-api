"""app/services/staleness.py
Read-time staleness computation for source nodes, test cases, and generations.
"""

import logging

from sqlalchemy.orm import Session

from app.models.schemas import (
    ChangeClass,
    GenerationStaleness,
    NodeStaleness,
    TestCaseSchema,
    TestCaseStaleness,
)
from app.models.sql import DocumentVersion, Node
from app.versioning.diff import compute_node_diff

logger = logging.getLogger(__name__)

SEVERITY_ORDER = {
    "source_removed": 5,
    "stale_numeric": 4,
    "stale_structural": 3,
    "stale_wording": 2,
    "dependency_changed": 1,
    "fresh": 0,
}


def compute_node_staleness(
    db: Session,
    document_id: str,
    logical_id: str,
    snapshot_hash: str,
    snapshot_node_version_id: str | None = None,
) -> NodeStaleness:
    """Compute staleness status for a single source node snapshot against latest version."""
    # Find latest version for document
    latest_version = (
        db.query(DocumentVersion)
        .filter(DocumentVersion.document_id == document_id)
        .order_by(DocumentVersion.version_number.desc())
        .first()
    )
    if not latest_version:
        return NodeStaleness(
            logical_id=logical_id,
            number_path="",
            status="source_removed",
        )

    # Find node in latest version
    latest_node = db.query(Node).filter(Node.version_id == latest_version.id, Node.logical_id == logical_id).first()
    if not latest_node:
        return NodeStaleness(
            logical_id=logical_id,
            number_path="",
            status="source_removed",
        )

    # Retrieve snapshot node for diffing
    snapshot_node = None
    if snapshot_node_version_id:
        snapshot_node = db.query(Node).filter(Node.node_version_id == snapshot_node_version_id).first()

    number_path = latest_node.number_path or (snapshot_node.number_path if snapshot_node else "")

    if latest_node.content_hash == snapshot_hash:
        # Check cross-reference dependencies!
        # If any node referenced by this node's cross_refs changed, flag dependency_changed
        cross_refs = latest_node.cross_refs or (snapshot_node.cross_refs if snapshot_node else [])
        if cross_refs:
            # Check if any referenced node changed between v1 and latest
            ref_changed = False
            for ref_path in cross_refs:
                ref_latest = (
                    db.query(Node).filter(Node.version_id == latest_version.id, Node.number_path == ref_path).first()
                )
                if ref_latest:
                    # Look up earlier version of this ref node
                    ref_earlier = (
                        db.query(Node)
                        .filter(Node.logical_id == ref_latest.logical_id)
                        .order_by(Node.document_order.asc())
                        .first()
                    )
                    if ref_earlier and ref_earlier.content_hash != ref_latest.content_hash:
                        ref_changed = True
                        break

            if ref_changed:
                return NodeStaleness(
                    logical_id=logical_id,
                    number_path=number_path,
                    status="dependency_changed",
                    word_diff_summary="Referenced section content has changed in latest version.",
                )

        return NodeStaleness(
            logical_id=logical_id,
            number_path=number_path,
            status="fresh",
            change_class=ChangeClass.UNCHANGED,
            word_diff_summary="Unchanged",
            numeric_changes=[],
        )

    # Content changed - compute diff to determine specific staleness
    diff = compute_node_diff(snapshot_node, latest_node)

    if diff.change_class == ChangeClass.NUMERIC:
        status = "stale_numeric"
    elif diff.change_class == ChangeClass.STRUCTURAL:
        status = "stale_structural"
    elif diff.change_class == ChangeClass.WORDING:
        status = "stale_wording"
    else:
        status = "stale_wording"

    return NodeStaleness(
        logical_id=logical_id,
        number_path=number_path,
        status=status,
        change_class=diff.change_class,
        word_diff_summary=diff.word_diff_summary,
        numeric_changes=diff.numeric_changes,
    )


def compute_generation_staleness(
    db: Session,
    document_id: str,
    source_nodes: list[dict],
    test_cases: list[TestCaseSchema],
) -> GenerationStaleness:
    """Compute staleness at read time for all test cases and source nodes."""
    node_staleness_map: dict[str, NodeStaleness] = {}
    source_node_staleness: list[NodeStaleness] = []

    for sn in source_nodes:
        lid = sn.get("logical_id", "")
        sn_hash = sn.get("content_hash", "")
        sn_nvid = sn.get("node_version_id")

        st = compute_node_staleness(
            db=db,
            document_id=document_id,
            logical_id=lid,
            snapshot_hash=sn_hash,
            snapshot_node_version_id=sn_nvid,
        )
        node_staleness_map[lid] = st
        source_node_staleness.append(st)

    # Compute per-test case staleness (worst cited node wins)
    case_staleness_list: list[TestCaseStaleness] = []
    has_fresh = False
    has_stale = False

    for tc in test_cases:
        cited_logical_ids = [ref.logical_id for ref in tc.source_refs]
        cited_nodes = [node_staleness_map[lid] for lid in cited_logical_ids if lid in node_staleness_map]

        if not cited_nodes:
            worst_status = "fresh"
        else:
            # Sort by severity descending
            worst_node = max(cited_nodes, key=lambda n: SEVERITY_ORDER.get(n.status, 0))
            worst_status = worst_node.status

        if worst_status == "fresh":
            has_fresh = True
        else:
            has_stale = True

        case_staleness_list.append(
            TestCaseStaleness(
                test_case_title=tc.title,
                status=worst_status,
                worst_change_class=worst_status,
                details=cited_nodes,
            )
        )

    # Determine generation-level status
    if not has_stale:
        gen_status = "fresh"
    elif has_fresh and has_stale:
        gen_status = "partially_stale"
    else:
        gen_status = "stale"

    return GenerationStaleness(
        status=gen_status,
        test_cases=case_staleness_list,
        source_nodes=source_node_staleness,
    )
