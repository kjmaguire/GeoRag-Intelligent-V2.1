"""Plan §6a — Unit consistency rule family.

Scans silver.assays_v2 for the cross-row pattern that silently
corrupts compositing: the same element on the same collar reported
with DIFFERENT units across samples. The DB normalizes to value_ppm
at ingest time, but heterogeneous source units are a signal worth
surfacing — when a vendor mixes 'ppm' and 'pct' on one element in
one hole, the most likely explanation is a transcription bug
upstream, and value_ppm derived from those mixed rows is suspect.

Single rule, summary-per-collar:

  • assay.mixed_units  ERROR  (collar, element) pair reports >1 distinct
                              normalized unit across its samples

Cross-row by design — unlike collar-row or per-row assay rules, this
one only fires when a GROUP BY (collar_id, element) reveals
heterogeneity. Per-row evaluation can't catch it.

Unit normalization:
  - Lowercase + strip whitespace
  - '%' → 'pct'  (common synonym, case-folded)

What we do NOT fold:
  - 'g/t' and 'ppm' are conceptually equivalent (1 g/t = 1 ppm) but
    if a vendor mixes them within one element on one hole that's a
    reporting-style change mid-collar — still worth a flag. The
    geologist can decide whether to ignore it.
  - 'oz/t' vs others — different unit class entirely; this is the
    flag's whole point.

Fan-IN — one summary flag per affected collar (NOT per affected
element). Description lists all mixed elements, threshold_payload
carries the per-element breakdown for drill-down. This matches the
silver_collar_dq + silver_assay_dq surface contract — at most 1
mixed_units flag on the badge per collar.

NOTE: Do NOT add ``from __future__ import annotations`` — Dagster 1.13.

Sized 2026-05-29 at 3h focused implementation against the
silver_collar_dq + silver_assay_dq templates; this is the last of
the four deferred §6a rule families.
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


# Trivial format synonyms that should NOT trigger the rule. Map raw
# unit string → canonical form. Anything not in this map passes through
# as lowercase-stripped.
_UNIT_SYNONYMS: dict[str, str] = {
    "%": "pct",
    "percent": "pct",
    "ppm": "ppm",
    "ppb": "ppb",
    "ppt": "ppt",
    "pct": "pct",
    "g/t": "g/t",
    "g/tonne": "g/t",
    "gpt": "g/t",
    "oz/t": "oz/t",
    "ozpt": "oz/t",
    "oz/ton": "oz/t",
    "kg/t": "kg/t",
}


def _normalize_unit(unit: str | None) -> str | None:
    """Fold trivial format synonyms so 'PPM ' and 'ppm' don't trip
    the rule. Returns None on empty/NULL input — None values are
    excluded from the distinct-unit set."""
    if unit is None:
        return None
    stripped = unit.strip().lower()
    if not stripped:
        return None
    return _UNIT_SYNONYMS.get(stripped, stripped)


# ---------------------------------------------------------------------------
# SQL — fetch one row per (collar, element, unit) bucket. Aggregation
# happens in Python because the per-element rollup needs a list[dict],
# not a SQL set.
# ---------------------------------------------------------------------------

SELECT_UNIT_BUCKETS_SQL = """
SELECT
    a.collar_id::text     AS collar_id,
    a.workspace_id::text  AS workspace_id,
    co.project_id::text   AS project_id,
    co.hole_id            AS hole_id,
    a.element             AS element,
    a.unit                AS unit,
    COUNT(*)::int         AS sample_count
FROM silver.assays_v2 a
JOIN silver.collars co ON co.collar_id = a.collar_id
GROUP BY a.collar_id, a.workspace_id, co.project_id, co.hole_id,
         a.element, a.unit
"""


class UnitConsistencyDQConfig(Config):
    """Asset config. Defaults to a full sweep; an operator can limit
    by workspace for an ad-hoc check during the rollout."""

    workspace_id: str | None = None
    """Optional workspace_id filter. None = sweep every workspace."""


# ---------------------------------------------------------------------------
# Pure rule evaluation — buckets in, list[DataQualityFlag] out
# ---------------------------------------------------------------------------


def evaluate_unit_buckets(rows: list[dict]) -> list[DataQualityFlag]:
    """Apply the mixed-units rule to pre-grouped (collar, element, unit)
    buckets.

    Each input row represents COUNT(*) samples of one (collar, element,
    unit) combination. The rule fires when, for a given (collar, element)
    pair, the set of NORMALIZED units has cardinality > 1.

    Args:
        rows: bucket rows from SELECT_UNIT_BUCKETS_SQL. Each carries
            collar_id, workspace_id, project_id, hole_id, element, unit,
            sample_count.

    Returns:
        One :class:`DataQualityFlag` per affected collar (NOT per
        affected element). Empty list = all (collar, element) pairs
        use consistent units. Pure function — no DB / no I/O.
    """
    if not rows:
        return []

    # Step 1 — bucket by (collar_id, element). Each entry collects the
    # distinct NORMALIZED unit set + the per-raw-unit sample counts.
    # Also keep workspace/project/hole metadata for the flag rows.
    per_pair: dict[tuple[str, str], dict] = {}
    for r in rows:
        key = (r["collar_id"], r["element"])
        entry = per_pair.setdefault(key, {
            "workspace_id": r["workspace_id"],
            "project_id": r["project_id"],
            "hole_id": r.get("hole_id"),
            "normalized_units": set(),
            "unit_counts": {},  # raw unit → sample_count
            "total_samples": 0,
        })
        norm = _normalize_unit(r.get("unit"))
        if norm is not None:
            entry["normalized_units"].add(norm)
        raw_unit = r.get("unit") or "(null)"
        entry["unit_counts"][raw_unit] = (
            entry["unit_counts"].get(raw_unit, 0) + int(r["sample_count"])
        )
        entry["total_samples"] += int(r["sample_count"])

    # Step 2 — keep only (collar, element) pairs with > 1 distinct
    # normalized unit. Group those by collar so each affected collar
    # produces ONE summary flag.
    by_collar: dict[str, list[dict]] = defaultdict(list)
    for (collar_id, element), entry in per_pair.items():
        if len(entry["normalized_units"]) > 1:
            by_collar[collar_id].append({
                "element": element,
                "normalized_units": sorted(entry["normalized_units"]),
                "unit_counts": entry["unit_counts"],
                "total_samples": entry["total_samples"],
                "workspace_id": entry["workspace_id"],
                "project_id": entry["project_id"],
                "hole_id": entry["hole_id"],
            })

    # Step 3 — emit one summary flag per affected collar.
    flags: list[DataQualityFlag] = []
    for collar_id, element_entries in by_collar.items():
        first = element_entries[0]
        hole_id = first["hole_id"] or collar_id[:8]
        elements = sorted(e["element"] for e in element_entries)
        # Build a compact description line per element so the
        # geologist can see "Au: 15 ppm + 3 pct" at a glance.
        breakdown_lines = []
        for entry in sorted(element_entries, key=lambda e: e["element"]):
            unit_summary = ", ".join(
                f"{u} ({n})" for u, n in sorted(entry["unit_counts"].items())
            )
            breakdown_lines.append(f"{entry['element']}: {unit_summary}")
        flags.append(DataQualityFlag(
            workspace_id=first["workspace_id"],
            project_id=first["project_id"],
            record_type="collar",
            record_id=collar_id,
            flag_type="assay.mixed_units",
            severity="ERROR",
            description=(
                f"Collar {hole_id}: {len(elements)} element"
                f"{'' if len(elements) == 1 else 's'} reported in "
                f"mixed units across samples — "
                f"{'; '.join(breakdown_lines)}. The DB normalises to "
                f"value_ppm at ingest, but heterogeneous source units "
                f"on one hole usually signal a transcription bug "
                f"upstream — verify before compositing."
            ),
            rule_id="assay.mixed_units",
            rule_version=RULE_VERSION,
            threshold_payload={
                "affected_element_count": len(elements),
                "elements": elements,
                "per_element": [
                    {
                        "element": e["element"],
                        "normalized_units": e["normalized_units"],
                        "unit_counts": e["unit_counts"],
                        "total_samples": e["total_samples"],
                    }
                    for e in sorted(element_entries, key=lambda x: x["element"])
                ],
            },
        ))

    return flags


# ---------------------------------------------------------------------------
# Asset — fetches buckets + writes flags via the shared helper
# ---------------------------------------------------------------------------


@asset(
    group_name="data_quality",
    description=(
        "Plan §6a unit consistency rule family. Cross-row scan on "
        "silver.assays_v2 — groups by (collar_id, element) and flags "
        "pairs reporting >1 distinct normalized unit. One summary "
        "ERROR flag per affected collar; per-element breakdown in "
        "threshold_payload. Idempotent via the shared dq_writer."
    ),
    compute_kind="postgres",
)
def silver_unit_consistency_dq(
    context: AssetExecutionContext,
    config: UnitConsistencyDQConfig,
    postgres: PostgresResource,
) -> MaterializeResult:
    """Emit mixed-units flags for cross-row inconsistencies in
    silver.assays_v2."""
    sql = SELECT_UNIT_BUCKETS_SQL
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
        "silver_unit_consistency_dq: scanned %d (collar, element, unit) "
        "bucket(s) (workspace_filter=%s)",
        len(rows), config.workspace_id or "all",
    )

    flags = evaluate_unit_buckets(rows)

    severity_counts = {"INFO": 0, "WARNING": 0, "ERROR": 0}
    for f in flags:
        severity_counts[f.severity] += 1

    written = 0
    if flags:
        with postgres.get_connection() as conn:
            try:
                written = upsert_flags_sync(conn, flags)
                conn.commit()
            except Exception:
                conn.rollback()
                context.log.exception(
                    "silver_unit_consistency_dq: batch upsert failed — "
                    "no flags written this run"
                )
                raise

    context.log.info(
        "silver_unit_consistency_dq: wrote %d flag(s) — severities=%s",
        written, severity_counts,
    )

    return MaterializeResult(
        metadata={
            "buckets_scanned": MetadataValue.int(len(rows)),
            "collars_flagged": MetadataValue.int(len(flags)),
            "flags_written": MetadataValue.int(written),
            "flag_severity_error": MetadataValue.int(severity_counts["ERROR"]),
            "rule_version": MetadataValue.text(RULE_VERSION),
            "workspace_filter": MetadataValue.text(
                config.workspace_id or "(all workspaces)",
            ),
        },
    )
