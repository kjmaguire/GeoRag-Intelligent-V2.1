---
name: georag-schema-contracts
description: GeoRAG §04e/§04f schema enforcement for Eloquent models, migrations, API resources, and Form Requests. Use when creating or editing Laravel models for project, collar, drillhole, lithology, alteration, structure, sampling, assay, geochemistry, or NI 43-101 entities, or when writing migrations that touch the geological domain schema. Triggers on tasks involving §04e schemas, JSONB shapes (assay results, resource_estimate, major_oxides, trace_elements), composite primary keys, geom columns, crs_epsg, workspace_id tenancy, or Neo4j label canonicalisation (DrillHole vs Drillhole).
metadata:
  origin: GeoRAG project (CLAUDE.md hard rule #6 + Section 04e/04f)
  authoritative-sources:
    - georag-architecture.html §04e (Core Data Schemas — PostGIS, 9 schemas)
    - georag-architecture.html §04f (Knowledge Graph Entity Model — 7 node types)
    - georag-architecture.html §04 + §04b (CRS handling, projects.crs_epsg default 32613)
    - CLAUDE.md hard rule #6 (schemas are contracts, no invented fields)
  scope: Laravel-side Eloquent + migrations + Form Requests. Pydantic models for the same schemas live in FastAPI (backend-fastapi agent).
---

# GeoRAG Schema Contracts (§04e + §04f)

The geological domain schema is a **contract**, not a suggestion. Field names, types, enumerations, JSONB shapes, and composite keys are pinned in §04e and §04f. Eloquent models, migrations, and API resources mirror those exactly.

> **CLAUDE.md hard rule #6:** "Schemas in Section 04e are contracts. Don't invent fields. Don't skip constraints. Don't change enumeration values without SME approval."

## When to Apply

Reference this skill when working on:
- Creating or editing Eloquent models in `app/Models/`
- Writing migrations under `database/migrations/`
- Validating user input against domain schemas (Form Requests)
- Persisting FastAPI response payloads (assay arrays, resource estimates, citation graphs)
- Anything mentioning `project_id`, `hole_id`, `crs_epsg`, `workspace_id`, `geom`, `from_m/to_m`, JSONB results
- Cypher producers / Neo4j model alignment (DrillHole label discipline)

## §04e — the 9 schemas (canonical reference)

| Schema | Key fields (verbatim from §04e) |
|---|---|
| **Project** | `project_id` (PK), `name`, `operator`, `commodity[]`, `bbox` (geometry), `crs_epsg`, `created_at` |
| **Collar** | `hole_id` (PK), `project_id` (FK), `easting`, `northing`, `elevation`, `geom` (POINT), `total_depth`, `azimuth`, `dip`, `drill_date` |
| **Downhole Survey** | `hole_id` (FK), `depth_m`, `azimuth`, `dip`, `survey_method` · **composite PK `(hole_id, depth_m)`** |
| **Lithology / Core Log** | `hole_id` (FK), `from_m`, `to_m`, `lith_code`, `lith_desc`, `alteration_intensity`, `structure_notes` · interval PK |
| **Alteration** | `hole_id` (FK), `from_m`, `to_m`, `alt_type`, `intensity` (1-5), `mineralogy[]` |
| **Structure** | `hole_id` (FK), `depth_m`, `structure_type`, `alpha`, `beta`, `dip_dir`, `dip`, `confidence` |
| **Sampling / Assay** | `sample_id` (PK), `hole_id` (FK), `from_m`, `to_m`, `sample_type`, `results` JSONB · e.g. `{"U3O8_ppm":1250,"Au_ppb":45}` |
| **Geochemistry (Whole-Rock)** | `sample_id` (FK), `major_oxides` JSONB, `trace_elements` JSONB, computed: `Mg#`, `CIA`, `Eu/Eu*` |
| **NI 43-101 Document** | `doc_id` (PK), `project_id` (FK), `title`, `author`, `qp_name`, `effective_date`, `resource_estimate` JSONB, `source_pdf_uri`, `page_index[]` |

**If you need a field that's not in this list — do NOT invent it.** Read §04e in full first. If it's genuinely missing, raise it with the SME (Kyle) before writing the migration.

## Tenancy is cross-cutting — every table needs `workspace_id`

Per §04i: "`workspace_id` population across PostgreSQL, Qdrant, and Neo4j is the joint responsibility of Module 3 ingestion (writes) and Module 9 RBAC enforcement". Every Laravel-managed table in the geological domain schema must include `workspace_id` and have a payload index for filtering.

## Eloquent ↔ §04e mapping rules

### Casts must reflect the schema, not Laravel defaults

```php
<?php

declare(strict_types=1);

namespace App\Models;

use Illuminate\Database\Eloquent\Model;
use Illuminate\Database\Eloquent\Relations\HasMany;

final class Project extends Model
{
    protected $primaryKey = 'project_id';
    public $incrementing = false;          // schema uses string/uuid project_ids, not auto-increment
    protected $keyType = 'string';

    protected $fillable = [
        'project_id',
        'name',
        'operator',
        'commodity',
        'bbox',
        'crs_epsg',
        'workspace_id',
    ];

    protected $casts = [
        'commodity'   => 'array',           // commodity[] in PostgreSQL
        'crs_epsg'    => 'integer',         // SRID, default 32613 per §04b
        // bbox is a PostGIS geometry — DO NOT cast it; load with ST_AsGeoJSON or use a
        // package like clickbar/laravel-magellan if real geometry handling is needed.
    ];

    // Default CRS per §04b
    public function getCrsEpsgAttribute(?int $value): int
    {
        return $value ?? 32613;
    }

    public function collars(): HasMany
    {
        return $this->hasMany(Collar::class, 'project_id', 'project_id');
    }
}
```

### Composite primary keys (Downhole Survey)

Eloquent's default doesn't fully support composite PKs. Disable auto-incrementing and override `setKeysForSaveQuery()`:

```php
final class DownholeSurvey extends Model
{
    public $incrementing = false;
    protected $primaryKey = null;             // intentional: Eloquent does not assume one column
    public $timestamps = false;               // §04e schema has no created_at on this table

    protected $fillable = ['hole_id', 'depth_m', 'azimuth', 'dip', 'survey_method', 'workspace_id'];

    protected function setKeysForSaveQuery($query)
    {
        return $query
            ->where('hole_id', $this->getAttribute('hole_id'))
            ->where('depth_m', $this->getAttribute('depth_m'));
    }
}
```

### JSONB fields: cast `array`, validate shape on write

```php
final class Assay extends Model
{
    protected $primaryKey = 'sample_id';
    public $incrementing = false;
    protected $keyType = 'string';

    protected $casts = [
        'results' => 'array',                  // JSONB
    ];

    /**
     * Variable-key payload — validate shape but don't enforce a closed schema here.
     * Actual key set is open by design (different commodities, different assay panels).
     * Numeric range validation belongs in the FormRequest, not here.
     */
    public function setResultsAttribute(array $value): void
    {
        foreach ($value as $key => $v) {
            if (! preg_match('/^[A-Z][A-Za-z0-9_]+_(ppm|ppb|pct|gpt)$/', $key)) {
                throw new \DomainException("Invalid assay key shape: {$key}. Expected like U3O8_ppm or Au_ppb.");
            }
            if (! is_numeric($v)) {
                throw new \DomainException("Assay value for {$key} must be numeric, got " . gettype($v));
            }
        }
        $this->attributes['results'] = json_encode($value, JSON_THROW_ON_ERROR);
    }
}
```

### Bounded enumerations (`alteration.intensity`, `commodity[]`)

§04e specifies `alteration.intensity (1-5)`. Enforce in the migration constraint **and** in a Form Request — defense in depth.

```php
// migration
$table->smallInteger('intensity')->checkBetween(1, 5);

// form request
public function rules(): array
{
    return [
        'intensity'    => ['required', 'integer', 'between:1,5'],
        'alt_type'     => ['required', Rule::in(SmeConfig::alterationTypes())],  // SME-managed list, NOT hardcoded
        'mineralogy'   => ['array'],
        'mineralogy.*' => ['string', Rule::in(SmeConfig::mineralVocabulary())],
    ];
}
```

**Per §04e key-note:** Feature engineering rules (grade thresholds, net pay, Mg# computation) and controlled vocabularies (mineral names, alteration types) are **SME-provided configuration loaded at runtime — never hardcoded.** Pull from a dedicated config service (`App\Services\SmeConfig`), not from constant arrays in PHP files.

## §04f — Neo4j label canonicalisation (D2, resolved 2026-04-27)

| ✅ Canonical label | ❌ Forbidden |
|---|---|
| `:DrillHole` | `:Drillhole` |
| `:Project`, `:Formation`, `:Report`, `:MineralOccurrence`, `:GeophysicalSurvey`, `:Publication` | (CamelCase per Global Invariant 4) |

When Laravel produces Cypher (rare — usually `graph-engineer` agent territory), use `:DrillHole`. The Cypher allowlist in `tools.py` rejects the lowercase form. Migration script for legacy data: `ops/migrations/neo4j/2026-04-27-drillhole-rename.cypher`.

## Migration discipline

1. **Match §04e field names exactly.** No `created_at` on tables that don't list it. No `is_active` columns invented for "future use".
2. **Use named foreign keys.** `$table->foreignId('project_id')->constrained('projects', 'project_id')->cascadeOnDelete();`
3. **Index the spatial columns.** `$table->index('geom', 'collars_geom_idx', 'gist');` — required for PostGIS retrieval performance (§06).
4. **Index `workspace_id` on every table.** Tenancy filter is on the hot path.
5. **Do not add `softDeletes()` unless §04e specifies it.** Geological data is immutable from a domain perspective; lineage is preserved by ingestion versioning, not by Laravel soft-delete columns.
6. **Audit/operational tables go in a separate schema** (per §05 step 6 — see `georag-rag-citations` skill). Keep Laravel-internal tables (`personal_access_tokens`, `failed_jobs`, etc.) out of the geological schema.

```php
// database/migrations/YYYY_MM_DD_create_collars.php
return new class extends Migration
{
    public function up(): void
    {
        Schema::create('collars', function (Blueprint $t): void {
            $t->string('hole_id')->primary();
            $t->string('project_id');
            $t->foreign('project_id')->references('project_id')->on('projects')->cascadeOnDelete();

            $t->double('easting');
            $t->double('northing');
            $t->double('elevation')->nullable();

            // PostGIS POINT in WGS84 (EPSG:4326) per §04 (storage CRS) — not project CRS
            DB::statement('ALTER TABLE collars ADD COLUMN geom geometry(Point, 4326);');

            $t->double('total_depth');
            $t->double('azimuth');
            $t->double('dip');
            $t->date('drill_date')->nullable();

            $t->string('workspace_id');
            $t->index('workspace_id');
            // GIST spatial index added below via raw SQL (Schema::create can't express it)
        });

        DB::statement('CREATE INDEX collars_geom_idx ON collars USING GIST (geom);');
    }

    public function down(): void
    {
        Schema::dropIfExists('collars');
    }
};
```

## Form Request validation pattern

API-side validation must catch field-shape violations before the model is touched.

```php
final class StoreCollarRequest extends FormRequest
{
    public function rules(): array
    {
        return [
            'hole_id'      => ['required', 'string', 'max:64'],
            'project_id'   => ['required', 'string', 'exists:projects,project_id'],
            'easting'      => ['required', 'numeric'],
            'northing'     => ['required', 'numeric'],
            'elevation'    => ['nullable', 'numeric'],
            'total_depth'  => ['required', 'numeric', 'min:0'],
            'azimuth'      => ['required', 'numeric', 'between:0,360'],
            'dip'          => ['required', 'numeric', 'between:-90,90'],
            'drill_date'   => ['nullable', 'date'],
            // workspace_id is set from the authenticated user's current workspace, NOT user-supplied
        ];
    }

    public function prepareForValidation(): void
    {
        $this->merge([
            'workspace_id' => $this->user()->currentWorkspace->id,
        ]);
    }
}
```

## Anti-patterns

| ❌ Don't | ✅ Do |
|---|---|
| Add `is_active`, `is_deleted`, `notes` "for future use" to a §04e table | If §04e doesn't list it, don't add it. Raise with SME first. |
| Hardcode commodity list `['Au', 'Ag', 'Cu', 'U']` in a PHP enum | Pull from `SmeConfig::commodityVocabulary()` — SME-managed config |
| Cast PostGIS `geom` as a string | Leave it raw and use `ST_AsGeoJSON()` or a geometry package; never serialize WKB as a Laravel string cast |
| Trust user-supplied `workspace_id` | Always overwrite from authenticated session in `prepareForValidation()` |
| Soft-delete a NI 43-101 doc | Geological documents are append-only history. Mark superseded via `effective_date` + a separate supersession relationship in §04f, not by deletion |
| Write `:Drillhole` in Cypher | `:DrillHole` is canonical. The allowlist will reject the wrong case. |
| Allow `intensity` outside 1-5 in a migration without a CHECK constraint | Defense in depth — Form Request *and* DB constraint |

## Validation checkpoints

| Stage | Command | Expected |
|---|---|---|
| Migration created | `php artisan migrate:fresh --pretend` | All §04e tables present, no extra columns, no missing FK constraints |
| Schema audit | `php artisan db:show` | Column types match §04e (e.g., `intensity` is `smallint`, `commodity` is `text[]`, `results` is `jsonb`) |
| Eloquent casts | `php artisan test --filter=ProjectModelTest` | `commodity` returns array, `crs_epsg` returns int with 32613 default |
| Composite PK save | `php artisan test --filter=DownholeSurveyModelTest` | Multiple rows per `hole_id` save and update by `(hole_id, depth_m)` correctly |
| JSONB key shape | `php artisan test --filter=AssayModelTest` | Invalid keys (`badkey`, `Au`, `Au_xyz`) throw `DomainException` |
| Workspace tenancy | `php artisan test --filter=CollarTenancyTest` | User from workspace A cannot read collars from workspace B |
| Cypher label discipline | `grep -rE ':Drillhole\b' app/ database/` | No matches (only `:DrillHole`) |

## When you're stuck

- **Field unclear?** Re-read §04e in `georag-architecture.html`. Don't infer from sibling tables.
- **JSONB shape ambiguous?** §04e's example payloads are illustrative — for a closed schema, ask SME.
- **Tempted to add a column not in §04e?** Stop. Ask Kyle. The schema is a contract.
- **Vocabulary question (mineral names, alteration types)?** Pull from `SmeConfig`, never hardcode.
- **Cross-cutting (touches Neo4j too)?** Hand off to `graph-engineer` agent for the Cypher side.

## Cross-references

- **Citation persistence (audit schema, separate from §04e):** read `georag-rag-citations` SKILL
- **HTTP payload validation when receiving FastAPI results:** read `georag-octane-bridge` SKILL
- **General Eloquent patterns:** read `laravel-best-practices`, `laravel-patterns`