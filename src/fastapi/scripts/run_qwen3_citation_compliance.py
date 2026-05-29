"""Run the Qwen3 citation compliance benchmark (plan §0c).

Standalone runner — NOT invoked by CI. Sets the
``QWEN3_COMPLIANCE_MANUAL=1`` env var that lifts the pytest skip on
``tests/test_qwen3_citation_compliance.py``, then drives the six tests
with progress logging and writes a JSON report.

Usage:

    cd src/fastapi
    python scripts/run_qwen3_citation_compliance.py \\
        --vllm-base http://localhost:8000 \\
        --model Qwen/Qwen3-30B-A3B-Instruct-AWQ \\
        --report ../../docs/audits/qwen3_compliance_$(date +%F).json

Decision gate (plan §0c):
    Test 1 compliance < 85% → STOP. System prompt needs redesign before
    any plan §4b citation guard implementation proceeds.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--vllm-base",
        default="http://localhost:8000",
        help="vLLM endpoint base URL.",
    )
    parser.add_argument(
        "--model",
        default="Qwen/Qwen3-14B-AWQ",
        help="Model name on the vLLM endpoint.",
    )
    parser.add_argument(
        "--report",
        default=None,
        help="Output JSON report path. Defaults to docs/audits/qwen3_compliance_YYYY-MM-DD.json.",
    )
    parser.add_argument(
        "--test-filter",
        default=None,
        help="Optional pytest -k filter, e.g. 'test1' to run only Test 1.",
    )
    args = parser.parse_args()

    if args.report is None:
        today = dt.date.today().isoformat()
        args.report = f"docs/audits/qwen3_compliance_{today}.json"

    # Sanity check
    repo_root = Path(__file__).resolve().parents[3]
    test_file = repo_root / "src" / "fastapi" / "tests" / "test_qwen3_citation_compliance.py"
    if not test_file.exists():
        print(f"FATAL: test file not found at {test_file}", file=sys.stderr)
        return 2

    env = os.environ.copy()
    env["QWEN3_COMPLIANCE_MANUAL"] = "1"
    env["VLLM_BASE_URL"] = args.vllm_base
    env["VLLM_MODEL"] = args.model

    pytest_cmd = [
        "pytest",
        str(test_file),
        "-v",
        "--no-header",
        "--tb=short",
    ]
    if args.test_filter:
        pytest_cmd.extend(["-k", args.test_filter])

    print(f"[runner] vLLM   : {args.vllm_base}")
    print(f"[runner] Model  : {args.model}")
    print(f"[runner] Report : {args.report}")
    print(f"[runner] Command: {' '.join(pytest_cmd)}")
    print()

    started_at = dt.datetime.now(dt.timezone.utc).isoformat()
    proc = subprocess.run(pytest_cmd, env=env, capture_output=True, text=True)
    finished_at = dt.datetime.now(dt.timezone.utc).isoformat()

    report = {
        "started_at": started_at,
        "finished_at": finished_at,
        "vllm_base": args.vllm_base,
        "model": args.model,
        "exit_code": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "decision_gate": (
            "PASS — proceed with plan §4b citation guards"
            if proc.returncode == 0
            else "FAIL — Test 1 < 85% or other failures. Redesign system prompt before §4b."
        ),
    }

    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2))

    print(proc.stdout)
    if proc.stderr:
        print(proc.stderr, file=sys.stderr)
    print(f"[runner] Report written to {report_path}")
    print(f"[runner] Decision gate: {report['decision_gate']}")
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
