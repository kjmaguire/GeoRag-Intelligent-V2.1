# Phase 65 Handoff — §04p dual-write Prometheus alerting

**Status:** Complete. Closes doc-phase 59 §5.3 carry-over.

## What landed

- `georag_p04p_dual_write_success_total` Counter in `app/metrics.py`
- `georag_p04p_dual_write_failures_total{error_kind}` Counter (low-cardinality label: exception / preflight_invalid / persist_failed / other)
- `ingest_pdf.persist` step increments the right counter based on `p04p_telemetry.ok` and error string classification
- Outer exception path (helper itself crashes) increments with `error_kind="exception"`
- `docker/prometheus/rules/p04p-dual-write-alerts.yml` — 3 rules:
  - `P04PDualWriteFailureRateHigh` (>10% over 5m, warning)
  - `P04PDualWriteFailureRateCritical` (>50% over 2m, critical)
  - `P04PDualWriteAllFailingByKind` (>=5 failures in 10m, all single error_kind, warning — catches systemic issues)

## Verifier: 18/18 PASS in 1.2 sec

## Key design choices

- `error_kind` is low-cardinality; actual error strings go to silver.parser_run_artifacts.errors JSONB + Loki logs
- Promtool validated all 3 rules
- Counters increment from the `ingest_pdf.persist` step; the rules file is already mounted into the running prometheus container

## Carry-overs added

- **Grafana panel** for the new counters — optional doc-phase 66+ work if time. Pattern: copy a similar fastapi panel JSON, edit metric names + query.
- **Counter visibility live**: requires fastapi container restart to reload the metrics module + serve them at /metrics. Verifier proves the import is correct.

## Master-plan §3 progress unchanged. Step 8 is closed; remaining: Step 9 (Kyle labeling), Step 10 (RAGFlow retirement).
