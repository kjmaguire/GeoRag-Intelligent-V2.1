# Evidence Model Runbook

Documents the §04j three-table evidence substrate. Use this when writing ingestion code that emits provenance, debugging a lineage trace, understanding FK cascade semantics, or rolling back evidence migrations.

---

## The three tables explained

### `silver.document_revisions`

One row per (document, parser run). Tracks every ingested version of a document in `silver.reports`. Re-ingesting a document with a newer parser creates revision 2 and sets `superseded_by_revision_id` on revision 1, preserving the full audit trail without mutating the Bronze object.

Key columns: `document_revision_id UUID PK`, `document_id FK → silver.reports`, `workspace_id FK → silver.workspaces`, `revision_number INTEGER ≥1`, `source_uri TEXT`, `source_sha256 CHAR(64)`, `parser_name VARCHAR(128)`, `parser_version VARCHAR(64)`, `superseded_by_revision_id UUID NULL` (self-FK).

### `silver.evidence_items`

The unified evidence substrate. Every citable unit of knowledge — a passage in a report, a structured data row, a graph edge, or a map tile feature — has exactly one row here. The `evidence_type` VARCHAR discriminator controls which of four mutually exclusive `*_ref` columns is populated.

Key columns: `evidence_id UUID PK`, `workspace_id FK → silver.workspaces`, `evidence_type VARCHAR(32) CHECK IN ('document_passage','structured_record','graph_edge','map_feature')`, `passage_id UUID NULL FK → silver.document_passages ON DELETE RESTRICT`, `structured_ref JSONB NULL`, `graph_edge_ref JSONB NULL`, `map_feature_ref JSONB NULL`, `source_uri TEXT`, `source_date DATE NULL`, `linked_node_ids JSONB NULL`.

### `silver.structured_record_lineage`

Row-level provenance for `structured_record` evidence items. Traces each evidence item back to the exact Bronze object, parser, and Dagster run ID. `native_locator` is a JSONB row pointer (schema + table + PK values) needed to re-derive the Silver row from Bronze if the parser is replayed.

Key columns: `lineage_id UUID PK`, `evidence_id FK → silver.evidence_items CASCADE`, `bronze_uri TEXT`, `bronze_sha256 CHAR(64)`, `parser_name VARCHAR(128)`, `parser_version VARCHAR(64)`, `ingestion_run_id UUID`, `native_locator JSONB NOT NULL`.

---

## Example rows per `evidence_type`

Each INSERT below demonstrates the mutual-exclusion CHECK and the type-consistency CHECK. All use `BEGIN; ... ROLLBACK;` — read-only verification only.

### document_passage

A passage from a parsed NI 43-101 report linked via `passage_id`.

```sql
BEGIN;
INSERT INTO silver.evidence_items (
    workspace_id,
    evidence_type,
    passage_id,
    structured_ref,
    graph_edge_ref,
    map_feature_ref,
    source_uri,
    source_date
) VALUES (
    'a0000000-0000-0000-0000-000000000001',
    'document_passage',
    'f47ac10b-58cc-4372-a567-0e02b2c3d479',  -- FK → silver.document_passages
    NULL, NULL, NULL,
    's3://bronze/reports/PLS-2024-Technical-Report.pdf',
    '2024-03-15'
);
ROLLBACK;
```

### structured_record

A collar row from `silver.collars` near an assay sample.

```sql
BEGIN;
INSERT INTO silver.evidence_items (
    workspace_id,
    evidence_type,
    passage_id,
    structured_ref,
    graph_edge_ref,
    map_feature_ref,
    source_uri
) VALUES (
    'a0000000-0000-0000-0000-000000000001',
    'structured_record',
    NULL,
    '{"schema": "silver", "table": "collars", "pk": {"collar_id": "c1a2b3c4-0000-0000-0000-000000000001"}}',
    NULL, NULL,
    's3://bronze/collars/sample_collars.csv'
);
-- Paired lineage row:
INSERT INTO silver.structured_record_lineage (
    evidence_id,
    bronze_uri,
    bronze_sha256,
    parser_name,
    parser_version,
    ingestion_run_id,
    native_locator
) VALUES (
    (SELECT evidence_id FROM silver.evidence_items
     WHERE structured_ref->>'table' = 'collars' LIMIT 1),
    's3://bronze/collars/sample_collars.csv',
    'a3f5c1e2d4b6a8c0e2f4a6b8c0d2e4f6a8b0c2d4e6f8a0b2c4d6e8f0a2b4c6d8',
    'csv-collar',
    '1.0.0',
    'b2e4d6f8-a0b2-c4d6-e8f0-a2b4c6d8e0f2',
    '{"schema": "silver", "table": "collars", "pk": {"collar_id": "c1a2b3c4-0000-0000-0000-000000000001"}}'
);
ROLLBACK;
```

### graph_edge

A Neo4j edge from a drillhole node to a formation node.

```sql
BEGIN;
INSERT INTO silver.evidence_items (
    workspace_id,
    evidence_type,
    passage_id,
    structured_ref,
    graph_edge_ref,
    map_feature_ref,
    source_uri
) VALUES (
    'a0000000-0000-0000-0000-000000000001',
    'graph_edge',
    NULL, NULL,
    '{"start_node_id": 1042, "end_node_id": 2087, "rel_type": "INTERSECTS_FORMATION"}',
    NULL,
    'neo4j://graph/edge/1042-INTERSECTS_FORMATION-2087'
);
ROLLBACK;
```

### map_feature

A spatial tile feature from the collars MVT layer.

```sql
BEGIN;
INSERT INTO silver.evidence_items (
    workspace_id,
    evidence_type,
    passage_id,
    structured_ref,
    graph_edge_ref,
    map_feature_ref,
    source_uri
) VALUES (
    'a0000000-0000-0000-0000-000000000001',
    'map_feature',
    NULL, NULL, NULL,
    '{"tile_function": "collars_mvt", "bbox": [498000, 6200000, 502000, 6204000], "properties": {"hole_id": "PLS-22-08"}}',
    's3://bronze/spatial/pls_property_boundary.geojson'
);
ROLLBACK;
```

**Violation test — two refs populated (should fail `evidence_items_exactly_one_ref`):**

```sql
BEGIN;
INSERT INTO silver.evidence_items (
    workspace_id, evidence_type, passage_id, structured_ref,
    graph_edge_ref, map_feature_ref, source_uri
) VALUES (
    'a0000000-0000-0000-0000-000000000001',
    'structured_record',
    NULL,
    '{"schema":"silver","table":"collars","pk":{"collar_id":"abc"}}',
    '{"start_node_id":1,"end_node_id":2,"rel_type":"TEST"}',
    NULL,
    's3://bronze/test'
);
-- Expected: ERROR: new row for relation "evidence_items" violates check constraint
--           "evidence_items_exactly_one_ref"
ROLLBACK;
```

---

## How lineage traces to Bronze

### Document passage lineage

```
answer (Module 6) → answer_citation_items.evidence_id
    → silver.evidence_items WHERE evidence_type = 'document_passage'
        → evidence_items.passage_id
            → silver.document_passages.document_id
                → silver.document_revisions WHERE document_id = <id>
                    → document_revisions.source_uri  ← Bronze URI
                    → document_revisions.source_sha256 ← Bronze SHA-256
```

SQL walk for a known passage:

```sql
SELECT
    dp.passage_id,
    dp.text_hash,
    dr.revision_number,
    dr.source_uri      AS bronze_uri,
    dr.source_sha256   AS bronze_sha256,
    dr.parser_name,
    dr.parser_version,
    dr.ingested_at
FROM silver.document_passages dp
JOIN silver.document_revisions dr
    ON dp.document_id = dr.document_id
   AND dr.superseded_by_revision_id IS NULL   -- current revision only
WHERE dp.passage_id = '<passage_uuid>';
```

### Structured record lineage

```
answer (Module 6) → answer_citation_items.evidence_id
    → silver.evidence_items WHERE evidence_type = 'structured_record'
        → silver.structured_record_lineage WHERE evidence_id = <id>
            → structured_record_lineage.bronze_uri    ← Bronze URI
            → structured_record_lineage.bronze_sha256 ← Bronze SHA-256
            → structured_record_lineage.ingestion_run_id ← Dagster run ID
            → structured_record_lineage.native_locator   ← row pointer
```

SQL walk for a known evidence item:

```sql
SELECT
    ei.evidence_id,
    ei.evidence_type,
    ei.structured_ref,
    srl.bronze_uri,
    srl.bronze_sha256,
    srl.parser_name,
    srl.parser_version,
    srl.ingestion_run_id,
    srl.native_locator
FROM silver.evidence_items ei
JOIN silver.structured_record_lineage srl
    ON srl.evidence_id = ei.evidence_id
WHERE ei.evidence_id = '<evidence_uuid>';
```

### "What supported this answer?" query

This full join will be possible once Module 6 creates `answer_citation_items` and `answer_runs`. Shown here as the target pattern — the two left tables do not exist yet.

```sql
-- NOTE: answer_citation_items and answer_runs do not yet exist (Module 6 scope).
-- This query is the target pattern for Module 6 implementation.
SELECT
    aci.citation_id,
    ei.evidence_type,
    ei.source_uri,
    -- document passage path
    dp.text_hash,
    dr.source_uri      AS bronze_uri,
    dr.source_sha256,
    -- structured record path
    srl.native_locator,
    srl.bronze_uri     AS structured_bronze_uri
FROM answer_citation_items aci                       -- Module 6
JOIN silver.evidence_items ei
    ON ei.evidence_id = aci.evidence_id
LEFT JOIN silver.document_passages dp
    ON dp.passage_id = ei.passage_id
LEFT JOIN silver.document_revisions dr
    ON dr.document_id = dp.document_id
   AND dr.superseded_by_revision_id IS NULL
LEFT JOIN silver.structured_record_lineage srl
    ON srl.evidence_id = ei.evidence_id
WHERE aci.answer_run_id = '<answer_run_uuid>';
```

---

## Coexistence with `bronze.provenance`

| Table | Purpose | Scope | Written by |
|---|---|---|---|
| `bronze.provenance` | General per-row ingest audit — every Bronze asset write | All Bronze assets | Bronze Dagster assets at upload time |
| `silver.document_revisions` | Document-specific version chain — one row per parse of a document | Document-type assets only | Silver Dagster assets (`silver_reports`) |

Consult `bronze.provenance` to answer: "when was this Bronze file first uploaded and by which parser version?"

Consult `silver.document_revisions` to answer: "what is the current live revision of this document, and which older revisions were superseded?"

**Dual-write pattern:** Future ingestion assets that parse documents must write to both tables in a single transaction — one `bronze.provenance` row (at Bronze stage) and one `silver.document_revisions` row (at Silver stage). Neither table is sufficient alone for the full audit trail.

---

## FK cascade semantics

| FK | Cascade | Rationale |
|---|---|---|
| `document_revisions.document_id → silver.reports` | CASCADE DELETE | Deleting a report removes all its revision history |
| `document_revisions.workspace_id → silver.workspaces` | CASCADE DELETE | Workspace delete clears all revisions |
| `document_revisions.superseded_by_revision_id → document_revisions` | SET NULL | Deleting a newer revision does not cascade-delete the older one, which remains valid |
| `evidence_items.workspace_id → silver.workspaces` | CASCADE DELETE | Workspace delete clears all evidence |
| `evidence_items.passage_id → silver.document_passages` | RESTRICT | Ingestion code must explicitly migrate evidence rows before pruning a passage — protects citation integrity (Global Invariant 1). SET NULL would leave a `document_passage` evidence item with no passage_id, violating both CHECK constraints. CASCADE would silently destroy citations. |
| `structured_record_lineage.evidence_id → silver.evidence_items` | CASCADE DELETE | Lineage is a child audit record; removing evidence removes its provenance rows |

---

## Rollback procedure

Reverses Phase B3 evidence-model migrations in order (160 → 150 → 140 → 130):

```bash
docker exec georag-laravel-octane php artisan migrate:rollback --step=4
```

Migration rollback order and what each down() does:

1. **160000** `backfill_document_revisions` — `DELETE FROM silver.document_revisions WHERE source_sha256 = '<64 zeros>'`. Targets only the legacy sentinel row.
2. **150000** `create_structured_record_lineage` — `DROP TABLE IF EXISTS silver.structured_record_lineage`.
3. **140000** `create_evidence_items` — `DROP TABLE IF EXISTS silver.evidence_items`.
4. **130000** `create_document_revisions` — drops the self-FK constraint, then `DROP TABLE IF EXISTS silver.document_revisions`.

Verify rollback succeeded:

```bash
docker exec georag-laravel-octane php artisan migrate:status | grep -E '13|14|15|16'
```

All four should show `Pending`.

---

## Known sentinel values

The 1 legacy `silver.reports` row (report_id `44a67709-b846-42ec-a361-9faa6e224170`, ingested before Module 3) has a matching `silver.document_revisions` row with:

- `source_sha256` = `'0000000000000000000000000000000000000000000000000000000000000000'` (64 zeros)
- `source_uri` = `'bronze://legacy-pre-2026-04-20/<report_id>'`
- `parser_name` = `'legacy-pre-2026-04-20'`
- `revision_number` = `1`
- `superseded_by_revision_id` = `NULL` (treated as current until the next real ingest of this document)

Filter sentinel rows from provenance queries:

```sql
WHERE source_sha256 != '0000000000000000000000000000000000000000000000000000000000000000'
  AND source_uri NOT LIKE 'bronze://legacy-%'
```

---

_Written 2026-04-20 during Module 3 Phase D. Update this file whenever the underlying procedure changes._
