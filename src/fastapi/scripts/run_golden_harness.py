"""CI bench — one-shot runner for the golden-query regression suite.

Loads ``src/fastapi/tests/golden_queries.json``, runs each query
through ``prepare_evidence_for_intent`` against the deterministic
mock packets, and reports the pass-rate.

Exit codes:
  0 — 100% pass rate
  1 — any criterion failed
  2 — fixture-loading error / script crash

Intended use cases:

  * Pre-commit gate: ``python -m scripts.run_golden_harness`` runs in
    < 2s and catches quota drift before it lands.
  * CI: same command in a GitHub Action / GitLab CI stage.
  * Local A/B benchmark: see ``--quota-override`` to swap the live
    QUOTA_BY_INTENT for an experimental table on the fly.

Output is plain-text (and optionally JSON via ``--json``) so it
embeds cleanly into CI logs.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

# Make `app.*` importable when the script runs from anywhere inside
# the container (working dir = /app in the standard image).
_FASTAPI_ROOT = Path(__file__).resolve().parent.parent
if str(_FASTAPI_ROOT) not in sys.path:
    sys.path.insert(0, str(_FASTAPI_ROOT))


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(levelname)s %(name)s — %(message)s",
    )


def _load_fixture(path: Path):
    from app.agent.golden_query_harness import load_golden_queries  # noqa: PLC0415

    if not path.exists():
        print(f"ERROR: fixture not found at {path}", file=sys.stderr)
        sys.exit(2)
    return load_golden_queries(path)


def _build_factory(quota_override: dict | None):
    """Return a packet_factory closure that runs each query through
    ``prepare_evidence_for_intent`` with the supplied quota override
    (or the per-intent default when ``quota_override is None``)."""
    from app.agent.context_prep import prepare_evidence_for_intent  # noqa: PLC0415

    # Reuse the test module's packet-shape map so we don't duplicate
    # the deterministic mock packets between the test and CLI surfaces.
    # Import lazily so the script can run without pytest installed.
    sys.path.insert(0, str(_FASTAPI_ROOT / "tests"))
    from test_golden_query_regression import _packet_for_query  # type: ignore[import-not-found]  # noqa: PLC0415

    def factory(golden):
        input_packet = _packet_for_query(golden)
        if input_packet is None:
            return None
        prepared = prepare_evidence_for_intent(
            input_packet, golden.intent, quota_override=quota_override,
        )
        return prepared.packet

    return factory


def _format_report_text(report) -> str:
    lines: list[str] = []
    lines.append(f"Golden-query regression — {report.summary()}")
    lines.append("")
    if report.failed_count == 0:
        lines.append("  All queries pass. ✓")
        return "\n".join(lines)
    lines.append(f"  {report.failed_count} failing queries:")
    lines.append("")
    for ev in report.failed_queries():
        lines.append(f"  ✗ {ev.golden.query_id}  (intent={ev.golden.intent})")
        for r in ev.failed_criteria:
            lines.append(f"      - [{r.criterion.kind}] {r.message}")
        lines.append("")
    return "\n".join(lines)


def _format_report_json(report) -> str:
    body = {
        "summary": report.summary(),
        "total": report.total,
        "passed": report.passed_count,
        "failed": report.failed_count,
        "pass_rate": report.pass_rate,
        "failures": [
            {
                "query_id": ev.golden.query_id,
                "intent": ev.golden.intent,
                "tags": list(ev.golden.tags),
                "failed_criteria": [
                    {
                        "kind": r.criterion.kind,
                        "value": r.criterion.value,
                        "actual": r.actual,
                        "message": r.message,
                    }
                    for r in ev.failed_criteria
                ],
                "packet_summary": ev.packet_summary,
            }
            for ev in report.failed_queries()
        ],
    }
    return json.dumps(body, indent=2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="run_golden_harness",
        description=(
            "Run the golden-query regression suite against "
            "prepare_evidence_for_intent. Exits 0 on 100% pass, 1 "
            "otherwise."
        ),
    )
    parser.add_argument(
        "--fixture",
        type=Path,
        default=_FASTAPI_ROOT / "tests" / "golden_queries.json",
        help="Path to the golden_queries.json fixture (default: shipped fixture).",
    )
    parser.add_argument(
        "--quota-override",
        type=str,
        default=None,
        help=(
            "Optional JSON dict to REPLACE the per-intent QUOTA_BY_INTENT "
            "table (applied to every query). Used for A/B benchmarks. "
            "Example: '{\"document\": 5, \"assay\": 0, \"spatial\": 0, "
            "\"table\": 0, \"collar\": 0, \"graph\": 0}'"
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the report as JSON instead of plain text.",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable INFO-level logging from the harness modules.",
    )
    parser.add_argument(
        "--filter-tag",
        type=str,
        default=None,
        help=(
            "Run only golden queries whose ``tags`` includes the given "
            "value (e.g. --filter-tag authority_ranking)."
        ),
    )
    args = parser.parse_args(argv)
    _configure_logging(args.verbose)

    try:
        queries = _load_fixture(args.fixture)
    except Exception as exc:
        print(f"ERROR: failed to load fixture: {exc}", file=sys.stderr)
        return 2

    if args.filter_tag:
        queries = [q for q in queries if args.filter_tag in q.tags]
        if not queries:
            print(f"WARNING: no queries match tag {args.filter_tag!r}", file=sys.stderr)

    quota_override = None
    if args.quota_override:
        try:
            quota_override = json.loads(args.quota_override)
            if not isinstance(quota_override, dict):
                raise ValueError("must be a JSON object")
        except Exception as exc:
            print(f"ERROR: --quota-override must be valid JSON dict: {exc}", file=sys.stderr)
            return 2

    factory = _build_factory(quota_override)

    from app.agent.golden_query_harness import run_golden_harness  # noqa: PLC0415

    report = run_golden_harness(queries, factory)

    if args.json:
        print(_format_report_json(report))
    else:
        print(_format_report_text(report))

    return 0 if report.failed_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
