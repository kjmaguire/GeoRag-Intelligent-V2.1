# Citation Pipeline Runbook

**Applies to:** Module 6 Phase B Chunk 2+
**Scope:** Feature flag management, rollback, smoke checks for the
two-stage citation span resolver

---

## Feature flag: `CITATION_SPAN_RESOLVER_ENABLED`

### Current default
`false` (set in `.env` and `.env.example`). The legacy one-step citation
path remains active. No rows are written to `silver.answer_citation_items`
or `silver.answer_citation_spans`.

### What the flag controls
| Flag | System prompt format | DB writes | Span resolver |
|------|----------------------|-----------|---------------|
| `false` | Dash-form `[DATA-N]` | None (Chunk 1 state) | Inactive |
| `true` | Colon-form `[DATA:N]` | citation_items + citation_spans | Active |

### Flip procedure (apply dispatch — after senior-reviewer approval only)

1. Confirm senior-reviewer has approved Chunk 2 design doc
   (`docs/module-6-chunk-2-design.md`).
2. Edit `.env`:
   ```
   CITATION_SPAN_RESOLVER_ENABLED=true
   ```
3. Restart FastAPI (zero-downtime preferred):
   ```bash
   docker compose up -d --no-deps fastapi
   ```
4. Smoke check:
   ```bash
   # Confirm flag is true
   docker exec georag-fastapi python -c \
     "import app.config as c; assert c.settings.CITATION_SPAN_RESOLVER_ENABLED is True; print('flag ON')"

   # Confirm imports are clean
   docker exec georag-fastapi python -c \
     "from app.agent.citation_binding import bind_evidence; \
      from app.services.span_resolver import resolve_spans; \
      from app.services.answer_run_store import insert_citation_items, batch_insert_citation_spans; \
      print('imports ok')"

   # Confirm /health still 200
   curl -s http://localhost:8000/health | python3 -m json.tool
   ```
5. After flip, bump `_SYSTEM_PROMPT_VERSION` from 8 to 9 in
   `src/fastapi/app/agent/orchestrator.py` (per Chunk 2 scope — this step
   is in the apply dispatch, not this runbook).

### Rollback

If any smoke check fails or anomalous behaviour is observed after flip:

```bash
# Revert the flag
CITATION_SPAN_RESOLVER_ENABLED=false
docker compose up -d --no-deps fastapi

# Confirm legacy path is active
docker exec georag-fastapi python -c \
  "import app.config as c; assert c.settings.CITATION_SPAN_RESOLVER_ENABLED is False; print('flag OFF — legacy path active')"
```

Rows already written to `answer_citation_items` and `answer_citation_spans`
during the flipped window are NOT deleted. They remain as an audit trail.
The legacy path will simply stop writing new rows.

---

## Run tests

```bash
docker exec georag-fastapi pytest \
  src/fastapi/tests/test_span_resolver.py \
  src/fastapi/tests/test_citation_binding.py \
  -v --tb=short
```

Expected: all 42 tests pass.

---

## Verify DB schema (pre-flip)

```bash
docker exec georag-postgresql psql -U georag -d georag -c "
SELECT
  column_name,
  data_type,
  is_nullable
FROM information_schema.columns
WHERE table_schema = 'silver'
  AND table_name = 'answer_citation_items'
ORDER BY ordinal_position;
"
```

Confirm columns: `answer_citation_item_id`, `answer_run_id`, `workspace_id`,
`evidence_id` (nullable), `passage_id` (nullable), `marker_text`,
`source_store` (nullable), `confidence` (nullable), `rejection_reason`
(nullable), `created_at`.

---

## Monitor after flip

Check that citation_items rows are being created:

```bash
docker exec georag-postgresql psql -U georag -d georag -c "
SELECT
  marker_text,
  source_store,
  created_at
FROM silver.answer_citation_items
ORDER BY created_at DESC
LIMIT 10;
"
```

Check that spans are being created:

```bash
docker exec georag-postgresql psql -U georag -d georag -c "
SELECT
  aci.marker_text,
  acs.span_start,
  acs.span_end,
  acs.created_at
FROM silver.answer_citation_spans acs
JOIN silver.answer_citation_items aci USING (answer_citation_item_id)
ORDER BY acs.created_at DESC
LIMIT 10;
"
```

Check telemetry in FastAPI logs:
```bash
docker logs georag-fastapi 2>&1 | grep "span_resolver telemetry"
```

---

## Deferred items

- **Chunk 3:** Close the dual-support window for dash-form markers; wire
  per-guard rejection routing into `citation_lifecycle_state='rejected'`.
- **Chunk 4:** Implement `hybrid_delayed_attachment` mode for partial
  resolution; add `partial_resolution_rate` column to `answer_runs`.
- **B8.5:** Enable ingestion to write non-passage `evidence_items` rows
  (coordinate with data-engineer). Gate: Module 6 citation consumer
  confirmed stable in production first.
