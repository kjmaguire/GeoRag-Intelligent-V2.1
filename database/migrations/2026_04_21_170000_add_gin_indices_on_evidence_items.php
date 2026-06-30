<?php

/**
 * Module 6 Phase B Chunk 1 — Add GIN indices on silver.evidence_items JSONB columns.
 *
 * Date: 2026-04-21.
 *
 * Purpose
 * -------
 * Partial GIN indices on the three JSONB reference columns in silver.evidence_items.
 * Added now while the table has zero rows (zero cost, no table-lock risk).  Once
 * Module 3 B8.5 enables non-passage evidence emission the indices will already be
 * in place — adding them post-population risks a long ACCESS SHARE lock on a
 * populated table.
 *
 * The JSONB operator patterns these indices support:
 *   - @> containment queries (inspector: does this structured_ref match {...}?)
 *   - ? key-existence queries
 *   - ->> text extraction queries (slower without GIN; btree won't help here)
 *
 * NULL-pruning WHEREs keep index size to zero until rows arrive.  If functional
 * JSONB path indices are needed post-ingestion, add them in a separate migration
 * once hot-path queries are known.
 *
 * Pre-condition
 * -------------
 * silver.evidence_items was created in migration 2026_04_20_140000.
 * The three JSONB columns (structured_ref, graph_edge_ref, map_feature_ref)
 * confirmed present.
 *
 * Rollback
 * --------
 * Drop indices in same order (reverse is identical for independent objects).
 *
 * Module 6 intake item 2 — RESOLVED 2026-04-21.
 */

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

return new class extends Migration
{
    public function up(): void
    {
        // Partial GIN index: structured_ref JSONB — only non-NULL rows.
        // Supports Module 6 inspector queries filtering by schema/table/PK.
        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_evidence_items_structured_ref_gin
                 ON silver.evidence_items USING GIN (structured_ref)
                 WHERE structured_ref IS NOT NULL',
        );

        // Partial GIN index: graph_edge_ref JSONB — only non-NULL rows.
        // Supports graph-edge evidence lookup by start_node_id / rel_type.
        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_evidence_items_graph_edge_ref_gin
                 ON silver.evidence_items USING GIN (graph_edge_ref)
                 WHERE graph_edge_ref IS NOT NULL',
        );

        // Partial GIN index: map_feature_ref JSONB — only non-NULL rows.
        // Supports map-feature evidence lookup by tile_function / bbox.
        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_evidence_items_map_feature_ref_gin
                 ON silver.evidence_items USING GIN (map_feature_ref)
                 WHERE map_feature_ref IS NOT NULL',
        );
    }

    public function down(): void
    {
        DB::statement('DROP INDEX IF EXISTS silver.idx_evidence_items_structured_ref_gin');
        DB::statement('DROP INDEX IF EXISTS silver.idx_evidence_items_graph_edge_ref_gin');
        DB::statement('DROP INDEX IF EXISTS silver.idx_evidence_items_map_feature_ref_gin');
    }
};
