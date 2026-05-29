<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\Schema;
use Illuminate\Support\Facades\DB;

return new class extends Migration
{
    /**
     * Run the migrations.
     */
    public function up(): void
    {
        Schema::create('silver.spatial_features', function (Blueprint $table) {
            $table->uuid('feature_id')->primary();
            $table->foreignUuid('project_id')->nullable()->references('project_id')->on('silver.projects')->nullOnDelete();
            $table->string('feature_type', 50);
            $table->string('feature_name', 255)->nullable();
            $table->string('source', 100)->nullable();
            $table->string('source_file', 255)->nullable();
            $table->string('source_crs', 64)->nullable();
            $table->jsonb('properties')->default(DB::raw("'{}'::jsonb"));
            $table->timestamps();

            $table->index('feature_type');
            $table->index('project_id');
        });

        // PostGIS geometry column — any geometry type in WGS84 (EPSG:4326)
        DB::statement("SELECT AddGeometryColumn('silver', 'spatial_features', 'geom', 4326, 'GEOMETRY', 2)");
        DB::statement("CREATE INDEX idx_spatial_features_geom ON silver.spatial_features USING GIST(geom)");
        DB::statement("CREATE INDEX idx_spatial_features_properties ON silver.spatial_features USING GIN(properties)");
    }

    /**
     * Reverse the migrations.
     */
    public function down(): void
    {
        Schema::dropIfExists('silver.spatial_features');
    }
};
