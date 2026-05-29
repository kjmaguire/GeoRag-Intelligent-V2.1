<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\Schema;
use Illuminate\Support\Facades\DB;

return new class extends Migration
{
    /**
     * Column mappings for Sprint 5 Phase 1 — vendor-specific remapping of canonical field names and units.
     * Constrains one source column per (profile, parser_type, canonical_field) and vice versa.
     * Phase 2 consumes these at parse time to normalize heterogeneous vendor data.
     */
    public function up(): void
    {
        Schema::create('column_mappings', function (Blueprint $table) {
            $table->bigIncrements('id');
            $table->foreignId('vendor_profile_id')->constrained('vendor_profiles')->cascadeOnDelete();
            $table->string('parser_type', 32);
            $table->string('canonical_field', 64);
            $table->string('source_column', 255);
            $table->string('source_unit', 32)->nullable();
            $table->string('target_unit', 32)->nullable();
            $table->text('notes')->nullable();
            $table->timestamps();

            $table->unique(['vendor_profile_id', 'parser_type', 'canonical_field']);
            $table->unique(['vendor_profile_id', 'parser_type', 'source_column']);
            $table->index('vendor_profile_id');
            $table->index('parser_type');
            $table->index('canonical_field');
        });

        DB::statement(
            "ALTER TABLE column_mappings ADD CONSTRAINT column_mappings_parser_type_check CHECK (parser_type IN ('csv_collar', 'csv_sample', 'csv_survey', 'csv_lithology', 'xlsx', 'spatial', 'pdf_report', 'docx', 'raster'))"
        );
    }

    public function down(): void
    {
        Schema::dropIfExists('column_mappings');
    }
};
