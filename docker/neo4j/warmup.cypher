// GeoRAG Neo4j Community Edition — Manual Page Cache Warmup
// Runs on container startup to populate page cache (Enterprise warmup.enable is unavailable)
// Owned by: graph-engineer | Executed by: neo4j-warmup init container
//
// Community Edition has no db.memory.pagecache.warmup.enable or .preload settings —
// those are Enterprise-only. Instead, this script forces representative node and
// relationship store reads so the OS page cache is warm before real traffic arrives.
//
// Update this file when new relationship types are added to the schema (Section 04f).
// Add a traversal for each new relationship so warmup coverage stays complete.

// ---------------------------------------------------------------------------
// 1. Full node store scan
//    Forces every node record page into the page cache. This is the broadest
//    possible warmup — all 7 entity types are covered in one pass.
// ---------------------------------------------------------------------------
MATCH (n) RETURN count(n);

// ---------------------------------------------------------------------------
// 2. Project → DrillHole traversals (highest-frequency query pattern)
//    HAS_HOLE is the most traversed relationship in the graph. Warming these
//    relationship store pages eliminates the cold-start penalty on the first
//    batch of strip-log and collar-map queries after startup.
// ---------------------------------------------------------------------------
MATCH (p:Project)-[:HAS_HOLE]->(h:DrillHole) RETURN count(h);

// ---------------------------------------------------------------------------
// 3. DrillHole → lithology (strip log rendering)
//    HAS_LITHOLOGY records are read in bulk when rendering drill logs.
//    LIMIT guards against very large datasets stalling the warmup container.
// ---------------------------------------------------------------------------
MATCH (h:DrillHole)-[:HAS_LITHOLOGY]->(l) RETURN count(l) LIMIT 10000;

// ---------------------------------------------------------------------------
// 4. Report → Formation cross-references (knowledge graph queries)
//    REFERENCES_FORMATION is the primary relationship for provenance lookups
//    and hallucination-prevention entity resolution (Section 04i, layer 4).
// ---------------------------------------------------------------------------
MATCH (r:Report)-[:REFERENCES_FORMATION]->(f:Formation) RETURN count(*);

// ---------------------------------------------------------------------------
// 5. Multi-hop path warmup (RAG traversal fan-out, depth 1–3)
//    The RAG pipeline performs 1–3 hop traversals when building context
//    windows. LIMIT 1000 prevents a full graph walk on large datasets while
//    still pulling the most-connected subgraph pages into cache.
// ---------------------------------------------------------------------------
MATCH path=(p:Project)-[*1..3]->(n) RETURN count(path) LIMIT 1000;

// ---------------------------------------------------------------------------
// 6. Mineral occurrence relationships
//    HOSTS_MINERALIZATION and INTERSECTED_BY_HOLE are read together during
//    grade/tonnage queries. Warm both sides of the relationship.
// ---------------------------------------------------------------------------
MATCH (f:Formation)-[:HOSTS_MINERALIZATION]->(m:MineralOccurrence) RETURN count(m) LIMIT 5000;
MATCH (m:MineralOccurrence)-[:INTERSECTED_BY_HOLE]->(h:DrillHole) RETURN count(h) LIMIT 5000;

// ---------------------------------------------------------------------------
// 7. Geophysical survey linkages
//    HAS_SURVEY is used in anomaly-correlation queries. Warming project →
//    survey pages reduces latency on the first geophysics query post-boot.
// ---------------------------------------------------------------------------
MATCH (p:Project)-[:HAS_SURVEY]->(s:GeophysicalSurvey) RETURN count(s);

// ---------------------------------------------------------------------------
// 8. Publication citation graph
//    CITES_DATA_FROM and CITES_DRILLHOLE are traversed during provenance
//    chain resolution. Warm these relationship pages last — they are the
//    least time-critical but still benefit from cache residency.
// ---------------------------------------------------------------------------
MATCH (pub:Publication)-[:CITES_DATA_FROM]->(r:Report) RETURN count(*) LIMIT 2000;
MATCH (r:Report)-[:CITES_DRILLHOLE]->(h:DrillHole) RETURN count(*) LIMIT 2000;

// ---------------------------------------------------------------------------
// 9. Public Geoscience entity warmup (Section 04g)
//    PG nodes carry a secondary :PublicGeo label alongside their primary
//    type. These queries are the RAG chat tool's backbone — every
//    "what occurrences are in this area?" / "any drillholes near this
//    target?" question hits these paths. Warm them explicitly so the
//    first chat query after a cold start doesn't pay the seek penalty.
// ---------------------------------------------------------------------------
MATCH (n:PublicGeo) RETURN count(n);
MATCH (m:Mine:PublicGeo) RETURN count(m);
MATCH (o:MineralOccurrence:PublicGeo) RETURN count(o);
MATCH (d:DrillHole:PublicGeo) RETURN count(d) LIMIT 50000;
MATCH (z:ResourcePotentialZone) RETURN count(z);

// PG → source / jurisdiction lookups (every citation assembly resolves these).
MATCH (n:PublicGeo)-[:SOURCED_FROM]->(s:PublicGeoSource) RETURN count(s) LIMIT 50000;
MATCH (s:PublicGeoSource)-[:PUBLISHED_BY]->(j:Jurisdiction) RETURN count(j);

// PG commodity graph (commodity-grouping filtering drives the map
// commodity chips + chat "show me uranium occurrences" queries).
MATCH (n:PublicGeo)-[r:HAS_COMMODITY|HAS_PRIMARY_COMMODITY|HAS_ASSOCIATED_COMMODITY]->(c:Commodity) RETURN count(r) LIMIT 20000;
MATCH (z:ResourcePotentialZone)-[:COVERS_AREA_FOR]->(c:Commodity) RETURN count(*);

// Cross-corpus linker mirror — every document→entity citation is walked
// when the chat agent builds response-level attribution.
MATCH (doc:Document)-[:REFERENCES]->(n:PublicGeo) RETURN count(*) LIMIT 10000;
