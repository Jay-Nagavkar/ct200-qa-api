"""app/models/schemas.py
Pydantic v2 schemas for API requests, responses, and serialization.
"""

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ChangeClass(str, Enum):
    """Classification of changes between node versions."""

    UNCHANGED = "unchanged"
    WORDING = "wording"
    NUMERIC = "numeric"
    STRUCTURAL = "structural"
    REMOVED = "removed"


class MatchMethod(str, Enum):
    """Strategy used to match a node across versions."""

    EXACT_PATH_TITLE = "exact_path_title"
    MATCHED_PARENT_TITLE = "matched_parent_title"
    FUZZY_BODY = "fuzzy_body"
    ADDED = "added"
    REMOVED = "removed"


# --- Table Schemas ---


class TableRowSchema(BaseModel):
    cells: list[str]


class TableSchema(BaseModel):
    headers: list[str]
    rows: list[TableRowSchema]


# --- Document & Version Schemas ---


class DocumentBase(BaseModel):
    name: str


class DocumentCreate(DocumentBase):
    pass


class DocumentVersionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    document_id: str
    version_number: int
    pdf_sha256: str
    created_at: datetime
    ingestion_report: dict[str, Any] | None = None


class DocumentResponse(DocumentBase):
    model_config = ConfigDict(from_attributes=True)

    id: str
    created_at: datetime
    versions: list[DocumentVersionResponse] = []


# --- Node Schemas ---


class NodeBase(BaseModel):
    heading: str
    number_path: str
    level: int
    numbering_depth: int
    document_order: int
    page_start: int
    page_end: int


class SectionResponse(NodeBase):
    """Lightweight section response for tree browsing."""

    model_config = ConfigDict(from_attributes=True)

    node_version_id: str
    logical_id: str
    parent_id: str | None = None
    content_hash: str
    anomalies: list[str] = []
    children: list["SectionResponse"] = []


class NodeDetailResponse(NodeBase):
    """Full node detail response."""

    model_config = ConfigDict(from_attributes=True)

    node_version_id: str
    logical_id: str
    version_id: str
    parent_id: str | None = None
    body_text: str
    normalized_text: str
    tables: list[TableSchema] = []
    lists: list[list[str]] = []
    cross_refs: list[str] = []
    anomalies: list[str] = []
    content_hash: str
    children: list[str] = []


# --- Diff Schemas ---


class NumericDiff(BaseModel):
    old_value: Any
    new_value: Any
    unit: str
    context: str = ""


class NodeDiffResponse(BaseModel):
    node_id: str
    logical_id: str
    from_version: int
    to_version: int
    changed: bool
    change_class: ChangeClass
    word_diff_summary: str
    numeric_changes: list[NumericDiff] = []
    old_hash: str
    new_hash: str


# --- Selection Schemas ---


class SelectionCreate(BaseModel):
    name: str = "Default Selection"
    node_ids: list[str] = Field(..., min_length=1, description="List of node_version_id or logical_id")


class SelectionItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    logical_id: str
    node_version_id: str
    number_path: str
    heading: str
    content_hash: str
    text_snapshot: str


class SelectionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    document_id: str
    name: str
    fingerprint: str
    created_at: datetime
    items: list[SelectionItemResponse] = []


# --- Generation & QA Test Case Schemas ---


class SourceRefSchema(BaseModel):
    logical_id: str
    verbatim_quote: str


class TestCaseSchema(BaseModel):
    title: str = Field(..., min_length=3)
    objective: str = Field(..., min_length=5)
    preconditions: list[str] = []
    steps: list[str] = Field(..., min_length=1)
    expected_result: str = Field(..., min_length=3)
    priority: str = Field(default="medium")  # high, medium, low
    category: str = Field(default="functional")  # functional, safety, edge_case, boundary
    source_refs: list[SourceRefSchema] = Field(..., min_length=1)


class GenerationRequest(BaseModel):
    force: bool = False


class NodeStaleness(BaseModel):
    logical_id: str
    number_path: str
    status: str  # fresh, stale_wording, stale_numeric, stale_structural, source_removed, dependency_changed
    change_class: ChangeClass | None = None
    word_diff_summary: str | None = None
    numeric_changes: list[NumericDiff] = []


class TestCaseStaleness(BaseModel):
    test_case_title: str
    status: str  # fresh, stale_wording, stale_numeric, stale_structural, source_removed, dependency_changed
    worst_change_class: str | None = None
    details: list[NodeStaleness] = []


class GenerationStaleness(BaseModel):
    status: str  # fresh, partially_stale, stale
    limits: str = (
        "This staleness check detects text and structural changes across document versions, not semantic impact."
    )
    test_cases: list[TestCaseStaleness] = []
    source_nodes: list[NodeStaleness] = []


class GenerationResponse(BaseModel):
    generation_id: str
    selection_id: str
    status: str  # ok, partial, failed
    model: str
    prompt_version: str
    created_at: datetime
    test_cases: list[TestCaseSchema] = []
    staleness: GenerationStaleness | None = None
    dropped_cases: list[dict[str, Any]] = []
    error: str | None = None
