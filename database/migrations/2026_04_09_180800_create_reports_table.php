<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Schema;

return new class extends Migration
{
    /**
     * Run the migrations.
     */
    public function up(): void
    {
        Schema::create('silver.reports', function (Blueprint $table) {
            $table->uuid('report_id')->primary();
            $table->text('title');
            $table->string('company', 255)->nullable();
            $table->date('filing_date')->nullable();
            $table->string('commodity', 50)->nullable();
            $table->string('project_name', 255)->nullable();
            $table->string('region', 255)->nullable();
            $table->jsonb('resource_estimate')->nullable();
            $table->jsonb('sections_text')->nullable();
            $table->timestamps();
        });

        // PostgreSQL TEXT[] array columns (not supported by Laravel Schema Builder)
        DB::statement('ALTER TABLE silver.reports ADD COLUMN authors TEXT[]');
        DB::statement('ALTER TABLE silver.reports ADD COLUMN embedding_ids TEXT[]');

        // PostGIS geometry column — Polygon with WGS84 (EPSG:4326) for project boundaries
        DB::statement("SELECT AddGeometryColumn('silver', 'reports', 'geom', 4326, 'POLYGON', 2)");
        DB::statement('CREATE INDEX idx_reports_geom ON silver.reports USING GIST(geom)');
    }

    /**
     * Reverse the migrations.
     */
    public function down(): void
    {
        Schema::dropIfExists('silver.reports');
    }
};
