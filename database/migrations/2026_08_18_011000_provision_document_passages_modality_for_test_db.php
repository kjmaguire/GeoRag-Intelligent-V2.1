<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Schema;

/**
 * Test-DB parity sibling for
 * 2026_08_18_010000_add_image_modality_to_document_passages.
 *
 * silver.document_passages is created by raw SQL
 * (2026_04_20_110000_create_document_passages), and the SQLite compatibility
 * hook in tests/TestCase.php does not mirror raw-SQL DDL — so without this
 * the multimodal columns exist in Postgres only and every fast-suite test
 * touching an image passage fails on "no such column: modality".
 *
 * See memory/project_test_db_parity_gap.md for why this sibling pattern is
 * mandatory rather than optional.
 *
 * The CHECK constraints from the Postgres migration are deliberately NOT
 * mirrored: SQLite cannot add constraints via ALTER TABLE, and the fast suite
 * asserts application behaviour, not database-level enforcement. The Postgres
 * suite covers the constraints themselves.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'sqlite') {
            return;
        }

        if (! Schema::hasTable('document_passages')) {
            return;
        }

        Schema::table('document_passages', function (Blueprint $table): void {
            if (! Schema::hasColumn('document_passages', 'modality')) {
                $table->string('modality')->default('text');
            }
            if (! Schema::hasColumn('document_passages', 'page_number')) {
                $table->integer('page_number')->nullable();
            }
            if (! Schema::hasColumn('document_passages', 'image_object_key')) {
                $table->text('image_object_key')->nullable();
            }
            if (! Schema::hasColumn('document_passages', 'verbalized_at')) {
                $table->timestamp('verbalized_at')->nullable();
            }
        });
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'sqlite') {
            return;
        }

        if (! Schema::hasTable('document_passages')) {
            return;
        }

        Schema::table('document_passages', function (Blueprint $table): void {
            $table->dropColumn([
                'modality',
                'page_number',
                'image_object_key',
                'verbalized_at',
            ]);
        });
    }
};
