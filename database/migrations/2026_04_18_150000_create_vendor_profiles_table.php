<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Schema;

return new class extends Migration
{
    /**
     * Vendor profiles for Sprint 5 Phase 1 — per-vendor column aliases and unit declarations.
     * Supports lab, driller, geophysics, internal, and other data sources.
     * Phase 2 wires these mappings into the parser pipeline at ingest time.
     */
    public function up(): void
    {
        Schema::create('vendor_profiles', function (Blueprint $table) {
            $table->bigIncrements('id');
            $table->string('name', 100)->unique();
            $table->text('description')->nullable();
            $table->string('profile_type', 20);
            $table->boolean('is_global')->default(true);
            $table->foreignId('created_by_user_id')->nullable()->constrained('users')->nullOnDelete();
            $table->timestamps();

            $table->index('profile_type');
            $table->index('created_by_user_id');
        });

        DB::statement(
            "ALTER TABLE vendor_profiles ADD CONSTRAINT vendor_profiles_type_check CHECK (profile_type IN ('lab', 'driller', 'geophysics', 'internal', 'other'))",
        );
    }

    public function down(): void
    {
        Schema::dropIfExists('vendor_profiles');
    }
};
