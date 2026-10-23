"""app/services/selections.py
Selection service: creates immutable pinned node selections with deduplication and pinning guarantees.
"""

import hashlib
import logging
import uuid

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.sql import DocumentVersion, Node, Selection, SelectionItem

logger = logging.getLogger(__name__)


def compute_selection_fingerprint(items: list[tuple[str, str, str]]) -> str:
    """Compute deterministic fingerprint of sorted (logical_id, node_version_id, content_hash) tuples."""
    sorted_items = sorted(items, key=lambda x: (x[0], x[1]))
    hasher = hashlib.sha256()
    for logical_id, node_version_id, content_hash in sorted_items:
        hasher.update(logical_id.encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(node_version_id.encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(content_hash.encode("utf-8"))
        hasher.update(b"\x01")
    return hasher.hexdigest()


def build_node_text_snapshot(n: Node) -> str:
    """Build complete text snapshot of node including heading, body, and table content."""
    parts = [n.heading]
    if n.body_text:
        parts.append(n.body_text)
    tables = n.tables or []
    for table in tables:
        if isinstance(table, dict):
            headers = table.get("headers", [])
            if headers:
                parts.append(" | ".join(headers))
            for row in table.get("rows", []):
                cells = row.get("cells", []) if isinstance(row, dict) else getattr(row, "cells", [])
                parts.append(" | ".join(cells))
    return "\n\n".join(p for p in parts if p).strip()


def create_selection(
    db: Session,
    name: str,
    node_ids: list[str],
) -> tuple[Selection, bool]:
    """Create an immutable selection storing (logical_id, node_version_id, content_hash, text_snapshot).

    Returns (selection, created_new: bool).
    Duplicate policy: if a selection with the exact same pinned node set already exists, returns it.
    Validation:
    - All node_ids must exist (404).
    - All node_ids must belong to the same document version (422).
    """
    if not node_ids:
        raise HTTPException(status_code=422, detail="Selection must include at least one node ID.")

    # Resolve nodes: accept either node_version_id or logical_id (if unambiguous)
    resolved_nodes: list[Node] = []
    for nid in node_ids:
        node = db.query(Node).filter(Node.node_version_id == nid).first()
        if not node:
            # Try by logical_id (latest version)
            node = db.query(Node).filter(Node.logical_id == nid).order_by(Node.document_order.asc()).first()
        if not node:
            raise HTTPException(status_code=404, detail=f"Node '{nid}' not found.")
        resolved_nodes.append(node)

    # Validate that all nodes belong to the exact same version
    version_ids = {n.version_id for n in resolved_nodes}
    if len(version_ids) > 1:
        raise HTTPException(
            status_code=422,
            detail="All selected nodes must belong to the exact same document version. Found multiple versions in selection.",
        )

    version_id = resolved_nodes[0].version_id
    version = db.query(DocumentVersion).filter(DocumentVersion.id == version_id).first()
    document_id = version.document_id

    # Compute fingerprint
    fingerprint_items = [(n.logical_id, n.node_version_id, n.content_hash) for n in resolved_nodes]
    fingerprint = compute_selection_fingerprint(fingerprint_items)

    # Check for existing duplicate selection
    existing = (
        db.query(Selection).filter(Selection.document_id == document_id, Selection.fingerprint == fingerprint).first()
    )
    if existing:
        logger.info("Found existing duplicate selection '%s' (%s). Returning existing.", existing.name, existing.id)
        return existing, False

    # Create new immutable selection
    selection_id = str(uuid.uuid4())
    selection = Selection(
        id=selection_id,
        document_id=document_id,
        name=name,
        fingerprint=fingerprint,
    )
    db.add(selection)

    # Create selection items pinning the text
    for n in resolved_nodes:
        full_text = build_node_text_snapshot(n)
        item = SelectionItem(
            id=str(uuid.uuid4()),
            selection_id=selection_id,
            logical_id=n.logical_id,
            node_version_id=n.node_version_id,
            content_hash=n.content_hash,
            number_path=n.number_path,
            heading=n.heading,
            text_snapshot=full_text,
        )
        db.add(item)

    db.commit()
    db.refresh(selection)

    logger.info("Created new selection '%s' (%s) with %d items", selection.name, selection.id, len(resolved_nodes))
    return selection, True


def get_selection(db: Session, selection_id: str) -> Selection:
    """Retrieve a selection by ID."""
    selection = db.query(Selection).filter(Selection.id == selection_id).first()
    if not selection:
        raise HTTPException(status_code=404, detail=f"Selection '{selection_id}' not found.")
    return selection
