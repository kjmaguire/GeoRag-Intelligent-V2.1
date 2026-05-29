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
        Schema::create('silver.geochemistry', function (Blueprint $table) {
            $table->uuid('geochem_id')->primary();
            $table->foreignUuid('collar_id')->references('collar_id')->on('silver.collars')->cascadeOnDelete();
            $table->float('from_depth');
            $table->float('to_depth');
            $table->float('sio2_wt_pct')->nullable();
            $table->float('al2o3_wt_pct')->nullable();
            $table->float('fe2o3_wt_pct')->nullable();
            $table->float('mgo_wt_pct')->nullable();
            $table->float('cao_wt_pct')->nullable();
            $table->float('na2o_wt_pct')->nullable();
            $table->float('k2o_wt_pct')->nullable();
            $table->jsonb('ree_json')->nullable();
            $table->float('mg_number')->nullable();
            $table->float('cia')->nullable();
            $table->float('eu_anomaly')->nullable();
            $table->timestamps();
        });
    }

    /**
     * Reverse the migrations.
     */
    public function down(): void
    {
        Schema::dropIfExists('silver.geochemistry');
    }
};
