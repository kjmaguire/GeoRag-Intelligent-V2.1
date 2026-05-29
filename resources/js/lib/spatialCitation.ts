/**
 * Phase G.4 — Evidence Map Mode parser.
 *
 * Inspects a Citation's `source_chunk_id` and returns the spatial entity
 * the map should highlight when the citation is clicked.
 *
 * Citation source_chunk_id patterns (built in
 * `app/agent/response_assembler.py::_extract_source_id`):
 *
 *   `silver.collars:count=63:first=<collar_uuid>`
 *   `silver.lithology_logs:hole=36-1042:collar=<uuid>:intervals=N`
 *   `silver.lithology_logs:intervals=N`         (no collar info)
 *   `silver.samples:element=X:count=N`
 *   `silver.projects:slug=<slug>:company=<company>:curves=<n>`
 *   `pg_<canonical_type>:<source_id>:feature=<id>:pg_id=<uuid>`
 *   `neo4j:entities=N:first=<id>`
 *   `georag_reports:<report_id>:section=<sec>:chunk=<chunk_id>`
 *
 * For map highlighting, the interesting cases are:
 *   - hole_id present                       → highlight that specific drill collar
 *   - silver.collars count >= 1             → highlight every collar in the project
 *   - pg_<canonical_type>:feature=<id>     → highlight that PublicGeo feature
 *   - everything else                       → no map signal
 *
 * Returns `null` when the citation isn't spatial.
 */

export type SpatialPin =
    | { kind: 'hole_id'; hole_id: string }
    | { kind: 'collar_set'; first_collar_id: string }
    | { kind: 'pg_feature'; canonical_type: string; feature_id: string };

interface CitationLike {
    source_chunk_id?: string | null;
}

const HOLE_ID_RE = /\bhole=([A-Za-z0-9_-]+)/;
const FIRST_COLLAR_RE = /:first=([0-9a-f-]+)/i;
const PG_FEATURE_RE = /^pg_([a-z_]+):.*?:feature=([A-Za-z0-9_.-]+)/;

export function parseSpatialCitation(c: CitationLike | null | undefined): SpatialPin | null {
    if (!c || !c.source_chunk_id) return null;
    const src = c.source_chunk_id;

    // PG feature gets first crack — its prefix is the most specific.
    const pgMatch = src.match(PG_FEATURE_RE);
    if (pgMatch) {
        const [, canonicalType, featureId] = pgMatch;
        // 'unknown' is the sentinel value emitted when source_feature_id
        // wasn't populated — that's not actually a usable pin target.
        if (featureId && featureId !== 'unknown') {
            return { kind: 'pg_feature', canonical_type: canonicalType, feature_id: featureId };
        }
    }

    // hole_id (from downhole / lithology source IDs)
    const holeMatch = src.match(HOLE_ID_RE);
    if (holeMatch) {
        return { kind: 'hole_id', hole_id: holeMatch[1] };
    }

    // silver.collars with first= → highlight the entire project
    if (src.startsWith('silver.collars')) {
        const firstMatch = src.match(FIRST_COLLAR_RE);
        if (firstMatch) {
            return { kind: 'collar_set', first_collar_id: firstMatch[1] };
        }
    }

    return null;
}

/**
 * Return true when a citation is spatial (would produce a non-null pin).
 * Lets components decide whether to render an extra "View on map" affordance.
 */
export function isSpatialCitation(c: CitationLike | null | undefined): boolean {
    return parseSpatialCitation(c) !== null;
}
