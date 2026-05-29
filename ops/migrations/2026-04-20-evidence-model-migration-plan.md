# Evidence-Model Migration Plan — Module 3 Phase B3
<!-- Authority: addendum §04j, module spec §6 B8.1–B8.4, Phase A audit EVID-01..04 -->
<!-- Status: DRAFT — pending senior-reviewer (Opus) approval before apply -->
<!-- Date: 2026-04-20 | Author: data-engineer agent (Claude Sonnet 4.6) -->

---

## Table Summaries

### 1. `silver.document_revisions`

Tracks every ingested version of a document in `silver.reports`.  One row per
(document, parser run).  Re-ingesting a document with a newer parser creates
revision 2 and sets `superseded_by_revision_id` on revision 1, preserving the
full audit trail without mutating the Bronze object.

| Column | Type | Notes |
|---|---|---|
| `document_revision_id` | UUID PK | gen_random_uuid() |
| `document_id` | UUID NOT NULL FK | → silver.reports.report_id CASCADE DELETE |
| `workspace_id` | UUID NOT NULL FK | → silver.workspaces.workspace_id CASCADE DELETE |
| `revision_number` | INTEGER NOT NULL | ≥ 1; unique with document_id |
| `source_uri` | TEXT NOT NULL | Bronze URI (s3://bronze/…) |
| `source_sha256` | CHAR(64) NOT NULL | lowercase hex SHA-256; CHECK format |
| `ingested_at` | TIMESTAMPTZ NOT NULL | when Bronze object landed |
| `parser_name` | VARCHAR(128) NOT NULL | e.g. "pdf-report-v2" |
| `parser_version` | VARCHAR(64) NOT NULL | semver or git SHA |
| `superseded_by_revision_id` | UUID NULL | self-FK ON DELETE SET NULL |
| `created_at` | TIMESTAMPTZ NOT NULL | DEFAULT NOW() |

Unique constraint: `(document_id, revision_number)`.
Indices: `document_id`, `workspace_id`, `source_sha256`.

### 2. `silver.evidence_items`

The unified evidence substrate.  Every citable unit of knowledge gets one row.
The discriminator column `evidence_type` is VARCHAR(32) with a CHECK constraint
(see ENUM vs CHECK section below).  Two CHECK constraints enforce mutual
exclusion and type-consistency of the four `*_ref` columns.

| Column | Type | Notes |
|---|---|---|
| `evidence_id` | UUID PK | gen_random_uuid() |
| `workspace_id` | UUID NOT NULL FK | → silver.workspaces.workspace_id CASCADE DELETE |
| `evidence_type` | VARCHAR(32) NOT NULL | CHECK IN ('document_passage','structured_record','graph_edge','map_feature') |
| `passage_id` | UUID NULL FK | → silver.document_passages.passage_id ON DELETE RESTRICT (senior-reviewer 2026-04-20) |
| `structured_ref` | JSONB NULL | schema+table+PK tuple |
| `graph_edge_ref` | JSONB NULL | start/end node IDs + rel type |
| `map_feature_ref` | JSONB NULL | tile function + bbox + properties |
| `source_uri` | TEXT NOT NULL | Bronze or canonical URI |
| `source_date` | DATE NULL | date of source document |
| `linked_node_ids` | JSONB NULL | array of Neo4j node IDs |
| `created_at` | TIMESTAMPTZ NOT NULL | DEFAULT NOW() |

Indices: `workspace_id`, `evidence_type`, `passage_id` (partial WHERE NOT NULL), `source_date`.

### 3. `silver.structured_record_lineage`

Row-level provenance for `structured_record` evidence items.  Traces each
evidence item back to the exact Bronze object, parser, and Dagster run.
`native_locator` is the JSONB row pointer (schema + table + PK values) needed
to re-derive the Silver row from Bronze if the parser is replayed.

| Column | Type | Notes |
|---|---|---|
| `lineage_id` | UUID PK | gen_random_uuid() |
| `evidence_id` | UUID NOT NULL FK | → silver.evidence_items.evidence_id CASCADE DELETE |
| `bronze_uri` | TEXT NOT NULL | Bronze object path |
| `bronze_sha256` | CHAR(64) NOT NULL | lowercase hex SHA-256; CHECK format |
| `parser_name` | VARCHAR(128) NOT NULL | |
| `parser_version` | VARCHAR(64) NOT NULL | |
| `ingestion_run_id` | UUID NOT NULL | Dagster run ID |
| `native_locator` | JSONB NOT NULL | row pointer into source format |
| `created_at` | TIMESTAMPTZ NOT NULL | DEFAULT NOW() |

Indices: `evidence_id`, `ingestion_run_id`, `bronze_sha256`.

---

## FK Graph

```
silver.workspaces
  └─ workspace_id ←── document_revisions.workspace_id (CASCADE)
  └─ workspace_id ←── evidence_items.workspace_id     (CASCADE)

silver.reports
  └─ report_id ←── document_revisions.document_id     (CASCADE)

silver.document_passages
  └─ passage_id ←── evidence_items.passage_id         (RESTRICT)

silver.document_revisions (self-referential)
  └─ document_revision_id ←── superseded_by_revision_id (SET NULL)

silver.evidence_items
  └─ evidence_id ←── structured_record_lineage.evidence_id (CASCADE)

[Module 6 — future]
silver.answer_citation_items
  └─ evidence_id ←── evidence_items.evidence_id (nullable FK, to be added by Module 6)
```

---

## CHECK Constraint Rationale for `evidence_items`

### Why CHECK + VARCHAR, not a PostgreSQL ENUM

Zero existing `CREATE TYPE … AS ENUM` statements were found across all 36
database migrations (exhaustive grep confirmed).  The established codebase
convention is `CHECK (col IN (…))` for discriminator columns (see `status`
columns on projects and silver tables).

PostgreSQL ENUM types have two operational drawbacks that informed this choice:

1. Adding a new value requires `ALTER TYPE … ADD VALUE` — DDL that holds an
   ACCESS EXCLUSIVE lock and cannot be wrapped in a transaction with data
   changes on older Postgres versions.
2. Removing or renaming a value requires dropping and recreating the type,
   which requires dropping and recreating all dependent columns.

VARCHAR(32) + CHECK is additive: a new evidence type requires only widening the
CHECK in a migration, which is a metadata-only change on PostgreSQL 17+.

### Mutual-exclusion constraint

```sql
CHECK (
    (CASE WHEN passage_id      IS NOT NULL THEN 1 ELSE 0 END +
     CASE WHEN structured_ref  IS NOT NULL THEN 1 ELSE 0 END +
     CASE WHEN graph_edge_ref  IS NOT NULL THEN 1 ELSE 0 END +
     CASE WHEN map_feature_ref IS NOT NULL THEN 1 ELSE 0 END) = 1
)
```

The CASE-SUM pattern is the canonical PostgreSQL approach for XOR across
nullable columns.  It prevents both "zero refs" and "multiple refs" in a single
expression without repeated IS NULL / IS NOT NULL pairs.

### Type-consistency constraint

```sql
CHECK (
    (evidence_type = 'document_passage' AND passage_id     IS NOT NULL) OR
    (evidence_type = 'structured_record' AND structured_ref IS NOT NULL) OR
    (evidence_type = 'graph_edge'        AND graph_edge_ref IS NOT NULL) OR
    (evidence_type = 'map_feature'       AND map_feature_ref IS NOT NULL)
)
```

This is enforced at the DB layer independently of application logic.  If a bug
in the ingestion asset sets the wrong `evidence_type` value for a given ref
column, this constraint rejects the row rather than silently storing corrupted
lineage.

---

## Backfill Count and Strategy (B8.4)

**Audit query result (2026-04-20):**

```sql
SELECT COUNT(*) FROM silver.reports;  -- Result: 1
```

One row exists:
- `report_id`: `44a67709-b846-42ec-a361-9faa6e224170`
- `title`: "NI 43-101 Technical Report"
- `created_at`: NULL (legacy — not set at ingest time)

**Backfill action:** The 160000 migration seeds one `document_revisions` row
(revision 1) for this report.

**Sentinel values used (provenance not recoverable):**

The `bronze.provenance` table was introduced after this report was ingested.
No provenance row links `target_id = 44a67709-…` to a Bronze object.  The
backfill uses:

- `source_uri`: `bronze://legacy-pre-2026-04-20/<report_id>` — a pseudo-URI
  that is syntactically distinct from real `s3://bronze/` URIs and is therefore
  filterable in queries.
- `source_sha256`: 64 × `'0'` (all-zeros sentinel) — detectable and
  filterable; not a valid SHA-256 of any real content.
- `parser_name`: `legacy-pre-2026-04-20`
- `parser_version`: `unknown`
- `ingested_at`: `NOW()` (migration run timestamp) — best available
  approximation since `silver.reports.created_at` is NULL for this row.

The migration is idempotent via `ON CONFLICT (document_id, revision_number) DO
NOTHING`.

---

## Rollback Sequence

Migrations must be rolled back in reverse creation order:

1. **160000** `backfill_document_revisions` — `DELETE FROM
   silver.document_revisions WHERE source_sha256 = '000…0' AND parser_name =
   'legacy-pre-2026-04-20'`.  Targets only sentinel rows.

2. **150000** `create_structured_record_lineage` — `DROP TABLE IF EXISTS
   silver.structured_record_lineage`.

3. **140000** `create_evidence_items` — `DROP TABLE IF EXISTS
   silver.evidence_items`.

4. **130000** `create_document_revisions` — drops self-FK constraint first,
   then `DROP TABLE IF EXISTS silver.document_revisions`.

All four down() methods are implemented in the migration files.

---

## Explicit "NOT in this migration" List

The following items are intentionally absent.  The senior-reviewer should
confirm these boundaries are respected:

| Item | Reason deferred |
|---|---|
| `answer_citation_items` table | Module 6 scope — no schema available yet |
| `evidence_id` column on any existing table | Module 6 adds it to `answer_citation_items` |
| `answer_citation_items.evidence_id` nullable FK | Module 6 step B8.2 (old numbering) |
| B8.3 backfill of `answer_citation_items` rows | 0 rows exist; Module 6 owns this |
| B8.5 ingestion-write enable for non-passage evidence | Gated on Module 6 readiness |
| B8.6 evidence_items + structured_record_lineage emission in Dagster assets | Gated on Module 6 + B8.5 |
| B8.7 evidence_id NOT NULL enforcement | Requires Module 6 to be production-ready |
| Any change to `silver.reports` | No column additions or modifications |
| Any change to `silver.document_passages` | Stays as-is from B1+B2 |
| Qdrant, Neo4j, Redis touches | Out of scope for schema migration |

---

## Coordination Notes with Module 6

When Module 6 creates `answer_citation_items`, it must:

1. Add `evidence_id UUID NULL REFERENCES silver.evidence_items(evidence_id)`
   to `answer_citation_items` (the nullable FK step).
2. Backfill: for every existing `answer_citation_items` row (currently zero),
   create a matching `evidence_items` row with `evidence_type='document_passage'`
   and populate `evidence_id`.
3. After the behavioral enable (B8.5 in ingestion + Module 6 citation writer
   ready), enforce `evidence_id IS NOT NULL` at the application layer.
4. Do NOT make `evidence_id` a DB-level NOT NULL constraint until the enable
   is confirmed stable — reversibility window closes at that point.

The Pydantic stubs in `src/fastapi/app/models/evidence.py` provide the type
contracts Module 6 will need.  No further changes to that file are required for
the Module 6 FK step.

### GIN index debt (deferred to Module 6)

The three JSONB columns on `silver.evidence_items` — `structured_ref`,
`graph_edge_ref`, `map_feature_ref` — are NOT indexed in this migration.
With zero rows today, the cost is nil. Once Module 6 enables the behavioral
write path (B8.5 equivalent) and non-passage evidence types start landing,
any lookup of the form `WHERE structured_ref @> '{...}'` or `WHERE
graph_edge_ref->>'start_node_id' = '...'` will table-scan.

When Module 6 opens, add per-column GIN indices with NULL-pruning partials:

    CREATE INDEX idx_evidence_items_structured_ref
      ON silver.evidence_items USING GIN (structured_ref)
      WHERE structured_ref IS NOT NULL;
    -- repeat for graph_edge_ref and map_feature_ref

Estimated post-ingestion volume and access patterns should drive the
decision on whether to also add functional indices on specific JSONB paths
that Module 6's query code hot-paths.

---

## Senior-Reviewer Focus Areas

The following items are flagged for Opus review:

1. **Self-FK ON DELETE SET NULL** on `document_revisions.superseded_by_revision_id` —
   confirm this is the right cascade semantics vs. RESTRICT or NO ACTION.
   Rationale used: deleting a newer revision should not cascade-delete the older
   one, which is still valid data.

2. **evidence_items.passage_id ON DELETE RESTRICT** — RESOLVED 2026-04-20
   per senior-reviewer. Earlier draft used SET NULL which would leave
   `evidence_type='document_passage'` with `passage_id IS NULL`, violating
   both the exactly-one-ref CHECK and the type-consistency CHECK. CASCADE
   was rejected because it silently destroys citations (violates Invariant 1
   citation-first). Tombstone relaxation was rejected because it lets
   downstream citations resolve to dead references. RESTRICT forces ingestion
   code to explicitly migrate evidence rows before pruning a passage —
   correct semantics for protecting citation integrity.

3. **Backfill sentinel sha256** — the all-zeros sentinel is filterable but not
   a real hash.  Any downstream code that validates `source_sha256 !=
   '000…0'` before trusting the value will correctly skip legacy rows.
   Confirm this is acceptable vs. using NULL (which would violate NOT NULL).

4. **created_at column type** — `TIMESTAMPTZ` used consistently in all three
   new tables.  Existing silver tables use `TIMESTAMP(0) WITHOUT TIME ZONE`
   (as established in document_passages and workspaces migrations).  This is a
   deliberate deviation: these audit/lineage tables need timezone-aware
   timestamps for cross-region deployment.  Senior-reviewer should confirm or
   require alignment with the existing `TIMESTAMP(0) WITHOUT TIME ZONE`
   convention.

5. **`bronze://` pseudo-URI scheme** — used in backfill source_uri to
   distinguish legacy rows from real `s3://` URIs.  Confirm this is an
   acceptable convention vs. using an empty string or NULL (the column is NOT
   NULL per spec).

---

## SME Decisions (Kyle, 2026-04-20)

### SME decisions (Kyle, 2026-04-20)

- **structured_record_lineage cardinality**: 1:N (KEPT). No UNIQUE on evidence_id.
  Reparse with a newer parser version legitimately appends a new lineage row
  for the same evidence_id. The pair (evidence_id, parser_name, parser_version)
  is effectively unique in practice but not enforced at the DB level.
- **document_revisions.is_current**: NOT ADDED. "Latest revision" queries
  use `WHERE superseded_by_revision_id IS NULL`. If this becomes hot, a
  partial index:
      CREATE INDEX idx_document_revisions_current
        ON silver.document_revisions (document_id)
        WHERE superseded_by_revision_id IS NULL;
  can be added later without schema churn.
