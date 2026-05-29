# DrillHole Label Rename Runbook

**Owner decision:** D2 in `docs/kyle-decisions.md` — approved 2026-04-27.  
**Migration script:** `ops/migrations/neo4j/2026-04-27-drillhole-rename.cypher`  
**Architecture reference:** `georag-architecture.html` §04f (Global Invariant 4)

## Context

All 33,510 live Neo4j nodes representing drill holes carry the label `:Drillhole`
(lowercase h). The spec in §04f uses `:DrillHole` (PascalCase). This mismatch
means any Cypher query written against the spec has silently returned zero rows.

This runbook coordinates the one-time rename that brings data and code into
alignment. It is a **Global Invariant 4 schema change** — data and code must
move together in a single maintenance window.

---

## Pre-flight

### 1. Snapshot the Neo4j volume

Before any changes, take a database dump so a rollback can be applied cleanly
if needed:

```bash
docker exec georag-neo4j neo4j-admin database dump neo4j \
  --to-path=/backups/neo4j-pre-drillhole-rename-$(date +%Y%m%d).dump

# Or via the backup script:
docker exec georag-neo4j /backups/backup.sh
```

### 2. Confirm pre-migration node counts

Connect to Neo4j (browser at `http://localhost:7474` or via `cypher-shell`)
and run:

```cypher
MATCH (n:Drillhole) RETURN count(n) AS legacy_count;
MATCH (n:DrillHole) RETURN count(n) AS camel_count;
```

Expected: `legacy_count ≈ 33510`, `camel_count = 0`.

If `camel_count > 0`, the migration has been partially applied — check whether
the code deployment also happened (see Section "Coordination" below). If only
the migration ran but code was not deployed yet, the graph is in a valid
intermediate state and you can continue from Step 3.

### 3. Identify current constraint names

```cypher
SHOW CONSTRAINTS YIELD name, labelsOrTypes, properties
WHERE 'Drillhole' IN labelsOrTypes;
```

Note the names. The migration script drops `drillhole_collar_id_unique` and
`drillhole_collar_id_internal` — if your environment generated different names,
add explicit `DROP CONSTRAINT <name> IF EXISTS` statements before running.

---

## Execution

### Step 1 — Stop Dagster ingestion

Ingestion assets (`index_neo4j`, `gold_public_geoscience`) write new `:Drillhole`
nodes. Stop them to prevent new legacy-label nodes appearing mid-migration:

```bash
# Pause all Dagster asset materialisations. Stop the daemon service:
docker compose stop georag-dagster-daemon
```

Verify no Dagster runs are in-progress before proceeding (check the Dagster UI
at `http://localhost:3000`).

### Step 2 — Apply the migration

Open `cypher-shell` against the running Neo4j container:

```bash
docker exec -it georag-neo4j cypher-shell \
  -u neo4j -p "$NEO4J_PASSWORD" \
  --file /ops/migrations/neo4j/2026-04-27-drillhole-rename.cypher
```

Alternatively, copy the script into the container and run it:

```bash
docker cp ops/migrations/neo4j/2026-04-27-drillhole-rename.cypher \
    georag-neo4j:/tmp/rename.cypher
docker exec -it georag-neo4j cypher-shell \
  -u neo4j -p "$NEO4J_PASSWORD" \
  --file /tmp/rename.cypher
```

The batched `CALL { } IN TRANSACTIONS OF 1000 ROWS` form runs in ~2–5 seconds
for 33,510 nodes. Watch the output — each batch commits independently so an
interruption leaves a consistent (though partially-renamed) graph that is safe
to resume from.

### Step 3 — Deploy updated application code

The application code (FastAPI + Dagster images) must be updated to the version
that uses `:DrillHole` in all Cypher strings before bringing services back up.

```bash
# Build and deploy the new images with the DrillHole label change:
docker compose build georag-fastapi georag-dagster-daemon
docker compose up -d georag-fastapi georag-dagster-daemon
```

If deploying to staging/prod via the CD pipeline, trigger the deployment for
the commit that includes this label rename (see `.github/workflows/cd.yml`).

### Step 4 — Restart services and Dagster

```bash
docker compose restart georag-fastapi
docker compose start georag-dagster-daemon
```

---

## Validation

### Post-migration node counts

```cypher
MATCH (n:Drillhole) RETURN count(n) AS legacy_count;
MATCH (n:DrillHole) RETURN count(n) AS camel_count;
```

Expected: `legacy_count = 0`, `camel_count ≈ 33510`.

### Smoke test — single hole traversal

Replace `<A_KNOWN_HOLE_ID>` with an actual hole ID from your project
(e.g. `PLS-20-01`):

```cypher
MATCH (h:DrillHole {hole_id: '<A_KNOWN_HOLE_ID>'})
RETURN h.hole_id, h.total_depth, h.status, labels(h);
```

Expected: one row with the correct properties and `labels(h)` containing
`["DrillHole"]` (and optionally `"PublicGeo"` for PG-sourced nodes).

### Project → DrillHole traversal

```cypher
MATCH (p:Project)-[:HAS_HOLE]->(h:DrillHole)
RETURN p.name, count(h) AS hole_count;
```

Expected: hole_count matches the pre-migration count per project.

### Application smoke test

Issue a chat query that names a known drill hole. The response must cite the
hole with correct depth/location data (not "no results"):

```bash
curl -s -X POST http://localhost:8000/api/v1/queries \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TEST_TOKEN" \
  -d '{"query": "What is the total depth of hole PLS-20-01?", "project_id": "<PROJECT_UUID>"}' \
  | jq '.text, .citations[].source_chunk_id'
```

### pytest unit tests

```bash
cd src/fastapi
pytest tests/test_neo4j_drillhole_label.py -v -m "not integration"
pytest tests/test_cypher_allowlist.py -v
```

Both must pass with zero failures.

---

## Rollback

If anything goes wrong, revert in the following order:

### 1. Revert the code deployment

Roll back to the previous FastAPI + Dagster image versions (see
`ops/runbooks/deploy-rollback.md`).

### 2. Run the inverse migration

```cypher
CALL {
    MATCH (n:DrillHole) WHERE NOT n:Drillhole
    SET n:Drillhole REMOVE n:DrillHole
} IN TRANSACTIONS OF 1000 ROWS;

DROP CONSTRAINT drillhole_hole_id_unique IF EXISTS;
DROP CONSTRAINT drillhole_collar_id_unique IF EXISTS;
DROP INDEX drillhole_type IF EXISTS;

CREATE CONSTRAINT drillhole_collar_id_unique IF NOT EXISTS
    FOR (d:Drillhole) REQUIRE d.collar_id IS UNIQUE;
CREATE CONSTRAINT drillhole_collar_id_internal IF NOT EXISTS
    FOR (d:Drillhole) REQUIRE d.collar_id IS UNIQUE;
```

### 3. Verify rollback

```cypher
MATCH (n:DrillHole) RETURN count(n) AS camel_count;
MATCH (n:Drillhole) RETURN count(n) AS legacy_count;
```

Expected: `camel_count = 0`, `legacy_count ≈ 33510`.

### 4. Restart services

```bash
docker compose restart georag-fastapi
docker compose start georag-dagster-daemon
```

---

## Coordination

This is a Global Invariant 4 schema change. Both the graph data and the
application code must be in sync at all times:

| State | Graph label | Code expects | Works? |
|-------|-------------|--------------|--------|
| Pre-migration | `:Drillhole` | `:Drillhole` | Yes (current state) |
| Migration done, code not deployed | `:DrillHole` | `:Drillhole` | **No** — zero rows |
| Migration done, code deployed | `:DrillHole` | `:DrillHole` | Yes (target state) |
| Rollback code, migration still applied | `:DrillHole` | `:Drillhole` | **No** — zero rows |

The window where graph and code disagree must be minimised. Stop Dagster before
running the migration and deploy the updated code immediately after validation.

**Reference:** `docs/kyle-decisions.md` D2.
