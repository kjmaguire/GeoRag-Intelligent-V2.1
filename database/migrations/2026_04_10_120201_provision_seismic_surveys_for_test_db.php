<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Schema;

/**
 * SQLite mirror for the raw-SQL silver.seismic_surveys migration.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'sqlite') {
            return;
        }

        Schema::create('seismic_surveys', function (Blueprint $table): void {
            $table->uuid('survey_id')->primary();
            $table->uuid('project_id')->nullable();
            $table->string('survey_name');
            $table->string('survey_type', 10);
            $table->integer('num_traces');
            $table->integer('num_samples_per_trace');
            $table->integer('sample_interval_us');
            $table->float('record_length_ms');
            $table->integer('inline_min')->nullable();
            $table->integer('inline_max')->nullable();
            $table->integer('xline_min')->nullable();
            $table->integer('xline_max')->nullable();
            $table->string('source_file');
            $table->unsignedBigInteger('file_size_bytes');
            $table->string('segy_revision', 10)->nullable();
            $table->text('header_text')->nullable();
            $table->timestamps();
        });
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            Schema::dropIfExists('seismic_surveys');
        }
    }
};
