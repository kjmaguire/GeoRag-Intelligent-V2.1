"""QA/QC field availability detector — Phase 4 / gate criterion.

The anomaly_detection subgraph (Phase 2) must "correctly use QA/QC fields
when present, and degrade gracefully (use Silver Review metadata only)
when they are absent." Phase 4 added 13 new columns to ``silver.assays_v2``
but legacy rows still have them all as NULL. This detector inspects the
tool results from a query and returns a structured availability summary
that the assemble node converts into a prompt-hint.

The 13 QA/QC fields tracked:
  - blank_result, blank_threshold, blank_pass
  - crm_id, crm_expected, crm_result, crm_pass
  - duplicate_pair_id, duplicate_rpd, duplicate_pass
  - half_dl_substituted
  - batch_id
  - digestion_code

Plus the pre-existing fields the plan considers part of the QA/QC surface:
  - detection_limit, under_detection (== plan's below_detection)
  - lab_name (== plan's lab_id), analysis_method (== plan's method_code)
  - qaqc_flag (legacy Silver Review classification)

The detector is **structure-agnostic** — it walks tool result dicts /
dataclasses looking for known keys. Tool results that don't include
assay data return zero counts; we don't false-fire on documents or
spatial queries.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# Per-row QA/QC fields, grouped by category for the prompt summary.
_QAQC_FIELD_GROUPS: dict[str, tuple[str, ...]] = {
    "blanks": ("blank_result", "blank_threshold", "blank_pass"),
    "crms": ("crm_id", "crm_expected", "crm_result", "crm_pass"),
    "duplicates": ("duplicate_pair_id", "duplicate_rpd", "duplicate_pass"),
    "detection_limits": (
        "detection_limit",
        "below_detection",
        "under_detection",
        "half_dl_substituted",
    ),
    "batch_metadata": ("batch_id", "digestion_code"),
    "lab_metadata": ("lab_name", "lab_id", "analysis_method", "method_code"),
    "legacy_flag": ("qaqc_flag", "qc_flag"),
}


@dataclass
class QaqcGroupAvailability:
    """Availability summary for one QA/QC field group."""

    group: str
    rows_total: int = 0
    rows_with_any_field: int = 0
    field_populated_counts: dict[str, int] = field(default_factory=dict)

    @property
    def status(self) -> str:
        """One of ``absent / partial / present``.

        ``absent``  — zero rows carry any field in the group
        ``partial`` — some rows carry at least one field
        ``present`` — every row carries at least one field
        """
        if self.rows_total == 0 or self.rows_with_any_field == 0:
            return "absent"
        if self.rows_with_any_field == self.rows_total:
            return "present"
        return "partial"

    def describe(self) -> str:
        """One-line human description used in the prompt hint."""
        if self.rows_total == 0:
            return f"{self.group}=no rows"
        return (
            f"{self.group}={self.status}"
            f"({self.rows_with_any_field}/{self.rows_total} rows)"
        )


@dataclass
class QaqcAvailability:
    """Top-level summary across all field groups."""

    groups: dict[str, QaqcGroupAvailability] = field(default_factory=dict)
    inspected_rows: int = 0

    @property
    def has_any_new_qaqc(self) -> bool:
        """True when at least one of the Phase-4 columns is populated.

        Used by the assemble node to decide whether to bias the prompt
        toward the "rich QA/QC table" path or the "graceful degrade to
        Silver Review legacy" path.
        """
        for group_name in ("blanks", "crms", "duplicates"):
            g = self.groups.get(group_name)
            if g is not None and g.rows_with_any_field > 0:
                return True
        return False

    @property
    def has_legacy_qaqc_flag(self) -> bool:
        """True when the legacy qaqc_flag / qc_flag column carries values."""
        g = self.groups.get("legacy_flag")
        return g is not None and g.rows_with_any_field > 0

    def to_prompt_hint(self) -> str:
        """Render the availability as a prompt suffix the LLM can read.

        Returns an empty string when no assay rows were in scope (no QA/QC
        hint is appropriate for a document-only answer).
        """
        if self.inspected_rows == 0:
            return ""
        lines: list[str] = ["QA/QC FIELD AVAILABILITY:"]
        for group_name in (
            "blanks",
            "crms",
            "duplicates",
            "detection_limits",
            "batch_metadata",
            "lab_metadata",
            "legacy_flag",
        ):
            g = self.groups.get(group_name)
            if g is None:
                continue
            lines.append(f"  - {g.describe()}")
        if self.has_any_new_qaqc:
            lines.append(
                "Use the rich QA/QC fields above per the anomaly_table rules. "
                "Mark each row's classification as either 'geological signal' or "
                "'QA/QC artifact' based on the populated fields."
            )
        elif self.has_legacy_qaqc_flag:
            lines.append(
                "GRACEFUL DEGRADE: rich Phase-4 QA/QC fields are absent on these "
                "rows. Fall back to the legacy `qaqc_flag` / `qc_flag` column "
                "(Silver Review classification) for each row. Note in the "
                "Uncertainty section that fuller QA/QC was not captured."
            )
        else:
            lines.append(
                "GRACEFUL DEGRADE: no QA/QC fields are populated on the matched "
                "assay rows. The anomaly classification must rely on the "
                "geological signal alone; flag this as a key uncertainty driver."
            )
        return "\n" + "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Detector
# ---------------------------------------------------------------------------


def _iter_assay_rows(tool_results: Iterable[tuple[str, Any]]) -> Iterable[dict[str, Any]]:
    """Yield assay-shaped rows from any of the orchestrator's tool results.

    The orchestrator's tool layer returns dataclasses (e.g. ``AssayDataResult``)
    that carry a ``samples`` or ``rows`` list of per-row dicts. This helper
    is best-effort — anything that doesn't look like assay data is skipped.
    """
    for tool_name, result in tool_results:
        if tool_name != "query_assay_data" and "assay" not in tool_name:
            continue
        # Try common attribute / key names.
        rows = (
            getattr(result, "samples", None)
            or getattr(result, "rows", None)
            or getattr(result, "results", None)
        )
        if rows is None and isinstance(result, dict):
            rows = result.get("samples") or result.get("rows") or result.get("results")
        if rows is None:
            continue
        for row in rows:
            if isinstance(row, dict):
                yield row
            else:
                # Pydantic-model or dataclass row → coerce to dict.
                if hasattr(row, "model_dump"):
                    try:
                        yield row.model_dump()
                    except Exception:  # pragma: no cover — defensive
                        continue
                elif hasattr(row, "__dict__"):
                    yield vars(row)


def detect_qaqc_availability(
    tool_results: Iterable[tuple[str, Any]],
) -> QaqcAvailability:
    """Walk *tool_results* and tally which QA/QC fields are populated.

    Returns a :class:`QaqcAvailability` summary. Always safe to call —
    non-assay tool results are silently skipped.
    """
    out = QaqcAvailability()
    for row in _iter_assay_rows(tool_results):
        out.inspected_rows += 1
        for group_name, fields_in_group in _QAQC_FIELD_GROUPS.items():
            g = out.groups.setdefault(
                group_name, QaqcGroupAvailability(group=group_name)
            )
            g.rows_total += 1
            row_has_field = False
            for fname in fields_in_group:
                if fname not in row:
                    continue
                val = row[fname]
                if val is None or val == "" or val == []:
                    continue
                g.field_populated_counts[fname] = (
                    g.field_populated_counts.get(fname, 0) + 1
                )
                row_has_field = True
            if row_has_field:
                g.rows_with_any_field += 1
    return out


__all__ = [
    "QaqcAvailability",
    "QaqcGroupAvailability",
    "detect_qaqc_availability",
]
