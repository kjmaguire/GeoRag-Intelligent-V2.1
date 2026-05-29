"""§04p orchestrator — chains preflight → profile → parsers → routing → summary.

**Master-plan §3 Step 7 (part A — doc-phase 55).** This module is the
"glue" that the Hatchet `ingest_pdf` step calls. It encapsulates the
full §04p decision flow against a single PDF and returns a structured
result that the persistence layer (doc-phase 56's `_persist.py`) can
write to the eight silver tables.

**Internal module** (leading underscore): not exported via `app/ocr/__init__.py`.
Only the Hatchet `ingest_pdf` workflow + integration tests should
import it.

Flow:
    1. preflight                                  → preflight_result
    2. If preflight invalid → bail with reject doc-level recommendation
    3. profile                                    → profile_result
    4. Dispatch parsers based on document profile:
       - native       → parse_native
       - scanned      → parse_scanned
       - mixed        → parse_mixed (Docling) +
                        parse_scanned on pages_needing_ocr
       - table_heavy  → parse_table_heavy
       - map_heavy    → no parser; every page routes to review
    5. For each page, call route_page
    6. For each re_ocr decision, retry parse_scanned with escalated settings
       (up to MAX_OCR_RETRIES per page); re-route after each retry
    7. summarize_document over all per-page routes
    8. Return everything in a single OrchestratorResult dict

The orchestrator does NOT write to the database. Persistence happens
in `_persist.py` (next doc-phase tick), called by the Hatchet
`ingest_pdf.persist` step.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from app.ocr.parse_mixed import parse_mixed
from app.ocr.parse_native import parse_native
from app.ocr.parse_scanned import parse_scanned
from app.ocr.parse_table_heavy import parse_table_heavy
from app.ocr.preflight import preflight
from app.ocr.profile import profile
from app.ocr.quality_graph import (
    MAX_OCR_RETRIES,
    route_page,
    summarize_document,
)


async def orchestrate(
    pdf_path: Path,
) -> dict[str, Any]:
    """Run the full §04p pipeline against a single PDF.

    Returns a dict with everything the persistence layer needs:

        {
            "preflight": dict,                # preflight result
            "profile": dict | None,           # None if preflight failed
            "parses": {                        # parser outputs by name
                "native": dict | None,
                "scanned": dict | None,        # may include retry passes merged
                "mixed": dict | None,
                "table_heavy": dict | None,
            },
            "route_decisions": list[dict],     # one per page, post-retry
            "document_summary": dict,          # summarize_document() output
            "retry_log": list[dict],           # diagnostic: per-retry attempts
        }
    """
    pf = await preflight(pdf_path)
    result: dict[str, Any] = {
        "preflight": pf,
        "profile": None,
        "parses": {
            "native": None,
            "scanned": None,
            "mixed": None,
            "table_heavy": None,
        },
        "route_decisions": [],
        "document_summary": {},
        "retry_log": [],
    }

    # Stage 1: preflight gate
    if not pf.get("valid"):
        # No page-level routes available; build a single doc-level reject.
        result["document_summary"] = summarize_document([
            {"route": "reject", "reason": _preflight_error_reason(pf.get("error"))}
        ])
        return result

    # Stage 2: profile
    prof = await profile(pdf_path)
    result["profile"] = prof
    document_profile = prof["document_profile"]
    per_page_profiles: list[str] = prof["per_page_profiles"]

    # Stage 3: parser dispatch by document profile
    parses = result["parses"]
    if document_profile == "native":
        parses["native"] = await parse_native(pdf_path)
    elif document_profile == "table_heavy":
        parses["table_heavy"] = await parse_table_heavy(pdf_path)
    elif document_profile == "scanned":
        parses["scanned"] = await parse_scanned(pdf_path)
    elif document_profile == "mixed":
        mixed_result = await parse_mixed(pdf_path)
        parses["mixed"] = mixed_result
        # Mixed documents may have pages that came back without text
        # from Docling (image-only pages); OCR those.
        if mixed_result.get("pages_needing_ocr"):
            scanned_for_ocr = await parse_scanned(
                pdf_path,
                pages=mixed_result["pages_needing_ocr"],
            )
            parses["scanned"] = scanned_for_ocr
    elif document_profile == "map_heavy":
        # No parser dispatched; every page routes to review.
        pass

    # Stage 4: per-page routing
    page_count = len(per_page_profiles)
    route_decisions: list[dict[str, Any]] = []
    retry_log: list[dict[str, Any]] = []

    for page_idx in range(page_count):
        page_profile = per_page_profiles[page_idx]
        # Pick the parse result that covers this page based on profile.
        parse_result_for_page = _parse_result_for_page(
            parses, page_profile, page_idx, document_profile
        )

        decision = await route_page(
            parse_result_for_page,
            page_idx,
            page_profile,
            preflight=pf,
            retry_count=0,
        )

        # Stage 5: re_ocr retry loop
        retry_count = 0
        current_parse_result = parse_result_for_page
        while (
            decision["route"] == "re_ocr"
            and retry_count < MAX_OCR_RETRIES
        ):
            retry_settings = decision["retry_settings"]
            retry_log.append({
                "page": page_idx,
                "attempt": retry_count + 1,
                "settings": retry_settings,
                "previous_confidence": decision["confidence_scores"].get(
                    "ocr_confidence"
                ),
            })

            retry_parse = await parse_scanned(
                pdf_path,
                pages=[page_idx],
                settings=retry_settings,
            )
            # Merge the retry parse into the current single-page result so
            # the next route_page call sees the new confidence values.
            current_parse_result = _merge_retry_into_parse(
                current_parse_result, retry_parse, page_idx
            )
            retry_count += 1

            decision = await route_page(
                current_parse_result,
                page_idx,
                page_profile,
                preflight=pf,
                retry_count=retry_count,
            )

        # Update the parses dict with the merged retry result so the
        # persistence layer sees the final scanned-OCR output.
        if retry_count > 0 and page_profile in ("scanned", "mixed"):
            parses["scanned"] = current_parse_result

        route_decisions.append({**decision, "page": page_idx})

    result["route_decisions"] = route_decisions
    result["retry_log"] = retry_log

    # Stage 6: document-level summary
    result["document_summary"] = summarize_document(
        route_decisions, page_profiles=per_page_profiles
    )

    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_result_for_page(
    parses: dict[str, Any],
    page_profile: str,
    page_idx: int,
    document_profile: str,
) -> dict[str, Any]:
    """Pick the right parse result for a page, considering both
    the page-level profile and the document-level dispatch decision.

    For mixed documents, native-profile pages read from `parses["mixed"]`
    and scanned-profile pages read from `parses["scanned"]` (the OCR
    pass that ran for pages_needing_ocr).
    """
    if page_profile == "native":
        return parses.get("native") or parses.get("mixed") or {}
    if page_profile == "table_heavy":
        return parses.get("table_heavy") or parses.get("native") or {}
    if page_profile == "scanned":
        return parses.get("scanned") or parses.get("mixed") or {}
    if page_profile == "mixed":
        return parses.get("mixed") or parses.get("native") or {}
    if page_profile == "map_heavy":
        return {}
    # Fallback: pick whatever parser ran for the document
    for key in ("native", "scanned", "mixed", "table_heavy"):
        if parses.get(key):
            return parses[key]
    return {}


def _merge_retry_into_parse(
    base: dict[str, Any],
    retry: dict[str, Any],
    page_idx: int,
) -> dict[str, Any]:
    """Return a new parse_result with the retry pass's data for `page_idx`
    merged in. Other pages keep the base values.

    The merge replaces:
      - per_page_ocr_confidence[page_idx]
      - per_page_text_line_counts[page_idx]
      - per_page_retry_counts[page_idx] (incremented)
      - per_page_deskew_applied[page_idx]
      - per_page_rotation_applied[page_idx]
      - any passages on this page get replaced with the retry's passages
    """
    merged = {**base}
    arrays = (
        "per_page_ocr_confidence",
        "per_page_text_line_counts",
        "per_page_deskew_applied",
        "per_page_rotation_applied",
        "per_page_retry_counts",
    )
    for key in arrays:
        if key in merged and key in retry:
            new_list = list(merged[key])
            retry_list = retry.get(key, [])
            # retry result indexes relative to the requested page range;
            # the orchestrator always passes pages=[page_idx] so the
            # retry's index 0 corresponds to page_idx in the merged frame.
            # The retry parser's per_page_* arrays have length equal to
            # the number of pages parsed (1), so retry_list[0] is the
            # value we need.
            if retry_list:
                # Special case: retry_count gets incremented, not replaced
                if key == "per_page_retry_counts":
                    new_list[page_idx] = new_list[page_idx] + 1
                else:
                    new_list[page_idx] = retry_list[0]
            merged[key] = new_list

    # Replace passages for this page
    if "passages" in merged and "passages" in retry:
        non_page_passages = [
            p for p in merged["passages"] if p.get("page") != page_idx
        ]
        retry_passages = retry["passages"]
        # The retry parser uses pages=[page_idx] so its passages already
        # have the correct page index assigned.
        merged["passages"] = non_page_passages + retry_passages

    return merged


def _preflight_error_reason(error: str | None) -> str:
    """Map preflight error → silver review reason for the document summary."""
    if not error:
        return "other"
    err = error.lower()
    if "encrypted" in err:
        return "encrypted_section"
    if "file_not_found" in err or "magic" in err or "pdf_error" in err:
        return "page_blank_or_corrupted"
    return "other"
