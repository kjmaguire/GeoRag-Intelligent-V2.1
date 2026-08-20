<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Seed the public-geoscience source registry: public_geo.jurisdictions and
 * public_geo.sources.
 *
 * Why this exists
 * ---------------
 * A seed already exists — 2026_05_13_180000 — and it declares the
 * jurisdictions plus NINE source rows under the original lowercase naming
 * (`sk_mineral_occurrence`, `nrcan_canadian_mines`, …). That much any
 * environment gets.
 *
 * What it does NOT declare is the working set. Thirty-two further feeds were
 * added to the local cluster out of band, under a different convention
 * (`CA-SK-MINE-LOC`, `CA-BC-MINFILE`), and those are the ones carrying real
 * ArcGIS layer indices and source CRS values. They were never put in the
 * migration chain, so Azure has the nine placeholders and none of the feeds
 * that actually resolve. Same recurring shape as silver.collars.geom_4326 and
 * the workflow.* functions: applied by hand locally, never declared, so Azure
 * silently diverged.
 *
 * The consequence is sharper here than usual: with only the legacy nine, an
 * Azure-side pull iterates rows whose service URLs do not describe the layers
 * the ingest expects, so it cannot bootstrap regardless of how correct the
 * pull code is.
 *
 * Overlap is real and deliberately NOT resolved here
 * --------------------------------------------------
 * Several of the nine legacy rows cover the same (jurisdiction, canonical
 * type) pair as a new feed — `CA-SK-SMDI` vs `sk_mineral_occurrence`,
 * `CA-BC-MINFILE` vs `bc_minfile_mineral_occurrence`, and the same for SK
 * drillhole_collar and assessment_survey. After this migration both exist.
 *
 * They are left coexisting on purpose. Deciding which of a duplicated pair is
 * authoritative — and deleting the loser — changes what a pull ingests and
 * what existing rows key against; a seed migration is the wrong place to make
 * that call silently. Flagged for a follow-up rather than resolved by
 * whichever row happens to sort last.
 *
 * (The larger counts are not duplication: CA-SK resource_potential_zone has
 * eleven rows and mineral_disposition twelve because each is a genuine
 * per-commodity or per-layer endpoint.)
 *
 * The consequence is sharper than usual. These rows are not data — they are
 * the *configuration* that tells an ingest which public ArcGIS REST endpoints
 * to pull from. With the table empty, an Azure-side public-geoscience pull has
 * nothing to iterate and can never bootstrap itself, no matter how correct the
 * pull code is. Verified 2026-08-19: Azure Qdrant holds no pg_* collections at
 * all and Azure Postgres holds 0 public_geo rows.
 *
 * What this is NOT
 * ----------------
 * This does not move the corpus. Every value below is a public government
 * service URL, a licence reference, or a CRS code — published configuration,
 * not the ~183k indexed records. Azure fetches the actual features from the
 * upstream provincial feeds itself; nothing is copied out of the local
 * database.
 *
 * Faithfulness
 * ------------
 * Transcribed verbatim from the local registry rather than re-derived, so the
 * two environments converge on identical configuration. Runtime state columns
 * (last_refreshed_at, last_service_edit_ms, created_at, updated_at) are
 * deliberately NOT seeded — they belong to whichever environment did the
 * pulling, and copying them would make Azure believe it had already fetched
 * feeds it has never touched.
 *
 * Both `CA-FED` and `CA-FEDERAL` appear in the jurisdiction list. That looks
 * like drift worth reconciling, but it is transcribed as-is: a source row
 * references one of them, and quietly collapsing the two here would change
 * referential meaning in a seed migration, which is the wrong place to make
 * that call.
 *
 * Idempotent: upserts on the primary key, so a re-run refreshes configuration
 * without duplicating, and an environment that already has these rows keeps
 * its own runtime-state columns untouched.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        if (! $this->tableExists('jurisdictions') || ! $this->tableExists('sources')) {
            // Fresh clusters that have not run the 2026_04_14 chain yet.
            return;
        }

        // Jurisdictions first — sources.jurisdiction_code references them.
        foreach (self::JURISDICTIONS as $j) {
            $bbox = $j['bbox_wkt'];
            unset($j['bbox_wkt']);

            $columns = array_keys($j);
            $placeholders = implode(', ', array_fill(0, count($columns), '?'));
            $columnList = implode(', ', $columns);
            $updates = implode(', ', array_map(
                static fn (string $c): string => "{$c} = EXCLUDED.{$c}",
                $columns,
            ));

            $bboxColumn = $bbox === null ? '' : ', bbox';
            $bboxValue = $bbox === null ? '' : ', ST_GeomFromText(?, 4326)';
            $bboxUpdate = $bbox === null ? '' : ', bbox = EXCLUDED.bbox';

            $bindings = array_values($j);
            if ($bbox !== null) {
                $bindings[] = $bbox;
            }

            DB::insert(
                "INSERT INTO public_geo.jurisdictions ({$columnList}{$bboxColumn}, created_at, updated_at)
                 VALUES ({$placeholders}{$bboxValue}, now(), now())
                 ON CONFLICT (jurisdiction_code) DO UPDATE SET {$updates}{$bboxUpdate}, updated_at = now()",
                $bindings,
            );
        }

        foreach (self::SOURCES as $s) {
            $columns = array_keys($s);
            $placeholders = implode(', ', array_fill(0, count($columns), '?'));
            $columnList = implode(', ', $columns);
            $updates = implode(', ', array_map(
                static fn (string $c): string => "{$c} = EXCLUDED.{$c}",
                $columns,
            ));

            DB::insert(
                "INSERT INTO public_geo.sources ({$columnList}, created_at, updated_at)
                 VALUES ({$placeholders}, now(), now())
                 ON CONFLICT (source_id) DO UPDATE SET {$updates}, updated_at = now()",
                array_values($s),
            );
        }
    }

    /**
     * Deliberately a no-op.
     *
     * Deleting the registry would break any environment that has since pulled
     * against it, and these rows are the configuration every other environment
     * already has. There is nothing to roll back to that is not simply
     * "cannot ingest".
     */
    public function down(): void
    {
        //
    }

    private function tableExists(string $table): bool
    {
        return DB::selectOne(
            'SELECT to_regclass(?) IS NOT NULL AS present',
            ["public_geo.{$table}"],
        )?->present ?? false;
    }

    /**
     * @var list<array<string, mixed>>
     */
    private const JURISDICTIONS = [
        ['jurisdiction_code' => 'CA-AB', 'country_code' => 'CA', 'display_name' => 'Alberta', 'level' => 'province', 'status' => 'coming_soon', 'primary_authority' => 'Alberta Geological Survey', 'license_summary' => 'Open Government Licence — Alberta', 'license_url' => 'https://open.alberta.ca/licence', 'default_source_crs' => 3402, 'refresh_cadence' => 'monthly', 'teaser' => 'AGS mineral deposits — limited coverage, strong O&G', 'sort_order' => 50, 'bbox_wkt' => null],
        ['jurisdiction_code' => 'CA-BC', 'country_code' => 'CA', 'display_name' => 'British Columbia', 'level' => 'province', 'status' => 'active', 'primary_authority' => 'British Columbia Geological Survey, Ministry of Energy, Mines and Low Carbon Innovation', 'license_summary' => 'Open Government Licence – British Columbia (v2.0)', 'license_url' => 'https://www2.gov.bc.ca/gov/content/data/open-data/open-government-licence-bc', 'default_source_crs' => 3005, 'refresh_cadence' => 'weekly', 'teaser' => 'BC MINFILE — 15,000+ mineral occurrences', 'sort_order' => 20, 'bbox_wkt' => 'POLYGON((-139.06 48.3,-114.03 48.3,-114.03 60,-139.06 60,-139.06 48.3))'],
        ['jurisdiction_code' => 'CA-FED', 'country_code' => 'CA', 'display_name' => 'Canada (federal)', 'level' => 'federal', 'status' => 'coming_soon', 'primary_authority' => 'Natural Resources Canada / Geological Survey of Canada', 'license_summary' => null, 'license_url' => null, 'default_source_crs' => null, 'refresh_cadence' => null, 'teaser' => 'NRCan / GSC — national databases (CDED, NTDB, mineral deposits)', 'sort_order' => 140, 'bbox_wkt' => null],
        ['jurisdiction_code' => 'CA-FEDERAL', 'country_code' => 'CAN', 'display_name' => 'Canada (Federal)', 'level' => 'federal', 'status' => 'active', 'primary_authority' => 'Natural Resources Canada (NRCan) — Geological Survey of Canada', 'license_summary' => 'Open Government Licence — Canada (v2.0)', 'license_url' => 'https://open.canada.ca/en/open-government-licence-canada', 'default_source_crs' => 3978, 'refresh_cadence' => 'monthly', 'teaser' => 'NRCan bedrock geology, mines and exploration deposits', 'sort_order' => 5, 'bbox_wkt' => null],
        ['jurisdiction_code' => 'CA-MB', 'country_code' => 'CA', 'display_name' => 'Manitoba', 'level' => 'province', 'status' => 'coming_soon', 'primary_authority' => 'Manitoba Geological Survey', 'license_summary' => 'Open Government Licence — Canada (v2.0)', 'license_url' => 'https://open.canada.ca/en/open-government-licence-canada', 'default_source_crs' => 3155, 'refresh_cadence' => 'quarterly', 'teaser' => 'Manitoba Mineral Deposit Database', 'sort_order' => 60, 'bbox_wkt' => null],
        ['jurisdiction_code' => 'CA-NB', 'country_code' => 'CA', 'display_name' => 'New Brunswick', 'level' => 'province', 'status' => 'coming_soon', 'primary_authority' => 'New Brunswick Geological Surveys Branch', 'license_summary' => null, 'license_url' => null, 'default_source_crs' => null, 'refresh_cadence' => null, 'teaser' => 'NB Mineral Occurrence Database', 'sort_order' => 70, 'bbox_wkt' => null],
        ['jurisdiction_code' => 'CA-NL', 'country_code' => 'CA', 'display_name' => 'Newfoundland & Labrador', 'level' => 'province', 'status' => 'coming_soon', 'primary_authority' => 'Geological Survey of Newfoundland and Labrador', 'license_summary' => null, 'license_url' => null, 'default_source_crs' => null, 'refresh_cadence' => null, 'teaser' => 'MODS — Mineral Occurrence Data System', 'sort_order' => 90, 'bbox_wkt' => null],
        ['jurisdiction_code' => 'CA-NS', 'country_code' => 'CA', 'display_name' => 'Nova Scotia', 'level' => 'province', 'status' => 'coming_soon', 'primary_authority' => 'Nova Scotia Department of Natural Resources and Renewables', 'license_summary' => null, 'license_url' => null, 'default_source_crs' => null, 'refresh_cadence' => null, 'teaser' => 'DP ME mineral occurrence database', 'sort_order' => 80, 'bbox_wkt' => null],
        ['jurisdiction_code' => 'CA-NT', 'country_code' => 'CA', 'display_name' => 'Northwest Territories', 'level' => 'territory', 'status' => 'coming_soon', 'primary_authority' => 'NWT Geological Survey', 'license_summary' => null, 'license_url' => null, 'default_source_crs' => null, 'refresh_cadence' => null, 'teaser' => 'NWT Geoscience Office — mineral inventory', 'sort_order' => 120, 'bbox_wkt' => null],
        ['jurisdiction_code' => 'CA-NU', 'country_code' => 'CA', 'display_name' => 'Nunavut', 'level' => 'territory', 'status' => 'coming_soon', 'primary_authority' => 'Canada-Nunavut Geoscience Office', 'license_summary' => null, 'license_url' => null, 'default_source_crs' => null, 'refresh_cadence' => null, 'teaser' => 'Canada-Nunavut Geoscience Office — mineral occurrences', 'sort_order' => 130, 'bbox_wkt' => null],
        ['jurisdiction_code' => 'CA-ON', 'country_code' => 'CA', 'display_name' => 'Ontario', 'level' => 'province', 'status' => 'coming_soon', 'primary_authority' => 'Ontario Geological Survey', 'license_summary' => null, 'license_url' => null, 'default_source_crs' => null, 'refresh_cadence' => null, 'teaser' => 'OGSEarth — Mineral Deposit Inventory (MDI)', 'sort_order' => 30, 'bbox_wkt' => null],
        ['jurisdiction_code' => 'CA-PE', 'country_code' => 'CA', 'display_name' => 'Prince Edward Island', 'level' => 'province', 'status' => 'coming_soon', 'primary_authority' => 'PEI Department of Environment, Energy and Climate Action', 'license_summary' => null, 'license_url' => null, 'default_source_crs' => null, 'refresh_cadence' => null, 'teaser' => 'Minimal mineral coverage — low priority', 'sort_order' => 100, 'bbox_wkt' => null],
        ['jurisdiction_code' => 'CA-QC', 'country_code' => 'CA', 'display_name' => 'Québec', 'level' => 'province', 'status' => 'coming_soon', 'primary_authority' => 'Ministère des Ressources naturelles et des Forêts', 'license_summary' => null, 'license_url' => null, 'default_source_crs' => null, 'refresh_cadence' => null, 'teaser' => 'SIGÉOM — bilingual mineral deposits database', 'sort_order' => 40, 'bbox_wkt' => null],
        ['jurisdiction_code' => 'CA-SK', 'country_code' => 'CA', 'display_name' => 'Saskatchewan', 'level' => 'province', 'status' => 'active', 'primary_authority' => 'Saskatchewan Geological Survey, Ministry of Energy and Resources', 'license_summary' => 'Government of Saskatchewan Standard Unrestricted Use Data License v2.0', 'license_url' => 'https://pubsaskdev.blob.core.windows.net/pubsask-prod/107346/107346-Standard_Unrestricted_Use_Data_Licence.pdf', 'default_source_crs' => 2957, 'refresh_cadence' => 'weekly', 'teaser' => 'Saskatchewan Geological Survey — mines, SMDI, drillholes, resource potential', 'sort_order' => 10, 'bbox_wkt' => 'POLYGON((-110 49,-101.36 49,-101.36 60,-110 60,-110 49))'],
        ['jurisdiction_code' => 'CA-YT', 'country_code' => 'CA', 'display_name' => 'Yukon', 'level' => 'territory', 'status' => 'coming_soon', 'primary_authority' => 'Yukon Geological Survey', 'license_summary' => null, 'license_url' => null, 'default_source_crs' => null, 'refresh_cadence' => null, 'teaser' => 'Yukon Minfile', 'sort_order' => 110, 'bbox_wkt' => null],
    ];

    /**
     * @var list<array<string, mixed>>
     */
    private const SOURCES = [
        ['source_id' => 'CA-BC-MINFILE', 'jurisdiction_code' => 'CA-BC', 'name' => 'BC MINFILE — Mineral Occurrences', 'canonical_type' => 'mineral_occurrence', 'service_url' => 'https://delivery.maps.gov.bc.ca/arcgis/rest/services/mpcm/bcgwpub/MapServer/137', 'layer_index' => 137, 'source_crs' => 3005, 'license_summary' => 'Open Government Licence – British Columbia (v2.0)', 'license_url' => 'https://www2.gov.bc.ca/gov/content/data/open-data/open-government-licence-bc', 'refresh_cadence' => 'weekly', 'notes' => 'BC MINFILE mineral occurrence records from BCGW public MapServer layer 137. Field names: MINFILE_NUMBER, MINFILE_NAME1/2, STATUS_DESCRIPTION, COMMODITY_CODE1..8, DEPOSIT_CLASS_DESCRIPTION1, PRODUCTION_IND, MINFILE_SUMMARY_URL. See FieldMapping registry.'],
        ['source_id' => 'CA-SK-ASSESSMENT-AIRBORNE', 'jurisdiction_code' => 'CA-SK', 'name' => 'Saskatchewan Mineral Assessment — Airborne Surveys', 'canonical_type' => 'assessment_survey', 'service_url' => 'https://gis.saskatchewan.ca/arcgis/rest/services/Economy/P_Mineral_Assessment_File_Information/MapServer/3', 'layer_index' => 3, 'source_crs' => 2957, 'license_summary' => 'Government of Saskatchewan Standard Unrestricted Use Data License v2.0', 'license_url' => 'https://pubsaskdev.blob.core.windows.net/pubsask-prod/107346/107346-Standard_Unrestricted_Use_Data_Licence.pdf', 'refresh_cadence' => 'weekly', 'notes' => 'Airborne survey footprint polygons from the SMAD index.'],
        ['source_id' => 'CA-SK-ASSESSMENT-GROUND', 'jurisdiction_code' => 'CA-SK', 'name' => 'Saskatchewan Mineral Assessment — Ground Surveys', 'canonical_type' => 'assessment_survey', 'service_url' => 'https://gis.saskatchewan.ca/arcgis/rest/services/Economy/P_Mineral_Assessment_File_Information/MapServer/2', 'layer_index' => 2, 'source_crs' => 2957, 'license_summary' => 'Government of Saskatchewan Standard Unrestricted Use Data License v2.0', 'license_url' => 'https://pubsaskdev.blob.core.windows.net/pubsask-prod/107346/107346-Standard_Unrestricted_Use_Data_Licence.pdf', 'refresh_cadence' => 'weekly', 'notes' => 'Ground survey footprint polygons from the SMAD index.'],
        ['source_id' => 'CA-SK-ASSESSMENT-UNDERGROUND', 'jurisdiction_code' => 'CA-SK', 'name' => 'Saskatchewan Mineral Assessment — Underground Surveys', 'canonical_type' => 'assessment_survey', 'service_url' => 'https://gis.saskatchewan.ca/arcgis/rest/services/Economy/P_Mineral_Assessment_File_Information/MapServer/1', 'layer_index' => 1, 'source_crs' => 2957, 'license_summary' => 'Government of Saskatchewan Standard Unrestricted Use Data License v2.0', 'license_url' => 'https://pubsaskdev.blob.core.windows.net/pubsask-prod/107346/107346-Standard_Unrestricted_Use_Data_Licence.pdf', 'refresh_cadence' => 'weekly', 'notes' => 'Underground survey footprint polygons from the SMAD index.'],
        ['source_id' => 'CA-SK-DRILLHOLE', 'jurisdiction_code' => 'CA-SK', 'name' => 'Saskatchewan Minerals & Quaternary Drillhole Compilation', 'canonical_type' => 'drillhole_collar', 'service_url' => 'https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Mineral_Exploration/MapServer/3', 'layer_index' => 3, 'source_crs' => 2957, 'license_summary' => 'Government of Saskatchewan Standard Unrestricted Use Data License v2.0', 'license_url' => 'https://pubsaskdev.blob.core.windows.net/pubsask-prod/107346/107346-Standard_Unrestricted_Use_Data_Licence.pdf', 'refresh_cadence' => 'weekly', 'notes' => 'Collar-level only. SOURCE field links back to originating SMAD filings (linker §07).'],
        ['source_id' => 'CA-SK-GEOLOGY-BEDROCK-250K', 'jurisdiction_code' => 'CA-SK', 'name' => 'SK Bedrock Geology 250K', 'canonical_type' => 'bedrock_geology', 'service_url' => 'https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Geology/MapServer/10', 'layer_index' => 10, 'source_crs' => 2957, 'license_summary' => 'Government of Saskatchewan Standard Unrestricted Use Data License v2.0', 'license_url' => 'https://pubsaskdev.blob.core.windows.net/pubsask-prod/107346/107346-Standard_Unrestricted_Use_Data_Licence.pdf', 'refresh_cadence' => 'weekly', 'notes' => 'SK Bedrock Geology 250K. Fields: ROCK_CODE, EON, ERA, PERIOD, GROUP_, FORMATION, MEMBER, DOMAIN, LITHOLOGY, NAME. ~several thousand polygons covering bedrock units at 1:250K scale.'],
        ['source_id' => 'CA-SK-MINE-LOC', 'jurisdiction_code' => 'CA-SK', 'name' => 'Saskatchewan Mine Locations', 'canonical_type' => 'mine', 'service_url' => 'https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Mineral_Exploration/MapServer/1', 'layer_index' => 1, 'source_crs' => 2957, 'license_summary' => 'Government of Saskatchewan Standard Unrestricted Use Data License v2.0', 'license_url' => 'https://pubsaskdev.blob.core.windows.net/pubsask-prod/107346/107346-Standard_Unrestricted_Use_Data_Licence.pdf', 'refresh_cadence' => 'weekly', 'notes' => 'Point geometry. Canonical: pg_mine. SK publishes via MapServer.'],
        ['source_id' => 'CA-SK-MINERAL-DISPOSITION-CROWN', 'jurisdiction_code' => 'CA-SK', 'name' => 'Saskatchewan Crown Dispositions — Oil and Gas (parent)', 'canonical_type' => 'mineral_disposition', 'service_url' => 'https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Mineral_Tenure_Crown_Dispositions/MapServer', 'layer_index' => null, 'source_crs' => 2957, 'license_summary' => 'Government of Saskatchewan Standard Unrestricted Use Data License v2.0', 'license_url' => 'https://pubsaskdev.blob.core.windows.net/pubsask-prod/107346/107346-Standard_Unrestricted_Use_Data_Licence.pdf', 'refresh_cadence' => 'weekly', 'notes' => 'Crown Dispositions service — only layer 8 (Oil and Gas Dispositions) has unique data; layers 0-7 duplicate Mining. Bronze registers only CA-SK-MINERAL-DISPOSITION-CROWN-OIL-GAS from layer 8. Fields: DISPID, DISPTYPE, DISPSTATUS, ISSUEDATE, LESSEES, GEOAREA, BONUSBID, DSTRATRGHT, PARCELHECT.'],
        ['source_id' => 'CA-SK-MINERAL-DISPOSITION-CROWN-OIL-GAS', 'jurisdiction_code' => 'CA-SK', 'name' => 'Saskatchewan Mineral Tenure - CROWN-OIL-GAS', 'canonical_type' => 'mineral_disposition', 'service_url' => 'https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Mineral_Tenure_Crown_Dispositions/MapServer/8', 'layer_index' => 8, 'source_crs' => 2957, 'license_summary' => 'Government of Saskatchewan Standard Unrestricted Use Data License v2.0', 'license_url' => 'https://pubsaskdev.blob.core.windows.net/pubsask-prod/107346/107346-Standard_Unrestricted_Use_Data_Licence.pdf', 'refresh_cadence' => 'weekly', 'notes' => 'Auto-registered from https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Mineral_Tenure_Crown_Dispositions/MapServer layer 8. Parent registry row: CA-SK-MINERAL-DISPOSITION-CROWN. Tenure layer tuple hint: CROWN-OIL-GAS.'],
        ['source_id' => 'CA-SK-MINERAL-DISPOSITION-MINING', 'jurisdiction_code' => 'CA-SK', 'name' => 'Saskatchewan Mineral Tenure — Mining Service (parent)', 'canonical_type' => 'mineral_disposition', 'service_url' => 'https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Mining/MapServer', 'layer_index' => null, 'source_crs' => 2957, 'license_summary' => 'Government of Saskatchewan Standard Unrestricted Use Data License v2.0', 'license_url' => 'https://pubsaskdev.blob.core.windows.net/pubsask-prod/107346/107346-Standard_Unrestricted_Use_Data_Licence.pdf', 'refresh_cadence' => 'weekly', 'notes' => 'Multi-layer Mining MapServer. Bronze auto-enumerates layers 0-8 into per-layer source_ids CA-SK-MINERAL-DISPOSITION-MINING-{0..8}. Layers 9-15 (CR Preclude) are out-of-scope. Mining service maxRecordCount=1000. Two field schemas: layers 0-4 legacy (DISPOSITIO, DISPOSIT_1, OWNERS, EFFECTIVED, GOODSTANDI); layers 5-8 modern (DISPOSITION, STATUS, HOLDER, ANNIVERSARYDATE, HECTARES). Silver extractor probes both.'],
        ['source_id' => 'CA-SK-MINERAL-DISPOSITION-MINING-0', 'jurisdiction_code' => 'CA-SK', 'name' => 'Saskatchewan Mineral Tenure - MINING-0', 'canonical_type' => 'mineral_disposition', 'service_url' => 'https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Mining/MapServer/0', 'layer_index' => 0, 'source_crs' => 2957, 'license_summary' => 'Government of Saskatchewan Standard Unrestricted Use Data License v2.0', 'license_url' => 'https://pubsaskdev.blob.core.windows.net/pubsask-prod/107346/107346-Standard_Unrestricted_Use_Data_Licence.pdf', 'refresh_cadence' => 'weekly', 'notes' => 'Auto-registered from https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Mining/MapServer layer 0. Parent registry row: CA-SK-MINERAL-DISPOSITION-MINING. Tenure layer tuple hint: MINING-0.'],
        ['source_id' => 'CA-SK-MINERAL-DISPOSITION-MINING-1', 'jurisdiction_code' => 'CA-SK', 'name' => 'Saskatchewan Mineral Tenure - MINING-1', 'canonical_type' => 'mineral_disposition', 'service_url' => 'https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Mining/MapServer/1', 'layer_index' => 1, 'source_crs' => 2957, 'license_summary' => 'Government of Saskatchewan Standard Unrestricted Use Data License v2.0', 'license_url' => 'https://pubsaskdev.blob.core.windows.net/pubsask-prod/107346/107346-Standard_Unrestricted_Use_Data_Licence.pdf', 'refresh_cadence' => 'weekly', 'notes' => 'Auto-registered from https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Mining/MapServer layer 1. Parent registry row: CA-SK-MINERAL-DISPOSITION-MINING. Tenure layer tuple hint: MINING-1.'],
        ['source_id' => 'CA-SK-MINERAL-DISPOSITION-MINING-2', 'jurisdiction_code' => 'CA-SK', 'name' => 'Saskatchewan Mineral Tenure - MINING-2', 'canonical_type' => 'mineral_disposition', 'service_url' => 'https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Mining/MapServer/2', 'layer_index' => 2, 'source_crs' => 2957, 'license_summary' => 'Government of Saskatchewan Standard Unrestricted Use Data License v2.0', 'license_url' => 'https://pubsaskdev.blob.core.windows.net/pubsask-prod/107346/107346-Standard_Unrestricted_Use_Data_Licence.pdf', 'refresh_cadence' => 'weekly', 'notes' => 'Auto-registered from https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Mining/MapServer layer 2. Parent registry row: CA-SK-MINERAL-DISPOSITION-MINING. Tenure layer tuple hint: MINING-2.'],
        ['source_id' => 'CA-SK-MINERAL-DISPOSITION-MINING-3', 'jurisdiction_code' => 'CA-SK', 'name' => 'Saskatchewan Mineral Tenure - MINING-3', 'canonical_type' => 'mineral_disposition', 'service_url' => 'https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Mining/MapServer/3', 'layer_index' => 3, 'source_crs' => 2957, 'license_summary' => 'Government of Saskatchewan Standard Unrestricted Use Data License v2.0', 'license_url' => 'https://pubsaskdev.blob.core.windows.net/pubsask-prod/107346/107346-Standard_Unrestricted_Use_Data_Licence.pdf', 'refresh_cadence' => 'weekly', 'notes' => 'Auto-registered from https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Mining/MapServer layer 3. Parent registry row: CA-SK-MINERAL-DISPOSITION-MINING. Tenure layer tuple hint: MINING-3.'],
        ['source_id' => 'CA-SK-MINERAL-DISPOSITION-MINING-4', 'jurisdiction_code' => 'CA-SK', 'name' => 'Saskatchewan Mineral Tenure - MINING-4', 'canonical_type' => 'mineral_disposition', 'service_url' => 'https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Mining/MapServer/4', 'layer_index' => 4, 'source_crs' => 2957, 'license_summary' => 'Government of Saskatchewan Standard Unrestricted Use Data License v2.0', 'license_url' => 'https://pubsaskdev.blob.core.windows.net/pubsask-prod/107346/107346-Standard_Unrestricted_Use_Data_Licence.pdf', 'refresh_cadence' => 'weekly', 'notes' => 'Auto-registered from https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Mining/MapServer layer 4. Parent registry row: CA-SK-MINERAL-DISPOSITION-MINING. Tenure layer tuple hint: MINING-4.'],
        ['source_id' => 'CA-SK-MINERAL-DISPOSITION-MINING-5', 'jurisdiction_code' => 'CA-SK', 'name' => 'Saskatchewan Mineral Tenure - MINING-5', 'canonical_type' => 'mineral_disposition', 'service_url' => 'https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Mining/MapServer/5', 'layer_index' => 5, 'source_crs' => 2957, 'license_summary' => 'Government of Saskatchewan Standard Unrestricted Use Data License v2.0', 'license_url' => 'https://pubsaskdev.blob.core.windows.net/pubsask-prod/107346/107346-Standard_Unrestricted_Use_Data_Licence.pdf', 'refresh_cadence' => 'weekly', 'notes' => 'Auto-registered from https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Mining/MapServer layer 5. Parent registry row: CA-SK-MINERAL-DISPOSITION-MINING. Tenure layer tuple hint: MINING-5.'],
        ['source_id' => 'CA-SK-MINERAL-DISPOSITION-MINING-6', 'jurisdiction_code' => 'CA-SK', 'name' => 'Saskatchewan Mineral Tenure - MINING-6', 'canonical_type' => 'mineral_disposition', 'service_url' => 'https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Mining/MapServer/6', 'layer_index' => 6, 'source_crs' => 2957, 'license_summary' => 'Government of Saskatchewan Standard Unrestricted Use Data License v2.0', 'license_url' => 'https://pubsaskdev.blob.core.windows.net/pubsask-prod/107346/107346-Standard_Unrestricted_Use_Data_Licence.pdf', 'refresh_cadence' => 'weekly', 'notes' => 'Auto-registered from https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Mining/MapServer layer 6. Parent registry row: CA-SK-MINERAL-DISPOSITION-MINING. Tenure layer tuple hint: MINING-6.'],
        ['source_id' => 'CA-SK-MINERAL-DISPOSITION-MINING-7', 'jurisdiction_code' => 'CA-SK', 'name' => 'Saskatchewan Mineral Tenure - MINING-7', 'canonical_type' => 'mineral_disposition', 'service_url' => 'https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Mining/MapServer/7', 'layer_index' => 7, 'source_crs' => 2957, 'license_summary' => 'Government of Saskatchewan Standard Unrestricted Use Data License v2.0', 'license_url' => 'https://pubsaskdev.blob.core.windows.net/pubsask-prod/107346/107346-Standard_Unrestricted_Use_Data_Licence.pdf', 'refresh_cadence' => 'weekly', 'notes' => 'Auto-registered from https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Mining/MapServer layer 7. Parent registry row: CA-SK-MINERAL-DISPOSITION-MINING. Tenure layer tuple hint: MINING-7.'],
        ['source_id' => 'CA-SK-MINERAL-DISPOSITION-MINING-8', 'jurisdiction_code' => 'CA-SK', 'name' => 'Saskatchewan Mineral Tenure - MINING-8', 'canonical_type' => 'mineral_disposition', 'service_url' => 'https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Mining/MapServer/8', 'layer_index' => 8, 'source_crs' => 2957, 'license_summary' => 'Government of Saskatchewan Standard Unrestricted Use Data License v2.0', 'license_url' => 'https://pubsaskdev.blob.core.windows.net/pubsask-prod/107346/107346-Standard_Unrestricted_Use_Data_Licence.pdf', 'refresh_cadence' => 'weekly', 'notes' => 'Auto-registered from https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Mining/MapServer layer 8. Parent registry row: CA-SK-MINERAL-DISPOSITION-MINING. Tenure layer tuple hint: MINING-8.'],
        ['source_id' => 'CA-SK-RESOURCE-POTENTIAL', 'jurisdiction_code' => 'CA-SK', 'name' => 'Saskatchewan Resource Potential (all commodities)', 'canonical_type' => 'resource_potential_zone', 'service_url' => 'https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Resource_Map/MapServer', 'layer_index' => null, 'source_crs' => 2957, 'license_summary' => 'Government of Saskatchewan Standard Unrestricted Use Data License v2.0', 'license_url' => 'https://pubsaskdev.blob.core.windows.net/pubsask-prod/107346/107346-Standard_Unrestricted_Use_Data_Licence.pdf', 'refresh_cadence' => 'weekly', 'notes' => 'Multi-layer MapServer — per-commodity polygons. Bronze asset auto-enumerates layers and filters out non-mineral layers (Oil and Gas Pools is layer 5 and is out-of-scope per plan §01).'],
        ['source_id' => 'CA-SK-RESOURCE-POTENTIAL-BASE', 'jurisdiction_code' => 'CA-SK', 'name' => 'Saskatchewan Resource Potential — Base Metals Potential', 'canonical_type' => 'resource_potential_zone', 'service_url' => 'https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Resource_Map/MapServer/13', 'layer_index' => 13, 'source_crs' => 2957, 'license_summary' => 'Government of Saskatchewan Standard Unrestricted Use Data License v2.0', 'license_url' => 'https://pubsaskdev.blob.core.windows.net/pubsask-prod/107346/107346-Standard_Unrestricted_Use_Data_Licence.pdf', 'refresh_cadence' => 'weekly', 'notes' => 'Auto-registered from Resource_Map FeatureServer layer 13 (\'Base Metals Potential\'). Parent registry row: CA-SK-RESOURCE-POTENTIAL.'],
        ['source_id' => 'CA-SK-RESOURCE-POTENTIAL-BITUMEN', 'jurisdiction_code' => 'CA-SK', 'name' => 'Saskatchewan Resource Potential — Bitumen (Oil Sands) Potential', 'canonical_type' => 'resource_potential_zone', 'service_url' => 'https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Resource_Map/MapServer/16', 'layer_index' => 16, 'source_crs' => 2957, 'license_summary' => 'Government of Saskatchewan Standard Unrestricted Use Data License v2.0', 'license_url' => 'https://pubsaskdev.blob.core.windows.net/pubsask-prod/107346/107346-Standard_Unrestricted_Use_Data_Licence.pdf', 'refresh_cadence' => 'weekly', 'notes' => 'Auto-registered from Resource_Map FeatureServer layer 16 (\'Bitumen (Oil Sands) Potential\'). Parent registry row: CA-SK-RESOURCE-POTENTIAL.'],
        ['source_id' => 'CA-SK-RESOURCE-POTENTIAL-COAL', 'jurisdiction_code' => 'CA-SK', 'name' => 'Saskatchewan Resource Potential — Coal Field', 'canonical_type' => 'resource_potential_zone', 'service_url' => 'https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Resource_Map/MapServer/3', 'layer_index' => 3, 'source_crs' => 2957, 'license_summary' => 'Government of Saskatchewan Standard Unrestricted Use Data License v2.0', 'license_url' => 'https://pubsaskdev.blob.core.windows.net/pubsask-prod/107346/107346-Standard_Unrestricted_Use_Data_Licence.pdf', 'refresh_cadence' => 'weekly', 'notes' => 'Auto-registered from Resource_Map FeatureServer layer 3 (\'Coal Field\'). Parent registry row: CA-SK-RESOURCE-POTENTIAL.'],
        ['source_id' => 'CA-SK-RESOURCE-POTENTIAL-GOLD', 'jurisdiction_code' => 'CA-SK', 'name' => 'Saskatchewan Resource Potential — Gold Potential', 'canonical_type' => 'resource_potential_zone', 'service_url' => 'https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Resource_Map/MapServer/14', 'layer_index' => 14, 'source_crs' => 2957, 'license_summary' => 'Government of Saskatchewan Standard Unrestricted Use Data License v2.0', 'license_url' => 'https://pubsaskdev.blob.core.windows.net/pubsask-prod/107346/107346-Standard_Unrestricted_Use_Data_Licence.pdf', 'refresh_cadence' => 'weekly', 'notes' => 'Auto-registered from Resource_Map FeatureServer layer 14 (\'Gold Potential\'). Parent registry row: CA-SK-RESOURCE-POTENTIAL.'],
        ['source_id' => 'CA-SK-RESOURCE-POTENTIAL-HELIUM', 'jurisdiction_code' => 'CA-SK', 'name' => 'Saskatchewan Resource Potential — Helium Potential', 'canonical_type' => 'resource_potential_zone', 'service_url' => 'https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Resource_Map/MapServer/4', 'layer_index' => 4, 'source_crs' => 2957, 'license_summary' => 'Government of Saskatchewan Standard Unrestricted Use Data License v2.0', 'license_url' => 'https://pubsaskdev.blob.core.windows.net/pubsask-prod/107346/107346-Standard_Unrestricted_Use_Data_Licence.pdf', 'refresh_cadence' => 'weekly', 'notes' => 'Auto-registered from Resource_Map FeatureServer layer 4 (\'Helium Potential\'). Parent registry row: CA-SK-RESOURCE-POTENTIAL.'],
        ['source_id' => 'CA-SK-RESOURCE-POTENTIAL-LITHIUM', 'jurisdiction_code' => 'CA-SK', 'name' => 'Saskatchewan Resource Potential — Lithium Potential', 'canonical_type' => 'resource_potential_zone', 'service_url' => 'https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Resource_Map/MapServer/15', 'layer_index' => 15, 'source_crs' => 2957, 'license_summary' => 'Government of Saskatchewan Standard Unrestricted Use Data License v2.0', 'license_url' => 'https://pubsaskdev.blob.core.windows.net/pubsask-prod/107346/107346-Standard_Unrestricted_Use_Data_Licence.pdf', 'refresh_cadence' => 'weekly', 'notes' => 'Auto-registered from Resource_Map FeatureServer layer 15 (\'Lithium Potential\'). Parent registry row: CA-SK-RESOURCE-POTENTIAL.'],
        ['source_id' => 'CA-SK-RESOURCE-POTENTIAL-OIL', 'jurisdiction_code' => 'CA-SK', 'name' => 'Saskatchewan Resource Potential — Oil and Gas Pools ', 'canonical_type' => 'resource_potential_zone', 'service_url' => 'https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Resource_Map/MapServer/5', 'layer_index' => 5, 'source_crs' => 2957, 'license_summary' => 'Government of Saskatchewan Standard Unrestricted Use Data License v2.0', 'license_url' => 'https://pubsaskdev.blob.core.windows.net/pubsask-prod/107346/107346-Standard_Unrestricted_Use_Data_Licence.pdf', 'refresh_cadence' => 'weekly', 'notes' => 'Auto-registered from Resource_Map FeatureServer layer 5 (\'Oil and Gas Pools \'). Parent registry row: CA-SK-RESOURCE-POTENTIAL.'],
        ['source_id' => 'CA-SK-RESOURCE-POTENTIAL-POTASH', 'jurisdiction_code' => 'CA-SK', 'name' => 'Saskatchewan Resource Potential — Potash and Salt Resource Area', 'canonical_type' => 'resource_potential_zone', 'service_url' => 'https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Resource_Map/MapServer/9', 'layer_index' => 9, 'source_crs' => 2957, 'license_summary' => 'Government of Saskatchewan Standard Unrestricted Use Data License v2.0', 'license_url' => 'https://pubsaskdev.blob.core.windows.net/pubsask-prod/107346/107346-Standard_Unrestricted_Use_Data_Licence.pdf', 'refresh_cadence' => 'weekly', 'notes' => 'Auto-registered from Resource_Map FeatureServer layer 9 (\'Potash and Salt Resource Area\'). Parent registry row: CA-SK-RESOURCE-POTENTIAL.'],
        ['source_id' => 'CA-SK-RESOURCE-POTENTIAL-RARE', 'jurisdiction_code' => 'CA-SK', 'name' => 'Saskatchewan Resource Potential — Rare Earths Potential', 'canonical_type' => 'resource_potential_zone', 'service_url' => 'https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Resource_Map/MapServer/17', 'layer_index' => 17, 'source_crs' => 2957, 'license_summary' => 'Government of Saskatchewan Standard Unrestricted Use Data License v2.0', 'license_url' => 'https://pubsaskdev.blob.core.windows.net/pubsask-prod/107346/107346-Standard_Unrestricted_Use_Data_Licence.pdf', 'refresh_cadence' => 'weekly', 'notes' => 'Auto-registered from Resource_Map FeatureServer layer 17 (\'Rare Earths Potential\'). Parent registry row: CA-SK-RESOURCE-POTENTIAL.'],
        ['source_id' => 'CA-SK-RESOURCE-POTENTIAL-URANIUM', 'jurisdiction_code' => 'CA-SK', 'name' => 'Saskatchewan Resource Potential — Uranium Potential', 'canonical_type' => 'resource_potential_zone', 'service_url' => 'https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Resource_Map/MapServer/11', 'layer_index' => 11, 'source_crs' => 2957, 'license_summary' => 'Government of Saskatchewan Standard Unrestricted Use Data License v2.0', 'license_url' => 'https://pubsaskdev.blob.core.windows.net/pubsask-prod/107346/107346-Standard_Unrestricted_Use_Data_Licence.pdf', 'refresh_cadence' => 'weekly', 'notes' => 'Auto-registered from Resource_Map FeatureServer layer 11 (\'Uranium Potential\'). Parent registry row: CA-SK-RESOURCE-POTENTIAL.'],
        ['source_id' => 'CA-SK-ROCK-SAMPLES', 'jurisdiction_code' => 'CA-SK', 'name' => 'Saskatchewan Government Rock Samples', 'canonical_type' => 'rock_sample', 'service_url' => 'https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Mineral_Exploration/MapServer/4', 'layer_index' => 4, 'source_crs' => 2957, 'license_summary' => 'Government of Saskatchewan Standard Unrestricted Use Data License v2.0', 'license_url' => 'https://pubsaskdev.blob.core.windows.net/pubsask-prod/107346/107346-Standard_Unrestricted_Use_Data_Licence.pdf', 'refresh_cadence' => 'weekly', 'notes' => 'Point locations of government-collected rock samples. Fields: STATION, SAMPLE_NUM, GEOLOGIST, GEOG_AREA, REPORT_NUM, MAP_NUM, NTS_250K, NTS_50K.'],
        ['source_id' => 'CA-SK-SMDI', 'jurisdiction_code' => 'CA-SK', 'name' => 'Saskatchewan Mineral Deposits Index (SMDI)', 'canonical_type' => 'mineral_occurrence', 'service_url' => 'https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Mineral_Exploration/MapServer/5', 'layer_index' => 5, 'source_crs' => 2957, 'license_summary' => 'Government of Saskatchewan Standard Unrestricted Use Data License v2.0', 'license_url' => 'https://pubsaskdev.blob.core.windows.net/pubsask-prod/107346/107346-Standard_Unrestricted_Use_Data_Licence.pdf', 'refresh_cadence' => 'weekly', 'notes' => 'Point geometry. Canonical: pg_mineral_occurrence. Public identifier: SMDI. Layer 5 on the Mineral_Exploration MapServer.'],
    ];
};
