"""§1b + §3d end-to-end smoke against the live PG.

One-shot verification script. Exercises:
  1. Chunker with flag-on emits parent + child rows
  2. _insert_passages writes them with FK
  3. silver.document_passages.parent_chunk_id is populated
  4. expand_parents_sync against the merged packet picks the parents up
  5. Cleans up the test rows so we don't leave fixture data behind

Run::

    docker exec georag-fastapi python /app/scripts/_smoke_parent_chunking.py

Returns 0 on success, 1 on failure. Intended for an operator to
spot-check after flipping `PARENT_CHUNKING_ENABLED=true` in dev .env;
not part of the regular test suite.
"""
import asyncio
import uuid

import asyncpg

from app.config import settings
from app.services.ingest.pdf_ingester import _chunk_pages, _insert_passages
from app.agent.parent_expansion import expand_parents_sync
from app.agent.evidence import DocumentEvidence, EvidencePacket


async def main() -> int:
    print(f"PARENT_CHUNKING_ENABLED: {settings.PARENT_CHUNKING_ENABLED}")
    print(f"PARENT_CHUNKING_GROUP_SIZE: {settings.PARENT_CHUNKING_GROUP_SIZE}")

    # Fake document with enough paragraphs to trigger ≥1 parent group.
    para = (
        "Geological context paragraph describing the host rock unit and "
        "mineralisation style observed in the drill core interval. "
    ) * 3
    pages = [
        f"{para}\n\nSecond paragraph on page 1 with assay grade data and structural "
        f"orientation measurements taken during the 2022 program. " * 3,
        f"{para}\n\nThird page paragraph showing alteration zones and lithology "
        f"code transitions through depth in the eastern drill section. " * 3,
    ]

    chunks = _chunk_pages(pages)
    parents = [c for c in chunks if c.get("chunk_kind") == "section"]
    children = [c for c in chunks if c.get("chunk_kind") == "paragraph"]
    narratives = [c for c in chunks if c.get("chunk_kind") == "narrative"]
    print(
        f"\nChunker output: {len(parents)} parent(s), "
        f"{len(children)} child(ren), {len(narratives)} narrative(s)"
    )
    if not parents:
        print("FAIL: no parents emitted — flag may not be active")
        return 1

    assert all("passage_id_override" in p for p in parents), "parent missing UUID"
    assert all(c["parent_chunk_id"] for c in children), "child missing parent FK"
    print("PASS: parent UUIDs + child FKs populated in dict shape")

    dsn = (
        f"postgres://{settings.POSTGRES_USER}:{settings.POSTGRES_PASSWORD}"
        f"@postgresql:5432/{settings.POSTGRES_DB}"
    )
    conn = await asyncpg.connect(dsn)
    try:
        workspace_id = "a0000000-0000-0000-0000-000000000001"
        await conn.execute(
            "SELECT set_config('georag.workspace_id', $1, true)", workspace_id,
        )

        project_row = await conn.fetchrow(
            "SELECT project_id::text FROM silver.projects "
            "WHERE workspace_id = $1::uuid LIMIT 1",
            workspace_id,
        )
        if not project_row:
            print("SKIP: no project rows in workspace — can't test ingest path")
            return 0
        project_id = project_row["project_id"]

        report_id = str(uuid.uuid4())
        await conn.execute(
            "INSERT INTO silver.reports "
            "  (report_id, project_id, workspace_id, title, "
            "   source_file_sha256, commodity, parser_used, "
            "   created_at, updated_at) "
            "VALUES ($1::uuid, $2::uuid, $3::uuid, $4, $5, 'uranium', "
            "  'smoke_test', NOW(), NOW())",
            report_id, project_id, workspace_id,
            "smoke-1b-3d-test", "smoke-" + uuid.uuid4().hex,
        )

        inserted = await _insert_passages(
            conn, document_id=report_id, workspace_id=workspace_id,
            chunks=chunks,
        )
        print(f"\nInserted: {inserted} rows")

        kind_counts = await conn.fetch(
            "SELECT chunk_kind, count(*)::int AS n, "
            "  count(*) FILTER (WHERE parent_chunk_id IS NOT NULL)::int "
            "    AS with_parent "
            "FROM silver.document_passages WHERE document_id = $1::uuid "
            "GROUP BY chunk_kind ORDER BY chunk_kind",
            report_id,
        )
        for r in kind_counts:
            print(
                f"  {r['chunk_kind']}: {r['n']} rows "
                f"({r['with_parent']} carry parent_chunk_id)"
            )

        child_row = await conn.fetchrow(
            "SELECT passage_id::text AS chunk_id, "
            "  parent_chunk_id::text AS parent_id, text "
            "FROM silver.document_passages "
            "WHERE document_id = $1::uuid AND chunk_kind = 'paragraph' LIMIT 1",
            report_id,
        )
        if child_row is None:
            print("FAIL: no paragraph rows in DB after insert")
            return 1

        parent_row = await conn.fetchrow(
            "SELECT passage_id::text AS chunk_id, text "
            "FROM silver.document_passages WHERE passage_id = $1::uuid",
            child_row["parent_id"],
        )
        print(f"\nChild passage_id: {child_row['chunk_id']}")
        print(f"Child's parent_chunk_id: {child_row['parent_id']}")
        print(f"Parent passage exists: {parent_row is not None}")
        assert parent_row is not None, "FK target not found"
        print("PASS: child→parent FK resolves in DB")

        ev = DocumentEvidence(
            document_id=report_id, document_title="smoke-1b-3d-test",
            document_type="NI 43-101", authority_rank=1, is_current=True,
            confidence=1.0, page=1,
            chunk_id=child_row["chunk_id"],
            parent_chunk_id=child_row["parent_id"],
            text=child_row["text"], char_start=0,
            char_end=len(child_row["text"]),
        )
        packet = EvidencePacket(
            query_id="q-smoke", query_text="test query",
            evidence=[ev], total_tokens=100,
            system_prompt_tokens=0, remaining_budget=5000,
        )
        result = expand_parents_sync(
            packet,
            parents_by_id={child_row["parent_id"]: {
                "text": parent_row["text"],
                "page_first": 1,
            }},
        )
        print(
            f"\nexpand_parents_sync result: "
            f"added={result.parents_added}, "
            f"skipped={result.parents_skipped}, "
            f"failed={result.parents_failed}"
        )
        if result.parents_added != 1:
            print(f"FAIL: expected 1 parent added, got {result.parents_added}")
            return 1
        print(
            f"Expanded packet now has {len(result.packet.evidence)} "
            f"evidence (child + parent)"
        )
        print("PASS: §3d expansion fired on the real chunk data")

        await conn.execute(
            "DELETE FROM silver.document_passages WHERE document_id = $1::uuid",
            report_id,
        )
        await conn.execute(
            "DELETE FROM silver.reports WHERE report_id = $1::uuid", report_id,
        )
        print("\nCleaned up smoke fixture")
    finally:
        await conn.close()

    print("\n[OK] §1b + §3d end-to-end VERIFIED")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
