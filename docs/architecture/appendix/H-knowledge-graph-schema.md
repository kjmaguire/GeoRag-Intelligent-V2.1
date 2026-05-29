# Appendix H — Knowledge Graph Schema (Neo4j)

Status: **Draft.** Source of truth is
[docker/neo4j/init-schema.cypher](../../../docker/neo4j/init-schema.cypher);
this appendix codifies the *contract*: required properties, workspace
fence, upsert/conflict rules, deletion rules, and example queries.

> **Edition: Community.** Hard Rule #9. No Enterprise features
> (existence constraints, node keys, multi-database, fine-grained RBAC,
> page-cache warmup). The init script uses only uniqueness constraints
> and RANGE indexes.

## 1. Node label canon

Per the §04e schema contract:

| Label (canonical) | Source table | Primary property | Notes |
|---|---|---|---|
| `:Project` | `silver.projects` | `name UNIQUE` | Anchor for cross-project MERGE |
| `:DrillHole` | `silver.collars` | `hole_id UNIQUE` | **Canonical capitalisation: `DrillHole`** (NOT `Drillhole`); enforced via uniqueness constraint |
| `:Formation` | `silver.geological_formations` | `name UNIQUE` | |
| `:RockUnit` | `silver.lithology` (derived) | `(unit_code, hole_id)` natural | |
| `:MineralOccurrence` | `silver.spatial_features (kind='mineral_occurrence')` + `public_geo.pg_mineral_occurrences` | `(jurisdiction, source_id)` | |
| `:Citation` | derived from `silver.evidence_items` | `evidence_id` (8-hex prefix) | |
| `:Document` | `silver.reports` | `report_id` | |
| `:Report` | `silver.reports` (NI 43-101 subset) | `title UNIQUE` | Subclass-style; `:Document:Report` dual labels allowed |
| `:Publication` | external journals (rare) | `title UNIQUE` | |
| `:Anomaly` | derived (`anomaly_detector` tool) | `anomaly_id` | |
| `:Hypothesis` | `silver.hypotheses` | `hypothesis_id` | |
| `:Entity` | derived NER (`silver_entity_ner_backfill`) | `(kind, normalised_name)` | Bag-of-entities surface |
| `:GeophysicalSurvey` | `silver.geophysics_surveys` | `survey_id` | |
| `:Tombstoned` | (additional label) | — | Soft-delete marker; readers must `NOT n:Tombstoned` |

## 2. Uniqueness constraints (live)

From [docker/neo4j/init-schema.cypher](../../../docker/neo4j/init-schema.cypher):

```cypher
CREATE CONSTRAINT project_name_unique     IF NOT EXISTS FOR (p:Project)            REQUIRE p.name  IS UNIQUE;
CREATE CONSTRAINT drillhole_hole_id_unique IF NOT EXISTS FOR (h:DrillHole)         REQUIRE h.hole_id IS UNIQUE;
CREATE CONSTRAINT formation_name_unique   IF NOT EXISTS FOR (f:Formation)          REQUIRE f.name  IS UNIQUE;
CREATE CONSTRAINT report_title_unique     IF NOT EXISTS FOR (r:Report)             REQUIRE r.title IS UNIQUE;
CREATE CONSTRAINT publication_title_unique IF NOT EXISTS FOR (pub:Publication)     REQUIRE pub.title IS UNIQUE;
```

## 3. RANGE indexes (live)

```cypher
CREATE INDEX mineral_occurrence_commodity      IF NOT EXISTS FOR (m:MineralOccurrence) ON (m.commodity);
CREATE INDEX geophysical_survey_type           IF NOT EXISTS FOR (s:GeophysicalSurvey) ON (s.type);
CREATE INDEX report_date                       IF NOT EXISTS FOR (r:Report)            ON (r.date);
CREATE INDEX drillhole_type                    IF NOT EXISTS FOR (h:DrillHole)         ON (h.type);
CREATE INDEX formation_age                     IF NOT EXISTS FOR (f:Formation)         ON (f.age);
CREATE INDEX project_region                    IF NOT EXISTS FOR (p:Project)           ON (p.region);
CREATE INDEX project_commodity                 IF NOT EXISTS FOR (p:Project)           ON (p.commodity);
CREATE INDEX mineral_occurrence_deposit_type   IF NOT EXISTS FOR (m:MineralOccurrence) ON (m.deposit_type);
CREATE INDEX geophysical_survey_date           IF NOT EXISTS FOR (s:GeophysicalSurvey) ON (s.date);
CREATE INDEX publication_year                  IF NOT EXISTS FOR (pub:Publication)     ON (pub.year);
```

## 4. Required and optional properties per label

### 4.1 `:Project`
**Required**: `workspace_id` (uuid), `project_id` (uuid),
`name` (unique), `region`, `commodity`.
**Optional**: `started_at`, `status`, `operator`.

### 4.2 `:DrillHole`
**Required**: `workspace_id`, `project_id`, `hole_id` (unique),
`easting`, `northing`, `srid`.
**Optional**: `elevation`, `total_depth`, `azimuth`, `dip`,
`drill_date`, `type`, `status`.

### 4.3 `:Formation`
**Required**: `workspace_id`, `name`, `age`.
**Optional**: `lithology`, `rock_class`, `structural_domain`.

### 4.4 `:Report`
**Required**: `workspace_id`, `report_id`, `title`, `date`,
`report_type` (NI 43-101 / internal / regulatory / …).
**Optional**: `author`, `company`, `language`.

### 4.5 `:MineralOccurrence`
**Required**: `workspace_id`, `commodity`, `name`.
**Optional**: `deposit_type`, `production_flag`, `discovery_year`,
`source_id`, `jurisdiction_code`.

### 4.6 `:Citation`
**Required**: `workspace_id`, `evidence_id` (full UUID),
`evidence_id_short` (8-hex prefix), `source_table`, `source_pk` (json).
**Optional**: `page_first`, `page_last`, `char_span_start`,
`char_span_end`.

### 4.7 `:Anomaly`
**Required**: `workspace_id`, `anomaly_id`, `kind`, `score`.
**Optional**: `detector`, `created_at`, `linked_run_id`.

### 4.8 `:Hypothesis`
**Required**: `workspace_id`, `hypothesis_id`, `status`,
`author_user_id`.
**Optional**: `score`, `created_at`.

### 4.9 `:GeophysicalSurvey`
**Required**: `workspace_id`, `survey_id`, `type`, `date`,
`srid`.
**Optional**: `contractor`, `line_spacing_m`, `flight_height_m`.

## 5. Relationships

Every relationship MUST carry `workspace_id` to keep the tenant fence
intact at edge level (Community Edition has no edge-property constraints
— enforce via writer convention + nightly verifier).

| Type | From | To | Required props | Notes |
|---|---|---|---|---|
| `:HAS_HOLE` | `:Project` | `:DrillHole` | `workspace_id`, `source_run_id` | |
| `:INTERSECTS` | `:DrillHole` | `:Formation` | `workspace_id`, `from_depth`, `to_depth`, `source_run_id` | |
| `:NEAR` | `:DrillHole` | `:DrillHole` | `workspace_id`, `distance_m` | Derived; rebuilt nightly |
| `:CITES` | `:Document` | `:Citation` | `workspace_id`, `source_run_id` | |
| `:CITES_DRILLHOLE` | `:Document` | `:DrillHole` | `workspace_id`, `page_first`, `page_last`, `source_run_id` | |
| `:CITES_DATA_FROM` | `:Document` | `:Publication` | `workspace_id`, `source_run_id` | |
| `:REFERENCES_FORMATION` | `:Document` | `:Formation` | `workspace_id`, `confidence` | |
| `:HOSTED_BY_FORMATION` | `:MineralOccurrence` | `:Formation` | `workspace_id` | |
| `:OVERLIES` / `:UNDERLIES` | `:Formation` | `:Formation` | `workspace_id` | Stratigraphic |
| `:HAS_SURVEY` | `:Project` | `:GeophysicalSurvey` | `workspace_id` | |
| `:OBSERVED_IN` | `:Anomaly` | `:DrillHole` \| `:GeophysicalSurvey` | `workspace_id`, `confidence` | |
| `:SUPPORTS` | `:Hypothesis` | `:Citation` | `workspace_id`, `weight` | |
| `:CONTRADICTS` | `:Hypothesis` | `:Citation` | `workspace_id`, `weight` | |

## 6. Workspace isolation (the fence)

- Every node carries `workspace_id`.
- Every relationship carries `workspace_id`.
- Every read tool MUST inject `{workspace_id: $ws}` into the pattern or
  filter:

```cypher
MATCH (h:DrillHole {workspace_id: $ws, hole_id: $hole_id})
WHERE NOT h:Tombstoned
RETURN h
```

- Cross-tenant reads are impossible at the engine level only because the
  writer convention guarantees the fence; Neo4j Community has no
  built-in RLS. A nightly verifier (`graph_tenant_audit` planned)
  scans for nodes/edges with mismatched `workspace_id` across a
  relationship.

## 7. Source table → node mapping

| Postgres source | Graph node | Triggering writer |
|---|---|---|
| `silver.projects` | `:Project` | Dagster `index_neo4j` asset |
| `silver.collars` | `:DrillHole` | same |
| `silver.geological_formations` | `:Formation` | same |
| `silver.reports` | `:Document` (+`:Report` if `report_type='ni43-101'`) | same |
| `silver.geophysics_surveys` | `:GeophysicalSurvey` | same |
| `silver.hypotheses` | `:Hypothesis` | Hatchet `sync_silver_to_kg` |
| `silver.evidence_items` | `:Citation` | Hatchet `sync_silver_to_kg` |

## 8. Source table → relationship mapping

| Relationship | Computed from |
|---|---|
| `:HAS_HOLE` | `silver.collars.project_id` |
| `:INTERSECTS` | `silver.lithology` rows joined to `silver.geological_formations` on rock-code match |
| `:NEAR` | nightly job: pairs of `silver.collars` within radius |
| `:CITES` / `:CITES_DRILLHOLE` / `:REFERENCES_FORMATION` | `silver.answer_citation_items` (post answer-write) |
| `:HOSTED_BY_FORMATION` | `silver.spatial_features (kind='mineral_occurrence')` ↔ formation polygons via ST_Within |
| `:OBSERVED_IN` | `anomaly_detector` tool emit |

## 9. Upsert rules

Always `MERGE` on the unique property, then `SET` non-unique fields:

```cypher
MERGE (h:DrillHole {hole_id: $hole_id})
  ON CREATE SET h.workspace_id = $ws,
                h.project_id   = $project_id,
                h.created_at   = datetime()
  ON MATCH  SET h.updated_at   = datetime()
SET h += $props
```

Relationships use the same pattern:

```cypher
MATCH (h:DrillHole {hole_id: $hole_id}), (f:Formation {name: $formation})
MERGE (h)-[r:INTERSECTS {from_depth: $from, to_depth: $to}]->(f)
  ON CREATE SET r.workspace_id   = $ws,
                r.source_run_id  = $run_id,
                r.created_at     = datetime()
```

`from_depth` + `to_depth` are part of the relationship's natural key
because the same (hole, formation) pair can have multiple intersections
at different depths.

## 10. Deletion rules

- **Hard delete on workspace deletion only.**
  ```cypher
  MATCH (n {workspace_id: $ws}) DETACH DELETE n;
  ```
- **Soft delete (typical):** add `:Tombstoned` label + `tombstoned_at`
  property. Every read tool excludes via `NOT n:Tombstoned`.
- **Re-ingestion cleanup:** before re-ingesting a document, soft-delete
  all `:Citation` and `:REFERENCES_FORMATION` edges sourced from that
  document's `source_run_id`.

## 11. Conflict resolution

Conflicting writes (same node, different sources, different property
values):

1. **Provenance wins by source rank.** Bronze-direct > Dagster
   silver-derive > LLM agent.
2. **Tie → most recent timestamp.**
3. **Tie + same timestamp → keep both as separate `:Property` sub-nodes**
   (rare; surfaced to the SME via the conflicts router endpoint).

## 12. Example Cypher queries (graph-backed agent tools)

### 12.1 `graph_traversal` — hole context

```cypher
MATCH (h:DrillHole {workspace_id: $ws, hole_id: $hole_id})
OPTIONAL MATCH (h)-[:INTERSECTS]->(f:Formation)
OPTIONAL MATCH (h)<-[:CITES_DRILLHOLE]-(d:Document)
OPTIONAL MATCH (h)-[:NEAR]-(neighbour:DrillHole)
RETURN h, collect(DISTINCT f) AS formations,
       collect(DISTINCT d) AS documents,
       collect(DISTINCT neighbour) AS neighbours
```

### 12.2 `entity-link` — find adjacent occurrences

```cypher
MATCH (h:DrillHole {workspace_id: $ws, hole_id: $hole_id})-[:NEAR]-(n:DrillHole)
WITH n
MATCH (n)-[:INTERSECTS]->(f:Formation)<-[:HOSTED_BY_FORMATION]-(m:MineralOccurrence)
WHERE m.workspace_id = $ws
RETURN DISTINCT m.name, m.commodity
```

### 12.3 `citation-graph` — Document → Citation → Document

```cypher
MATCH (d1:Document {workspace_id: $ws, report_id: $doc})-[:CITES]->(c:Citation)
MATCH (c)<-[:CITES]-(d2:Document)
WHERE d2.workspace_id = $ws AND d2 <> d1
RETURN d2.title, count(c) AS shared_citations
ORDER BY shared_citations DESC
```

## 13. Tests

- **Schema bootstrap**: `tests/Feature/Graph/SchemaBootstrapTest.php` —
  runs `init-schema.cypher` against an ephemeral Neo4j and asserts every
  constraint and index exists.
- **Workspace fence**: writes nodes in two workspaces; asserts a query
  with `$ws=A` never returns workspace B nodes.
- **Upsert idempotence**: runs the same `MERGE` block twice; asserts the
  node + edge counts are unchanged.
- **Soft-delete behaviour**: tombstone a node; asserts read tools
  exclude it.
- **Provenance edge**: every edge has a non-null `source_run_id` after
  any Dagster `index_neo4j` materialisation.
