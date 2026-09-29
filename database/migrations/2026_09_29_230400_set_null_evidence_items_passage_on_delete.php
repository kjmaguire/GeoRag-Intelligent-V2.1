<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Log;

/**
 * §04e: silver.evidence_items.passage_id is ON DELETE SET NULL
 * (SME-approved, Kyle, 2026-09-29).
 *
 * It was ON DELETE RESTRICT (2026_04_20_140000_create_evidence_items.php:70-71),
 * so a document whose passages had ever been cited could not be deleted or
 * re-ingested until something migrated the evidence rows by hand — and
 * nothing did. The column was already nullable.
 *
 * SET NULL alone would fail on the two CHECKs RESTRICT was protecting (that
 * migration's own comment says so): a document_passage row with passage_id
 * NULL has zero refs (evidence_items_exactly_one_ref) and no passage
 * (evidence_items_type_ref_consistent). Both are widened by exactly one case,
 * the TOMBSTONE: evidence_type = 'document_passage' with every ref NULL. Every
 * other shape is refused as before — a row still cannot carry two refs, or a
 * ref that disagrees with its type. Readers answer a tombstone with 410
 * `evidence_source_deleted` (src/fastapi/app/routers/evidence.py).
 *
 * The FK is re-added NOT VALID then VALIDATEd: the existing rows already
 * satisfied it, and VALIDATE takes only a SHARE UPDATE EXCLUSIVE lock.
 *
 * down(): back to RESTRICT and the strict CHECKs. If tombstones exist by
 * then, the strict CHECKs are added NOT VALID (new rows held to them, the
 * tombstones kept) and a warning names the count.
 */
return new class extends Migration
{
    private const FK = 'evidence_items_passage_id_fkey';

    private const TOMBSTONE = "(evidence_type = 'document_passage' AND passage_id IS NULL"
        .' AND structured_ref IS NULL AND graph_edge_ref IS NULL AND map_feature_ref IS NULL)';

    private const REF_COUNT = '(CASE WHEN passage_id IS NOT NULL THEN 1 ELSE 0 END'
        .' + CASE WHEN structured_ref IS NOT NULL THEN 1 ELSE 0 END'
        .' + CASE WHEN graph_edge_ref IS NOT NULL THEN 1 ELSE 0 END'
        .' + CASE WHEN map_feature_ref IS NOT NULL THEN 1 ELSE 0 END)';

    private const TYPE_REF = "(evidence_type = 'document_passage' AND passage_id IS NOT NULL)"
        ." OR (evidence_type = 'structured_record' AND structured_ref IS NOT NULL)"
        ." OR (evidence_type = 'graph_edge' AND graph_edge_ref IS NOT NULL)"
        ." OR (evidence_type = 'map_feature' AND map_feature_ref IS NOT NULL)";

    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement('ALTER TABLE silver.evidence_items DROP CONSTRAINT IF EXISTS evidence_items_exactly_one_ref');
        DB::statement(
            'ALTER TABLE silver.evidence_items ADD CONSTRAINT evidence_items_exactly_one_ref'
            .' CHECK ('.self::REF_COUNT.' = 1 OR '.self::TOMBSTONE.')',
        );
        DB::statement('ALTER TABLE silver.evidence_items DROP CONSTRAINT IF EXISTS evidence_items_type_ref_consistent');
        DB::statement(
            'ALTER TABLE silver.evidence_items ADD CONSTRAINT evidence_items_type_ref_consistent'
            .' CHECK ('.self::TYPE_REF.' OR '.self::TOMBSTONE.')',
        );

        $this->replaceForeignKey('SET NULL');

        DB::statement(<<<'SQL'
            COMMENT ON COLUMN silver.evidence_items.passage_id IS
            'silver.document_passages(passage_id), ON DELETE SET NULL (§04e, SME-approved 2026-09-29). NULL on a document_passage row = the cited passage was deleted (a tombstone); readers return 410 evidence_source_deleted.'
        SQL);
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        $tombstones = (int) DB::scalar(
            'SELECT count(*) FROM silver.evidence_items WHERE '.self::TOMBSTONE,
        );
        $notValid = $tombstones > 0 ? ' NOT VALID' : '';

        $this->replaceForeignKey('RESTRICT');

        DB::statement('ALTER TABLE silver.evidence_items DROP CONSTRAINT IF EXISTS evidence_items_exactly_one_ref');
        DB::statement(
            'ALTER TABLE silver.evidence_items ADD CONSTRAINT evidence_items_exactly_one_ref'
            .' CHECK ('.self::REF_COUNT.' = 1)'.$notValid,
        );
        DB::statement('ALTER TABLE silver.evidence_items DROP CONSTRAINT IF EXISTS evidence_items_type_ref_consistent');
        DB::statement(
            'ALTER TABLE silver.evidence_items ADD CONSTRAINT evidence_items_type_ref_consistent'
            .' CHECK ('.self::TYPE_REF.')'.$notValid,
        );

        if ($tombstones > 0) {
            Log::warning('evidence_items CHECKs restored NOT VALID: tombstoned evidence rows kept', [
                'tombstones' => $tombstones,
            ]);
        }
    }

    private function replaceForeignKey(string $onDelete): void
    {
        DB::statement('ALTER TABLE silver.evidence_items DROP CONSTRAINT IF EXISTS '.self::FK);
        DB::statement(
            'ALTER TABLE silver.evidence_items ADD CONSTRAINT '.self::FK
            .' FOREIGN KEY (passage_id) REFERENCES silver.document_passages (passage_id)'
            .' ON DELETE '.$onDelete.' NOT VALID',
        );
        DB::statement('ALTER TABLE silver.evidence_items VALIDATE CONSTRAINT '.self::FK);
    }
};
