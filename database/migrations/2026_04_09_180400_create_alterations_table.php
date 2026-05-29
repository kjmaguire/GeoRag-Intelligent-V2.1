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
        Schema::create('silver.alterations', function (Blueprint $table) {
            $table->uuid('alteration_id')->primary();
            $table->foreignUuid('collar_id')->references('collar_id')->on('silver.collars')->cascadeOnDelete();
            $table->float('from_depth');
            $table->float('to_depth');
            $table->string('alteration_type', 50);
            $table->string('intensity', 20);
            $table->string('minerals', 255)->nullable();
            $table->timestamps();
        });
    }

    /**
     * Reverse the migrations.
     */
    public function down(): void
    {
        Schema::dropIfExists('silver.alterations');
    }
};
