<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\Schema;

return new class extends Migration
{
    /**
     * Run the migrations.
     */
    public function up(): void
    {
        Schema::create('silver.projects', function (Blueprint $table) {
            $table->uuid('project_id')->primary();
            $table->string('project_name', 255);
            $table->string('crs_datum', 50)->default('EPSG:32613');
            $table->string('company', 255)->nullable();
            $table->float('magnetic_declination')->nullable();
            $table->string('orientation_reference', 10);
            $table->string('commodity', 50)->nullable();
            $table->string('region', 255)->nullable();
            $table->timestamps();
        });
    }

    /**
     * Reverse the migrations.
     */
    public function down(): void
    {
        Schema::dropIfExists('silver.projects');
    }
};
