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
        Schema::create('silver.well_log_curves', function (Blueprint $table) {
            $table->uuid('curve_id')->primary();
            $table->foreignUuid('collar_id')->references('collar_id')->on('silver.collars')->cascadeOnDelete();
            $table->string('curve_name', 50);
            $table->string('curve_unit', 20)->nullable();
            $table->text('curve_description')->nullable();
            $table->float('min_depth');
            $table->float('max_depth');
            $table->float('step')->nullable();
            $table->float('null_value')->default(-999.25);
            $table->integer('sample_count');
            $table->string('las_version', 10)->nullable();
            $table->string('source_file', 255)->nullable();
            $table->timestamps();

            $table->unique(['collar_id', 'curve_name']);
        });

        // PostgreSQL DOUBLE PRECISION[] array columns (not supported by Laravel Schema Builder)
        DB::statement('ALTER TABLE silver.well_log_curves ADD COLUMN depths DOUBLE PRECISION[] NOT NULL');
        DB::statement('ALTER TABLE silver.well_log_curves ADD COLUMN values DOUBLE PRECISION[] NOT NULL');

        // Indexes
        DB::statement('CREATE INDEX idx_well_log_curves_collar_curve ON silver.well_log_curves (collar_id, curve_name)');
        DB::statement('CREATE INDEX idx_well_log_curves_name ON silver.well_log_curves (curve_name)');
    }

    /**
     * Reverse the migrations.
     */
    public function down(): void
    {
        Schema::dropIfExists('silver.well_log_curves');
    }
};
