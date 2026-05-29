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
        Schema::create('silver.samples', function (Blueprint $table) {
            $table->uuid('sample_id')->primary();
            $table->foreignUuid('collar_id')->references('collar_id')->on('silver.collars')->cascadeOnDelete();
            $table->float('from_depth');
            $table->float('to_depth');
            $table->string('sample_type', 20);
            $table->string('lab_id', 50)->nullable();
            $table->jsonb('commodity_assays')->nullable();
            $table->string('qaqc_type', 20)->nullable();
            $table->timestamps();
        });
    }

    /**
     * Reverse the migrations.
     */
    public function down(): void
    {
        Schema::dropIfExists('silver.samples');
    }
};
