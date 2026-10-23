# CardioTrack CT-200 QA API Backend

FastAPI backend that turns the **CardioTrack CT-200 Home Blood Pressure Monitor — Technical & User Manual** PDF into a browsable, structured, versioned tree, generates 3–5 QA test-case ideas from user-selected sections via an LLM, and evaluates test-case staleness at read time when new document revisions (v2) are ingested.

---

## Features

- **Direct PDF Parsing**: Extracts text spans directly using PyMuPDF (`fitz`), preserving font weights, sizes, and bounding boxes. No markdown intermediary.
- **Robust Irregularity Handling**:
  - Multi-line Title merged into single Root Node (`level=0`).
  - Section `2.1.1.1` under `2.1` with level gap flagged as `level_gap` anomaly.
  - Section `3.4` before `3.3` preserved in document order with `out_of_order` anomaly flagged on `3.3`.
  - Disambiguates duplicate "Error Codes" headings (`4.2` and `7.1`) under distinct parents.
  - Filters numbered clinical classification list in `3.3` so it does not become section headings.
  - Structured table extraction for `2.1 General Specifications` (2 columns) and `4.2 Error Codes` (3 columns).
- **100% Ingestion Coverage Check**: Validates that every text span is assigned to a node; reports any unassigned spans.
- **Immutable Versioning & Stable Logical IDs**:
  - Re-uploading identical PDF is idempotent (returns existing version).
  - Multi-tier Matcher (`exact path+title` -> `matched parent + title similarity >= 0.9` -> `fuzzy body similarity >= 0.85` -> `added/removed`) preserves stable `logical_id`s across document revisions.
- **Word-Level & Numeric Diffing**: Classifies node changes as `numeric`, `wording`, `structural`, `unchanged`, or `removed`.
- **Immutable Selections**: Pinned node selections with cryptographic fingerprint deduplication, ensuring original text is preserved even after v2 ingestion.
- **LLM QA Test-Case Generation**:
  - Strict Pydantic validation (3–5 cases, non-empty steps).
  - Verbatim quote verification: quotes must be substrings of the source section, rejecting fabricated quotes.
  - Automatic retry loop (up to 2 retries) feeding validation errors back to the model.
  - Append-only generation history stored in MongoDB.
- **Read-Time Staleness Engine**: Dynamically calculates staleness status (`fresh`, `stale_numeric`, `stale_wording`, `stale_structural`, `dependency_changed`, `source_removed`) for each test case without mutating historical records.

---

## Prerequisites

- **Python**: 3.11+ (Tested on Python 3.12)
- **MongoDB**: 7.0+ (Running locally or via Docker Compose)
- **Optional**: Tesseract OCR for optional cross-check (gracefully degrades if omitted)

---

## Quickstart

### 1. Environment Setup

```bash
# Create and activate virtual environment
python -m venv venv
.\venv\Scripts\activate   # Windows
# source venv/bin/activate # Linux / macOS

# Install pinned dependencies
pip install -r requirements.txt
```

### 2. Configure Environment

Copy `.env.example` to `.env`:

```bash
cp .env.example .env
```

Edit `.env` as needed:
```env
LLM_PROVIDER=gemini        # "gemini" or "groq"
LLM_MODEL=gemini-2.0-flash # or llama-3.3-70b-versatile
LLM_API_KEY=your_key_here  # Optional: app runs without key; generation returns 503

DATABASE_URL=sqlite:///./ct200qa.db
MONGODB_URI=mongodb://localhost:27017
MONGODB_DB=ct200qa
```

### 3. Start MongoDB via Docker Compose

```bash
docker compose up -d
```

### 4. Run the API Server

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Interactive API documentation will be available at:
- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

---

## Running the Automated Demo

Run the end-to-end self-contained demo:

```bash
python scripts/demo.py
# or
bash scripts/demo.sh
```

The demo demonstrates the complete 7-step lifecycle:
1. Ingests v1 (`ct200_manual.pdf`).
2. Displays the browsable tree and verified 100% coverage report.
3. Creates a pinned selection of sections `3.2`, `4.2`, and `6.1`.
4. Generates QA test cases with verbatim quotes.
5. Ingests v2 (`ct200_manual_v2.pdf`).
6. Computes diffs: `2.1.1.1`, `3.2`, `4.2`, and `4.3` are `numeric`; `5.3` is `structural` (added); `6.1` is `unchanged`.
7. Fetches generation showing read-time computed staleness: cases citing `3.2` and `4.2` become `stale_numeric`, while `6.1` remains `fresh`.

---

## Running Tests & Linters

```bash
# Run full test suite (26 tests)
pytest -v

# Run Ruff linter and format check
ruff check app tests scripts
ruff format --check app tests scripts
```

---

## API Endpoints Reference

### Documents & Versions
- `POST /documents`: Upload PDF to create document (v1).
- `GET /documents`: List all documents.
- `GET /documents/{id}`: Get document details and versions.
- `POST /documents/{id}/versions`: Upload new PDF version (v2).
- `GET /documents/{id}/versions`: List versions of a document.
- `GET /documents/{id}/sections?version=`: Get hierarchical tree for version (default latest).
- `GET /documents/{id}/search?q=&version=&scope=heading|text|all`: Search within version.
- `GET /documents/{id}/ingestion-report?version=`: Get ingestion report with coverage audit.

### Nodes & Changes
- `GET /nodes/{node_version_id}`: Full node details, tables, lists, and anomalies.
- `GET /nodes/{node_id}/changes?from=&to=`: Word-level and numeric diff between versions.
- `GET /nodes/{node_id}/generations`: Get QA generations citing this node with staleness block.

### Selections
- `POST /selections`: Create an immutable selection `{name, node_ids[]}`.
- `GET /selections/{id}`: Fetch selection with pinned text snapshots.
- `POST /selections/{id}/generate?force=false`: Generate QA test cases via LLM.
- `GET /selections/{id}/generations`: Fetch all generations for selection with staleness block.

---

## Exact cURL Workflow Commands

```bash
# 1. Ingest v1
curl -X POST "http://localhost:8000/documents" \
  -F "name=CardioTrack CT-200" \
  -F "file=@data/ct200_manual.pdf"

# 2. View section tree
curl "http://localhost:8000/documents/<DOC_ID>/sections?version=1"

# 3. Create selection (using node version IDs)
curl -X POST "http://localhost:8000/selections" \
  -H "Content-Type: application/json" \
  -d '{"name": "Core Safety", "node_ids": ["<NODE_ID_3_2>", "<NODE_ID_4_2>", "<NODE_ID_6_1>"]}'

# 4. Generate test cases
curl -X POST "http://localhost:8000/selections/<SELECTION_ID>/generate"

# 5. Ingest v2
curl -X POST "http://localhost:8000/documents/<DOC_ID>/versions" \
  -F "file=@data/ct200_manual_v2.pdf"

# 6. Check node changes (e.g. section 3.2)
curl "http://localhost:8000/nodes/<NODE_ID_3_2>/changes?from=1&to=2"

# 7. Fetch generation with read-time staleness
curl "http://localhost:8000/selections/<SELECTION_ID>/generations"
```

---

## License

MIT License. Designed and implemented for hiring assignment submission.
