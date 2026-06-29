// ============================================================================
// GeoRAG Concept Graph — CGI geological vocabulary hierarchy
// ============================================================================
// Run via: docker exec georag-neo4j cypher-shell -u neo4j -p <pass> -f /seed_concept_graph.cypher
// Or paste directly in Neo4j Browser.
//
// This file captures the static CGI vocabulary structure that the Python
// seeder (seed_concept_graph.py) doesn't generate: geological age hierarchy,
// deposit model type hierarchy, and tectonic setting groupings.
// ============================================================================

// ── Geological age hierarchy ─────────────────────────────────────────────────

MERGE (:GeologyConceptClass {name: 'geological_age'});

// Eons
MERGE (archean:GeologyConcept {canonical_term: 'Archean', ontology_class: 'geological_age', cgi_uri: 'http://resource.geosciml.org/classifier/ics/ischart/Archean'});
MERGE (proterozoic:GeologyConcept {canonical_term: 'Proterozoic', ontology_class: 'geological_age', cgi_uri: 'http://resource.geosciml.org/classifier/ics/ischart/Proterozoic'});
MERGE (phanerozoic:GeologyConcept {canonical_term: 'Phanerozoic', ontology_class: 'geological_age'});

// Proterozoic subdivisions
MERGE (paleo_proto:GeologyConcept {canonical_term: 'Paleoproterozoic', ontology_class: 'geological_age'})
MERGE (paleo_proto)-[:IS_TYPE_OF]->(proterozoic);

MERGE (meso_proto:GeologyConcept {canonical_term: 'Mesoproterozoic', ontology_class: 'geological_age'})
MERGE (meso_proto)-[:IS_TYPE_OF]->(proterozoic);

MERGE (neo_proto:GeologyConcept {canonical_term: 'Neoproterozoic', ontology_class: 'geological_age'})
MERGE (neo_proto)-[:IS_TYPE_OF]->(proterozoic);

// Phanerozoic eras
MERGE (paleozoic:GeologyConcept {canonical_term: 'Paleozoic', ontology_class: 'geological_age'})
MERGE (paleozoic)-[:IS_TYPE_OF]->(phanerozoic);

MERGE (mesozoic:GeologyConcept {canonical_term: 'Mesozoic', ontology_class: 'geological_age'})
MERGE (mesozoic)-[:IS_TYPE_OF]->(phanerozoic);

MERGE (cenozoic:GeologyConcept {canonical_term: 'Cenozoic', ontology_class: 'geological_age'})
MERGE (cenozoic)-[:IS_TYPE_OF]->(phanerozoic);

// ── Deposit model hierarchy ───────────────────────────────────────────────────

MERGE (:GeologyConceptClass {name: 'deposit_model'});

// Uranium deposit subtypes
MERGE (unconformity_u:GeologyConcept {canonical_term: 'Unconformity-related uranium', ontology_class: 'deposit_model'});
MERGE (athabasca:GeologyConcept {canonical_term: 'Athabasca uranium', ontology_class: 'deposit_model'})
MERGE (athabasca)-[:IS_TYPE_OF]->(unconformity_u);

MERGE (rollfront_u:GeologyConcept {canonical_term: 'Roll-front uranium', ontology_class: 'deposit_model'})
MERGE (rollfront_u)-[:IS_TYPE_OF]->(unconformity_u);

// Gold deposit subtypes
MERGE (orogenic_au:GeologyConcept {canonical_term: 'Orogenic gold', ontology_class: 'deposit_model'});
MERGE (epithermal_au:GeologyConcept {canonical_term: 'Epithermal gold', ontology_class: 'deposit_model'});
MERGE (porphyry_cu:GeologyConcept {canonical_term: 'Porphyry copper', ontology_class: 'deposit_model'});

// ── Tectonic settings ────────────────────────────────────────────────────────

MERGE (:GeologyConceptClass {name: 'tectonic_setting'});
MERGE (:GeologyConcept {canonical_term: 'Cratonic', ontology_class: 'tectonic_setting'});
MERGE (:GeologyConcept {canonical_term: 'Rift basin', ontology_class: 'tectonic_setting'});
MERGE (:GeologyConcept {canonical_term: 'Fold and thrust belt', ontology_class: 'tectonic_setting'});
MERGE (:GeologyConcept {canonical_term: 'Volcanic arc', ontology_class: 'tectonic_setting'});
MERGE (:GeologyConcept {canonical_term: 'Back-arc basin', ontology_class: 'tectonic_setting'});

// ── Connect deposit models to tectonic settings ──────────────────────────────

MATCH (atha:GeologyConcept {canonical_term: 'Athabasca uranium'})
MATCH (crat:GeologyConcept {canonical_term: 'Cratonic'})
MERGE (atha)-[:ASSOCIATED_WITH]->(crat);

MATCH (orogen:GeologyConcept {canonical_term: 'Orogenic gold'})
MATCH (fold:GeologyConcept {canonical_term: 'Fold and thrust belt'})
MERGE (orogen)-[:ASSOCIATED_WITH]->(fold);
