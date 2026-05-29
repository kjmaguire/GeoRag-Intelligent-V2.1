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
        Schema::create('silver.collars', function (Blueprint $table) {
            $table->uuid('collar_id')->primary();
            $table->string('hole_id', 50);
            $table->foreignUuid('project_id')->references('project_id')->on('silver.projects')->cascadeOnDelete();
            $table->float('easting');
            $table->float('northing');
            $table->float('elevation')->nullable();
            $table->float('total_depth');
            $table->string('hole_type', 20);
            $table->float('azimuth')->nullable();
            $table->float('dip')->nullable();
            $table->date('drill_date')->nullable();
            $table->string('status', 20);
            $table->timestamps();

            $table->unique(['project_id', 'hole_id']);
        });

        // PostGIS geometry column — Point with default project CRS (EPSG:32613 UTM Zone 13N)
        DB::statement("SELECT AddGeometryColumn('silver', 'collars', 'geom', 32613, 'POINT', 2)");
        DB::statement("CREATE INDEX idx_collars_geom ON silver.collars USING GIST(geom)");
    }

    /**
     * Reverse the migrations.
     */
    public function down(): void
    {
        Schema::dropIfExists('silver.collars');
    }
};
