<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\Schema;

/**
 * Add versioning and quality tracking to silver.reports for amendment support.
 */
return new class extends Migration
{
    public function up(): void
    {
        Schema::table('silver.reports', function (Blueprint $table) {
            $table->integer('version')->default(1)->after('updated_at');
            $table->uuid('supersedes_report_id')->nullable()->after('version');
            $table->float('parse_quality_pct')->nullable()->after('supersedes_report_id');
            $table->float('extraction_confidence')->nullable()->after('parse_quality_pct');
            $table->string('parser_used', 30)->nullable()->after('extraction_confidence');
            $table->boolean('is_scanned')->default(false)->after('parser_used');
            $table->string('source_file_sha256', 64)->nullable()->after('is_scanned');

            $table->index('supersedes_report_id');
        });
    }

    public function down(): void
    {
        Schema::table('silver.reports', function (Blueprint $table) {
            $table->dropColumn([
                'version', 'supersedes_report_id', 'parse_quality_pct',
                'extraction_confidence', 'parser_used', 'is_scanned',
                'source_file_sha256',
            ]);
        });
    }
};
