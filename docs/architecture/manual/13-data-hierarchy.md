# Chapter 13 — Data Hierarchy (geologist-facing classification)

> Status: **Partial.** Schema columns + UI tag surface defined here; the
> upload classifier and per-dataset multi-category storage land alongside
> the chat-cards work (ADR-0007 PR-2/PR-3).

The internal medallion (bronze/silver/gold) is the *engineering* view of
data. Geologists navigate by a different taxonomy. This chapter defines
that taxonomy as a first-class product contract.

## 1. The four top-level categories

```
Reports
  ├── Environmental / permitting / social reports
  ├── Government & tenure-maintenance reports
  ├── Regulatory & market disclosure
  ├── NI 43-101 technical reports
  ├── Feasibility studies (PEA / PFS / FS)
  └── Internal operations & exploration reports

Geology
  ├── 2D geologic maps
  ├── 3D geologic models
  ├── Structural mapping
  └── Logged lithology

Geochemistry
  ├── Soil samples
  ├── Rock chip samples
  ├── Drill core samples
  └── Assays

Geophysics
  ├── EM
  ├── MT (Magnetotellurics)
  ├── IP (Induced Polarisation)
  ├── Resistivity
  ├── Airborne EM
  ├── Gravity
  ├── Seismic
  └── Radiometric
```

## 2. Multi-category classification (mandatory)

A single dataset can — and often must — sit under multiple top-level
categories simultaneously. The canonical example:

- A drillhole record belongs to **Geology** (logged lithology), **Geochemistry**
  (assays), *and* **Geophysics** (downhole geophysics) when all three
  attached.

The data model must therefore allow many-to-many between a `dataset_id`
and a `category_path`.

## 3. Schema (proposed — to land alongside ADR-0007 PR-2)

```sql
CREATE TABLE silver.data_categories (
    category_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    parent_id      UUID NULL REFERENCES silver.data_categories(category_id),
    code           TEXT NOT NULL UNIQUE,         -- 'geology.lithology'
    label          TEXT NOT NULL,                 -- 'Logged lithology'
    description    TEXT NOT NULL,
    sort_order     INT  NOT NULL DEFAULT 0
);

CREATE TABLE silver.dataset_categories (
    dataset_kind   TEXT NOT NULL,                 -- 'document'|'drillhole'|'sample'|'survey'|'map'|'model'
    dataset_id     UUID NOT NULL,
    workspace_id   UUID NOT NULL REFERENCES silver.workspaces(workspace_id)
                   ON DELETE CASCADE,
    category_id    UUID NOT NULL REFERENCES silver.data_categories(category_id),
    source         TEXT NOT NULL,                 -- 'parser'|'user'|'agent'
    confidence     NUMERIC(4,3) NULL,             -- when source='parser'/'agent'
    assigned_by    BIGINT NULL REFERENCES public.users(id),
    assigned_at    TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (dataset_kind, dataset_id, category_id)
);

CREATE INDEX dataset_categories_workspace_idx
    ON silver.dataset_categories (workspace_id, dataset_kind, category_id);
```

- RLS: `workspace_id = current_setting('app.workspace_id', true)::uuid`.
- Seed `silver.data_categories` from a checked-in fixture
  (`database/seeders/data_categories_seed.sql`) so codes are stable across
  environments.
- `dataset_kind` values use one canonical token per source table
  (`document` → `silver.reports`, `drillhole` → `silver.collars`,
  `sample` → `silver.samples`, `survey` → `silver.geophysics_surveys`,
  `map` → `silver.spatial_features` where `feature_type='map'`,
  `model` → `silver.cog_rasters`/`silver.raster_layers` etc.).

## 4. Per-document vs per-dataset tagging

| Asset | Categorisation flow |
|---|---|
| Documents (silver.reports) | (a) parser-default from `report_type` enum at ingest, (b) agent refinement at chat time when answers need to filter, (c) user override in the Sources page |
| Drillholes (silver.collars) | (a) parser default `geology.lithology` always, (b) auto-added `geochemistry.assays` if any `silver.assays_v2` row exists for the hole, (c) auto-added `geophysics.downhole_*` if `silver.geophysics_surveys` references the hole, (d) user override |
| Samples (silver.samples) | (a) parser default from sample_kind (`soil`→geochemistry.soil_samples, `rock_chip`→geochemistry.rock_chip_samples, `drill_core`→geochemistry.drill_core_samples) |
| Surveys (silver.geophysics_surveys) | (a) parser default by `survey_type`; (b) user re-classification |
| Maps / 3D models | (a) parser default; (b) user-provided |

## 5. User-correction flow

- **Where:** Sources page ([resources/js/Pages/Foundry/Sources.tsx](../../../resources/js/Pages/Foundry/Sources.tsx))
  and Corpus page ([resources/js/Pages/Foundry/Corpus.tsx](../../../resources/js/Pages/Foundry/Corpus.tsx)).
- **API:** `POST /api/projects/{project}/datasets/{kind}/{id}/categories`
  with body `{add: ["geology.lithology"], remove: ["reports.internal"]}`.
- **Side effects:** writes to `silver.dataset_categories` with
  `source='user'`; emits an `audit.audit_ledger` row of `action_type =
  'dataset.categorise'`; emits Reverb `workspace-data-updated.{workspace_id}`
  so other connected clients re-fetch.

## 6. UI display rules

- Sidebar in every Foundry page renders the four top-level categories +
  per-category badges showing the live count.
- A dataset card displays all its categories as small pills
  (Geology/Lithology, Geochemistry/Assays, etc.). The first category
  drives the icon; the rest are additive.
- The Lakehouse / map view uses category as a primary layer filter facet
  (orthogonal to layer source).
- The Chat page shows category facets in the retrieval drawer (Chapter 06):
  the user can constrain a query to one or more categories before sending.

## 7. Retrieval filters by category

- Retrieval profile (Ch 06 §2) accepts an optional `category_filter:
  list[str]`.
- The route node injects `category_filter` into the Qdrant payload filter
  (`payload.category_codes` is a payload-indexed `text[]` written by the
  embedder workflow).
- Postgres BM25 path joins to `silver.dataset_categories`.
- Neo4j graph path filters on the `category_codes` node property.

## 8. Map / layer relationship to categories

- Public-geo layers carry an implicit category map:
  - `pg_bedrock_geology` → `geology.maps_2d`
  - `pg_mines` + `pg_mineral_occurrences` → cross-category (both geology
    and geochemistry)
  - `pg_drillhole_collars` → cross-category (geology + geochemistry +
    geophysics where downhole present)
  - `pg_assessment_surveys` → `reports.assessment` + `geophysics.*` by
    survey_type
- Silver workspace layers inherit from the source table’s default
  classification.

## 9. Acceptance criteria

- Every dataset in `silver.*` has ≥ 1 row in `silver.dataset_categories`
  before it can be rendered on any UI surface.
- A drillhole with attached assays renders under all three of Geology +
  Geochemistry + Geophysics (only when downhole geophysics exists)
  category facets simultaneously.
- A user override is persisted, audit-logged, and survives a parser
  re-run (parser categories are merged, not overwritten — the user’s
  source='user' row wins on conflict).
- RLS coverage test (`WorkspaceRlsCoverageTest`) includes
  `silver.dataset_categories`.

## 10. Open questions

- How do we represent "this drillhole has *no* assays" — absence as a
  positive signal? Likely a derived view rather than a row.
- Should the `Reports.*` subtree be ML-classified from PDF content (the
  `silver.reports.report_type` enum already exists) or always user-set?
  Default: parser sets, user can correct, agent suggests via the
  `decision_support_classifier`.
- Multi-jurisdiction taxonomy harmonisation (SMDI vs BC MINFILE vs NTGS)
  — currently each public-geo source uses its own status codes; an
  intermediate `category_aliases` table may be needed.
