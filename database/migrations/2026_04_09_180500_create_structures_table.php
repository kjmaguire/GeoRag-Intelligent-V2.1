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
        Schema::create('silver.structures', function (Blueprint $table) {
            $table->uuid('structure_id')->primary();
            $table->foreignUuid('collar_id')->references('collar_id')->on('silver.collars')->cascadeOnDelete();
            $table->float('depth');
            $table->string('structure_type', 20);
            $table->float('alpha_angle')->nullable();
            $table->float('beta_angle')->nullable();
            $table->float('true_dip')->nullable();
            $table->float('dip_direction')->nullable();
            $table->text('description')->nullable();
            $table->timestamps();
        });
    }

    /**
     * Reverse the migrations.
     */
    public function down(): void
    {
        Schema::dropIfExists('silver.structures');
    }
};
