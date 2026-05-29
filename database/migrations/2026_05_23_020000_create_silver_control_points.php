<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * CC-01 Item 3 (stub) — silver.control_points.
 *
 * Schema scaffold for Qwen-VL map digitisation. The VL pipeline itself
 * is gated on Milestone 2 (VL-model-size benchmark); this migration lands
 * the storage shape so:
 *   - downstream code can reference the table without a missing-table FK,
 *   - the /maps/ingest 501 stub can document the column contract that
 *     callers will eventually populate,
 *   - the spatial uncertainty contract (CC-01 Item 2) extends here too.
 *
 * Each row = one ground-control point: a (pixel_x, pixel_y) on a
 * source map image, the corresponding real-world coordinate (point_geom
 * in EPSG:4326), the VL-model self-confidence for that pairing, and
 * the method used to extract it. Used downstream by a georeferencing
 * affine/projective fit to register the map raster into PostGIS.
 *
 * Columns:
 *   point_id            uuid PK
 *   workspace_id        uuid → silver.workspaces (tenancy)
 *   source_pdf_id       char(64) — §04p Bronze SHA-256 of the source PDF
 *                       (NULL when the source is a standalone image upload)
 *   source_page         int — 1-indexed page within the PDF (NULL if image)
 *   source_image_key    text — Bronze object key for standalone image
 *                       uploads (NULL when source_pdf_id is set)
 *   pixel_x, pixel_y    double precision — pixel-space coordinates on the
 *                       rendered map image (at the rendering DPI captured
 *                       in render_dpi)
 *   render_dpi          int — DPI the source was rendered at when pixel
 *                       coordinates were picked
 *   point_geom          geometry(Point, 4326) — world coordinate, WGS84
 *   georef_confidence   real (0-1) — VL model self-reported confidence
 *   method              varchar(32) CHECK enum — how this control point
 *                       was extracted:
 *                         'qwen_vl_grid'    — VL detected a labelled
 *                                             coordinate grid intersection
 *                         'qwen_vl_legend'  — VL extracted the legend bbox
 *                                             with a scale marker
 *                         'qwen_vl_manual'  — VL paired a labelled feature
 *                                             with its coordinate
 *                         'human_pick'      — geologist manually digitised
 *                         'survey_marker'   — known survey marker matched
 *   notes               text NULL — optional rationale / VL excerpt
 *   created_at / updated_at
 *
 * SQLite — gated on Postgres (PostGIS geometry + uuid + FK).
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.control_points (
                point_id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                workspace_id        uuid NOT NULL,
                source_pdf_id       char(64),
                source_page         integer,
                source_image_key    text,
                pixel_x             double precision NOT NULL,
                pixel_y             double precision NOT NULL,
                render_dpi          integer NOT NULL DEFAULT 200,
                point_geom          geometry(Point, 4326) NOT NULL,
                georef_confidence   real,
                method              varchar(32) NOT NULL,
                notes               text,
                created_at          timestamptz NOT NULL DEFAULT now(),
                updated_at          timestamptz NOT NULL DEFAULT now(),

                CONSTRAINT chk_control_points_method
                    CHECK (method IN (
                        'qwen_vl_grid',
                        'qwen_vl_legend',
                        'qwen_vl_manual',
                        'human_pick',
                        'survey_marker'
                    )),
                CONSTRAINT chk_control_points_pdf_id_hex
                    CHECK (source_pdf_id IS NULL OR source_pdf_id ~ '^[0-9a-f]{64}$'),
                CONSTRAINT chk_control_points_source_xor
                    CHECK ((source_pdf_id IS NOT NULL) <> (source_image_key IS NOT NULL)),
                CONSTRAINT chk_control_points_confidence
                    CHECK (georef_confidence IS NULL
                           OR (georef_confidence >= 0 AND georef_confidence <= 1)),
                CONSTRAINT chk_control_points_render_dpi
                    CHECK (render_dpi >= 72 AND render_dpi <= 600),
                CONSTRAINT chk_control_points_pixels_positive
                    CHECK (pixel_x >= 0 AND pixel_y >= 0),

                CONSTRAINT fk_control_points_workspace
                    FOREIGN KEY (workspace_id)
                    REFERENCES silver.workspaces (workspace_id)
                    ON DELETE CASCADE
            )
        SQL);

        DB::statement('CREATE INDEX IF NOT EXISTS idx_control_points_workspace ON silver.control_points (workspace_id)');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_control_points_pdf ON silver.control_points (source_pdf_id, source_page) WHERE source_pdf_id IS NOT NULL');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_control_points_image ON silver.control_points (source_image_key) WHERE source_image_key IS NOT NULL');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_control_points_geom_gist ON silver.control_points USING gist (point_geom)');

        DB::statement("COMMENT ON TABLE silver.control_points IS
            'CC-01 Item 3 (stub) — ground-control points for map digitisation. Schema landed 2026-05-23; population gated on Milestone 2 VL benchmark.'");
        DB::statement("COMMENT ON COLUMN silver.control_points.method IS
            'How this control point was extracted. qwen_vl_* values populated by the §04p VL pipeline; human_pick + survey_marker are manual paths.'");
        DB::statement("COMMENT ON COLUMN silver.control_points.render_dpi IS
            'The DPI the source PDF / image was rendered at when pixel_x / pixel_y were picked. Downstream affine fits need this to map back to PDF user-space.'");
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }
        DB::statement('DROP TABLE IF EXISTS silver.control_points CASCADE');
    }
};
