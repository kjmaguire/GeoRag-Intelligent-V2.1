<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Allow chunk_kind = 'page_image' (2026-08-18).
 *
 * document_passages_chunk_kind_check enumerates 17 permitted chunk kinds and
 * did not include the one the multimodal page-image writer produces, so
 * INSERT_IMAGE_PASSAGE_SQL failed its CHECK on every row:
 *
 *   new row for relation "document_passages" violates check constraint
 *   "document_passages_chunk_kind_check"
 *
 * Caught by exercising the real INSERT against the dev database rather than
 * trusting that it would work — the same class of bug as the
 * review_routing_enum failure on 2026-08-07 and the answer_runs query_class
 * violation on 2026-05-20. A value the code believes in but the database does
 * not is always found in production, at night.
 *
 * 'page_image' is added rather than reusing 'caption_figure': that kind means
 * "text of a figure caption", which is a genuinely different thing from "a
 * render of a whole page", and the GC in ingest_pdf.py keys off chunk_kind to
 * decide what a re-parse may delete.
 */
return new class extends Migration
{
    private const KINDS = [
        'narrative', 'table', 'caption_figure', 'character_window', 'section',
        'paragraph', 'structured_summary', 'public_geo_synthesis',
        'kg_narrative', 'internal_docs', 'real_query_supervision',
        'data_quality_flag', 'check_constraint', 'db_comment',
        'frontend_component', 'test_scenario', 'taxonomy',
        // New 2026-08-18 — multimodal page renders.
        'page_image',
    ];

    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        $this->replaceCheck(self::KINDS);
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        $previous = array_values(array_filter(
            self::KINDS,
            static fn (string $kind): bool => $kind !== 'page_image',
        ));

        // Rows using the kind being removed would make the restored constraint
        // invalid, so clear them first. They are page renders; without the
        // kind they cannot be represented, and the image passages themselves
        // are re-derivable from a re-parse.
        DB::statement("DELETE FROM silver.document_passages WHERE chunk_kind = 'page_image'");

        $this->replaceCheck($previous);
    }

    /**
     * @param list<string> $kinds
     */
    private function replaceCheck(array $kinds): void
    {
        $list = implode(', ', array_map(
            static fn (string $kind): string => "'".$kind."'",
            $kinds,
        ));

        DB::statement(
            'ALTER TABLE silver.document_passages
                DROP CONSTRAINT IF EXISTS document_passages_chunk_kind_check',
        );
        DB::statement(
            "ALTER TABLE silver.document_passages
                ADD CONSTRAINT document_passages_chunk_kind_check
                CHECK (chunk_kind IS NULL OR chunk_kind::text = ANY (ARRAY[{$list}]::text[]))",
        );
    }
};
