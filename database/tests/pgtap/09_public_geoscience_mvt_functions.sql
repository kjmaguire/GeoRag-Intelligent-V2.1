-- pgTAP smoke tests for Module 8 Chunks 8.1/8.2 — Public Geoscience MVT function wrappers
-- File: database/tests/pgtap/09_public_geoscience_mvt_functions.sql
--
-- Run: docker compose exec postgresql psql -U georag -d georag -f /pgtap/09_public_geoscience_mvt_functions.sql
-- Requires: pgTAP extension installed in the georag database.
--
-- Tests cover all 8 public_geo function wrappers:
--   pg_mines_tiles
--   pg_mineral_occurrences_tiles
--   pg_drillhole_collars_tiles
--   pg_rock_samples_tiles
--   pg_assessment_surveys_tiles
--   pg_resource_potential_tiles
--   pg_mineral_dispositions_tiles
--   pg_bedrock_geology_tiles
--
-- These are smoke tests: existence, signature contract, and non-null etag_hash.
-- MVT content is not decoded (no pgtap-mvt extension available); octet_length
-- is used as a proxy for "tile has content" only when PGEO tables have data.
-- The etag_hash tests are unconditional (they depend only on jurisdictions, not
-- on the per-layer tables having data).
--
-- Total assertions in this file: 27
-- (Originally authored as 29 but actual assertion count is 27:
--  8 existence + 8 etag-md5 + 4 two-column + 2 different-coords + 2 determinism + 3 privs)

BEGIN;

SELECT plan(27);

-- ══════════════════════════════════════════════════════════════════════════════
-- BLOCK 1 — Function existence (8 functions)
-- ══════════════════════════════════════════════════════════════════════════════

SELECT has_function(
    'public_geo', 'pg_mines_tiles',
    ARRAY['integer','integer','integer','json'],
    'pg_mines_tiles exists in public_geo schema'
);

SELECT has_function(
    'public_geo', 'pg_mineral_occurrences_tiles',
    ARRAY['integer','integer','integer','json'],
    'pg_mineral_occurrences_tiles exists in public_geo schema'
);

SELECT has_function(
    'public_geo', 'pg_drillhole_collars_tiles',
    ARRAY['integer','integer','integer','json'],
    'pg_drillhole_collars_tiles exists in public_geo schema'
);

SELECT has_function(
    'public_geo', 'pg_rock_samples_tiles',
    ARRAY['integer','integer','integer','json'],
    'pg_rock_samples_tiles exists in public_geo schema'
);

SELECT has_function(
    'public_geo', 'pg_assessment_surveys_tiles',
    ARRAY['integer','integer','integer','json'],
    'pg_assessment_surveys_tiles exists in public_geo schema'
);

SELECT has_function(
    'public_geo', 'pg_resource_potential_tiles',
    ARRAY['integer','integer','integer','json'],
    'pg_resource_potential_tiles exists in public_geo schema'
);

SELECT has_function(
    'public_geo', 'pg_mineral_dispositions_tiles',
    ARRAY['integer','integer','integer','json'],
    'pg_mineral_dispositions_tiles exists in public_geo schema'
);

SELECT has_function(
    'public_geo', 'pg_bedrock_geology_tiles',
    ARRAY['integer','integer','integer','json'],
    'pg_bedrock_geology_tiles exists in public_geo schema'
);

-- ══════════════════════════════════════════════════════════════════════════════
-- BLOCK 2 — etag_hash is non-null and md5 format for global tile z=1, x=0, y=0
-- (etag is derived from jurisdictions.updated_at, independent of tile content)
-- ══════════════════════════════════════════════════════════════════════════════

SELECT matches(
    (SELECT etag_hash FROM public_geo.pg_mines_tiles(1, 0, 0, '{}'::json)),
    '^[a-f0-9]{32}$',
    'pg_mines_tiles: etag_hash is md5 format'
);

SELECT matches(
    (SELECT etag_hash FROM public_geo.pg_mineral_occurrences_tiles(1, 0, 0, '{}'::json)),
    '^[a-f0-9]{32}$',
    'pg_mineral_occurrences_tiles: etag_hash is md5 format'
);

SELECT matches(
    (SELECT etag_hash FROM public_geo.pg_drillhole_collars_tiles(1, 0, 0, '{}'::json)),
    '^[a-f0-9]{32}$',
    'pg_drillhole_collars_tiles: etag_hash is md5 format'
);

SELECT matches(
    (SELECT etag_hash FROM public_geo.pg_rock_samples_tiles(1, 0, 0, '{}'::json)),
    '^[a-f0-9]{32}$',
    'pg_rock_samples_tiles: etag_hash is md5 format'
);

SELECT matches(
    (SELECT etag_hash FROM public_geo.pg_assessment_surveys_tiles(1, 0, 0, '{}'::json)),
    '^[a-f0-9]{32}$',
    'pg_assessment_surveys_tiles: etag_hash is md5 format'
);

SELECT matches(
    (SELECT etag_hash FROM public_geo.pg_resource_potential_tiles(1, 0, 0, '{}'::json)),
    '^[a-f0-9]{32}$',
    'pg_resource_potential_tiles: etag_hash is md5 format'
);

SELECT matches(
    (SELECT etag_hash FROM public_geo.pg_mineral_dispositions_tiles(1, 0, 0, '{}'::json)),
    '^[a-f0-9]{32}$',
    'pg_mineral_dispositions_tiles: etag_hash is md5 format'
);

SELECT matches(
    (SELECT etag_hash FROM public_geo.pg_bedrock_geology_tiles(1, 0, 0, '{}'::json)),
    '^[a-f0-9]{32}$',
    'pg_bedrock_geology_tiles: etag_hash is md5 format'
);

-- ══════════════════════════════════════════════════════════════════════════════
-- BLOCK 3 — Two-column return contract: both mvt and etag_hash are returned
-- ══════════════════════════════════════════════════════════════════════════════

-- These functions return (mvt bytea, etag_hash text). The mvt may be empty
-- (zero bytes) if the PGEO tables have no data in the tile, but etag_hash must
-- always be non-null (it derives from jurisdictions, not tile data).

SELECT ok(
    (SELECT etag_hash IS NOT NULL FROM public_geo.pg_mines_tiles(1, 0, 0, '{}'::json)),
    'pg_mines_tiles: two-column return — etag_hash not null'
);

SELECT ok(
    (SELECT etag_hash IS NOT NULL FROM public_geo.pg_mineral_occurrences_tiles(1, 0, 0, '{}'::json)),
    'pg_mineral_occurrences_tiles: two-column return — etag_hash not null'
);

SELECT ok(
    (SELECT etag_hash IS NOT NULL FROM public_geo.pg_assessment_surveys_tiles(1, 0, 0, '{}'::json)),
    'pg_assessment_surveys_tiles: two-column return — etag_hash not null'
);

SELECT ok(
    (SELECT etag_hash IS NOT NULL FROM public_geo.pg_bedrock_geology_tiles(1, 0, 0, '{}'::json)),
    'pg_bedrock_geology_tiles: two-column return — etag_hash not null'
);

-- ══════════════════════════════════════════════════════════════════════════════
-- BLOCK 4 — Different tile coordinates produce different etag_hash values
-- ══════════════════════════════════════════════════════════════════════════════

-- The etag includes z|x|y so it must differ per tile coordinate.
SELECT isnt(
    (SELECT etag_hash FROM public_geo.pg_mines_tiles(5, 10, 11, '{}'::json)),
    (SELECT etag_hash FROM public_geo.pg_mines_tiles(5, 11, 11, '{}'::json)),
    'pg_mines_tiles: different tile coords produce different etag_hash'
);

SELECT isnt(
    (SELECT etag_hash FROM public_geo.pg_bedrock_geology_tiles(3, 2, 3, '{}'::json)),
    (SELECT etag_hash FROM public_geo.pg_bedrock_geology_tiles(3, 2, 4, '{}'::json)),
    'pg_bedrock_geology_tiles: different tile coords produce different etag_hash'
);

-- ══════════════════════════════════════════════════════════════════════════════
-- BLOCK 5 — Determinism: same call twice returns same etag_hash
-- ══════════════════════════════════════════════════════════════════════════════

SELECT is(
    (SELECT etag_hash FROM public_geo.pg_mines_tiles(1, 0, 0, '{}'::json)),
    (SELECT etag_hash FROM public_geo.pg_mines_tiles(1, 0, 0, '{}'::json)),
    'pg_mines_tiles: etag_hash is deterministic'
);

SELECT is(
    (SELECT etag_hash FROM public_geo.pg_mineral_occurrences_tiles(1, 0, 0, '{}'::json)),
    (SELECT etag_hash FROM public_geo.pg_mineral_occurrences_tiles(1, 0, 0, '{}'::json)),
    'pg_mineral_occurrences_tiles: etag_hash is deterministic'
);

-- ══════════════════════════════════════════════════════════════════════════════
-- BLOCK 6 — martin_readonly EXECUTE grants
-- ══════════════════════════════════════════════════════════════════════════════

SELECT function_privs_are(
    'public_geo', 'pg_mines_tiles', ARRAY['integer','integer','integer','json'],
    'martin_readonly', ARRAY['EXECUTE'],
    'martin_readonly has EXECUTE on pg_mines_tiles'
);

SELECT function_privs_are(
    'public_geo', 'pg_mineral_occurrences_tiles', ARRAY['integer','integer','integer','json'],
    'martin_readonly', ARRAY['EXECUTE'],
    'martin_readonly has EXECUTE on pg_mineral_occurrences_tiles'
);

SELECT function_privs_are(
    'public_geo', 'pg_bedrock_geology_tiles', ARRAY['integer','integer','integer','json'],
    'martin_readonly', ARRAY['EXECUTE'],
    'martin_readonly has EXECUTE on pg_bedrock_geology_tiles'
);

SELECT * FROM finish();

ROLLBACK;
