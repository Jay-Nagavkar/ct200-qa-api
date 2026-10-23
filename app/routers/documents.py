"""app/routers/documents.py
Document upload, versioning, tree browsing, and search endpoints.
"""

import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.schemas import (
    DocumentResponse,
    DocumentVersionResponse,
    SectionResponse,
)
from app.models.sql import Document, DocumentVersion, Node
from app.services.ingest import ingest_document

router = APIRouter(prefix="/documents", tags=["documents"])


def _build_tree_response(nodes: list[Node]) -> list[SectionResponse]:
    """Build nested SectionResponse tree from flat list of nodes."""
    node_map: dict[str, SectionResponse] = {}
    roots: list[SectionResponse] = []

    for n in nodes:
        resp = SectionResponse(
            node_version_id=n.node_version_id,
            logical_id=n.logical_id,
            parent_id=n.parent_id,
            heading=n.heading,
            number_path=n.number_path,
            level=n.level,
            numbering_depth=n.numbering_depth,
            document_order=n.document_order,
            page_start=n.page_start,
            page_end=n.page_end,
            content_hash=n.content_hash,
            anomalies=n.anomalies or [],
            children=[],
        )
        node_map[n.node_version_id] = resp

    for n in nodes:
        resp = node_map[n.node_version_id]
        if n.parent_id and n.parent_id in node_map:
            node_map[n.parent_id].children.append(resp)
        elif n.level <= 1:
            roots.append(resp)

    return roots


@router.post("", response_model=DocumentResponse, status_code=201)
async def create_document(
    name: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Upload a PDF to create a new document (v1)."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = Path(tmp.name)

    try:
        doc, v1, report, _ = ingest_document(db, tmp_path, document_name=name)
        return doc
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


@router.post("/{document_id}/versions", response_model=DocumentVersionResponse, status_code=201)
async def create_document_version(
    document_id: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Upload a new PDF to create the next version of a document."""
    doc = db.query(Document).filter(Document.id == document_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = Path(tmp.name)

    try:
        _, version, _, _ = ingest_document(
            db,
            tmp_path,
            document_name=doc.name,
            document_id=doc.id,
        )
        return version
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


@router.get("", response_model=list[DocumentResponse])
def list_documents(db: Session = Depends(get_db)):
    """List all documents."""
    return db.query(Document).order_by(Document.created_at.desc()).all()


@router.get("/{document_id}", response_model=DocumentResponse)
def get_document(document_id: str, db: Session = Depends(get_db)):
    """Get document details including versions."""
    doc = db.query(Document).filter(Document.id == document_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    return doc


@router.get("/{document_id}/versions", response_model=list[DocumentVersionResponse])
def list_document_versions(document_id: str, db: Session = Depends(get_db)):
    """List all versions of a document."""
    doc = db.query(Document).filter(Document.id == document_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    return doc.versions


@router.get("/{document_id}/sections", response_model=list[SectionResponse])
def get_sections(
    document_id: str,
    version: int | None = Query(None, description="Version number (defaults to latest)"),
    db: Session = Depends(get_db),
):
    """Get browsable structured section tree for a document version."""
    doc = db.query(Document).filter(Document.id == document_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    # Resolve version
    v_query = db.query(DocumentVersion).filter(DocumentVersion.document_id == document_id)
    if version is not None:
        target_version = v_query.filter(DocumentVersion.version_number == version).first()
    else:
        target_version = v_query.order_by(DocumentVersion.version_number.desc()).first()

    if not target_version:
        raise HTTPException(status_code=404, detail=f"Version '{version or 'latest'}' not found.")

    nodes = db.query(Node).filter(Node.version_id == target_version.id).order_by(Node.document_order.asc()).all()
    return _build_tree_response(nodes)


@router.get("/{document_id}/search", response_model=list[SectionResponse])
def search_document(
    document_id: str,
    q: str = Query(..., min_length=1, description="Search term"),
    version: int | None = Query(None, description="Version number (defaults to latest)"),
    scope: str = Query("all", pattern="^(heading|text|all)$"),
    db: Session = Depends(get_db),
):
    """Search sections within a document version."""
    v_query = db.query(DocumentVersion).filter(DocumentVersion.document_id == document_id)
    if version is not None:
        target_version = v_query.filter(DocumentVersion.version_number == version).first()
    else:
        target_version = v_query.order_by(DocumentVersion.version_number.desc()).first()

    if not target_version:
        raise HTTPException(status_code=404, detail="Version not found.")

    term = f"%{q.lower()}%"
    node_query = db.query(Node).filter(Node.version_id == target_version.id)

    if scope == "heading":
        node_query = node_query.filter(Node.heading.ilike(term))
    elif scope == "text":
        node_query = node_query.filter(Node.body_text.ilike(term))
    else:
        node_query = node_query.filter((Node.heading.ilike(term)) | (Node.body_text.ilike(term)))

    matched_nodes = node_query.order_by(Node.document_order.asc()).all()

    return [
        SectionResponse(
            node_version_id=n.node_version_id,
            logical_id=n.logical_id,
            parent_id=n.parent_id,
            heading=n.heading,
            number_path=n.number_path,
            level=n.level,
            numbering_depth=n.numbering_depth,
            document_order=n.document_order,
            page_start=n.page_start,
            page_end=n.page_end,
            content_hash=n.content_hash,
            anomalies=n.anomalies or [],
            children=[],
        )
        for n in matched_nodes
    ]


@router.get("/{document_id}/ingestion-report")
def get_ingestion_report(
    document_id: str,
    version: int | None = Query(None, description="Version number (defaults to latest)"),
    db: Session = Depends(get_db),
):
    """Get the stored ingestion report (including coverage check) for a version."""
    v_query = db.query(DocumentVersion).filter(DocumentVersion.document_id == document_id)
    if version is not None:
        target_version = v_query.filter(DocumentVersion.version_number == version).first()
    else:
        target_version = v_query.order_by(DocumentVersion.version_number.desc()).first()

    if not target_version:
        raise HTTPException(status_code=404, detail="Version not found.")

    return target_version.ingestion_report or {}
