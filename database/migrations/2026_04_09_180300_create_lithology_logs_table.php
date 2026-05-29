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
        Schema::create('silver.lithology_logs', function (Blueprint $table) {
            $table->uuid('log_id')->primary();
            $table->foreignUuid('collar_id')->references('collar_id')->on('silver.collars')->cascadeOnDelete();
            $table->float('from_depth');
            $table->float('to_depth');
            $table->string('lithology_code', 20)->nullable();
            $table->text('lithology_description')->nullable();
            $table->string('grain_size', 20)->nullable();
            $table->string('color', 50)->nullable();
            $table->string('hardness', 20)->nullable();
            $table->float('rqd')->nullable();
            $table->float('recovery')->nullable();
            $table->string('weathering', 20)->nullable();
            $table->timestamps();
        });
    }

    /**
     * Reverse the migrations.
     */
    public function down(): void
    {
        Schema::dropIfExists('silver.lithology_logs');
    }
};
