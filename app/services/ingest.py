"""app/services/ingest.py
Document ingestion service: parses PDF, matches versions, creates stable logical IDs, and persists tree.
"""

import logging
import uuid
from pathlib import Path

from sqlalchemy.orm import Session

from app.models.sql import Document, DocumentVersion, LogicalNode, MatchReport, Node
from app.parsing.extract import compute_pdf_sha256, extract_spans
from app.parsing.report import IngestionReport, generate_report
from app.parsing.tree import ParsedNode, build_tree
from app.versioning.matcher import MatchResult, match_nodes

logger = logging.getLogger(__name__)


def ingest_document(
    db: Session,
    pdf_path: str | Path,
    document_name: str,
    document_id: str | None = None,
) -> tuple[Document, DocumentVersion, IngestionReport, MatchResult | None]:
    """Ingest a PDF document, either as v1 (new doc) or next version (existing doc).

    Guarantees:
    - Never mutates older versions.
    - Re-uploading an identical PDF (same sha256) returns existing version without duplicating.
    - Stable logical_id preserved across versions via Matcher.
    """
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF file not found at: {pdf_path}")

    pdf_sha256 = compute_pdf_sha256(pdf_path)

    # 1. Resolve or create Document
    if document_id:
        doc = db.query(Document).filter(Document.id == document_id).first()
        if not doc:
            raise ValueError(f"Document with id {document_id} not found")
    else:
        # Check by name
        doc = db.query(Document).filter(Document.name == document_name).first()
        if not doc:
            doc = Document(id=str(uuid.uuid4()), name=document_name)
            db.add(doc)
            db.flush()

    # 2. Check for duplicate upload (same sha256)
    existing_version = (
        db.query(DocumentVersion)
        .filter(DocumentVersion.document_id == doc.id, DocumentVersion.pdf_sha256 == pdf_sha256)
        .first()
    )
    if existing_version:
        logger.info(
            "Identical PDF already ingested for doc '%s' as v%d (sha256=%s). Returning existing version.",
            doc.name,
            existing_version.version_number,
            pdf_sha256[:8],
        )
        report_dict = existing_version.ingestion_report or {}
        # Return mock IngestionReport object or reconstruct
        return doc, existing_version, report_dict, None

    # 3. Determine next version number
    current_versions = (
        db.query(DocumentVersion)
        .filter(DocumentVersion.document_id == doc.id)
        .order_by(DocumentVersion.version_number.desc())
        .all()
    )
    version_number = (current_versions[0].version_number + 1) if current_versions else 1

    # 4. Extract spans and build tree
    pages = extract_spans(pdf_path)
    parsed_nodes = build_tree(pages)
    report = generate_report(doc.name, version_number, pages, parsed_nodes)

    # 5. Create new DocumentVersion record
    version_id = str(uuid.uuid4())
    doc_version = DocumentVersion(
        id=version_id,
        document_id=doc.id,
        version_number=version_number,
        pdf_sha256=pdf_sha256,
        ingestion_report=report.to_dict(),
    )
    db.add(doc_version)
    db.flush()

    # 6. Matching & Logical ID assignment
    match_result: MatchResult | None = None
    node_logical_ids: dict[int, str] = {}

    if version_number > 1 and current_versions:
        # Fetch previous version nodes
        prev_version = current_versions[0]
        prev_nodes = db.query(Node).filter(Node.version_id == prev_version.id).order_by(Node.document_order.asc()).all()
        match_result = match_nodes(prev_nodes, parsed_nodes)
        node_logical_ids = match_result.new_node_logical_ids

        # Ensure any newly added logical_ids are recorded in logical_nodes table
        existing_logical_ids = {
            ln.id for ln in db.query(LogicalNode.id).filter(LogicalNode.document_id == doc.id).all()
        }
        for idx, lid in node_logical_ids.items():
            if lid not in existing_logical_ids:
                db.add(LogicalNode(id=lid, document_id=doc.id))
                existing_logical_ids.add(lid)

        # Record match report
        db_match_report = MatchReport(
            id=str(uuid.uuid4()),
            document_id=doc.id,
            from_version_id=prev_version.id,
            to_version_id=doc_version.id,
            report_data=match_result.to_dict(),
        )
        db.add(db_match_report)
    else:
        # First version: every node gets a brand new logical_id
        for idx, pn in enumerate(parsed_nodes):
            lid = str(uuid.uuid4())
            node_logical_ids[idx] = lid
            db.add(LogicalNode(id=lid, document_id=doc.id))

    db.flush()

    # 7. Create Node records and establish parent relationships
    # First pass: instantiate Node models
    path_to_node_version_id: dict[str, str] = {}
    node_models: list[tuple[ParsedNode, Node]] = []

    for idx, pn in enumerate(parsed_nodes):
        nid = str(uuid.uuid4())
        path_to_node_version_id[pn.number_path] = nid
        node_db = Node(
            node_version_id=nid,
            logical_id=node_logical_ids[idx],
            version_id=doc_version.id,
            parent_id=None,  # will be set in second pass
            heading=pn.heading,
            number_path=pn.number_path,
            level=pn.level,
            numbering_depth=pn.numbering_depth,
            document_order=pn.document_order,
            body_text=pn.body_text,
            normalized_text=pn.normalized_text,
            tables=[t.to_dict() for t in pn.tables],
            lists=pn.lists,
            cross_refs=pn.cross_refs,
            page_start=pn.page_start,
            page_end=pn.page_end,
            anomalies=pn.anomalies,
            content_hash=pn.content_hash,
        )
        node_models.append((pn, node_db))

    # Second pass: link parent_id by parent_number_path
    for pn, node_db in node_models:
        if pn.parent_number_path is not None:
            parent_nid = path_to_node_version_id.get(pn.parent_number_path)
            node_db.parent_id = parent_nid
        db.add(node_db)

    db.commit()
    db.refresh(doc)
    db.refresh(doc_version)

    logger.info(
        "Successfully ingested '%s' v%d: %d nodes created (sha256=%s)",
        doc.name,
        doc_version.version_number,
        len(node_models),
        pdf_sha256[:8],
    )
    return doc, doc_version, report, match_result
