"""app/models/sql.py
SQLAlchemy models for persistence: documents, versions, nodes, selections, and match reports.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from app.db import Base


def generate_uuid() -> str:
    return str(uuid.uuid4())


def utc_now() -> datetime:
    return datetime.now(UTC)


class Document(Base):
    """A document containing multiple versions."""

    __tablename__ = "documents"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    name = Column(String(255), nullable=False)
    created_at = Column(DateTime, default=utc_now, nullable=False)

    versions = relationship("DocumentVersion", back_populates="document", cascade="all, delete-orphan")
    logical_nodes = relationship("LogicalNode", back_populates="document", cascade="all, delete-orphan")
    selections = relationship("Selection", back_populates="document", cascade="all, delete-orphan")


class DocumentVersion(Base):
    """An immutable version of a document parsed from a PDF."""

    __tablename__ = "document_versions"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    document_id = Column(String(36), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    version_number = Column(Integer, nullable=False)
    pdf_sha256 = Column(String(64), nullable=False)
    created_at = Column(DateTime, default=utc_now, nullable=False)
    ingestion_report = Column(JSON, nullable=True)

    __table_args__ = (
        UniqueConstraint("document_id", "version_number", name="uq_doc_version"),
        UniqueConstraint("document_id", "pdf_sha256", name="uq_doc_sha256"),
    )

    document = relationship("Document", back_populates="versions")
    nodes = relationship("Node", back_populates="version", cascade="all, delete-orphan")


class LogicalNode(Base):
    """Stable logical identifier across document versions."""

    __tablename__ = "logical_nodes"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    document_id = Column(String(36), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    created_at = Column(DateTime, default=utc_now, nullable=False)

    document = relationship("Document", back_populates="logical_nodes")
    nodes = relationship("Node", back_populates="logical_node", cascade="all, delete-orphan")


class Node(Base):
    """A node snapshot in a specific document version."""

    __tablename__ = "nodes"

    node_version_id = Column(String(36), primary_key=True, default=generate_uuid)
    logical_id = Column(String(36), ForeignKey("logical_nodes.id", ondelete="CASCADE"), nullable=False)
    version_id = Column(String(36), ForeignKey("document_versions.id", ondelete="CASCADE"), nullable=False)
    parent_id = Column(String(36), nullable=True)  # node_version_id of parent in this version

    heading = Column(String(500), nullable=False)
    number_path = Column(String(100), nullable=False, default="")
    level = Column(Integer, nullable=False)  # tree depth (0=root, 1=top sections, etc.)
    numbering_depth = Column(Integer, nullable=False)
    document_order = Column(Integer, nullable=False)
    body_text = Column(Text, nullable=False, default="")
    normalized_text = Column(Text, nullable=False, default="")

    tables = Column(JSON, nullable=False, default=list)
    lists = Column(JSON, nullable=False, default=list)
    cross_refs = Column(JSON, nullable=False, default=list)
    page_start = Column(Integer, nullable=False, default=1)
    page_end = Column(Integer, nullable=False, default=1)
    anomalies = Column(JSON, nullable=False, default=list)
    content_hash = Column(String(64), nullable=False)

    logical_node = relationship("LogicalNode", back_populates="nodes")
    version = relationship("DocumentVersion", back_populates="nodes")


class Selection(Base):
    """An immutable set of pinned nodes selected by a user."""

    __tablename__ = "selections"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    document_id = Column(String(36), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(255), nullable=False)
    fingerprint = Column(String(64), nullable=False)  # hash of sorted (logical_id, node_version_id, content_hash)
    created_at = Column(DateTime, default=utc_now, nullable=False)

    document = relationship("Document", back_populates="selections")
    items = relationship("SelectionItem", back_populates="selection", cascade="all, delete-orphan")


class SelectionItem(Base):
    """A pinned node in a selection."""

    __tablename__ = "selection_items"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    selection_id = Column(String(36), ForeignKey("selections.id", ondelete="CASCADE"), nullable=False)
    logical_id = Column(String(36), nullable=False)
    node_version_id = Column(String(36), nullable=False)
    content_hash = Column(String(64), nullable=False)
    number_path = Column(String(100), nullable=False, default="")
    heading = Column(String(500), nullable=False, default="")
    text_snapshot = Column(Text, nullable=False, default="")

    selection = relationship("Selection", back_populates="items")


class MatchReport(Base):
    """Report generated when matching nodes from one version to the next."""

    __tablename__ = "match_reports"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    document_id = Column(String(36), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    from_version_id = Column(String(36), nullable=False)
    to_version_id = Column(String(36), nullable=False)
    created_at = Column(DateTime, default=utc_now, nullable=False)
    report_data = Column(JSON, nullable=False)
