"""Master-plan §3 Step 9 acceptance test — 50-PDF corpus validator.

Run from inside the georag-fastapi (or any container with app.ocr
importable + asyncpg + access to the running Postgres).

Iterates `tests/fixtures/phase3_pdf_corpus/<profile>/*.pdf`, reads
the sibling `*.label.json` ground-truth, runs the §04p orchestrator
against the PDF, and asserts the actual outcomes match the labels.

Output: per-PDF pass/fail + summary line. Exits 0 only when 100% pass.

Usage:
    docker exec georag-fastapi python /app/scripts/phase3_master_plan_acceptance.py

Or from a host shell (cleaner):
    bash scripts/phase3_master_plan_acceptance.sh

The .sh wrapper just shells into the container + runs this script.

What gets checked per PDF:
    1. expected_profile == orchestrator's profile.document_profile
    2. expected_recommended_action == document_summary.recommended_action
    3. expected_silver_review_page_count == len(silver_review routes)
    4. For each page in review_page_reasons: actual reason matches

The script does NOT touch the live DB — it runs orchestrator() in
memory and reads route_decisions directly. So this is non-destructive
and idempotent.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any


import os as _os
CORPUS_DIR = Path(_os.environ.get(
    "CORPUS_DIR",
    "/tmp/phase3_pdf_corpus",  # copied in by the .sh wrapper
))
PROFILES = ("native", "scanned", "mixed", "table_heavy", "map_heavy")


def _color(s: str, ok: bool) -> str:
    return f"\033[{'32' if ok else '31'}m{s}\033[0m"


def _load_label(pdf_path: Path) -> dict[str, Any] | None:
    label_path = pdf_path.with_suffix(pdf_path.suffix + ".label.json")
    # Convention: <name>.pdf → <name>.pdf.label.json. But the guide
    # also supports <name>.label.json. Try both.
    if not label_path.exists():
        alt = pdf_path.with_suffix(".label.json")
        if alt.exists():
            label_path = alt
        else:
            return None
    try:
        return json.loads(label_path.read_text())
    except Exception as exc:
        print(f"  ! cannot read {label_path}: {exc}")
        return None


async def _validate_pdf(pdf_path: Path, label: dict[str, Any]) -> dict[str, Any]:
    """Run orchestrator, compare to label, return per-check pass/fail."""
    from app.ocr._orchestrator import orchestrate

    result = await orchestrate(pdf_path)

    checks: list[tuple[str, bool, str]] = []

    # Check 1: profile classification
    actual_profile = (result.get("profile") or {}).get("document_profile")
    expected_profile = label.get("profile")
    checks.append((
        "profile",
        actual_profile == expected_profile,
        f"actual={actual_profile} expected={expected_profile}",
    ))

    # Check 2: recommended_action
    actual_action = (result.get("document_summary") or {}).get("recommended_action")
    expected_action = label.get("expected_recommended_action")
    checks.append((
        "recommended_action",
        actual_action == expected_action,
        f"actual={actual_action} expected={expected_action}",
    ))

    # Check 3: review page count
    route_decisions = result.get("route_decisions") or []
    actual_review_count = sum(
        1 for d in route_decisions if d.get("route") == "silver_review"
    )
    expected_review_count = label.get("expected_silver_review_page_count", 0)
    checks.append((
        "review_page_count",
        actual_review_count == expected_review_count,
        f"actual={actual_review_count} expected={expected_review_count}",
    ))

    # Check 4: per-page reasons (when expected_silver_review_page_count > 0
    # AND review_page_reasons provided)
    expected_reasons = label.get("review_page_reasons") or {}
    if expected_reasons:
        all_match = True
        mismatches: list[str] = []
        for page_str, expected_reason in expected_reasons.items():
            page_idx = int(page_str)
            page_decision = next(
                (d for d in route_decisions if d.get("page") == page_idx),
                None,
            )
            actual_reason = (page_decision or {}).get("reason")
            if actual_reason != expected_reason:
                all_match = False
                mismatches.append(
                    f"page{page_idx}: actual={actual_reason} expected={expected_reason}"
                )
        checks.append((
            "review_reasons",
            all_match,
            "; ".join(mismatches) if mismatches else "all match",
        ))

    return {
        "pdf": str(pdf_path.relative_to(CORPUS_DIR.parent)),
        "label_profile": expected_profile,
        "all_pass": all(ok for _, ok, _ in checks),
        "checks": [
            {"name": name, "ok": ok, "detail": detail}
            for name, ok, detail in checks
        ],
    }


async def main() -> int:
    if not CORPUS_DIR.exists():
        print(f"FATAL: corpus dir not found: {CORPUS_DIR}")
        return 2

    results: list[dict[str, Any]] = []
    total_pdfs = 0
    total_labeled = 0

    for profile in PROFILES:
        profile_dir = CORPUS_DIR / profile
        if not profile_dir.exists():
            continue
        for pdf_path in sorted(profile_dir.glob("*.pdf")):
            total_pdfs += 1
            label = _load_label(pdf_path)
            if not label:
                print(_color(
                    f"  SKIP {pdf_path.relative_to(CORPUS_DIR.parent)} "
                    "— no label JSON",
                    True,
                ))
                continue
            total_labeled += 1
            try:
                r = await _validate_pdf(pdf_path, label)
            except Exception as exc:
                print(_color(
                    f"  FAIL {pdf_path.relative_to(CORPUS_DIR.parent)} "
                    f"— orchestrator threw: {type(exc).__name__}: {exc}",
                    False,
                ))
                results.append({
                    "pdf": str(pdf_path.relative_to(CORPUS_DIR.parent)),
                    "all_pass": False,
                    "exception": str(exc),
                })
                continue

            mark = "✓" if r["all_pass"] else "✗"
            line = f"  {mark} {r['pdf']} [{r['label_profile']}]"
            print(_color(line, r["all_pass"]))
            for c in r["checks"]:
                cmark = "✓" if c["ok"] else "✗"
                print(_color(f"      {cmark} {c['name']}: {c['detail']}", c["ok"]))
            results.append(r)

    # Summary
    passed = sum(1 for r in results if r.get("all_pass"))
    failed = len(results) - passed
    print()
    print(f"=== §3 Step 9 acceptance summary ===")
    print(f"  PDFs found:       {total_pdfs}")
    print(f"  PDFs labeled:     {total_labeled}")
    print(f"  Passed:           {passed}")
    print(f"  Failed:           {failed}")
    print()
    if total_labeled == 0:
        print(_color(
            "  No labeled PDFs found — drop PDFs + .label.json files into "
            "tests/fixtures/phase3_pdf_corpus/<profile>/ and re-run.",
            True,
        ))
        return 0  # not a failure; just empty corpus

    if total_labeled < 25:
        print(_color(
            f"  Coverage < 25 PDFs — Step 9 done-test wants 50 (or 25 minimum "
            "for v1 acceptance per LABELING_TRACKER.md reduce-scope option).",
            True,
        ))

    overall_ok = failed == 0 and total_labeled >= 25
    if overall_ok:
        print(_color("  STEP 9 GATE: PASS", True))
        return 0
    else:
        print(_color("  STEP 9 GATE: NOT YET", False))
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
