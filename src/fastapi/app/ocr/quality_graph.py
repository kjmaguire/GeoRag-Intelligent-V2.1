"""§04p LangGraph OCR Quality Graph — confidence-driven page routing.

**Master-plan §9.7 reference.** Routes parsed pages by composite
confidence (OCR × layout × table) to one of:
- ``accept`` — high confidence, no further action
- ``re_ocr`` — retry with escalated engine settings (max 2 retries)
- ``silver_review`` — human in the loop; writes
  ``silver.low_confidence_page_reviews`` row with a reason code
- ``reject`` — hard failure (corrupted, password-protected); writes
  ``silver.document_ingestion_quality.recommended_action = "reject"``

**Status:** Step 6 implementation (doc-phase 54). Final skeleton
graduation.

**Design note — pure function, not LangGraph state machine.** The
master plan calls this the "LangGraph OCR Quality Graph"; the actual
decision per page is a bounded one-shot classification. Wrapping it
in a LangGraph state machine would add framework complexity without
adding capability — the retry LOOP lives at the Hatchet orchestrator
level (Step 7), not inside this module. If future steps demonstrate
real value from LangGraph wrapping (replay, observability, branching
that calls back into other graphs), this module can be wrapped
without changing its public API. For now: clear pure function with
the graph structure documented in the docstring below.

**State diagram** (per page):
```
                ┌─────────────────────┐
                │   profile == ?      │
                └──────────┬──────────┘
            ┌──────────────┼───────────────┬──────────────────┐
            ▼              ▼               ▼                  ▼
        map_heavy    scanned/mixed    native/table_heavy   preflight invalid
            │              │               │                  │
            │       conf < REVIEW_FLOOR    │                  ▼
            │              │               │              reject(reason)
            │              ▼               │
            │       retries < MAX?         │
            │       ┌──────┴──────┐        │
            │       ▼             ▼        │
            │     re_ocr   silver_review   │
            │  (escalated)  (retry_max     │
            │               _exceeded)     │
            │                              │
            │       conf >= ACCEPT_FLOOR   │
            │              │               │
            ▼              ▼               ▼
        silver_review  accept         accept
        (map_heavy
        _v1_deferral)
```

**Reason codes** match the ``silver.low_confidence_page_reviews.reason``
CHECK enum (doc-phase 50):
- ``map_heavy_v1_deferral`` — §9.4 deferral; map pages always route here
- ``ocr_confidence_below_threshold`` — composite confidence below floor
- ``layout_confidence_below_threshold`` — Docling layout score too low
- ``table_confidence_below_threshold`` — table structure below threshold
- ``page_blank_or_corrupted`` — zero text lines OR parse error
- ``retry_max_exceeded`` — both retries exhausted, still below floor
- ``rotation_undetectable``, ``deskew_failed_image_quality``,
  ``handwriting_unparseable``, ``non_english_unsupported_language``,
  ``encrypted_section``, ``other`` — reserved for Step 7+ when richer
  diagnostics flow through
"""
from __future__ import annotations

import asyncio
from typing import Any, Literal

PageRoute = Literal["accept", "re_ocr", "silver_review", "reject"]


# ---------------------------------------------------------------------------
# Routing thresholds. All currently module-level constants for easy
# tuning during Step 9 acceptance-corpus runs. Production callers
# may override via the `thresholds` kwarg to route_page().
# ---------------------------------------------------------------------------
ACCEPT_OCR_CONFIDENCE = 0.85       # ≥ this → accept (scanned/mixed)
REVIEW_OCR_CONFIDENCE = 0.50       # < this → silver_review (no retry)
                                    # in between → re_ocr (if retries available)
ACCEPT_LAYOUT_CONFIDENCE = 0.70
REVIEW_LAYOUT_CONFIDENCE = 0.40

MAX_OCR_RETRIES = 2

# Escalated PaddleOCR settings for retry passes. The Hatchet orchestrator
# (Step 7) reads these from the route decision and re-invokes parse_scanned.
RETRY_SETTINGS_BY_ATTEMPT: list[dict[str, Any]] = [
    # Attempt 1 (first retry): more aggressive binarization
    {
        "use_angle_cls": True,
        "lang": "en",
        "render_scale": 3.0,             # higher DPI
        "_retry_note": "higher_render_scale",
    },
    # Attempt 2 (second retry): try multi-language hint
    {
        "use_angle_cls": True,
        "lang": "en",                    # PaddleOCR currently no PP-OCRv5 multi yet
        "render_scale": 4.0,
        "_retry_note": "max_render_scale",
    },
]


async def route_page(
    parse_result: dict[str, Any],
    page: int,
    profile: str,
    preflight: dict[str, Any] | None = None,
    retry_count: int = 0,
    thresholds: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Route a parsed page to accept / re-OCR / Silver Review / reject.

    Args:
        parse_result: Output from one of the ``parse_*`` functions
            (parse_native, parse_scanned, parse_mixed, parse_table_heavy).
            The function reads per-page confidences from this dict.
        page: 0-indexed page number being routed.
        profile: Per-page profile from app.ocr.profile (native,
            scanned, mixed, map_heavy, table_heavy).
        preflight: Optional preflight result (from app.ocr.preflight).
            If preflight.valid is False, the route is `reject`
            regardless of parse result.
        retry_count: How many re-OCR attempts have already been made
            on this page. The orchestrator increments this between
            retry passes.
        thresholds: Optional per-call override of module-level
            threshold constants (e.g. for testing). Keys:
            "accept_ocr", "review_ocr", "accept_layout", "review_layout".

    Returns:
        Route decision dict:
            {
                "route": PageRoute,
                "reason": str | None,
                "confidence_scores": dict[str, float],
                "retry_settings": dict | None,  # when route == "re_ocr"
                "retry_count": int,             # incremented if re_ocr
            }
    """
    return await asyncio.to_thread(
        _route_page_sync, parse_result, page, profile, preflight, retry_count, thresholds
    )


def _route_page_sync(
    parse_result: dict[str, Any],
    page: int,
    profile: str,
    preflight: dict[str, Any] | None,
    retry_count: int,
    thresholds: dict[str, float] | None,
) -> dict[str, Any]:
    """Synchronous implementation."""
    t = _resolve_thresholds(thresholds)

    # ---- Stage 0: preflight gate ----
    if preflight is not None and preflight.get("valid") is False:
        err = preflight.get("error") or "unknown"
        reject_reason = _map_preflight_error_to_reason(err)
        return _decision(
            "reject",
            reason=reject_reason,
            confidence_scores={},
            retry_count=retry_count,
        )

    # ---- Stage 1: profile-based shortcut ----
    if profile == "map_heavy":
        return _decision(
            "silver_review",
            reason="map_heavy_v1_deferral",
            confidence_scores={},
            retry_count=retry_count,
        )

    # ---- Stage 2: extract per-page confidence signals ----
    scores = _extract_scores(parse_result, page)

    # ---- Stage 3: native + table-heavy (no OCR) routing ----
    if profile in ("native", "table_heavy"):
        # Native parser is high-confidence by construction
        # (text-layer extraction is deterministic). The main reason
        # to flag native pages is when zero passages came back — that
        # typically means the page is blank or corrupted.
        if scores["text_region_count"] == 0:
            return _decision(
                "silver_review",
                reason="page_blank_or_corrupted",
                confidence_scores=scores,
                retry_count=retry_count,
            )
        # Table-heavy: also check structure / cell confidence
        if profile == "table_heavy" and scores["table_count"] > 0:
            if scores["min_table_structure_confidence"] < t["accept_table_structure"]:
                return _decision(
                    "silver_review",
                    reason="table_confidence_below_threshold",
                    confidence_scores=scores,
                    retry_count=retry_count,
                )
        return _decision(
            "accept",
            reason=None,
            confidence_scores=scores,
            retry_count=retry_count,
        )

    # ---- Stage 4: scanned + mixed (OCR-bearing) routing ----
    if profile in ("scanned", "mixed"):
        ocr_conf = scores["ocr_confidence"]
        text_lines = scores["text_region_count"]

        if text_lines == 0:
            return _decision(
                "silver_review",
                reason="page_blank_or_corrupted",
                confidence_scores=scores,
                retry_count=retry_count,
            )

        if ocr_conf >= t["accept_ocr"]:
            # Also check layout if mixed (Docling provides layout
            # confidence)
            layout_conf = scores.get("layout_confidence")
            if layout_conf is not None and layout_conf < t["review_layout"]:
                return _decision(
                    "silver_review",
                    reason="layout_confidence_below_threshold",
                    confidence_scores=scores,
                    retry_count=retry_count,
                )
            return _decision(
                "accept",
                reason=None,
                confidence_scores=scores,
                retry_count=retry_count,
            )

        # ocr_conf < accept_ocr → either re-OCR (if retries left) or review
        if ocr_conf < t["review_ocr"]:
            # Very low confidence — straight to review, retries won't help
            return _decision(
                "silver_review",
                reason="ocr_confidence_below_threshold",
                confidence_scores=scores,
                retry_count=retry_count,
            )

        # Between review_ocr and accept_ocr → try a retry
        if retry_count >= MAX_OCR_RETRIES:
            return _decision(
                "silver_review",
                reason="retry_max_exceeded",
                confidence_scores=scores,
                retry_count=retry_count,
            )

        retry_settings = RETRY_SETTINGS_BY_ATTEMPT[retry_count]
        return _decision(
            "re_ocr",
            reason=None,
            confidence_scores=scores,
            retry_settings=retry_settings,
            retry_count=retry_count + 1,
        )

    # ---- Stage 5: unknown profile fallback ----
    return _decision(
        "silver_review",
        reason="other",
        confidence_scores=scores,
        retry_count=retry_count,
    )


def summarize_document(
    route_decisions: list[dict[str, Any]],
    page_profiles: list[str] | None = None,
) -> dict[str, Any]:
    """Build a document-level `recommended_action` from per-page routes.

    Mapping to silver.document_ingestion_quality.recommended_action
    (CHECK enum from doc-phase 50):

    - ``reject`` — any page returned route=='reject' (preflight gate)
    - ``review_all_pages`` — every page routed to silver_review
    - ``accept_with_review`` — at least one silver_review page; rest accept
    - ``accept`` — all pages route to accept

    Args:
        route_decisions: list of dicts from route_page, one per page.
        page_profiles: optional per-page profiles (for map-heavy
            doc-level detection). When ≥50% of pages are map_heavy
            and the rest are accepted, we still recommend
            ``accept_with_review`` rather than ``review_all_pages``
            (the accepted text pages are still useful).

    Returns:
        Dict with:
            {
                "recommended_action": str,
                "total_pages": int,
                "accept_count": int,
                "review_count": int,
                "retry_count": int,    # pages still in retry state
                "reject_count": int,
                "review_reasons": dict[str, int],  # reason → count
            }
    """
    total = len(route_decisions)
    accept = sum(1 for d in route_decisions if d["route"] == "accept")
    review = sum(1 for d in route_decisions if d["route"] == "silver_review")
    retry = sum(1 for d in route_decisions if d["route"] == "re_ocr")
    reject = sum(1 for d in route_decisions if d["route"] == "reject")

    review_reasons: dict[str, int] = {}
    for d in route_decisions:
        if d["route"] == "silver_review" and d.get("reason"):
            review_reasons[d["reason"]] = review_reasons.get(d["reason"], 0) + 1

    if reject > 0:
        action = "reject"
    elif review == total and total > 0:
        action = "review_all_pages"
    elif review > 0 or retry > 0:
        action = "accept_with_review"
    else:
        action = "accept"

    return {
        "recommended_action": action,
        "total_pages": total,
        "accept_count": accept,
        "review_count": review,
        "retry_count": retry,
        "reject_count": reject,
        "review_reasons": review_reasons,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _decision(
    route: PageRoute,
    *,
    reason: str | None,
    confidence_scores: dict[str, float],
    retry_settings: dict[str, Any] | None = None,
    retry_count: int = 0,
) -> dict[str, Any]:
    return {
        "route": route,
        "reason": reason,
        "confidence_scores": confidence_scores,
        "retry_settings": retry_settings,
        "retry_count": retry_count,
    }


def _resolve_thresholds(overrides: dict[str, float] | None) -> dict[str, float]:
    defaults = {
        "accept_ocr": ACCEPT_OCR_CONFIDENCE,
        "review_ocr": REVIEW_OCR_CONFIDENCE,
        "accept_layout": ACCEPT_LAYOUT_CONFIDENCE,
        "review_layout": REVIEW_LAYOUT_CONFIDENCE,
        # Reuse the parse_table_heavy thresholds for the routing decision.
        "accept_table_structure": 0.70,
    }
    if overrides:
        defaults.update(overrides)
    return defaults


def _extract_scores(parse_result: dict[str, Any], page: int) -> dict[str, float]:
    """Pull per-page signals from whatever parser produced the result.

    Different parsers expose different arrays. This helper normalizes
    them into a single dict the routing logic reads from.
    """
    scores: dict[str, float] = {
        "ocr_confidence": 1.0,         # native default — text layer is authoritative
        "layout_confidence": None,
        "text_region_count": 0,
        "table_count": 0,
        "min_table_structure_confidence": 1.0,
    }

    # OCR confidence — only present in parse_scanned output
    per_page_ocr = parse_result.get("per_page_ocr_confidence")
    if per_page_ocr is not None and 0 <= page < len(per_page_ocr):
        scores["ocr_confidence"] = float(per_page_ocr[page])

    # Text region count — varies by parser
    per_page_text = (
        parse_result.get("per_page_text_line_counts")           # scanned
        or parse_result.get("per_page_text_region_counts")      # mixed
        or parse_result.get("per_page_passage_counts")          # native
    )
    if per_page_text is not None and 0 <= page < len(per_page_text):
        scores["text_region_count"] = int(per_page_text[page])

    # Layout confidence — present in parse_mixed output
    per_page_layout = parse_result.get("per_page_layout_confidence")
    if per_page_layout is not None and 0 <= page < len(per_page_layout):
        scores["layout_confidence"] = float(per_page_layout[page])

    # Table count + minimum structure confidence (table-heavy + mixed)
    per_page_tables = parse_result.get("per_page_table_counts")
    if per_page_tables is not None and 0 <= page < len(per_page_tables):
        scores["table_count"] = int(per_page_tables[page])

    page_tables = [
        t for t in (parse_result.get("tables") or [])
        if t.get("page") == page
    ]
    if page_tables:
        scores["min_table_structure_confidence"] = float(
            min(t.get("structure_confidence", 1.0) for t in page_tables)
        )

    return scores


def _map_preflight_error_to_reason(error: str) -> str:
    """Map preflight error strings to silver.low_confidence_page_reviews.reason.

    Used when preflight fails — the route is `reject` but we also want
    a meaningful reason code for `silver.document_ingestion_quality`.
    """
    error_lower = error.lower()
    if "encrypted" in error_lower:
        return "encrypted_section"
    if "file_not_found" in error_lower:
        return "page_blank_or_corrupted"
    if "magic" in error_lower or "pdf_error" in error_lower:
        return "page_blank_or_corrupted"
    return "other"
