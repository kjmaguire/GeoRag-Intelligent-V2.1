"""One-shot script: seed GeologyConcept nodes + concept edges into Neo4j
from silver.geological_ontology_terms.

Creates:
  - (:GeologyConcept {name, canonical_term, ontology_class, cgi_uri, pg_id})
    nodes for every seeded ontology term
  - (:GeologyConceptClass {name}) root nodes per ontology class
  - [:IS_TYPE_OF] edges from each concept to its class root
  - [:RELATED_TO] edges where deposit model names contain commodity names
  - [:INSTANCE_OF] edges from existing :Commodity nodes to matching GeologyConcepts
  - Full-text index on GeologyConcept for query expansion

Run inside georag-fastapi container:
    python3 /app/scripts/seed_concept_graph.py
"""
from __future__ import annotations

import asyncio
import os

PG_DSN = "postgresql://georag:OMljaORhiA7RGQN3ilfemNWpezF9waU@pgbouncer:6432/georag"
NEO4J_URI = "bolt://neo4j:7687"
NEO4J_USER = "neo4j"
NEO4J_PASS = "24kNKWLbX20bgHEXAuMSGjCp228LIfUE"


async def main() -> None:
    import asyncpg
    from neo4j import GraphDatabase

    # ── 1. Fetch ontology terms from PG ───────────────────────────────────
    print("Connecting to Postgres...")
    conn = await asyncpg.connect(PG_DSN)
    try:
        rows = await conn.fetch(
            "SELECT term_id::text AS id, canonical_term, "
            "       class AS ontology_class, "
            "       COALESCE(payload->>'cgi_uri', '') AS cgi_uri "
            "FROM silver.geological_ontology_terms "
            "ORDER BY class, canonical_term"
        )
    finally:
        await conn.close()

    if not rows:
        print("No ontology terms found in silver.geological_ontology_terms.")
        print("Run the CGI vocab seeders first (Dagster assets).")
        return

    print(f"Loaded {len(rows)} ontology terms from PG.")

    # Group by class
    by_class: dict[str, list] = {}
    for r in rows:
        by_class.setdefault(r["ontology_class"], []).append(r)
    print(f"Classes: {list(by_class.keys())}")

    # ── 2. Neo4j seeding ──────────────────────────────────────────────────
    print("Connecting to Neo4j...")
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))
    total_created = 0
    total_matched = 0

    try:
        with driver.session() as session:

            # ── 2a. Create GeologyConcept nodes (idempotent MERGE) ────────
            print("\nStep 1: Creating GeologyConcept nodes...")
            for row in rows:
                result = session.run(
                    """
                    MERGE (c:GeologyConcept {canonical_term: $canonical_term})
                    ON CREATE SET
                        c.name         = $canonical_term,
                        c.ontology_class = $ontology_class,
                        c.cgi_uri      = $cgi_uri,
                        c.pg_id        = $pg_id
                    ON MATCH SET
                        c.ontology_class = $ontology_class,
                        c.cgi_uri        = $cgi_uri
                    RETURN c, (count(*) - 1) AS was_existing
                    """,
                    canonical_term=row["canonical_term"],
                    ontology_class=row["ontology_class"],
                    cgi_uri=row["cgi_uri"],
                    pg_id=row["id"],
                )
                record = result.single()
                if record and record["was_existing"] == 0:
                    total_created += 1
                else:
                    total_matched += 1
            print(f"  GeologyConcept nodes: {total_created} created, {total_matched} already existed.")

            # ── 2b. Create class root nodes + IS_TYPE_OF edges ────────────
            print("\nStep 2: Creating class root nodes + IS_TYPE_OF edges...")
            for class_name, terms in by_class.items():
                session.run(
                    """
                    MERGE (root:GeologyConceptClass {name: $class_name})
                    WITH root
                    MATCH (c:GeologyConcept {ontology_class: $class_name})
                    MERGE (c)-[:IS_TYPE_OF]->(root)
                    """,
                    class_name=class_name,
                )
                print(f"  {class_name}: {len(terms)} concepts → IS_TYPE_OF → class root")

            # ── 2c. RELATED_TO between deposit models and commodities ─────
            print("\nStep 3: Wiring RELATED_TO (deposit model → commodity)...")
            result = session.run(
                """
                MATCH (deposit:GeologyConcept {ontology_class: 'deposit_model'})
                MATCH (commodity:GeologyConcept {ontology_class: 'commodity'})
                WHERE deposit.canonical_term CONTAINS commodity.canonical_term
                MERGE (deposit)-[:RELATED_TO]->(commodity)
                RETURN count(*) AS edges
                """
            )
            record = result.single()
            related_edges = record["edges"] if record else 0
            print(f"  RELATED_TO edges created: {related_edges}")

            # ── 2d. INSTANCE_OF from existing :Commodity Neo4j nodes ──────
            print("\nStep 4: Wiring INSTANCE_OF from :Commodity → :GeologyConcept...")
            result = session.run(
                """
                MATCH (c:Commodity)
                WHERE c.name IS NOT NULL
                MATCH (gc:GeologyConcept)
                WHERE toLower(gc.canonical_term) = toLower(c.name)
                MERGE (c)-[:INSTANCE_OF]->(gc)
                RETURN count(*) AS wired
                """
            )
            record = result.single()
            instance_of_edges = record["wired"] if record else 0
            print(f"  INSTANCE_OF edges wired: {instance_of_edges}")

            # ── 2e. Full-text index for query expansion ───────────────────
            print("\nStep 5: Creating full-text index on GeologyConcept...")
            try:
                session.run(
                    """
                    CREATE FULLTEXT INDEX geology_concepts_text IF NOT EXISTS
                    FOR (n:GeologyConcept) ON EACH [n.canonical_term, n.name]
                    """
                )
                print("  Full-text index created (or already exists).")
            except Exception as exc:
                print(f"  Full-text index skipped: {exc}")

            # ── 2f. Summary stats ─────────────────────────────────────────
            print("\nPost-seed counts:")
            result = session.run(
                "MATCH (c:GeologyConcept) "
                "RETURN c.ontology_class AS cls, count(*) AS cnt ORDER BY cnt DESC"
            )
            for record in result:
                print(f"  {record['cls']}: {record['cnt']} concepts")

            result = session.run(
                "MATCH ()-[r:IS_TYPE_OF]->() RETURN count(r) AS cnt"
            )
            record = result.single()
            print(f"  IS_TYPE_OF edges: {record['cnt'] if record else 0}")

    finally:
        driver.close()

    print(f"\nDone. {total_created + total_matched} GeologyConcept nodes seeded.")


if __name__ == "__main__":
    asyncio.run(main())
