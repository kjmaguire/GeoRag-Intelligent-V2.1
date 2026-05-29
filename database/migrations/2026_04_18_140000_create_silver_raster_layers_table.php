<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Create silver.raster_layers — one row per ingested raster file.
 *
 * Stores metadata and spatial bounds for GeoTIFF, NetCDF, ASCII Grid, JPEG2000, and other
 * raster formats parsed by raster_parser.py. Acts as a searchable catalog for raster assets.
 * Actual pixel data remains on disk or in MinIO Bronze — this table indexes only metadata,
 * bounds, band info, and parser warnings for deduplication and spatial queries.
 *
 * The bbox (bounding polygon) column is added via PostGIS AddGeometryColumn for GIST indexing.
 * References Section 04e (Core Data Schemas) and Section 04d (raster_parser.py).
 */
return new class extends Migration
{
    /**
     * Run the migrations.
     */
    public function up(): void
    {
        // Create the table without the geometry column first
        DB::statement("
            CREATE TABLE IF NOT EXISTS silver.raster_layers (
                raster_id              UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
                project_id             UUID          NULL,
                layer_name             VARCHAR(255)  NOT NULL,
                source_file            TEXT          NOT NULL,
                source_file_sha256     CHAR(64)      NOT NULL,
                format                 VARCHAR(32)   NOT NULL,
                driver                 VARCHAR(32)   NULL,
                width                  INT           NOT NULL CHECK (width > 0),
                height                 INT           NOT NULL CHECK (height > 0),
                band_count             INT           NOT NULL CHECK (band_count > 0),
                crs                    VARCHAR(100)  NULL,
                crs_confidence         REAL          NULL CHECK (crs_confidence IS NULL OR (crs_confidence >= 0 AND crs_confidence <= 1)),
                pixel_size_x           DOUBLE PRECISION NULL,
                pixel_size_y           DOUBLE PRECISION NULL,
                bounds_native          JSONB         NULL,
                compression            VARCHAR(32)   NULL,
                is_cog                 BOOLEAN       NOT NULL DEFAULT false,
                has_alpha              BOOLEAN       NOT NULL DEFAULT false,
                band_stats             JSONB         NULL,
                tags                   JSONB         NULL,
                warnings               JSONB         NULL,
                created_at             TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
                updated_at             TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
                CONSTRAINT fk_raster_layers_project
                    FOREIGN KEY (project_id)
                    REFERENCES silver.projects(project_id)
                    ON DELETE SET NULL
            )
        ");

        // Add PostGIS geometry column for bounding polygon (EPSG:4326)
        DB::statement(
            "SELECT AddGeometryColumn('silver', 'raster_layers', 'bbox', 4326, 'POLYGON', 2)"
        );

        // Indexes: spatial (GIST), project, format, and SHA256 deduplication
        DB::statement("
            CREATE INDEX IF NOT EXISTS idx_raster_layers_bbox
                ON silver.raster_layers USING GIST (bbox)
        ");

        DB::statement("
            CREATE INDEX IF NOT EXISTS idx_raster_layers_project
                ON silver.raster_layers (project_id)
        ");

        DB::statement("
            CREATE INDEX IF NOT EXISTS idx_raster_layers_format
                ON silver.raster_layers (format)
        ");

        DB::statement("
            CREATE INDEX IF NOT EXISTS idx_raster_layers_sha256
                ON silver.raster_layers (source_file_sha256)
        ");

        // Partial unique on (project_id, source_file_sha256) where project_id is NOT NULL
        // Allows same raster to exist globally (project_id IS NULL) but not duplicated within a project
        DB::statement("
            CREATE UNIQUE INDEX IF NOT EXISTS uq_raster_layers_project_sha
                ON silver.raster_layers (project_id, source_file_sha256)
                WHERE project_id IS NOT NULL
        ");
    }

    /**
     * Reverse the migrations.
     */
    public function down(): void
    {
        DB::statement('DROP TABLE IF EXISTS silver.raster_layers CASCADE');
    }
};
