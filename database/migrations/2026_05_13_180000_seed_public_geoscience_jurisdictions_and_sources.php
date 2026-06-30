<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Doc-phase 135 — seed `public_geo.jurisdictions` +
 * `public_geo.sources` foundation rows.
 *
 * Master-plan §6 prerequisite: before BC MINFILE + NRCan adapters can
 * land in their respective ingestion ticks, the jurisdictions metadata
 * and source-registry rows need to exist. This migration seeds the
 * authoritative reference data for 5 Canadian jurisdictions (SK, BC,
 * AB, MB, CAN-federal) and 9 source registrations covering the major
 * public-data feeds expected in §6.
 *
 * Sources are seeded with `status='registered'` style metadata; the
 * actual data ingestion stays empty until the per-jurisdiction
 * adapters land. `last_refreshed_at` is intentionally NULL so the
 * §6 admin surface can show "never pulled" state correctly.
 *
 * License columns are populated with the upstream attributions (OGL
 * Saskatchewan, OGL British Columbia, Open Government Licence Canada
 * v2.0). License text matters for §6 frontend export compliance.
 *
 * Idempotent: ON CONFLICT DO NOTHING on the primary keys.
 */
return new class extends Migration
{
    public function up(): void
    {
        // Doc-phase 157 — gate on driver. The public_geo.*
        // schema is PG-only (PostGIS types). SQLite test runs skip
        // the seed; pgsql migrate:fresh applies it.
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        // Jurisdictions.
        DB::table('public_geo.jurisdictions')->upsert(
            [
                [
                    'jurisdiction_code' => 'CA-SK',
                    'country_code' => 'CAN',
                    'display_name' => 'Saskatchewan',
                    'level' => 'province',
                    'status' => 'active',
                    'primary_authority' => 'Government of Saskatchewan — Ministry of Energy and Resources',
                    'license_summary' => 'Open Government Licence — Saskatchewan',
                    'license_url' => 'https://www.saskatchewan.ca/government/government-data',
                    'default_source_crs' => 3401,  // NAD83 / Saskatchewan
                    'refresh_cadence' => 'weekly',
                    'teaser' => 'SaskGeoAtlas — minerals, drillholes, assessment files',
                    'sort_order' => 10,
                ],
                [
                    'jurisdiction_code' => 'CA-BC',
                    'country_code' => 'CAN',
                    'display_name' => 'British Columbia',
                    'level' => 'province',
                    'status' => 'active',
                    'primary_authority' => 'Government of British Columbia — Ministry of Energy, Mines and Low Carbon Innovation',
                    'license_summary' => 'Open Government Licence — British Columbia',
                    'license_url' => 'https://www2.gov.bc.ca/gov/content/data/open-data/open-government-licence-bc',
                    'default_source_crs' => 3005,  // NAD83 / BC Albers
                    'refresh_cadence' => 'monthly',
                    'teaser' => 'MINFILE mineral occurrences, ARIS assessment files, drillholes',
                    'sort_order' => 20,
                ],
                [
                    'jurisdiction_code' => 'CA-AB',
                    'country_code' => 'CAN',
                    'display_name' => 'Alberta',
                    'level' => 'province',
                    'status' => 'active',
                    'primary_authority' => 'Alberta Energy Regulator + Alberta Geological Survey',
                    'license_summary' => 'Open Government Licence — Alberta',
                    'license_url' => 'https://open.alberta.ca/licence',
                    'default_source_crs' => 3402,  // NAD83 / Alberta 3TM ref merid 114 W
                    'refresh_cadence' => 'monthly',
                    'teaser' => 'AGS bedrock geology, oil-sands wells, minerals',
                    'sort_order' => 30,
                ],
                [
                    'jurisdiction_code' => 'CA-MB',
                    'country_code' => 'CAN',
                    'display_name' => 'Manitoba',
                    'level' => 'province',
                    'status' => 'coming_soon',
                    'primary_authority' => 'Manitoba Geological Survey',
                    'license_summary' => 'Open Government Licence — Canada (v2.0)',
                    'license_url' => 'https://open.canada.ca/en/open-government-licence-canada',
                    'default_source_crs' => 3155,  // NAD83(CSRS) / UTM zone 14N
                    'refresh_cadence' => 'quarterly',
                    'teaser' => 'MGS minerals, bedrock, drillholes',
                    'sort_order' => 40,
                ],
                [
                    'jurisdiction_code' => 'CA-FEDERAL',
                    'country_code' => 'CAN',
                    'display_name' => 'Canada (Federal)',
                    'level' => 'federal',
                    'status' => 'active',
                    'primary_authority' => 'Natural Resources Canada (NRCan) — Geological Survey of Canada',
                    'license_summary' => 'Open Government Licence — Canada (v2.0)',
                    'license_url' => 'https://open.canada.ca/en/open-government-licence-canada',
                    'default_source_crs' => 3978,  // NAD83 / Canada Atlas Lambert
                    'refresh_cadence' => 'monthly',
                    'teaser' => 'NRCan bedrock geology, mines and exploration deposits',
                    'sort_order' => 5,
                ],
            ],
            ['jurisdiction_code'],
            // Update fields if upserting.
            [
                'country_code', 'display_name', 'level', 'status',
                'primary_authority', 'license_summary', 'license_url',
                'default_source_crs', 'refresh_cadence', 'teaser', 'sort_order',
                'updated_at',
            ],
        );

        // Sources registry — known public datasets per jurisdiction.
        DB::table('public_geo.sources')->upsert(
            [
                // Saskatchewan
                [
                    'source_id' => 'sk_mineral_occurrence',
                    'jurisdiction_code' => 'CA-SK',
                    'name' => 'SaskGeoAtlas — Mineral Occurrences',
                    'canonical_type' => 'mineral_occurrence',
                    'service_url' => 'https://gisapp.saskatchewan.ca/arcgis/rest/services/SaskGeoAtlas/MineralOccurrences/MapServer/0',
                    'layer_index' => 0,
                    'source_crs' => 3401,
                    'license_summary' => 'OGL Saskatchewan',
                    'refresh_cadence' => 'weekly',
                    'notes' => 'ArcGIS REST FeatureServer.',
                ],
                [
                    'source_id' => 'sk_drillhole_collar',
                    'jurisdiction_code' => 'CA-SK',
                    'name' => 'SaskGeoAtlas — Drillhole Collars',
                    'canonical_type' => 'drillhole_collar',
                    'service_url' => 'https://gisapp.saskatchewan.ca/arcgis/rest/services/SaskGeoAtlas/Drillholes/MapServer/0',
                    'layer_index' => 0,
                    'source_crs' => 3401,
                    'license_summary' => 'OGL Saskatchewan',
                    'refresh_cadence' => 'weekly',
                    'notes' => 'Includes assessment-file linkage.',
                ],
                [
                    'source_id' => 'sk_assessment_survey',
                    'jurisdiction_code' => 'CA-SK',
                    'name' => 'SaskGeoAtlas — Assessment Surveys',
                    'canonical_type' => 'assessment_survey',
                    'service_url' => 'https://gisapp.saskatchewan.ca/arcgis/rest/services/SaskGeoAtlas/AssessmentFiles/MapServer/0',
                    'layer_index' => 0,
                    'source_crs' => 3401,
                    'license_summary' => 'OGL Saskatchewan',
                    'refresh_cadence' => 'monthly',
                    'notes' => 'Polygons of historical exploration assessment file footprints.',
                ],
                // British Columbia — MINFILE + ARIS
                [
                    'source_id' => 'bc_minfile_mineral_occurrence',
                    'jurisdiction_code' => 'CA-BC',
                    'name' => 'BC MINFILE — Mineral Occurrences',
                    'canonical_type' => 'mineral_occurrence',
                    'service_url' => 'https://maps.gov.bc.ca/arcgis/rest/services/mpcm/MINFILE_PUB/MapServer/0',
                    'layer_index' => 0,
                    'source_crs' => 3005,
                    'license_summary' => 'OGL British Columbia',
                    'refresh_cadence' => 'monthly',
                    'notes' => 'Canonical BC mineral occurrence database with deposit type, status, host rock.',
                ],
                [
                    'source_id' => 'bc_aris_assessment_survey',
                    'jurisdiction_code' => 'CA-BC',
                    'name' => 'BC ARIS — Assessment Report Indexing System',
                    'canonical_type' => 'assessment_survey',
                    'service_url' => 'https://maps.gov.bc.ca/arcgis/rest/services/mpcm/ARIS_PUB/MapServer/0',
                    'layer_index' => 0,
                    'source_crs' => 3005,
                    'license_summary' => 'OGL British Columbia',
                    'refresh_cadence' => 'monthly',
                    'notes' => 'Assessment-report footprints linked to MINFILE.',
                ],
                [
                    'source_id' => 'bc_minfile_drillhole_collar',
                    'jurisdiction_code' => 'CA-BC',
                    'name' => 'BC MINFILE — Drillhole Database',
                    'canonical_type' => 'drillhole_collar',
                    'service_url' => 'https://maps.gov.bc.ca/arcgis/rest/services/mpcm/MINFILE_DRILL/MapServer/0',
                    'layer_index' => 0,
                    'source_crs' => 3005,
                    'license_summary' => 'OGL British Columbia',
                    'refresh_cadence' => 'monthly',
                    'notes' => 'BC drillhole collars cross-referenced to MINFILE occurrences.',
                ],
                // Alberta — bedrock + minerals
                [
                    'source_id' => 'ab_ags_bedrock_geology',
                    'jurisdiction_code' => 'CA-AB',
                    'name' => 'AGS — Bedrock Geology of Alberta',
                    'canonical_type' => 'bedrock_geology',
                    'service_url' => 'https://geology-ahs.opendata.arcgis.com/datasets/bedrock-geology-of-alberta',
                    'layer_index' => null,
                    'source_crs' => 3402,
                    'license_summary' => 'OGL Alberta',
                    'refresh_cadence' => 'annual',
                    'notes' => 'Alberta Geological Survey 1:1M bedrock geology compilation.',
                ],
                // Federal / NRCan
                [
                    'source_id' => 'nrcan_canadian_mines',
                    'jurisdiction_code' => 'CA-FEDERAL',
                    'name' => 'NRCan — Canadian Mines Database',
                    'canonical_type' => 'mine',
                    'service_url' => 'https://atlas.gc.ca/mines/en/index.html',
                    'layer_index' => null,
                    'source_crs' => 3978,
                    'license_summary' => 'OGL Canada v2.0',
                    'refresh_cadence' => 'quarterly',
                    'notes' => 'NRCan-curated registry of operating + past-producing mines across Canada.',
                ],
                [
                    'source_id' => 'nrcan_geo_bedrock_geology',
                    'jurisdiction_code' => 'CA-FEDERAL',
                    'name' => 'GEO.ca — Bedrock Geology of Canada',
                    'canonical_type' => 'bedrock_geology',
                    'service_url' => 'https://atlas.gc.ca/bedrock/en/',
                    'layer_index' => null,
                    'source_crs' => 3978,
                    'license_summary' => 'OGL Canada v2.0',
                    'refresh_cadence' => 'annual',
                    'notes' => 'NRCan compilation of bedrock geology across Canada (1:5M scale).',
                ],
            ],
            ['source_id'],
            [
                'jurisdiction_code', 'name', 'canonical_type', 'service_url',
                'layer_index', 'source_crs', 'license_summary',
                'refresh_cadence', 'notes', 'updated_at',
            ],
        );
    }

    public function down(): void
    {
        // Conservative: leave the seed data in place. Downstream
        // adapters reference these source_ids; deleting them would
        // break ingestion provenance. Removal is a deliberate
        // operator action.
    }
};
