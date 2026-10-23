# ruff: noqa: E402
"""Quick parser validation script - run before building full tests."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

from app.parsing.extract import extract_spans
from app.parsing.report import generate_report
from app.parsing.tree import build_tree


def validate_pdf(pdf_path: str, version: int) -> None:
    name = Path(pdf_path).stem
    print(f"\n{'=' * 60}")
    print(f"Validating: {name} (v{version})")
    print(f"{'=' * 60}")

    pages = extract_spans(pdf_path)
    nodes = build_tree(pages)

    print(f"\nTotal nodes: {len(nodes)}")
    print("\nDocument tree:")
    for node in nodes:
        indent = "  " * node.level
        anomaly_str = f" [{', '.join(node.anomalies)}]" if node.anomalies else ""
        table_str = f" [TABLE: {len(node.tables[0].rows)} rows]" if node.tables else ""
        refs_str = f" [refs: {node.cross_refs}]" if node.cross_refs else ""
        print(
            f"  {indent}{node.number_path or 'ROOT'}: "
            f"{node.heading[:60]}"
            f" (level={node.level}, depth={node.numbering_depth},"
            f" pages={node.page_start}-{node.page_end})"
            f"{anomaly_str}{table_str}{refs_str}"
        )

    print("\n--- Body text samples ---")
    for node in nodes:
        if node.body_text:
            body_preview = node.body_text[:120].replace("\n", " ")
            print(f"  {node.number_path or 'ROOT'}: {body_preview}...")

    report = generate_report(name, version, pages, nodes)
    print("\n--- Report ---")
    print(json.dumps(report.to_dict(), indent=2))

    # Verify specific expectations
    print("\n--- Verification checks ---")

    # Check node count
    paths = [n.number_path for n in nodes if n.number_path]
    print(f"  Sections found: {paths}")

    # Check 2.1.1.1 parent
    n2111 = next((n for n in nodes if n.number_path == "2.1.1.1"), None)
    if n2111:
        print(f"  2.1.1.1 parent: {n2111.parent_number_path} (expected: 2.1)")
        print(f"  2.1.1.1 level_gap anomaly: {'level_gap' in n2111.anomalies}")
    else:
        print("  ERROR: 2.1.1.1 not found!")

    # Check 3.4 before 3.3
    order_34 = next((n.document_order for n in nodes if n.number_path == "3.4"), -1)
    order_33 = next((n.document_order for n in nodes if n.number_path == "3.3"), -1)
    if order_34 >= 0 and order_33 >= 0:
        print(
            f"  3.4 order={order_34}, 3.3 order={order_33}: {'3.4 before 3.3' if order_34 < order_33 else 'WRONG ORDER!'}"
        )
        n33 = next(n for n in nodes if n.number_path == "3.3")
        print(f"  3.3 out_of_order anomaly: {'out_of_order' in n33.anomalies}")

    # Check duplicate Error Codes
    error_nodes = [n for n in nodes if "Error Codes" in n.heading]
    print(f"  Error Codes sections: {len(error_nodes)} (expected 2)")
    if len(error_nodes) >= 2:
        print(f"    4.2 Error Codes parent: {error_nodes[0].parent_number_path}")
        print(f"    7.1 Error Codes parent: {error_nodes[1].parent_number_path}")

    # Check tables
    table_nodes = [n for n in nodes if n.tables]
    print(f"  Nodes with tables: {[n.number_path for n in table_nodes]}")
    for tn in table_nodes:
        for t in tn.tables:
            print(f"    {tn.number_path}: {len(t.rows)} rows, headers={t.headers}")

    # Check cross-references
    ref_nodes = [n for n in nodes if n.cross_refs]
    print(f"  Nodes with cross-refs: {[(n.number_path, n.cross_refs) for n in ref_nodes]}")


if __name__ == "__main__":
    data_dir = Path(__file__).parent.parent / "data"
    validate_pdf(str(data_dir / "ct200_manual.pdf"), 1)
    validate_pdf(str(data_dir / "ct200_manual_v2.pdf"), 2)
