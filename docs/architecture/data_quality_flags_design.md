# Data quality flags — design

**Status:** Schema drafted (not applied). Validation rules + gate wiring NOT implemented.
**Plan reference:** §1g (QA/QC schema), §0g (ingestion readiness gate), §6a (document view UI surface)

## What this closes

Plan §1g calls out QA/QC validation rules referenced in architecture-doc v1.49 but not landed in any ticket. `silver.data_quality_flags` is the storage layer; this doc specifies the rules that write into it.

## Validation rules (plan §1g verbatim, with rule IDs assigned)

Rule IDs are stable identifiers (stored in `data_quality_flags.rule_id`) so a fix on one rule doesn't disturb history of other rules' detections.

### Assay value validation (`assay_*`)

- **`assay_outlier_3sigma`** (WARNING) — assay value > 3σ from property-level median for the same commodity. Source: per-commodity rolling statistic precomputed daily.
- **`assay_negative_value`** (ERROR) — negative assay value where the commodity is not a geophysical measurement (e.g. Au, Cu, Zn cannot be negative).
- **`assay_unit_mismatch_within_hole`** (ERROR) — same commodity in same hole reported in different units (g/t mixed with ppm).
- **`assay_interval_inverted`** (ERROR) — From > To OR From = To. Indicates parsing error.

### Collar coordinate validation (`collar_*`)

- **`collar_bbox_violation`** (ERROR) — easting/northing falls outside expected bounding box for the project area (project polygon ± 10 km buffer).
- **`collar_elevation_implausible`** (WARNING) — elevation outside the property-level min/max ± 500 m.
- **`collar_in_ocean`** (ERROR) — falls within the world ocean polygon. Almost always a CRS error.
- **`collar_missing_crs`** (WARNING) — CRS not specified. Block from `ready`; demote to `ready_needs_review`.

### Depth interval overlap detection (`interval_*`)

- **`interval_overlap_within_hole`** (ERROR) — two intervals in the same table for the same hole have overlapping depth ranges. Note: Kyle's WIP includes `interval_overlap_checks.py` Dagster check that already does this for some tables — this rule formalises the writeback so the flag is visible in the UI.
- **`interval_gap_exceeds_threshold`** (INFO) — gap > 0.5 m between adjacent intervals in same table. May indicate missing data.

### Unit consistency (`unit_*`)

- **`unit_change_mid_hole`** (ERROR) — unit changes within a hole without an explicit conversion column. Compounds with `assay_unit_mismatch_within_hole` but flagged separately for narrative clarity in the UI.

### CRS consistency (`crs_*`)

- **`crs_mismatch_within_program`** (ERROR) — two collars in the same drill program use different CRS designators.

## Readiness gate contract (plan §0g)

A document's status flips to `ready` ONLY when:

```
NOT EXISTS (
    SELECT 1 FROM silver.data_quality_flags
    WHERE source_document_id = :document_id
      AND severity = 'ERROR'
      AND resolved_at IS NULL
)
```

If at least one open ERROR flag exists, status is `ready_needs_review`. WARNING-only documents flip to `ready` but the UI badge surfaces the warning count.

The check is enforced at two layers:

1. **Application layer** (FastAPI ingest finalizer) — checks the flag count before flipping status to `ready`. Authoritative.
2. **Database-level CHECK or trigger** — defence-in-depth; rejects the state transition if a stale `ready` write slips through.

## Where flags are written

| Rule family | Writer | Hook point |
|---|---|---|
| Assay validation | Dagster `silver/assays_v2` asset | After unit normalisation |
| Collar validation | Dagster `silver_drill_traces` asset | After CRS resolution |
| Interval overlap | Dagster check `interval_overlap_checks.py` (existing WIP) | Asset check fail → writeback |
| Unit consistency | Dagster `silver/assays_v2` asset | Cross-row pass |
| CRS consistency | Dagster `silver_drill_traces` asset | Per-program rollup |

Writes go through a single helper `src/fastapi/app/services/silver_dq_flag_writer.py` (landed 2026-05-29) that handles idempotency by `(workspace_id, record_type, record_id, flag_type, rule_version)` upsert — re-running the same rule against the same row does NOT create a duplicate flag. The helper:

* Sets the `georag.workspace_id` GUC inside its own transaction so RLS lets the write through without the caller wiring it.
* Validates `severity` + `record_type` against the DB CHECK constraints BEFORE roundtrip — typos raise `ValueError` instead of `CheckViolationError`.
* Provides `upsert_flag(conn, flag)` (single row) + `upsert_flags(conn, [flags...])` (batch in one transaction).
* Best-effort on DB failure — logs at WARNING + returns False/0 so a flag-write failure can't abort the Dagster pipeline that's producing it.

Re-emit semantics: when a rule fires again on a row that already had the same flag, the writer clears the resolution lifecycle (`reviewed_at`, `resolved_at`, `resolution`, …) to NULL. The rule is saying "this is still a problem" — the SME review flow should restart.

**Status:** Helper + 17 unit tests landed. The 5 rule families are still to wire (each follows the pattern: compute the rule output inside its Dagster asset, build a list of `DataQualityFlag` instances, call `upsert_flags(conn, flags)`).

## UI surfacing (plan §6a)

The document-view QA/QC badge reads:

```sql
SELECT severity, count(*) AS open_count
FROM silver.data_quality_flags
WHERE source_document_id = :document_id
  AND resolved_at IS NULL
GROUP BY severity
```

Render: red badge with ERROR count, amber with WARNING count, grey with INFO count. Click expands to the flag list with rule_id, description, and resolution controls.

## Decisions captured — 2026-05-27 morning

Kyle reviewed and accepted all four recommendations:

| Q | Decision | Implication |
|---|---|---|
| Q5 | **Two tables** — keep `silver.data_quality_flags` separate from `silver.completeness_findings` | Correctness vs completeness stays cleanly separated. UI badge does two queries (cheap; both are workspace-scoped). |
| Q6 | **Daily Dagster asset** writes to `silver.assay_statistics_rolling` | New asset to author when wiring `assay_outlier_3sigma`. Asset reads from `silver.assays_v2`, computes per-(property, commodity) rolling median+sd, writes a denormalised lookup table the rule writer reads in O(1). |
| Q7 | **Claim boundaries + 10 km buffer** for `collar_bbox_violation`; **defer (no flag) when boundaries unknown** | Implementation note: lookup `silver.spatial_features WHERE feature_type = 'claim_boundary' AND project_id = :project_id`. If empty → rule short-circuits without writing a flag, logs a debug `bbox_source_unavailable` info. |
| Q8 | **No retroactive re-evaluation** — `rule_version` stays on the historical flag | When a threshold changes (e.g. 3σ → 2.5σ), the new rule_version writes new flags going forward; the old flags retain their original rule_version + threshold_payload as the audit record. |
