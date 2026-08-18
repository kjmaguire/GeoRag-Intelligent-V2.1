<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Image passages must be keyed by PAGE, not by text hash (2026-08-18).
 *
 * silver.document_passages has UNIQUE (document_id, revision_number,
 * text_hash), which makes re-parses idempotent for narrative chunks: the same
 * parsed section yields the same hash, so ON CONFLICT absorbs the re-insert.
 *
 * That invariant does not hold for image passages. A page-image row is created
 * with a placeholder text ("[Page N — page image, not yet described]") and the
 * verbalization sweep later REWRITES that text with the vision model's
 * description — which changes text_hash. A subsequent re-parse then inserts
 * the placeholder again under its original hash, matching nothing, and the
 * document ends up with two passages for the same page: one described, one
 * placeholder. Both retrievable, one useless, and no error anywhere.
 *
 * The natural key for an image passage is the page it depicts. This adds a
 * partial unique index on (document_id, revision_number, page_number) for
 * modality='image' rows so re-parse hits a real conflict target and updates in
 * place. Narrative rows are untouched and keep hashing as before.
 *
 * Guard: the index creation will fail if duplicate image rows already exist.
 * None can at time of writing (the feature has never run outside dev), but the
 * dedupe below makes the migration safe to run against a database where the
 * placeholder path did execute — it keeps the verbalized row, or the oldest.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        // Collapse any pre-existing duplicates before the unique index lands.
        // Preference order: a verbalized row beats a placeholder; ties break
        // on the older row, which owns the Qdrant point that was embedded first.
        DB::statement(
            "DELETE FROM silver.document_passages dp
             USING silver.document_passages keep
             WHERE dp.modality = 'image'
               AND keep.modality = 'image'
               AND dp.document_id = keep.document_id
               AND dp.revision_number = keep.revision_number
               AND dp.page_number = keep.page_number
               AND dp.passage_id <> keep.passage_id
               AND (
                     (keep.verbalized_at IS NOT NULL AND dp.verbalized_at IS NULL)
                  OR (
                        (keep.verbalized_at IS NULL) = (dp.verbalized_at IS NULL)
                        AND keep.created_at < dp.created_at
                     )
               )",
        );

        DB::statement(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_document_passages_image_page
                 ON silver.document_passages (document_id, revision_number, page_number)
                 WHERE modality = 'image'",
        );
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement('DROP INDEX IF EXISTS silver.uq_document_passages_image_page');
    }
};
