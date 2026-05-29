"""Plan §6a — Assay validation rule family.

Scans silver.assays_v2 + emits collar-level summary flags to
silver.data_quality_flags so the DrillholeDetail badge surfaces
QA/QC failures + implausibly-high values without forcing the
geologist to dive into per-sample inspection.

Six rules, fan-IN by collar (one summary flag per affected collar
per rule type, count + element list in threshold_payload):

  • assay.qaqc_flag_failed       ERROR    qaqc_flag != 'pass'
  • assay.crm_failed             ERROR    crm_pass = FALSE
  • assay.blank_failed           ERROR    blank_pass = FALSE
  • assay.duplicate_failed       ERROR    duplicate_pass = FALSE
  • assay.value_implausibly_high WARNING  value_ppm > ELEMENT_CEILING
  • assay.detection_paradox      INFO     over_detection AND under_detection both TRUE

Why fan-IN, not fan-OUT (one flag per assay row):

  • The badge counts rows per record_id. A collar with 200 CRM-failed
    assays would render 200 ERROR entries on the badge and drown the
    list. Fan-in to 1 ERROR per (collar, rule) keeps the surface
    actionable.
  • Per-sample granularity is preserved in the threshold_payload
    (sample_ids[] + element list + max value + count). A reviewer
    drilling down sees everything they need.
  • Idempotency key (workspace, collar, flag_type, rule_version)
    means re-runs are no-ops on stable data — the row UPDATEs in
    place with the new count when the assay set changes.

Why NOT a negative-value rule:

  ``silver_assays_v2_valid_value CHECK (value >= 0 OR value IS NULL)``
  already blocks negatives at INSERT time. Routing it as a DQ flag
  would never fire. Implausibly-high values are the real risk because
  they pass the DB CHECK but typically encode a unit error
  (% logged as ppm).

NOTE: Do NOT add ``from __future__ import annotations`` — Dagster 1.13.

Sized 2026-05-29 at 3-4h focused implementation against the
silver_collar_dq template.
"""

import logging
from collections import defaultdict

from dagster import (
    AssetExecutionContext,
    Config,
    MaterializeResult,
    MetadataValue,
    asset,
)

from georag_dagster.dq_writer import DataQualityFlag, upsert_flags_sync
from georag_dagster.resources import PostgresResource


logger = logging.getLogger(__name__)


# Pinned across re-runs — bumping retires the prior rows for SME review.
# Don't change without a planned data migration; the upsert key relies
# on it staying stable.
RULE_VERSION = "v1.0"


# ---------------------------------------------------------------------------
# Per-element ceilings (ppm). Above these is almost certainly a unit
# error — e.g. an assay reported in % accidentally ingested as ppm
# (1.5% Cu → 1.5 ppm, 0.0001% logged as 1 ppm by another lab, etc.).
#
# Values picked to be conservative — they're 10-100× higher than the
# richest documented assays for each element. False positives are
# expected to be near-zero; the real intent is catching
# digit-shift-by-1000 errors common in legacy CSV ingests.
#
# Source: USGS / Wikipedia commodity grade references + Cameco
# production-data sanity-checked against silver.assays_v2.
# ---------------------------------------------------------------------------
# Values of qaqc_flag that mean "this row passed QA/QC" — anything
# outside this set is treated as a failure and flagged.
#
# Why a set, not a single literal: different ingesters use different
# pass-tokens. Cameco's parser writes 'ok'; the public-geoscience
# pipeline writes 'pass'; legacy spreadsheets sometimes write 'good'
# or are NULL. NULL is treated as pass (legacy ingest default) so we
# don't flood the badge for older holes that pre-date QA/QC capture.
QAQC_PASS_VALUES: frozenset[str] = frozenset({"pass", "ok", "good", "valid"})


ELEMENT_CEILING_PPM: dict[str, float] = {
    # Precious metals — small native concentrations
    "AU": 100_000.0,   # 10% Au = 100 oz/t — richest single-shot in history
    "AG": 100_000.0,
    "PT": 100.0,       # PGMs are mass-trace; >100 ppm is a unit error
    "PD": 100.0,
    # Base metals — bigger ore-grade ranges
    "CU": 500_000.0,   # 50% Cu = native copper
    "PB": 500_000.0,
    "ZN": 500_000.0,
    "NI": 200_000.0,
    "CO": 100_000.0,
    "FE": 1_000_000.0,  # 100% Fe = pure ore (the upper bound; physical limit)
    # Energy metals
    "U": 500_000.0,
    "U3O8": 500_000.0,
    "MO": 100_000.0,
    "W": 200_000.0,
    "LI": 50_000.0,
    "BE": 50_000.0,
    "REE": 200_000.0,
}


# ---------------------------------------------------------------------------
# SQL — fetch one row per assay sample. Caller-side aggregation by
# collar happens in Python because the multi-rule output cardinality
# doesn't map cleanly to a single SQL GROUP BY.
# ---------------------------------------------------------------------------

SELECT_ASSAYS_SQL = """
SELECT
    a.id::text                  AS assay_id,
    a.collar_id::text           AS collar_id,
    a.workspace_id::text        AS workspace_id,
    a.sample_id                 AS sample_id,
    a.element                   AS element,
    a.value                     AS value,
    a.value_ppm                 AS value_ppm,
    a.unit                      AS unit,
    a.qaqc_flag                 AS qaqc_flag,
    a.crm_pass                  AS crm_pass,
    a.blank_pass                AS blank_pass,
    a.duplicate_pass            AS duplicate_pass,
    a.over_detection            AS over_detection,
    a.under_detection           AS under_detection,
    co.project_id::text         AS project_id,
    co.hole_id                  AS hole_id
FROM silver.assays_v2 a
JOIN silver.collars co ON co.collar_id = a.collar_id
"""


class AssayDQConfig(Config):
    """Asset config. Defaults to a full sweep; an operator can limit
    by workspace for an ad-hoc check during the rollout."""

    workspace_id: str | None = None
    """Optional workspace_id filter. None = sweep every workspace."""


# ---------------------------------------------------------------------------
# Pure rule evaluation — set of rows in, list[DataQualityFlag] out
# ---------------------------------------------------------------------------


def _element_key(element: str | None) -> str:
    """Normalise element symbol for the ceiling lookup."""
    if not element:
        return ""
    return element.strip().upper()


def _exceeds_ceiling(element: str | None, value_ppm) -> bool:
    """Return True if value_ppm exceeds the per-element ceiling."""
    if value_ppm is None:
        return False
    key = _element_key(element)
    ceiling = ELEMENT_CEILING_PPM.get(key)
    if ceiling is None:
        # Unknown element — no ceiling defined, can't flag.
        return False
    try:
        return float(value_ppm) > ceiling
    except (TypeError, ValueError):
        return False


def evaluate_assay_rows_for_collar(
    *,
    collar_id: str,
    workspace_id: str,
    project_id: str,
    hole_id: str | None,
    rows: list[dict],
) -> list[DataQualityFlag]:
    """Apply the 6 assay rules to all rows for one collar.

    Fan-IN: one collar's N assay rows → up to 6 summary flags
    (one per rule type that fires). Pure function — no DB / no I/O —
    so unit-testable without a Dagster harness.

    Args:
        collar_id: the collar this batch belongs to.
        workspace_id: tenancy.
        project_id: tenancy.
        hole_id: human-friendly identifier for descriptions.
        rows: assay rows from SELECT_ASSAYS_SQL (or test fixtures).
            Each dict carries assay_id, sample_id, element, value_ppm,
            qaqc_flag, crm_pass, blank_pass, duplicate_pass,
            over_detection, under_detection.

    Returns:
        Up to 6 :class:`DataQualityFlag` rows. Empty list = all rules
        pass for this collar.
    """
    hole_label = hole_id or collar_id[:8]
    base = dict(
        workspace_id=workspace_id,
        project_id=project_id,
        record_type="collar",
        record_id=collar_id,
        rule_version=RULE_VERSION,
    )

    # Bucket each row by the rule(s) it triggers. Element + sample_id
    # roll up into the threshold_payload so a reviewer can drill down.
    qaqc_hits: list[dict] = []
    crm_hits: list[dict] = []
    blank_hits: list[dict] = []
    dup_hits: list[dict] = []
    high_hits: list[dict] = []  # implausibly-high
    paradox_hits: list[dict] = []  # over+under detection both true

    for r in rows:
        sample_id = r.get("sample_id")
        element = r.get("element")
        assay_id = r.get("assay_id")

        # QAQC flag — anything not in QAQC_PASS_VALUES is a fail.
        # NULL is treated as a legacy pass so we don't flood the badge
        # for older holes that pre-date QA/QC capture.
        qaqc = r.get("qaqc_flag")
        if qaqc is not None and qaqc.strip().lower() not in QAQC_PASS_VALUES:
            qaqc_hits.append({
                "assay_id": assay_id,
                "sample_id": sample_id,
                "element": element,
                "qaqc_flag": qaqc,
            })

        if r.get("crm_pass") is False:
            crm_hits.append({
                "assay_id": assay_id,
                "sample_id": sample_id,
                "element": element,
            })

        if r.get("blank_pass") is False:
            blank_hits.append({
                "assay_id": assay_id,
                "sample_id": sample_id,
                "element": element,
            })

        if r.get("duplicate_pass") is False:
            dup_hits.append({
                "assay_id": assay_id,
                "sample_id": sample_id,
                "element": element,
            })

        if _exceeds_ceiling(element, r.get("value_ppm")):
            high_hits.append({
                "assay_id": assay_id,
                "sample_id": sample_id,
                "element": element,
                "value_ppm": float(r["value_ppm"]),
                "ceiling_ppm": ELEMENT_CEILING_PPM[_element_key(element)],
            })

        if r.get("over_detection") and r.get("under_detection"):
            paradox_hits.append({
                "assay_id": assay_id,
                "sample_id": sample_id,
                "element": element,
            })

    flags: list[DataQualityFlag] = []

    if qaqc_hits:
        n = len(qaqc_hits)
        elems = sorted({h["element"] for h in qaqc_hits if h["element"]})
        flags.append(DataQualityFlag(
            **base,
            flag_type="assay.qaqc_flag_failed",
            severity="ERROR",
            description=(
                f"Collar {hole_label}: {n} assay sample(s) with "
                f"qaqc_flag != 'pass'. Affected elements: "
                f"{', '.join(elems) or '(unknown)'}. "
                f"Investigate the lab certificate before relying on these values."
            ),
            rule_id="assay.qaqc_flag_failed",
            threshold_payload={
                "fail_count": n,
                "elements": elems,
                "samples": qaqc_hits[:50],
            },
        ))

    if crm_hits:
        n = len(crm_hits)
        elems = sorted({h["element"] for h in crm_hits if h["element"]})
        flags.append(DataQualityFlag(
            **base,
            flag_type="assay.crm_failed",
            severity="ERROR",
            description=(
                f"Collar {hole_label}: {n} assay sample(s) where the "
                f"certified reference material check failed. The lab "
                f"batch is suspect; re-run or re-certify before use. "
                f"Affected elements: {', '.join(elems) or '(unknown)'}."
            ),
            rule_id="assay.crm_failed",
            threshold_payload={
                "fail_count": n,
                "elements": elems,
                "samples": crm_hits[:50],
            },
        ))

    if blank_hits:
        n = len(blank_hits)
        elems = sorted({h["element"] for h in blank_hits if h["element"]})
        flags.append(DataQualityFlag(
            **base,
            flag_type="assay.blank_failed",
            severity="ERROR",
            description=(
                f"Collar {hole_label}: {n} assay sample(s) where the "
                f"blank check failed (contamination detected). "
                f"Re-examine the affected lab batch. "
                f"Affected elements: {', '.join(elems) or '(unknown)'}."
            ),
            rule_id="assay.blank_failed",
            threshold_payload={
                "fail_count": n,
                "elements": elems,
                "samples": blank_hits[:50],
            },
        ))

    if dup_hits:
        n = len(dup_hits)
        elems = sorted({h["element"] for h in dup_hits if h["element"]})
        flags.append(DataQualityFlag(
            **base,
            flag_type="assay.duplicate_failed",
            severity="ERROR",
            description=(
                f"Collar {hole_label}: {n} assay sample(s) where the "
                f"duplicate-pair RPD exceeded threshold. Repeatability "
                f"is questionable; consider re-assaying. "
                f"Affected elements: {', '.join(elems) or '(unknown)'}."
            ),
            rule_id="assay.duplicate_failed",
            threshold_payload={
                "fail_count": n,
                "elements": elems,
                "samples": dup_hits[:50],
            },
        ))

    if high_hits:
        n = len(high_hits)
        max_hit = max(high_hits, key=lambda h: h["value_ppm"])
        elems = sorted({h["element"] for h in high_hits if h["element"]})
        flags.append(DataQualityFlag(
            **base,
            flag_type="assay.value_implausibly_high",
            severity="WARNING",
            description=(
                f"Collar {hole_label}: {n} assay sample(s) above the "
                f"per-element ppm ceiling. Likeliest cause is a unit "
                f"error (% logged as ppm). Max observed: "
                f"{max_hit['value_ppm']:g} ppm {max_hit['element']} "
                f"(ceiling {max_hit['ceiling_ppm']:g} ppm). "
                f"Affected elements: {', '.join(elems)}."
            ),
            rule_id="assay.value_implausibly_high",
            threshold_payload={
                "fail_count": n,
                "elements": elems,
                "max_value_ppm": max_hit["value_ppm"],
                "max_element": max_hit["element"],
                "samples": high_hits[:50],
            },
        ))

    if paradox_hits:
        n = len(paradox_hits)
        elems = sorted({h["element"] for h in paradox_hits if h["element"]})
        flags.append(DataQualityFlag(
            **base,
            flag_type="assay.detection_paradox",
            severity="INFO",
            description=(
                f"Collar {hole_label}: {n} assay sample(s) with both "
                f"over_detection AND under_detection set true — "
                f"contradictory state, almost certainly an ingest bug. "
                f"Affected elements: {', '.join(elems) or '(unknown)'}."
            ),
            rule_id="assay.detection_paradox",
            threshold_payload={
                "fail_count": n,
                "elements": elems,
                "samples": paradox_hits[:50],
            },
        ))

    return flags


# ---------------------------------------------------------------------------
# Asset — fetches rows + writes flags via the shared helper
# ---------------------------------------------------------------------------


@asset(
    group_name="data_quality",
    description=(
        "Plan §6a assay validation rule family. Scans silver.assays_v2 "
        "and writes one summary flag per (collar, rule) to "
        "silver.data_quality_flags. Six rules: qaqc_flag_failed + "
        "crm_failed + blank_failed + duplicate_failed (ERROR), "
        "value_implausibly_high (WARNING), detection_paradox (INFO). "
        "Fan-IN: a collar's N bad assays → 1 flag per rule type."
    ),
    compute_kind="postgres",
)
def silver_assay_dq(
    context: AssetExecutionContext,
    config: AssayDQConfig,
    postgres: PostgresResource,
) -> MaterializeResult:
    """Emit data-quality flags for every silver.assays_v2 row, grouped
    by collar for the badge surface."""
    sql = SELECT_ASSAYS_SQL
    params: tuple = ()
    if config.workspace_id:
        sql += " WHERE a.workspace_id = %s::uuid"
        params = (config.workspace_id,)

    with postgres.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    context.log.info(
        "silver_assay_dq: scanning %d assay row(s) (workspace_filter=%s)",
        len(rows), config.workspace_id or "all",
    )

    # Group by collar — fan-in cardinality match.
    by_collar: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_collar[r["collar_id"]].append(r)

    all_flags: list[DataQualityFlag] = []
    for collar_id, collar_rows in by_collar.items():
        first = collar_rows[0]
        all_flags.extend(
            evaluate_assay_rows_for_collar(
                collar_id=collar_id,
                workspace_id=first["workspace_id"],
                project_id=first["project_id"],
                hole_id=first.get("hole_id"),
                rows=collar_rows,
            )
        )

    # Per-severity rollup for asset metadata.
    severity_counts = {"INFO": 0, "WARNING": 0, "ERROR": 0}
    for f in all_flags:
        severity_counts[f.severity] += 1

    written = 0
    if all_flags:
        with postgres.get_connection() as conn:
            try:
                written = upsert_flags_sync(conn, all_flags)
                conn.commit()
            except Exception:
                conn.rollback()
                context.log.exception(
                    "silver_assay_dq: batch upsert failed — "
                    "no flags written this run"
                )
                raise

    context.log.info(
        "silver_assay_dq: wrote %d flag(s) across %d collar(s) — "
        "severities=%s",
        written, len(by_collar), severity_counts,
    )

    return MaterializeResult(
        metadata={
            "assays_scanned": MetadataValue.int(len(rows)),
            "collars_with_flags": MetadataValue.int(
                sum(1 for v in by_collar.values() if v)
            ),
            "flags_written": MetadataValue.int(written),
            "flag_severity_error": MetadataValue.int(severity_counts["ERROR"]),
            "flag_severity_warning": MetadataValue.int(
                severity_counts["WARNING"]
            ),
            "flag_severity_info": MetadataValue.int(severity_counts["INFO"]),
            "rule_version": MetadataValue.text(RULE_VERSION),
            "workspace_filter": MetadataValue.text(
                config.workspace_id or "(all workspaces)",
            ),
        },
    )
