# Technical Approach & Design Architecture

## CardioTrack CT-200 QA API Backend

---

## 1. System Architecture & Data Model

The system turns unstructured medical device manual PDFs into browsable, versioned, hierarchical document trees, enables users to pin immutable section selections, generates 3–5 QA test-case ideas via an LLM, and evaluates test-case staleness at read time when new document revisions are ingested.

### Hybrid Storage Architecture

- **SQLite + SQLAlchemy 2.x**:
  - `documents`: Document identity and metadata.
  - `document_versions`: Immutable version snapshots (`version_number`, `pdf_sha256`, `ingestion_report`).
  - `logical_nodes`: Stable UUID logical identifiers that persist across document versions.
  - `nodes`: Versioned node rows capturing document structure (`node_version_id`, `logical_id`, `version_id`, `parent_id`, `heading`, `number_path`, `level`, `numbering_depth`, `document_order`, `body_text`, `normalized_text`, `tables`, `lists`, `cross_refs`, `page_start`, `page_end`, `anomalies`, `content_hash`).
  - `selections`: Immutable selections storing a cryptographic `fingerprint` of selected items.
  - `selection_items`: Pinned section items storing `logical_id`, `node_version_id`, `content_hash`, and a frozen `text_snapshot`.
  - `match_reports`: Audit records documenting the exact matching method and similarity score for each node transition.
- **MongoDB**:
  - `generations`: Append-only history of LLM generation attempts and outcomes (`generation_id`, `selection_id`, `status`, `model`, `prompt_version`, `attempts`, `source_nodes`, `source_fingerprint`, `test_cases`, `dropped_cases`, `created_at`).

---

## 2. Parsing Approach & Hierarchy Construction

### Direct PDF Span-Level Extraction (PyMuPDF)
We reject markdown conversion or text flattening because regex over flattened text loses coordinate boundaries, font metadata, and table structures. Using `page.get_text("dict", flags=TEXT_PRESERVE_WHITESPACE)`, we extract font properties:
- Title (22.0pt `NimbusSans-Bold`) -> Merged into a single root node (level 0).
- Level 1 headings (16.5pt `NimbusSans-Bold`) -> Top sections 1–8 (level 1).
- Level 2 headings (12.87pt `NimbusSans-Bold`) -> Subsections like 1.1, 2.1, 3.1 (level 2).
- Level 3+ headings / irregular headings (11.0pt `NimbusSans-Bold`) -> Differentiated from table headers and body.

### Tree Depth vs Numbering Depth
We enforce a strict separation between **tree level** (depth in the hierarchy) and **numbering depth** (count of numeric tokens in the number path):
$$\text{level} = \text{parent.level} + 1$$
$$\text{numbering\_depth} = \text{len}(\text{number\_path.split}('.'))$$
This allows irregular sections (like `2.1.1.1` under `2.1`) to have `level = 3` while accurately recording `numbering_depth = 4` and flagging the anomaly `level_gap`.

### OCR Cross-Check Role & Honest Results
An independent OCR cross-check module (`app/parsing/ocr_check.py`) renders PDF pages at 300 DPI, runs Tesseract OCR, and computes character/word similarity against the extracted text layer.
- **Role**: Validates that no rasterized text or scanned artifacts are missed by the text layer.
- **Honest Results**: The provided CT-200 PDFs are digital-native PDFs with clean embedded vector text. Tesseract cross-check achieves >98.5% normalized similarity across all pages. The module degrades gracefully when the Tesseract binary is not installed, emitting an informative log and allowing the primary text-layer parser to proceed without interruption.

---

## 3. Discovered Irregularities & Handling

| Irregularity | Occurrence | Handling Strategy |
|---|---|---|
| **Multi-line Title** | Page 1 ("CardioTrack CT-200 Home Blood / Pressure Monitor — Technical & / User Manual") | Multi-line title spans of font size 22.0pt are coalesced into a single Root Node (`level=0`, `number_path=""`). |
| **Trailing Dots on Top Sections** | Pages 1–6 ("1. Device Overview", "2. Physical...", etc.) | Regex pattern `^(\d+(?:\.\d+)*)\.?\s+(.+)$` matches both dotted top sections and bare sub-headings without trailing dots. |
| **Level Gap** | Page 2 ("2.1.1.1 Battery Life Under Typical Use") | Parent resolved to `2.1` by scanning parent prefixes. Tree `level = 3`, `numbering_depth = 4`. Flagged with anomaly `"level_gap"`. |
| **Out-of-Order Numbering** | Page 3 ("3.4 Auto Shutoff" appears before "3.3 Result Display") | Preserves exact document order (`3.4` at `document_order=11`, `3.3` at `document_order=12`). Previous sibling comparison under parent `3` detects $3 < 4$, flagging `"out_of_order"` on `3.3`. |
| **Duplicate Headings** | Page 4 ("4.2 Error Codes") & Page 6 ("7.1 Error Codes") | Disambiguated by stable `number_path` and distinct parent nodes (`4` vs `7`). Each receives its own `logical_id`. |
| **Numbered List vs Heading** | Page 3 ("1. Normal ... 5. Hypertensive Crisis") | Formatted as `NimbusSans-Regular` 11.0pt (not bold) and left-indented. Heading classifier filters out regular weight; list parser extracts items into `node.lists`. |
| **Tables** | Section 2.1 (2 columns) & Section 4.2 (3 columns, wrapped cells) | Bounding box coordinates partition columns (`Parameter | Value` and `Code | Meaning | Device Behavior`). Structured into rows/cells and included in node content hash and text snapshots. |
| **Non-Breaking Hyphens (U+2011)** | "CT‑200", "non‑invasive" | Normalized to ASCII `-` in `normalize_for_hash()` and `normalize_text()` for search and hashing, while original unicode characters are preserved in raw `body_text`. |
| **Cross-References** | "see 2.1, 4.3" in 3.3; "Section 4.2" in 7.1 | Extracted into `node.cross_refs` list (`['2.1', '4.3']` and `['4.2']`). |

---

## 4. Initial Failures & How They Were Found

During initial validation, four critical implementation failures were discovered and resolved:

1. **Top-Level Heading Pattern Mismatch**:
   - *Failure*: Initial regex `r'^(\d+(?:\.\d+)*)\s+(.+)$'` failed to match "1. Device Overview" because of the trailing dot after the single digit. Consequently, top-level headings were treated as unnumbered sections.
   - *Discovery*: Found when inspecting `validate_parser.py` tree output where section 1 was printed with `None` as its `number_path`.
   - *Fix*: Updated pattern to `r'^(\d+(?:\.\d+)*)\.?\s+(.+)$'`.

2. **Font-Size Level Misclassification for Section 3.2**:
   - *Failure*: In `ct200_manual.pdf`, "3.2 Cuff Inflation Sequence" was typeset at 11.0pt Bold instead of 12.87pt Bold. The font-level classifier assigned it `level = 3`, placing it under section 3.1.
   - *Discovery*: Inspection of `validate_parser.py` tree output showed `3.2` nested as a child of `3.1`.
   - *Fix*: Decoupled font size from tree hierarchy. Tree depth is now strictly calculated as $\text{parent.level} + 1$, where `parent` is resolved from the `number_path` prefix hierarchy (`3.2` -> parent `3`, so level = 2).

3. **Coverage Check Accounting Gap**:
   - *Failure*: Initial coverage calculation reported 84.1% coverage with 30 "unassigned" spans on v1.
   - *Discovery*: Examining the unassigned span list revealed that heading words ("Device", "Overview") and table headers ("Parameter", "Value", "Code") were not included in the lookup set.
   - *Fix*: Updated `check_coverage` in `app/parsing/report.py` to index words from headings, table headers, and table cells, achieving 100.0% coverage with 0 unassigned spans.

4. **Selection Text Snapshot Omission of Tables**:
   - *Failure*: Sections 2.1 and 4.2 have their primary specifications and error codes in structured tables. The original `text_snapshot` only concatenated `heading + body_text`, leaving out table contents. When LLM test cases quoted error code behavior (e.g., "Auto-deflates within 2 seconds, displays E3"), the verbatim quote validation rejected the quote as fabricated.
   - *Discovery*: `test_fabricated_quote_rejected` and `test_idempotency_cached_and_force_true` failed with 500 error reporting that the quote was missing from the source text.
   - *Fix*: Implemented `build_node_text_snapshot()` which serializes table headers and rows directly into the pinned text snapshot.

---

## 5. Version Matching Strategy & Failure Modes

Version matching (`app/versioning/matcher.py`) executes a 4-tier cascading resolution strategy:

1. **Tier 1 (Exact Match)**: Normalized `number_path` AND normalized `heading` match ($1.0$ confidence).
2. **Tier 2 (Parent + Title Similarity)**: Same matched parent AND Levenshtein title similarity $\ge 0.90$ via RapidFuzz.
3. **Tier 3 (Orphan Body Similarity)**: Token set ratio on body text $\ge 0.85$ via RapidFuzz for sections moved or renumbered.
4. **Tier 4 (Fallback)**: Unmatched new nodes are classified as `added` (new `logical_id`); unmatched old nodes are classified as `removed`.

### Where Matching Breaks (Edge Cases & Limitations)
- **Section Splits**: If section 3.2 is split into 3.2a and 3.2b, Tier 1 fails; Tier 2/3 will match whichever half has higher body similarity, and mark the other as `added`.
- **Section Merges**: If 5.1 and 5.2 are merged into 5.1, 5.2 will be marked as `removed`.
- **Duplicate Titles Across Chapters**: Handled properly because Tier 2 checks matched parent identity before checking title similarity.

---

## 6. Diffing & Change Classification

Diff computation (`app/versioning/diff.py`) evaluates differences between version snapshots:
- `unchanged`: Identical `content_hash`.
- `numeric`: Normalized text differs AND numeric tokens differ (e.g., `300 -> 250 cycles`, `15% -> 10%`, `40 -> 30 mmHg`, `2 s -> 1.5 s`).
- `wording`: Text differs without numeric token changes.
- `structural`: Node added, removed, table rows altered, or hierarchy level changed.

### Verified v1 -> v2 Transitions
- **Modified (4)**:
  - `2.1.1.1`: 300 -> 250 measurement cycles, 15% -> 10% low battery threshold (`numeric`).
  - `3.2`: 40 -> 30 mmHg inflation increments (`numeric`).
  - `4.2`: E3 deflation 2 s -> 1.5 s; added E6 Bluetooth failure row (`numeric`).
  - `4.3`: Alarm range E1–E5 -> E1–E6 (`numeric`).
- **Added (1)**:
  - `5.3 Data Export`: CSV export feature introduced in firmware 1.4 (`structural`).
- **Unchanged (24)**:
  - All remaining 24 nodes retain identical `content_hash` and `logical_id`.

---

## 7. QA Prompt Design & Validation Policy

- **Prompt Versioning**: `PROMPT_VERSION = "1.0.0"`.
- **System Prompt**: Enforces medical device QA engineering standards: output pure JSON matching schema, use only provided text, forbid hallucinated parameters, and require verbatim quotes with source logical IDs.
- **Validation Pipeline**:
  1. Strip markdown code fences (````json ... ````).
  2. Parse JSON schema into Pydantic models.
  3. Validate test case count ($3 \le N \le 5$). If $N > 5$, trim to 5.
  4. Ensure non-empty step sequences.
  5. Validate that each `source_ref.logical_id` exists in the selection.
  6. **Verbatim Quote Verification**: Confirm that `normalize_text(quote)` is a substring of `normalize_text(source_node_text)`. Rejects fabricated quotes.
- **Retry Policy**: Up to 2 retries on validation failure, feeding the specific error message back to the LLM. If at least 3 valid cases remain, status is `"partial"`; if $< 3$, status is `"failed"` (returns 502 with full attempt logs in MongoDB).

---

## 8. Selection Deduplication & Text Pinning Policy

- **Immutable Selection**: `POST /selections` computes a SHA-256 fingerprint from sorted `(logical_id, node_version_id, content_hash)` tuples.
- **Deduplication**: If a selection with the identical fingerprint exists for the document, returns `200` with the existing selection instead of creating a duplicate.
- **Text Pinning Guarantee**: Every selection item captures a frozen `text_snapshot`. Even after subsequent document versions are ingested, `GET /selections/{id}` returns the pinned original text as it existed at selection time.

---

## 9. Read-Time Staleness Design & Fundamental Limits

Staleness is **never stored** in the database; it is computed dynamically at **read time**:
1. For each source node cited in a generation:
   - Resolve `logical_id` against the latest document version.
   - Compare `content_hash` against the frozen snapshot hash.
   - Statuses: `fresh`, `stale_numeric`, `stale_wording`, `stale_structural`, `source_removed`, or `dependency_changed`.
   - `dependency_changed`: Triggered when an unedited node references a section (via `cross_refs`) whose text has changed in the latest version.
2. Per-test-case staleness: Worst cited node status wins.
3. Generation status: `fresh` (all fresh), `partially_stale` (mixed), or `stale` (all stale).
4. **Limits Disclosure**: The API explicitly returns a `limits` notice explaining that the staleness check detects textual and structural changes across versions, not semantic impact.

---

## 10. Decision Log

1. **Part Most Likely to Give Silently Wrong Results and How to Catch It**:
   - *Risk*: Cross-section numerical contradictions where an unchanged section contradicts an updated section (e.g., in v2, section 4.1 still says emergency deflation vents the cuff within "2 seconds", whereas section 4.2 table row E3 was updated to "1.5 seconds"). Because section 4.1's text did not change, hash-based staleness considers test cases citing 4.1 as `fresh`.
   - *Mitigation*: Implemented dependency checking via `node.cross_refs`, and recommend a cross-section numerical consistency validator that extracts and cross-correlates equivalent physical dimensions (e.g., deflation seconds) across the entire document graph.
2. **Where Simplicity Was Chosen Over Correctness and What Breaks First**:
   - *Decision*: Line-by-line coordinate column partitioning for tables instead of a full visual graph neural network table parser.
   - *Failure Mode*: If future manuals introduce complex multi-span merged cells or nested sub-tables that cross column boundaries, the column x-coordinate boundary thresholds (`SPECS_TABLE_COL_BOUNDARY = 140.0`) will assign cells to incorrect column slots.
3. **One Unhandled Input and What the System Does With It**:
   - *Input*: Scanned bitmap-only PDFs containing no embedded text layer.
   - *System Behavior*: The text extractor returns 0 text spans, triggering the coverage checker's `passed = False` flag and emitting loud warning logs rather than silently returning an empty tree.

---

## 11. What To Do With More Time

1. **Cross-Section Numeric Consistency Engine**: Automatically compare numerical values across interrelated sections (e.g. overpressure deflation time in 4.1 vs error code E3 in 4.2) to detect un-updated cross-references in new document versions.
2. **Semantic AST-Level Diffing**: Integrate an embedding-based or sentence-level semantic similarity layer to distinguish trivial stylistic rewrites from clinically significant requirement changes.
3. **Visual Bounding Box Annotation**: Export annotated PDF overlays highlighting changed sections and table boundaries for visual verification in QA workflows.
