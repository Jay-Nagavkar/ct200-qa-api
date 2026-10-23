#!/usr/bin/env bash
# scripts/demo.sh
# Runs the end-to-end Python demo script showcasing the full lifecycle:
# 1. Ingest v1 -> 2. Show tree -> 3. Create selection -> 4. Generate QA ->
# 5. Ingest v2 -> 6. Show changes/diffs -> 7. Fetch staleness report.

set -e

echo "Running CardioTrack CT-200 QA API Demo..."
python3 scripts/demo.py || python scripts/demo.py
