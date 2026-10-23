"""app/routers/nodes.py
Node inspection and change tracking endpoints.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.schemas import NodeDetailResponse, NodeDiffResponse
from app.models.sql import DocumentVersion, Node
from app.versioning.diff import compute_node_diff

router = APIRouter(prefix="/nodes", tags=["nodes"])


@router.get("/{node_version_id}", response_model=NodeDetailResponse)
def get_node_detail(node_version_id: str, db: Session = Depends(get_db)):
    """Retrieve full details of a specific node version."""
    node = db.query(Node).filter(Node.node_version_id == node_version_id).first()
    if not node:
        # Try logical_id (latest)
        node = db.query(Node).filter(Node.logical_id == node_version_id).order_by(Node.document_order.asc()).first()
    if not node:
        raise HTTPException(status_code=404, detail="Node not found.")

    # Find children in the same version
    children = (
        db.query(Node.node_version_id)
        .filter(Node.version_id == node.version_id, Node.parent_id == node.node_version_id)
        .order_by(Node.document_order.asc())
        .all()
    )
    children_ids = [c[0] for c in children]

    return NodeDetailResponse(
        node_version_id=node.node_version_id,
        logical_id=node.logical_id,
        version_id=node.version_id,
        parent_id=node.parent_id,
        heading=node.heading,
        number_path=node.number_path,
        level=node.level,
        numbering_depth=node.numbering_depth,
        document_order=node.document_order,
        body_text=node.body_text,
        normalized_text=node.normalized_text,
        tables=node.tables or [],
        lists=node.lists or [],
        cross_refs=node.cross_refs or [],
        page_start=node.page_start,
        page_end=node.page_end,
        anomalies=node.anomalies or [],
        content_hash=node.content_hash,
        children=children_ids,
    )


@router.get("/{node_id}/changes", response_model=NodeDiffResponse)
def get_node_changes(
    node_id: str,
    from_version: int | None = Query(None, description="Starting version number"),
    to_version: int | None = Query(None, description="Target version number (defaults to latest)"),
    db: Session = Depends(get_db),
):
    """Get change diff for a node between two versions."""
    # Resolve node and document
    sample_node = db.query(Node).filter((Node.node_version_id == node_id) | (Node.logical_id == node_id)).first()
    if not sample_node:
        raise HTTPException(status_code=404, detail="Node not found.")

    logical_id = sample_node.logical_id
    version = db.query(DocumentVersion).filter(DocumentVersion.id == sample_node.version_id).first()
    document_id = version.document_id

    # Get all document versions ordered
    doc_versions = (
        db.query(DocumentVersion)
        .filter(DocumentVersion.document_id == document_id)
        .order_by(DocumentVersion.version_number.asc())
        .all()
    )
    if not doc_versions:
        raise HTTPException(status_code=404, detail="No versions found for document.")

    v_from_num = from_version if from_version is not None else 1
    v_to_num = to_version if to_version is not None else doc_versions[-1].version_number

    v_from = next((v for v in doc_versions if v.version_number == v_from_num), None)
    v_to = next((v for v in doc_versions if v.version_number == v_to_num), None)

    if not v_from:
        raise HTTPException(status_code=404, detail=f"From-version {v_from_num} not found.")
    if not v_to:
        raise HTTPException(status_code=404, detail=f"To-version {v_to_num} not found.")

    old_node = db.query(Node).filter(Node.version_id == v_from.id, Node.logical_id == logical_id).first()
    new_node = db.query(Node).filter(Node.version_id == v_to.id, Node.logical_id == logical_id).first()

    if not old_node and not new_node:
        raise HTTPException(status_code=404, detail="Node does not exist in either specified version.")

    return compute_node_diff(old_node, new_node, from_version=v_from_num, to_version=v_to_num)
