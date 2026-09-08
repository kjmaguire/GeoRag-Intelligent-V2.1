#!/usr/bin/env bash
# Run the Bedrock wire-contract probe with the FastAPI service's venv.
#
# Replaces cohere_parse_probe.sh (ADR-0022), and covers all four models
# rather than just Parse — because on Bedrock all four are unverified, not
# only the one that was unverified on Foundry.
#
# Needs AWS credentials with bedrock:InvokeModel, bedrock:Converse,
# bedrock:Rerank and sagemaker:DescribeEndpoint. On a developer machine
# that is a profile; in production it is the task role.
#
# Writes ops/validation/reports/bedrock_probe_<timestamp>.json. COMMIT THE
# REPORT: the adapters carry [UNVERIFIED] at the top until one exists.
#
# Usage:
#   bash ops/validation/bedrock_probe.sh                       # fixture PDF
#   PROBE_PDF=/path/to/scan.pdf PROBE_PAGES=1,3 bash ops/validation/bedrock_probe.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT"

PDF="${PROBE_PDF:-src/fastapi/tests/fixtures/ocr/PLS-2024-Technical-Report.pdf}"
PAGES="${PROBE_PAGES:-1}"

(cd src/fastapi && uv run --no-sync python "$REPO_ROOT/ops/validation/bedrock_probe.py" \
    --pdf "$REPO_ROOT/$PDF" --pages "$PAGES" \
    --out "$REPO_ROOT/ops/validation/reports")
