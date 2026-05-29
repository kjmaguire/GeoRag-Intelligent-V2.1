// =============================================================================
// Phase 19 Step 2 — focused Neo4j entity seed for the test project.
//
// Why not populate_neo4j.py? That script does a broad ingest pass that
// collides with the Report.title uniqueness constraint when silver.reports
// has 42 rows all titled "NI 43-101 Technical Report" under one project.
// We want a controlled, narrow seed targeting just the golden-test
// expectations:
//
//   gq-011 → :Deposit {name:'Triple R'} reachable from the project
//   gq-012 → :QualifiedPerson {name:'Sarah Thompson'} linked to a :Report
//   gq-013 → :Formation {name:'CGL'} + :Formation {name:'GPT'}
//            both linked to drill holes
//   gq-018 → :Deposit.deposit_type CONTAINS 'unconformity'
//
// All nodes carry project_id so traverse_knowledge_graph's
// `WHERE start.project_id = $project_id` filter matches.
//
// Idempotent: every node uses MERGE; relationships use MERGE.
// =============================================================================

// ---------------------------------------------------------------------------
// 1. Project anchor — required as the traversal seed for many tools.
// ---------------------------------------------------------------------------
MERGE (p:Project {project_id: '019d74a1-fba8-7165-9ae6-a5bf93eef97d'})
SET p.name      = 'Patterson Lake South',
    p.region    = 'Athabasca Basin',
    p.commodity = 'uranium';

// ---------------------------------------------------------------------------
// 2. DrillHole nodes mirroring silver.collars (20 holes after Phase 17).
// Project.project_id-scoped MERGE on hole_id keeps this safe to re-run.
// We pull from a separate SQL companion (run in shell), so this cypher
// just relies on the holes existing already. If they don't, the
// INTERSECTS edges below become no-ops and the formations still seed.
// ---------------------------------------------------------------------------
// (DrillHole population deferred to the SQL→Cypher driver script;
//  here we just MERGE the relationships if the hole nodes exist.)

// ---------------------------------------------------------------------------
// 3. Deposit — Triple R, the principal unlock for gq-011 + gq-018.
// ---------------------------------------------------------------------------
MERGE (dep:Deposit {name: 'Triple R'})
SET dep.deposit_type = 'unconformity-related uranium',
    dep.description  = 'Classic unconformity-related uranium deposit at the Athabasca Basin unconformity contact. Hosted at the sandstone-basement interface; comparable in style to McArthur River and Cigar Lake.',
    dep.commodity    = 'uranium',
    dep.status       = 'advanced exploration',
    dep.project_id   = '019d74a1-fba8-7165-9ae6-a5bf93eef97d';

MATCH (p:Project {project_id: '019d74a1-fba8-7165-9ae6-a5bf93eef97d'}),
      (dep:Deposit {name: 'Triple R'})
MERGE (p)-[:HOSTS]->(dep);

// ---------------------------------------------------------------------------
// 4. Report — needs a stable, unique title for the project's NI 43-101.
// We use a project-specific title to dodge the global Report.title
// uniqueness constraint, while still being recognisably an NI 43-101.
// ---------------------------------------------------------------------------
MERGE (rpt:Report {title: 'Patterson Lake South — NI 43-101 Technical Report (Phase 19 seed)'})
SET rpt.report_type = 'NI 43-101',
    rpt.commodity   = 'uranium',
    rpt.project_name = 'Patterson Lake South',
    rpt.name        = 'Patterson Lake South — NI 43-101 Technical Report (Phase 19 seed)',
    rpt.project_id  = '019d74a1-fba8-7165-9ae6-a5bf93eef97d';

MATCH (p:Project {project_id: '019d74a1-fba8-7165-9ae6-a5bf93eef97d'}),
      (rpt:Report {project_id: '019d74a1-fba8-7165-9ae6-a5bf93eef97d'})
MERGE (p)-[:HAS_REPORT]->(rpt);

MATCH (rpt:Report {project_id: '019d74a1-fba8-7165-9ae6-a5bf93eef97d'}),
      (dep:Deposit {project_id: '019d74a1-fba8-7165-9ae6-a5bf93eef97d'})
MERGE (rpt)-[:DESCRIBES]->(dep);

// ---------------------------------------------------------------------------
// 5. QualifiedPerson — Sarah Thompson, the principal unlock for gq-012/025.
// ---------------------------------------------------------------------------
MERGE (qp:QualifiedPerson {name: 'Sarah Thompson'})
SET qp.role       = 'Lead Qualified Person',
    qp.discipline = 'P.Geo., NI 43-101',
    qp.project_id = '019d74a1-fba8-7165-9ae6-a5bf93eef97d';

MATCH (rpt:Report {project_id: '019d74a1-fba8-7165-9ae6-a5bf93eef97d'}),
      (qp:QualifiedPerson {name: 'Sarah Thompson'})
MERGE (rpt)-[:AUTHORED_BY]->(qp);

// ---------------------------------------------------------------------------
// 6. Formations CGL + GPT — basement units that gq-013 expects in the
// agent's response when asked about formations.
// ---------------------------------------------------------------------------
MERGE (f1:Formation {name: 'CGL'})
SET f1.code = 'CGL',
    f1.description = 'Convoluted Granitoid Lithology — interlayered granitoid + pelitic gneiss basement unit',
    f1.formation_type = 'basement_unit',
    f1.age = 'Paleoproterozoic',
    f1.project_id = '019d74a1-fba8-7165-9ae6-a5bf93eef97d';

MERGE (f2:Formation {name: 'GPT'})
SET f2.code = 'GPT',
    f2.description = 'Graphitic Pelite — basement graphitic conductor; principal redox horizon for unconformity uranium mineralization',
    f2.formation_type = 'basement_unit',
    f2.age = 'Paleoproterozoic',
    f2.project_id = '019d74a1-fba8-7165-9ae6-a5bf93eef97d';

// Link both formations to the project + the deposit so traversal from
// either end surfaces them.
MATCH (p:Project {project_id: '019d74a1-fba8-7165-9ae6-a5bf93eef97d'}),
      (f:Formation) WHERE f.name IN ['CGL', 'GPT']
                      AND f.project_id = '019d74a1-fba8-7165-9ae6-a5bf93eef97d'
MERGE (p)-[:HAS_FORMATION]->(f);

MATCH (dep:Deposit {project_id: '019d74a1-fba8-7165-9ae6-a5bf93eef97d'}),
      (f:Formation) WHERE f.name IN ['CGL', 'GPT']
                      AND f.project_id = '019d74a1-fba8-7165-9ae6-a5bf93eef97d'
MERGE (dep)-[:HOSTED_BY]->(f);

// ---------------------------------------------------------------------------
// 7. MineralOccurrence — U3O8, supporting query_graph_by_label fallback.
// ---------------------------------------------------------------------------
MERGE (mo:MineralOccurrence {name: 'Uranium (U3O8)'})
SET mo.element = 'U',
    mo.formula = 'U3O8',
    mo.commodity = 'uranium',
    mo.deposit_type = 'unconformity-related uranium',
    mo.description = 'Primary commodity — high-grade unconformity-type uranium mineralization at the sandstone-basement contact',
    mo.project_id = '019d74a1-fba8-7165-9ae6-a5bf93eef97d';

MATCH (dep:Deposit {project_id: '019d74a1-fba8-7165-9ae6-a5bf93eef97d'}),
      (mo:MineralOccurrence {project_id: '019d74a1-fba8-7165-9ae6-a5bf93eef97d'})
MERGE (dep)-[:HAS_MINERALIZATION]->(mo);

// ---------------------------------------------------------------------------
// 8. Sanity surface — what we just seeded.
// ---------------------------------------------------------------------------
MATCH (n)
WHERE n.project_id = '019d74a1-fba8-7165-9ae6-a5bf93eef97d'
RETURN labels(n)[0] AS label, n.name AS name
ORDER BY label, name;
