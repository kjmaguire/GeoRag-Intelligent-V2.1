<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Multimodal page-image passages (Cohere Embed v4), 2026-08-18.
 *
 * Embed v4 places image vectors in the same 1024-dim space as text vectors,
 * so a rendered page image is retrievable from `georag_chunks` alongside text
 * passages with no change to the collection schema or the retrieval path.
 * What it does need is a way to tell the two apart on the way in and on the
 * way out — hence `modality`.
 *
 * Design notes worth keeping:
 *
 *  - `text` stays NOT NULL for image rows. An image passage carries either
 *    the vision model's description of the page (once verbalization is
 *    wired — see IMAGE_VERBALIZATION_ENABLED) or, until then, a synthetic
 *    "[Page N — page image]" placeholder. Keeping real text on every row is
 *    what lets image passages flow through the reranker, the citation
 *    machinery and the Section 04i grounding layers completely unchanged.
 *    The chat model (Cohere Command A+) is text-only, so a passage with no
 *    text could never be cited anyway.
 *
 *  - The placeholder embeds the page number, which keeps it distinct under
 *    the existing UNIQUE (document_id, revision_number, text_hash)
 *    constraint. Two image pages in one document cannot collide.
 *
 *  - `verbalized_at` distinguishes "described by a vision model" from "still
 *    a placeholder", so a backfill can find the placeholders later without
 *    string-matching the text.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement(
            "ALTER TABLE silver.document_passages
                ADD COLUMN IF NOT EXISTS modality TEXT NOT NULL DEFAULT 'text'",
        );
        DB::statement(
            'ALTER TABLE silver.document_passages
                ADD COLUMN IF NOT EXISTS page_number INTEGER NULL',
        );
        DB::statement(
            'ALTER TABLE silver.document_passages
                ADD COLUMN IF NOT EXISTS image_object_key TEXT NULL',
        );
        DB::statement(
            'ALTER TABLE silver.document_passages
                ADD COLUMN IF NOT EXISTS verbalized_at TIMESTAMP(0) WITHOUT TIME ZONE NULL',
        );

        // Guard the enum rather than trusting every writer. Mirrors the
        // review_routing_enum lesson from 2026-08-07: a value the code
        // believes in but the database does not is found at 3am.
        DB::statement(
            "DO $$
             BEGIN
                 IF NOT EXISTS (
                     SELECT 1 FROM pg_constraint
                     WHERE conname = 'document_passages_modality_valid'
                 ) THEN
                     ALTER TABLE silver.document_passages
                         ADD CONSTRAINT document_passages_modality_valid
                         CHECK (modality IN ('text', 'image'));
                 END IF;
             END
             $$",
        );

        // An image passage without its page number or its stored render is
        // unusable: retrieval can surface it but the UI has nothing to show
        // and no backfill can repair it. Reject the row instead.
        DB::statement(
            "DO $$
             BEGIN
                 IF NOT EXISTS (
                     SELECT 1 FROM pg_constraint
                     WHERE conname = 'document_passages_image_requires_source'
                 ) THEN
                     ALTER TABLE silver.document_passages
                         ADD CONSTRAINT document_passages_image_requires_source
                         CHECK (
                             modality <> 'image'
                             OR (page_number IS NOT NULL AND image_object_key IS NOT NULL)
                         );
                 END IF;
             END
             $$",
        );

        // Partial index: image rows are the minority under scope=figures and
        // roughly half under scope=all, and every consumer of this column
        // filters to modality='image' (verbalization backfill, the Reader's
        // page-image panel, ingestion diagnostics).
        DB::statement(
            "CREATE INDEX IF NOT EXISTS idx_document_passages_image
                 ON silver.document_passages (document_id, page_number)
                 WHERE modality = 'image'",
        );
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement('DROP INDEX IF EXISTS silver.idx_document_passages_image');
        DB::statement(
            'ALTER TABLE silver.document_passages
                DROP CONSTRAINT IF EXISTS document_passages_image_requires_source',
        );
        DB::statement(
            'ALTER TABLE silver.document_passages
                DROP CONSTRAINT IF EXISTS document_passages_modality_valid',
        );
        DB::statement('ALTER TABLE silver.document_passages DROP COLUMN IF EXISTS verbalized_at');
        DB::statement('ALTER TABLE silver.document_passages DROP COLUMN IF EXISTS image_object_key');
        DB::statement('ALTER TABLE silver.document_passages DROP COLUMN IF EXISTS page_number');
        DB::statement('ALTER TABLE silver.document_passages DROP COLUMN IF EXISTS modality');
    }
};
