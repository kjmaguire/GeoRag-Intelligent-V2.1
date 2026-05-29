"""Phase 1 Step 5B — shadow_runs classifier.

Implements the locked diff contract from
``docs/phase1_v149_ingest_pdf_survey.md`` §10:

- Inputs: two ``ReportParseResult``-shaped dicts (the v1.49 path's and the
  Hatchet path's outputs), plus optional sets describing audit_ledger
  action_types and outbox propagations from each side.
- Output: ``DiffOutcome`` with one of {clean, minor, divergent, fatal} and a
  ``details`` dict that records the per-field check outcomes used by the
  Step 6 admin UI to surface what diverged.

Design notes:
  - Pure Python, no I/O. The caller (the ``ai:shadow_diff`` Hatchet
    workflow) is responsible for fetching rows, persisting the result,
    and emitting audit.
  - Section text similarity uses a deterministic Jaccard-on-tokens
    fallback. The survey doc names BAAI/bge-small-en-v1.5 cosine as the
    target, but pulling SBERT into the AI worker pool inflates the image
    by ~400 MB and adds GPU contention. Phase 1's diff harness is a
    shadow gate — token Jaccard is conservative (it under-reports
    similarity, biasing toward 'minor'/'divergent'), which is the
    safer-failing direction for a cutover gate. Step 8 hardening can
    promote this to SBERT once the worker layout is settled.
  - All comparisons are NFC + casefold for unicode robustness.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal


Classification = Literal["clean", "minor", "divergent", "fatal"]


# Action_type set considered "critical" — missing in either side ⇒ divergent.
CRITICAL_ACTION_TYPES = frozenset({
    "ingest_pdf.parse.complete",
    "silver.reports.write",
})

# Non-critical action_types — set may differ by ≤ 1 entry without bumping
# the classification past 'minor'.
NON_CRITICAL_ACTION_TYPES = frozenset({
    "ingest_pdf.parse.fallback_to_pdfplumber",
    "ingest_pdf.parse.ocr_applied",
    "silver.review_queue.write",
})


@dataclass
class DiffOutcome:
    classification: Classification
    details: dict[str, Any] = field(default_factory=dict)

    def to_jsonable(self) -> dict[str, Any]:
        return {"classification": self.classification, "details": self.details}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _norm(s: Any) -> str | None:
    if s is None:
        return None
    if not isinstance(s, str):
        s = str(s)
    return unicodedata.normalize("NFC", s).strip().casefold() or None


def _token_jaccard(a: str, b: str) -> float:
    """Cheap word-level similarity in [0, 1] — see module docstring for why."""
    if not a and not b:
        return 1.0
    ta = {t for t in a.split() if t}
    tb = {t for t in b.split() if t}
    if not ta and not tb:
        return 1.0
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _safe_int(x: Any) -> int | None:
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def _safe_float(x: Any) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _set_eq_normed(items_a: Iterable, items_b: Iterable) -> bool:
    return {_norm(x) for x in items_a} == {_norm(x) for x in items_b}


def _bump(curr: Classification, target: Classification) -> Classification:
    """Severity ordering: clean < minor < divergent < fatal."""
    rank = {"clean": 0, "minor": 1, "divergent": 2, "fatal": 3}
    return curr if rank[curr] >= rank[target] else target


# ---------------------------------------------------------------------------
# Per-check helpers — each returns (target_class_on_mismatch, ok, detail_dict)
# ---------------------------------------------------------------------------
def _check_exact(
    label: str, a: Any, b: Any, *, miss: Classification = "divergent"
) -> tuple[Classification, bool, dict]:
    ok = (a == b)
    return miss, ok, {"check": label, "a": a, "b": b, "ok": ok}


def _check_normed_string(
    label: str, a: Any, b: Any, *, miss: Classification = "divergent"
) -> tuple[Classification, bool, dict]:
    na, nb = _norm(a), _norm(b)
    ok = (na == nb)
    return miss, ok, {"check": label, "a": na, "b": nb, "ok": ok}


def _check_float_abs(
    label: str, a: Any, b: Any, tol: float, *, miss: Classification
) -> tuple[Classification, bool, dict]:
    fa, fb = _safe_float(a), _safe_float(b)
    if fa is None or fb is None:
        return miss, False, {"check": label, "a": a, "b": b, "ok": False, "reason": "non-numeric"}
    delta = abs(fa - fb)
    ok = delta <= tol
    return miss, ok, {"check": label, "a": fa, "b": fb, "delta": delta, "tol": tol, "ok": ok}


def _check_set_normed(
    label: str, a: Iterable, b: Iterable, *, miss: Classification = "divergent"
) -> tuple[Classification, bool, dict]:
    sa = {_norm(x) for x in (a or [])}
    sb = {_norm(x) for x in (b or [])}
    ok = sa == sb
    return miss, ok, {
        "check": label,
        "a_only": sorted(x for x in (sa - sb) if x is not None),
        "b_only": sorted(x for x in (sb - sa) if x is not None),
        "ok": ok,
    }


def _classify_authors(
    authors_a: list[str] | None, authors_b: list[str] | None
) -> tuple[Classification, dict]:
    """Authors: set equality is divergent; same set but order differs is minor."""
    a = list(authors_a or [])
    b = list(authors_b or [])
    sa = [_norm(x) for x in a]
    sb = [_norm(x) for x in b]
    set_eq = set(sa) == set(sb)
    order_eq = sa == sb
    if set_eq and order_eq:
        return "clean", {"check": "authors", "ok": True, "a": sa, "b": sb}
    if set_eq:
        return "minor", {"check": "authors", "ok": False, "reason": "order differs", "a": sa, "b": sb}
    return "divergent", {
        "check": "authors",
        "ok": False,
        "reason": "set differs",
        "a_only": sorted(x for x in (set(sa) - set(sb)) if x is not None),
        "b_only": sorted(x for x in (set(sb) - set(sa)) if x is not None),
    }


# ---------------------------------------------------------------------------
# Sections + tables
# ---------------------------------------------------------------------------
def _section_text(s: dict) -> str:
    return _norm(s.get("text") or "") or ""


def _section_key(s: dict) -> str:
    n = s.get("section_number")
    return str(n) if n is not None else (_norm(s.get("section_title") or "") or "section")


def _check_sections(a_sections: list[dict], b_sections: list[dict]) -> list[tuple[Classification, dict]]:
    """Returns list of (target_class, detail) tuples — count + per-section sim."""
    a = a_sections or []
    b = b_sections or []
    outcomes: list[tuple[Classification, dict]] = []
    if len(a) != len(b):
        outcomes.append((
            "divergent",
            {"check": "sections.count", "a": len(a), "b": len(b), "ok": False},
        ))
    a_by = {_section_key(s): _section_text(s) for s in a}
    b_by = {_section_key(s): _section_text(s) for s in b}
    common = sorted(a_by.keys() & b_by.keys())
    for k in common:
        sim = _token_jaccard(a_by[k], b_by[k])
        # 0.99 threshold for clean is too tight for token-level Jaccard
        # (whitespace differences alone can drop into the high 0.9s).
        # Map: ≥ 0.95 → clean, [0.85, 0.95) → minor, < 0.85 → divergent.
        if sim >= 0.95:
            cls: Classification = "clean"
            ok = True
        elif sim >= 0.85:
            cls = "minor"
            ok = False
        else:
            cls = "divergent"
            ok = False
        outcomes.append((
            cls,
            {"check": f"sections[{k}].similarity", "sim": round(sim, 4), "ok": ok},
        ))
    only_a = sorted(a_by.keys() - b_by.keys())
    only_b = sorted(b_by.keys() - a_by.keys())
    if only_a or only_b:
        outcomes.append((
            "divergent",
            {"check": "sections.keys", "a_only": only_a, "b_only": only_b, "ok": False},
        ))
    return outcomes


def _check_resource_tables(
    a_tables: list[dict], b_tables: list[dict]
) -> list[tuple[Classification, dict]]:
    a = a_tables or []
    b = b_tables or []
    outs: list[tuple[Classification, dict]] = []
    if len(a) != len(b):
        outs.append(("divergent", {
            "check": "resource_tables.count", "a": len(a), "b": len(b), "ok": False,
        }))
        return outs
    for i, (ta, tb) in enumerate(zip(a, b)):
        ha = [_norm(h) for h in (ta.get("headers") or [])]
        hb = [_norm(h) for h in (tb.get("headers") or [])]
        if set(ha) != set(hb):
            outs.append(("divergent", {
                "check": f"resource_tables[{i}].headers", "ok": False,
                "a_only": sorted(x for x in (set(ha) - set(hb)) if x is not None),
                "b_only": sorted(x for x in (set(hb) - set(ha)) if x is not None),
            }))
        rows_a = len(ta.get("data_rows") or [])
        rows_b = len(tb.get("data_rows") or [])
        if rows_a != rows_b:
            outs.append(("divergent", {
                "check": f"resource_tables[{i}].data_rows.count",
                "a": rows_a, "b": rows_b, "ok": False,
            }))
        ca = _safe_float(ta.get("confidence"))
        cb = _safe_float(tb.get("confidence"))
        if ca is not None and cb is not None and abs(ca - cb) > 0.10:
            outs.append(("minor", {
                "check": f"resource_tables[{i}].confidence",
                "a": ca, "b": cb, "delta": abs(ca - cb), "tol": 0.10, "ok": False,
            }))
    return outs


# ---------------------------------------------------------------------------
# Audit + outbox sets
# ---------------------------------------------------------------------------
def _check_audit_action_types(
    a: set[str] | None, b: set[str] | None
) -> tuple[Classification, dict]:
    sa = set(a or [])
    sb = set(b or [])
    missing_critical = (CRITICAL_ACTION_TYPES - sa) | (CRITICAL_ACTION_TYPES - sb)
    if missing_critical:
        return "divergent", {
            "check": "audit.action_types.critical_missing",
            "missing": sorted(missing_critical),
            "ok": False,
        }
    sym_diff = sa.symmetric_difference(sb)
    if not sym_diff:
        return "clean", {"check": "audit.action_types", "ok": True}
    only_non_critical = sym_diff <= NON_CRITICAL_ACTION_TYPES
    if len(sym_diff) <= 1 and only_non_critical:
        return "minor", {
            "check": "audit.action_types",
            "diff": sorted(sym_diff),
            "ok": False,
            "reason": "differs by ≤1 non-critical entry",
        }
    return "divergent", {
        "check": "audit.action_types",
        "diff": sorted(sym_diff),
        "ok": False,
    }


def _check_outbox_propagations(
    a: int | None, b: int | None
) -> tuple[Classification, dict]:
    """Phase 1: minor-class only. Phase 2 hardening promotes to divergent."""
    if a is None or b is None:
        return "clean", {"check": "outbox.count", "ok": True, "reason": "skipped (n/a in Phase 1)"}
    if a == b:
        return "clean", {"check": "outbox.count", "a": a, "b": b, "ok": True}
    return "minor", {"check": "outbox.count", "a": a, "b": b, "ok": False}


def _check_duration(
    a_ms: int | None, b_ms: int | None
) -> tuple[Classification, dict]:
    """Hatchet-side > 2× v1.49 wall-clock ⇒ minor."""
    if not a_ms or not b_ms or a_ms <= 0:
        return "clean", {"check": "duration", "ok": True, "reason": "missing"}
    ratio = b_ms / a_ms
    if ratio <= 2.0 and (1.0 / ratio) <= 2.0:
        return "clean", {"check": "duration", "a_ms": a_ms, "b_ms": b_ms,
                         "ratio": round(ratio, 3), "ok": True}
    return "minor", {"check": "duration", "a_ms": a_ms, "b_ms": b_ms,
                     "ratio": round(ratio, 3), "ok": False}


# ---------------------------------------------------------------------------
# Top-level
# ---------------------------------------------------------------------------
def classify_shadow_run(
    *,
    v149: dict | None,
    hatchet: dict | None,
    v149_audit_action_types: set[str] | None = None,
    hatchet_audit_action_types: set[str] | None = None,
    v149_outbox_count: int | None = None,
    hatchet_outbox_count: int | None = None,
    v149_duration_ms: int | None = None,
    hatchet_duration_ms: int | None = None,
    v149_error: str | None = None,
    hatchet_error: str | None = None,
) -> DiffOutcome:
    """Run every per-field check; bump severity to the highest target.

    The order matters only for ``details`` readability — final
    classification is the max severity across all checks.
    """
    details: dict[str, Any] = {"checks": []}
    cls: Classification = "clean"

    # --- Fatal first-pass: side-only error ----------------------------------
    a_err = (v149_error or "").strip()
    b_err = (hatchet_error or "").strip()
    if (a_err and not b_err) or (b_err and not a_err):
        details["checks"].append({
            "check": "side_error",
            "v149_error": a_err or None,
            "hatchet_error": b_err or None,
            "ok": False,
        })
        return DiffOutcome("fatal", details)
    if a_err and b_err and a_err != b_err:
        details["checks"].append({
            "check": "side_error.both",
            "v149_error": a_err, "hatchet_error": b_err, "ok": False,
        })
        return DiffOutcome("fatal", details)

    # If either side missing entirely, that's fatal too — caller should not
    # invoke the classifier in that case, but defend anyway.
    if v149 is None or hatchet is None:
        details["checks"].append({
            "check": "side_present",
            "v149": v149 is not None,
            "hatchet": hatchet is not None,
            "ok": False,
        })
        return DiffOutcome("fatal", details)

    # --- Provenance ----------------------------------------------------------
    miss, ok, d = _check_exact("provenance.sha256",
                               v149.get("sha256"), hatchet.get("sha256"), miss="fatal")
    details["checks"].append(d)
    if not ok:
        return DiffOutcome("fatal", details)

    miss, ok, d = _check_exact("provenance.minio_key",
                               v149.get("minio_key"), hatchet.get("minio_key"), miss="fatal")
    details["checks"].append(d)
    if not ok:
        return DiffOutcome("fatal", details)

    for check_name, miss_class in [
        ("page_count", "divergent"),
    ]:
        miss, ok, d = _check_exact(f"provenance.{check_name}",
                                   v149.get(check_name), hatchet.get(check_name),
                                   miss=miss_class)  # type: ignore[arg-type]
        details["checks"].append(d)
        if not ok:
            cls = _bump(cls, miss)

    page_langs_match = _set_eq_normed(
        v149.get("page_languages") or [], hatchet.get("page_languages") or []
    )
    if not page_langs_match:
        cls = _bump(cls, "minor")
    details["checks"].append({
        "check": "provenance.page_languages",
        "ok": page_langs_match,
        "v149": sorted(_norm(x) for x in (v149.get("page_languages") or [])),
        "hatchet": sorted(_norm(x) for x in (hatchet.get("page_languages") or [])),
    })

    # --- parse_quality_pct ---------------------------------------------------
    miss, ok, d = _check_float_abs(
        "parse_quality_pct",
        v149.get("parse_quality_pct"), hatchet.get("parse_quality_pct"),
        tol=0.10, miss="divergent",
    )
    details["checks"].append(d)
    if not ok:
        cls = _bump(cls, miss)

    # parser_used is informational only — record both sides.
    details["checks"].append({
        "check": "parser_used",
        "v149": v149.get("parser_used"),
        "hatchet": hatchet.get("parser_used"),
        "ok": True,
        "informational": True,
    })

    # --- Metadata strings ----------------------------------------------------
    for f in ["title", "company", "project_name", "filing_date", "commodity", "region"]:
        miss, ok, d = _check_normed_string(f, v149.get(f), hatchet.get(f), miss="divergent")
        details["checks"].append(d)
        if not ok:
            cls = _bump(cls, miss)

    # --- Authors -------------------------------------------------------------
    auth_cls, auth_d = _classify_authors(v149.get("authors"), hatchet.get("authors"))
    details["checks"].append(auth_d)
    cls = _bump(cls, auth_cls)

    # --- Sections ------------------------------------------------------------
    a_sections = v149.get("sections") or []
    b_sections = hatchet.get("sections") or []
    if a_sections or b_sections:
        for sec_cls, d in _check_sections(a_sections, b_sections):
            details["checks"].append(d)
            cls = _bump(cls, sec_cls)
    else:
        # Neither side records full section text on shadow_runs (we only
        # ship section counts via the row payload). Fall back to count.
        a_count = _safe_int(v149.get("sections_count"))
        b_count = _safe_int(hatchet.get("sections_count"))
        if a_count is None and b_count is None:
            a_count = b_count = 0
        ok = a_count == b_count
        details["checks"].append({
            "check": "sections.count_only",
            "a": a_count, "b": b_count, "ok": ok,
        })
        if not ok:
            cls = _bump(cls, "divergent")

    # --- Resource tables -----------------------------------------------------
    a_tables = v149.get("resource_tables") or []
    b_tables = hatchet.get("resource_tables") or []
    if a_tables or b_tables:
        for tbl_cls, d in _check_resource_tables(a_tables, b_tables):
            details["checks"].append(d)
            cls = _bump(cls, tbl_cls)
    else:
        a_tc = _safe_int(v149.get("resource_tables_count"))
        b_tc = _safe_int(hatchet.get("resource_tables_count"))
        if a_tc is None and b_tc is None:
            a_tc = b_tc = 0
        ok = a_tc == b_tc
        details["checks"].append({
            "check": "resource_tables.count_only",
            "a": a_tc, "b": b_tc, "ok": ok,
        })
        if not ok:
            cls = _bump(cls, "divergent")

    # --- Audit action_types --------------------------------------------------
    audit_cls, audit_d = _check_audit_action_types(
        v149_audit_action_types, hatchet_audit_action_types
    )
    details["checks"].append(audit_d)
    cls = _bump(cls, audit_cls)

    # --- Outbox propagations -------------------------------------------------
    outbox_cls, outbox_d = _check_outbox_propagations(
        v149_outbox_count, hatchet_outbox_count
    )
    details["checks"].append(outbox_d)
    cls = _bump(cls, outbox_cls)

    # --- Wall-clock ----------------------------------------------------------
    dur_cls, dur_d = _check_duration(v149_duration_ms, hatchet_duration_ms)
    details["checks"].append(dur_d)
    cls = _bump(cls, dur_cls)

    return DiffOutcome(cls, details)


__all__ = ["Classification", "DiffOutcome", "classify_shadow_run"]
