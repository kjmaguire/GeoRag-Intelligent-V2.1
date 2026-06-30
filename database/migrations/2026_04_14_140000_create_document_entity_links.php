<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Phase 3.6 — cross-corpus linker storage (plan §07b).
 *
 * One row per (document_id, canonical_type, entity_id) link.  Append-only:
 * a row is NEVER updated in place.  When the linker re-evaluates and changes
 * its verdict, the old row's `superseded_at` is stamped and a new row is
 * inserted.  Preserves the full audit trail of linker decisions.
 *
 * V1 only ships deterministic signals (plan §07f locked):
 *   - `smdi_id_match`       — document body contains an SMDI number that
 *                             exists in pg_mineral_occurrence.
 *   - `drillhole_id_match`  — document body contains GOS_UNIQUE_DRILLHOLE_ID
 *                             or drillhole name matching pg_drillhole_collar.
 *   - `nts_filename_match`  — SMAD filename's NTS tile matches a sheet
 *                             within a jurisdiction (stored on document node,
 *                             not currently used to create REFERENCES edges).
 *
 * Structural / spatial / textual signals are V2 — schema already accommodates
 * them because `signals` is JSONB and `confidence` is a free NUMERIC.
 *
 * Sits in the public_geo schema alongside the canonical entity tables
 * it links to, so FK cascades behave predictably. The document_id side is a
 * UUID reference into silver.reports — kept as an opaque UUID (no FK) because
 * SMAD documents may ingest later via a different path than silver.reports
 * and we don't want the link table to block document deletion.
 */
return new class extends Migration
{
    public function up(): void
    {
        $canonicalTypes = "'mine','mineral_occurrence','drillhole_collar','resource_potential_zone'";

        DB::statement(<<<SQL
            CREATE TABLE IF NOT EXISTS public_geo.document_entity_links (
                id                 BIGSERIAL     PRIMARY KEY,
                document_id        UUID          NOT NULL,
                document_filename  VARCHAR(512)  NULL,
                canonical_type     VARCHAR(32)   NOT NULL
                    CHECK (canonical_type IN ({$canonicalTypes})),
                entity_id          UUID          NOT NULL,
                confidence         NUMERIC(4,3)  NOT NULL
                    CHECK (confidence >= 0.0 AND confidence <= 1.0),
                signals            JSONB         NOT NULL DEFAULT '[]'::jsonb,
                extracted_context  TEXT          NULL,
                established_at     TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
                established_by     VARCHAR(64)   NOT NULL,
                superseded_at      TIMESTAMPTZ   NULL,
                supersedes_id      BIGINT        NULL
                    REFERENCES public_geo.document_entity_links(id) ON DELETE SET NULL
            )
        SQL);

        DB::statement("COMMENT ON TABLE public_geo.document_entity_links IS 'Append-only audit log of cross-corpus document↔entity links (plan §07b). Superseded rows are kept; new verdicts insert a new row and set the old row''s superseded_at.'");

        // ── Indexes ──────────────────────────────────────────────────────
        // Most queries are one of:
        //   1. "all active links for this document" (document card: References N mines / M occurrences…)
        //   2. "all active links for this entity"   (entity card: Referenced in N reports)
        //   3. "all historical links"               (audit)
        DB::statement('
            CREATE INDEX IF NOT EXISTS idx_del_document_active
                ON public_geo.document_entity_links (document_id, canonical_type)
                WHERE superseded_at IS NULL
        ');
        DB::statement('
            CREATE INDEX IF NOT EXISTS idx_del_entity_active
                ON public_geo.document_entity_links (entity_id, canonical_type)
                WHERE superseded_at IS NULL
        ');
        DB::statement('
            CREATE INDEX IF NOT EXISTS idx_del_established_by
                ON public_geo.document_entity_links (established_by)
        ');

        // At most one active link per (document, canonical_type, entity) tuple.
        // Superseded rows are excluded from the uniqueness check — that's the
        // append-only invariant.
        DB::statement('
            CREATE UNIQUE INDEX IF NOT EXISTS uq_del_active_triple
                ON public_geo.document_entity_links (document_id, canonical_type, entity_id)
                WHERE superseded_at IS NULL
        ');
    }

    public function down(): void
    {
        DB::statement('DROP TABLE IF EXISTS public_geo.document_entity_links CASCADE');
    }
};
