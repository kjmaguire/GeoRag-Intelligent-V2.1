<?php

declare(strict_types=1);

namespace Database\Seeders\PublicGeoscience;

use Illuminate\Database\Seeder;
use Illuminate\Support\Facades\DB;

/**
 * Seed the Canadian jurisdictions + Saskatchewan feature sources for Phase 1
 * of the Public Geoscience feature.
 *
 * - 13 provinces/territories + 1 federal entry = 14 jurisdiction rows.
 * - Saskatchewan = 'active' with 4 feature-source rows.
 * - All others = 'coming_soon' (visible as muted tiles, not queryable).
 *
 * Idempotent: re-running on an already-seeded database updates values without
 * creating duplicates.
 *
 * Usage:
 *   php artisan db:seed --class='Database\Seeders\PublicGeoscience\CanadaJurisdictionsSeeder'
 *
 * References: plan §02a (jurisdiction matrix), §03a (SK source inventory),
 *             §09c (coming-soon teaser copy).
 */
class CanadaJurisdictionsSeeder extends Seeder
{
    public function run(): void
    {
        $now = now();

        // Rough Saskatchewan extent in EPSG:4326 — refined by SME later.
        // SW corner (-110.0, 49.0), NE corner (-101.36, 60.0).
        $skBboxWkt = 'POLYGON((-110.0 49.0, -101.36 49.0, -101.36 60.0, -110.0 60.0, -110.0 49.0))';

        $jurisdictions = [
            [
                'jurisdiction_code' => 'CA-SK',
                'country_code' => 'CA',
                'display_name' => 'Saskatchewan',
                'level' => 'province',
                'status' => 'active',
                'primary_authority' => 'Saskatchewan Geological Survey, Ministry of Energy and Resources',
                'license_summary' => 'Government of Saskatchewan Standard Unrestricted Use Data License v2.0',
                'license_url' => 'https://pubsaskdev.blob.core.windows.net/pubsask-prod/107346/107346-Standard_Unrestricted_Use_Data_Licence.pdf',
                'default_source_crs' => 2957,
                'refresh_cadence' => 'weekly',
                'teaser' => 'Saskatchewan Geological Survey — mines, SMDI, drillholes, resource potential',
                'sort_order' => 10,
                'bbox_wkt' => $skBboxWkt,
            ],
            [
                'jurisdiction_code' => 'CA-BC',
                'country_code' => 'CA',
                'display_name' => 'British Columbia',
                'level' => 'province',
                'status' => 'active',
                'primary_authority' => 'British Columbia Geological Survey, Ministry of Energy, Mines and Low Carbon Innovation',
                'license_summary' => 'Open Government Licence – British Columbia (v2.0)',
                'license_url' => 'https://www2.gov.bc.ca/gov/content/data/open-data/open-government-licence-bc',
                'default_source_crs' => 3005, // NAD83 / BC Albers — native CRS for BC provincial datasets
                'refresh_cadence' => 'weekly',
                'teaser' => 'BC MINFILE — 15,000+ mineral occurrences',
                'sort_order' => 20,
                'bbox_wkt' => 'POLYGON((-139.06 48.30, -114.03 48.30, -114.03 60.0, -139.06 60.0, -139.06 48.30))',
            ],
            [
                'jurisdiction_code' => 'CA-ON',
                'country_code' => 'CA',
                'display_name' => 'Ontario',
                'level' => 'province',
                'status' => 'coming_soon',
                'primary_authority' => 'Ontario Geological Survey',
                'teaser' => 'OGSEarth — Mineral Deposit Inventory (MDI)',
                'sort_order' => 30,
            ],
            [
                'jurisdiction_code' => 'CA-QC',
                'country_code' => 'CA',
                'display_name' => 'Québec',
                'level' => 'province',
                'status' => 'coming_soon',
                'primary_authority' => 'Ministère des Ressources naturelles et des Forêts',
                'teaser' => 'SIGÉOM — bilingual mineral deposits database',
                'sort_order' => 40,
            ],
            [
                'jurisdiction_code' => 'CA-AB',
                'country_code' => 'CA',
                'display_name' => 'Alberta',
                'level' => 'province',
                'status' => 'coming_soon',
                'primary_authority' => 'Alberta Geological Survey',
                'teaser' => 'AGS mineral deposits — limited coverage, strong O&G',
                'sort_order' => 50,
            ],
            [
                'jurisdiction_code' => 'CA-MB',
                'country_code' => 'CA',
                'display_name' => 'Manitoba',
                'level' => 'province',
                'status' => 'coming_soon',
                'primary_authority' => 'Manitoba Geological Survey',
                'teaser' => 'Manitoba Mineral Deposit Database',
                'sort_order' => 60,
            ],
            [
                'jurisdiction_code' => 'CA-NB',
                'country_code' => 'CA',
                'display_name' => 'New Brunswick',
                'level' => 'province',
                'status' => 'coming_soon',
                'primary_authority' => 'New Brunswick Geological Surveys Branch',
                'teaser' => 'NB Mineral Occurrence Database',
                'sort_order' => 70,
            ],
            [
                'jurisdiction_code' => 'CA-NS',
                'country_code' => 'CA',
                'display_name' => 'Nova Scotia',
                'level' => 'province',
                'status' => 'coming_soon',
                'primary_authority' => 'Nova Scotia Department of Natural Resources and Renewables',
                'teaser' => 'DP ME mineral occurrence database',
                'sort_order' => 80,
            ],
            [
                'jurisdiction_code' => 'CA-NL',
                'country_code' => 'CA',
                'display_name' => 'Newfoundland & Labrador',
                'level' => 'province',
                'status' => 'coming_soon',
                'primary_authority' => 'Geological Survey of Newfoundland and Labrador',
                'teaser' => 'MODS — Mineral Occurrence Data System',
                'sort_order' => 90,
            ],
            [
                'jurisdiction_code' => 'CA-PE',
                'country_code' => 'CA',
                'display_name' => 'Prince Edward Island',
                'level' => 'province',
                'status' => 'coming_soon',
                'primary_authority' => 'PEI Department of Environment, Energy and Climate Action',
                'teaser' => 'Minimal mineral coverage — low priority',
                'sort_order' => 100,
            ],
            [
                'jurisdiction_code' => 'CA-YT',
                'country_code' => 'CA',
                'display_name' => 'Yukon',
                'level' => 'territory',
                'status' => 'coming_soon',
                'primary_authority' => 'Yukon Geological Survey',
                'teaser' => 'Yukon Minfile',
                'sort_order' => 110,
            ],
            [
                'jurisdiction_code' => 'CA-NT',
                'country_code' => 'CA',
                'display_name' => 'Northwest Territories',
                'level' => 'territory',
                'status' => 'coming_soon',
                'primary_authority' => 'NWT Geological Survey',
                'teaser' => 'NWT Geoscience Office — mineral inventory',
                'sort_order' => 120,
            ],
            [
                'jurisdiction_code' => 'CA-NU',
                'country_code' => 'CA',
                'display_name' => 'Nunavut',
                'level' => 'territory',
                'status' => 'coming_soon',
                'primary_authority' => 'Canada-Nunavut Geoscience Office',
                'teaser' => 'Canada-Nunavut Geoscience Office — mineral occurrences',
                'sort_order' => 130,
            ],
            [
                'jurisdiction_code' => 'CA-FED',
                'country_code' => 'CA',
                'display_name' => 'Canada (federal)',
                'level' => 'federal',
                'status' => 'coming_soon',
                'primary_authority' => 'Natural Resources Canada / Geological Survey of Canada',
                'teaser' => 'NRCan / GSC — national databases (CDED, NTDB, mineral deposits)',
                'sort_order' => 140,
            ],
        ];

        foreach ($jurisdictions as $row) {
            $bboxWkt = $row['bbox_wkt'] ?? null;
            unset($row['bbox_wkt']);

            $row['updated_at'] = $now;

            // updateOrInsert is idempotent on re-run; does not touch created_at
            // if the row already exists.
            DB::table('public_geo.jurisdictions')
                ->updateOrInsert(
                    ['jurisdiction_code' => $row['jurisdiction_code']],
                    array_merge($row, ['created_at' => $now]),
                );

            if ($bboxWkt !== null) {
                DB::statement(
                    'UPDATE public_geo.jurisdictions
                        SET bbox = ST_GeomFromText(?, 4326)
                      WHERE jurisdiction_code = ?',
                    [$bboxWkt, $row['jurisdiction_code']],
                );
            }
        }

        // ── Saskatchewan feature sources (plan §03a) ─────────────────────
        // Per-commodity Resource Potential layer expansion is a Phase 2
        // concern (§11 item 3); Phase 1 carries a single registry row.
        $skLicense = 'Government of Saskatchewan Standard Unrestricted Use Data License v2.0';
        $skLicenseUrl = 'https://pubsaskdev.blob.core.windows.net/pubsask-prod/107346/107346-Standard_Unrestricted_Use_Data_Licence.pdf';

        // Saskatchewan endpoints verified live April 2026:
        //   service type = MapServer (NOT FeatureServer as the plan doc stated)
        //   layer 0 = Mineral Exploration (group header, skip)
        //   layer 1 = Mine Locations (Point)
        //   layer 2 = Drillholes and Samples (group header, skip)
        //   layer 3 = Drillholes (Point)
        //   layer 4 = Government Rock Samples (Point — not mapped in V1)
        //   layer 5 = Mineral Deposits Index / SMDI (Point)
        //   Resource_Map MapServer publishes layer IDs 3,4,5,9,11,13,14,15,16,17;
        //   layer 5 (Oil and Gas Pools) is explicitly out of V1 scope per plan §01
        //   and is filtered in the Bronze resource-potential asset.
        //   maxRecordCount = 2000, latestWkid = 2957 (NAD83(CSRS) / UTM zone 13N)
        $sources = [
            [
                'source_id' => 'CA-SK-MINE-LOC',
                'jurisdiction_code' => 'CA-SK',
                'name' => 'Saskatchewan Mine Locations',
                'canonical_type' => 'mine',
                'service_url' => 'https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Mineral_Exploration/MapServer/1',
                'layer_index' => 1,
                'source_crs' => 2957,
                'license_summary' => $skLicense,
                'license_url' => $skLicenseUrl,
                'refresh_cadence' => 'weekly',
                'notes' => 'Point geometry. Canonical: pg_mine. SK publishes via MapServer.',
            ],
            [
                'source_id' => 'CA-SK-SMDI',
                'jurisdiction_code' => 'CA-SK',
                'name' => 'Saskatchewan Mineral Deposits Index (SMDI)',
                'canonical_type' => 'mineral_occurrence',
                'service_url' => 'https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Mineral_Exploration/MapServer/5',
                'layer_index' => 5,
                'source_crs' => 2957,
                'license_summary' => $skLicense,
                'license_url' => $skLicenseUrl,
                'refresh_cadence' => 'weekly',
                'notes' => 'Point geometry. Canonical: pg_mineral_occurrence. Public identifier: SMDI. Layer 5 on the Mineral_Exploration MapServer.',
            ],
            [
                'source_id' => 'CA-SK-DRILLHOLE',
                'jurisdiction_code' => 'CA-SK',
                'name' => 'Saskatchewan Minerals & Quaternary Drillhole Compilation',
                'canonical_type' => 'drillhole_collar',
                'service_url' => 'https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Mineral_Exploration/MapServer/3',
                'layer_index' => 3,
                'source_crs' => 2957,
                'license_summary' => $skLicense,
                'license_url' => $skLicenseUrl,
                'refresh_cadence' => 'weekly',
                'notes' => 'Collar-level only. SOURCE field links back to originating SMAD filings (linker §07).',
            ],
            [
                'source_id' => 'CA-SK-RESOURCE-POTENTIAL',
                'jurisdiction_code' => 'CA-SK',
                'name' => 'Saskatchewan Resource Potential (all commodities)',
                'canonical_type' => 'resource_potential_zone',
                'service_url' => 'https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Resource_Map/MapServer',
                'layer_index' => null,
                'source_crs' => 2957,
                'license_summary' => $skLicense,
                'license_url' => $skLicenseUrl,
                'refresh_cadence' => 'weekly',
                'notes' => 'Multi-layer MapServer — per-commodity polygons. Bronze asset auto-enumerates layers and filters out non-mineral layers (Oil and Gas Pools is layer 5 and is out-of-scope per plan §01).',
            ],

            // ── Saskatchewan — Government Rock Samples (Tier 1 expansion) ──
            [
                'source_id' => 'CA-SK-ROCK-SAMPLES',
                'jurisdiction_code' => 'CA-SK',
                'name' => 'Saskatchewan Government Rock Samples',
                'canonical_type' => 'rock_sample',
                'service_url' => 'https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Mineral_Exploration/MapServer/4',
                'layer_index' => 4,
                'source_crs' => 2957,
                'license_summary' => $skLicense,
                'license_url' => $skLicenseUrl,
                'refresh_cadence' => 'weekly',
                'notes' => 'Point locations of government-collected rock samples. Fields: STATION, SAMPLE_NUM, GEOLOGIST, GEOG_AREA, REPORT_NUM, MAP_NUM, NTS_250K, NTS_50K.',
            ],
            // ── Saskatchewan — SMAD Assessment Survey footprints ──────────
            // Three sub-layers: Underground (1), Ground (2), Airborne (3).
            // Pulled as separate source_ids like Resource Potential commodities.
            [
                'source_id' => 'CA-SK-ASSESSMENT-UNDERGROUND',
                'jurisdiction_code' => 'CA-SK',
                'name' => 'Saskatchewan Mineral Assessment — Underground Surveys',
                'canonical_type' => 'assessment_survey',
                'service_url' => 'https://gis.saskatchewan.ca/arcgis/rest/services/Economy/P_Mineral_Assessment_File_Information/MapServer/1',
                'layer_index' => 1,
                'source_crs' => 2957,
                'license_summary' => $skLicense,
                'license_url' => $skLicenseUrl,
                'refresh_cadence' => 'weekly',
                'notes' => 'Underground survey footprint polygons from the SMAD index.',
            ],
            [
                'source_id' => 'CA-SK-ASSESSMENT-GROUND',
                'jurisdiction_code' => 'CA-SK',
                'name' => 'Saskatchewan Mineral Assessment — Ground Surveys',
                'canonical_type' => 'assessment_survey',
                'service_url' => 'https://gis.saskatchewan.ca/arcgis/rest/services/Economy/P_Mineral_Assessment_File_Information/MapServer/2',
                'layer_index' => 2,
                'source_crs' => 2957,
                'license_summary' => $skLicense,
                'license_url' => $skLicenseUrl,
                'refresh_cadence' => 'weekly',
                'notes' => 'Ground survey footprint polygons from the SMAD index.',
            ],
            [
                'source_id' => 'CA-SK-ASSESSMENT-AIRBORNE',
                'jurisdiction_code' => 'CA-SK',
                'name' => 'Saskatchewan Mineral Assessment — Airborne Surveys',
                'canonical_type' => 'assessment_survey',
                'service_url' => 'https://gis.saskatchewan.ca/arcgis/rest/services/Economy/P_Mineral_Assessment_File_Information/MapServer/3',
                'layer_index' => 3,
                'source_crs' => 2957,
                'license_summary' => $skLicense,
                'license_url' => $skLicenseUrl,
                'refresh_cadence' => 'weekly',
                'notes' => 'Airborne survey footprint polygons from the SMAD index.',
            ],

            // ── British Columbia (Phase 4 — second jurisdiction) ─────────
            // BC MINFILE is the province-wide mineral occurrence database.
            // Served from the BC Geographic Warehouse public ArcGIS REST
            // MapServer at layer 137. Verified live April 2026:
            //   - geometryType: esriGeometryPoint
            //   - maxRecordCount: 1000 (Bronze paginates; our client honors
            //     exceededTransferLimit so pagination completes cleanly)
            //   - spatial reference: wkid=102190, latestWkid=3005 (BC Albers)
            //   - MINFILE_NUMBER is the authoritative identifier (analogous
            //     to SMDI) — stored in pg_mineral_occurrence.smdi_id via the
            //     FieldMapping external_id_field indirection
            //
            // BC does NOT publish province-wide drillhole compilation or
            // resource-potential polygons; those live in per-assessment-
            // report artifacts that flow through internal document ingestion.
            // ── Saskatchewan — Mineral Tenure / Dispositions (Phase 2 Tier 2) ──
            // Two root parent rows, one per ArcGIS service. Bronze auto-
            // registers one derived source_id per layer on first fetch,
            // named CA-SK-MINERAL-DISPOSITION-<SUFFIX> (MINING-0 through -8
            // and CROWN-OIL-GAS). Silver unifies them into the single
            // pg_mineral_disposition canonical table via a per-suffix hint
            // lookup (see _MINERAL_DISPOSITION_LAYER_HINTS in Silver).
            //
            // Not activated in the Martin yaml / TileProxy whitelist yet —
            // those gates open once Bronze + Silver have been materialized
            // at least once so the tile view has rows.
            [
                'source_id' => 'CA-SK-MINERAL-DISPOSITION-MINING',
                'jurisdiction_code' => 'CA-SK',
                'name' => 'Saskatchewan Mineral Tenure — Mining Service (parent)',
                'canonical_type' => 'mineral_disposition',
                'service_url' => 'https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Mining/MapServer',
                'layer_index' => null, // multi-layer; Bronze enumerates 0–8
                'source_crs' => 2957,
                'license_summary' => $skLicense,
                'license_url' => $skLicenseUrl,
                'refresh_cadence' => 'weekly',
                'notes' => 'Multi-layer Mining MapServer. Bronze auto-enumerates layers 0-8 into per-layer source_ids CA-SK-MINERAL-DISPOSITION-MINING-{0..8}. Layers 9-15 (CR Preclude) are out-of-scope. Mining service maxRecordCount=1000. Two field schemas: layers 0-4 legacy (DISPOSITIO, DISPOSIT_1, OWNERS, EFFECTIVED, GOODSTANDI); layers 5-8 modern (DISPOSITION, STATUS, HOLDER, ANNIVERSARYDATE, HECTARES). Silver extractor probes both.',
            ],
            [
                'source_id' => 'CA-SK-MINERAL-DISPOSITION-CROWN',
                'jurisdiction_code' => 'CA-SK',
                'name' => 'Saskatchewan Crown Dispositions — Oil and Gas (parent)',
                'canonical_type' => 'mineral_disposition',
                'service_url' => 'https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Mineral_Tenure_Crown_Dispositions/MapServer',
                'layer_index' => null,
                'source_crs' => 2957,
                'license_summary' => $skLicense,
                'license_url' => $skLicenseUrl,
                'refresh_cadence' => 'weekly',
                'notes' => 'Crown Dispositions service — only layer 8 (Oil and Gas Dispositions) has unique data; layers 0-7 duplicate Mining. Bronze registers only CA-SK-MINERAL-DISPOSITION-CROWN-OIL-GAS from layer 8. Fields: DISPID, DISPTYPE, DISPSTATUS, ISSUEDATE, LESSEES, GEOAREA, BONUSBID, DSTRATRGHT, PARCELHECT.',
            ],

            // ── Saskatchewan — Bedrock Geology (Phase 2 Tier 2) ─────────────
            // Single layer (10) from the Geology MapServer. Bronze registers
            // exactly one source_id (CA-SK-GEOLOGY-BEDROCK-250K). Silver maps
            // ArcGIS fields ROCK_CODE, EON, ERA, PERIOD, GROUP_, FORMATION,
            // MEMBER, DOMAIN, LITHOLOGY, NAME → pg_bedrock_geology canonical
            // columns. scale constant = '250K'.
            [
                'source_id' => 'CA-SK-GEOLOGY-BEDROCK-250K',
                'jurisdiction_code' => 'CA-SK',
                'name' => 'SK Bedrock Geology 250K',
                'canonical_type' => 'bedrock_geology',
                // Layer index is baked into service_url to match the other SK entries —
                // Bronze's _run_single_layer_asset calls fetch_layer_geojson(service_url, ...)
                // directly and does NOT splice in layer_index.
                'service_url' => 'https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Geology/MapServer/10',
                'layer_index' => 10,
                'source_crs' => 2957,
                'license_summary' => $skLicense,
                'license_url' => $skLicenseUrl,
                'refresh_cadence' => 'weekly',
                'notes' => 'SK Bedrock Geology 250K. Fields: ROCK_CODE, EON, ERA, PERIOD, GROUP_, FORMATION, MEMBER, DOMAIN, LITHOLOGY, NAME. ~several thousand polygons covering bedrock units at 1:250K scale.',
            ],

            [
                'source_id' => 'CA-BC-MINFILE',
                'jurisdiction_code' => 'CA-BC',
                'name' => 'BC MINFILE — Mineral Occurrences',
                'canonical_type' => 'mineral_occurrence',
                'service_url' => 'https://delivery.maps.gov.bc.ca/arcgis/rest/services/mpcm/bcgwpub/MapServer/137',
                'layer_index' => 137,
                'source_crs' => 3005, // NAD83 / BC Albers (latestWkid)
                'license_summary' => 'Open Government Licence – British Columbia (v2.0)',
                'license_url' => 'https://www2.gov.bc.ca/gov/content/data/open-data/open-government-licence-bc',
                'refresh_cadence' => 'weekly',
                'notes' => 'BC MINFILE mineral occurrence records from BCGW public MapServer layer 137. Field names: MINFILE_NUMBER, MINFILE_NAME1/2, STATUS_DESCRIPTION, COMMODITY_CODE1..8, DEPOSIT_CLASS_DESCRIPTION1, PRODUCTION_IND, MINFILE_SUMMARY_URL. See FieldMapping registry.',
            ],
        ];

        foreach ($sources as $row) {
            DB::table('public_geo.sources')->updateOrInsert(
                ['source_id' => $row['source_id']],
                array_merge(
                    $row,
                    ['created_at' => $now, 'updated_at' => $now],
                ),
            );
        }

        $this->command?->info('Seeded 14 Canadian jurisdictions + 5 Saskatchewan sources.');
    }
}
