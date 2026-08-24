#!/usr/bin/env python
"""Delete ingestion artefacts that no live project or report can reach.

A project delete removes Postgres rows and nothing else (measured 2026-08-24:
32,890 of 33,742 Qdrant points and 682 blob objects survived a delete that left
zero projects and zero reports behind). This reclaims what is already stranded.

## The safety rule

Nothing is deleted unless it is UNREACHABLE — that is, it references a
project or report that no longer exists, or is referenced by nothing at all.
Anything reachable from a live row is kept, and the counts are printed before
anything is touched.

Deletion is OPT-IN. Without ``--delete`` this prints the plan and exits, which
is the same shape as scripts/ci/acr_orphan_sweep.py — a sweep whose default is
"delete" is one typo away from an outage.

## Recoverability, honestly

- Postgres  — 35-day point-in-time restore on georag-pg-cc.
- Blob      — versioning on, 30-day soft delete on blobs and containers, so a
              deleted object can be restored for 30 days.
- Qdrant    — NO backup. Vector deletion is irreversible. This is acceptable
              only because the vectors being deleted are already unreachable:
              their report row is gone, so no citation can resolve to them and
              no project-scoped query can match them. They cannot be restored,
              but they also cannot be used.

## Usage

    python purge_orphaned_artifacts.py                 # plan only
    python purge_orphaned_artifacts.py --delete        # act
    python purge_orphaned_artifacts.py --delete --only postgres,qdrant

``--max-fraction`` refuses to proceed when a store would lose more than the
given share of its contents, unless ``--force`` is passed. The guard exists
because the failure mode that matters is a bad query matching everything: a
sweep that quietly deletes 100% is indistinguishable from a sweep that works.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass, field
from typing import Any

import asyncpg

sys.path.insert(0, "/app")

from app.db.dsn import build_dsn  # noqa: E402

# Blob prefixes that ingestion writes. Anything outside these is left alone:
# this sweep has no opinion about containers it did not create.
BLOB_CONTAINERS = ("bronze", "bronze-raster")


@dataclass
class Plan:
    """What one store would lose, and out of how much."""

    store: str
    doomed: int = 0
    total: int = 0
    detail: list[str] = field(default_factory=list)

    @property
    def fraction(self) -> float:
        return (self.doomed / self.total) if self.total else 0.0

    def render(self) -> str:
        pct = f"{self.fraction * 100:5.1f}%"
        head = f"  {self.store:22} {self.doomed:>7} of {self.total:>7}  ({pct})"
        return "\n".join([head, *(f"      {d}" for d in self.detail)])


async def plan_postgres(conn: asyncpg.Connection) -> tuple[Plan, dict[str, str]]:
    """Rows whose owning project or report is gone.

    `silver.ingest_progress` is the awkward one: `project_id` is nullable, so
    runs that never bound a project survived the cascade. A null project_id is
    only orphaned when its report is also absent — otherwise it is a live run
    that simply has not been associated yet, and deleting it would erase an
    in-flight upload.
    """
    plan = Plan(store="postgres")
    statements: dict[str, str] = {}

    targets = {
        "silver.ingest_progress": """
            (project_id IS NOT NULL
                AND NOT EXISTS (SELECT 1 FROM silver.projects p
                                 WHERE p.project_id = silver.ingest_progress.project_id))
            OR (project_id IS NULL
                AND (report_id IS NULL
                     OR NOT EXISTS (SELECT 1 FROM silver.reports r
                                     WHERE r.report_id = silver.ingest_progress.report_id))
                AND completed_at IS NOT NULL)
        """,
        # NOT a target: silver.corpus_health_findings looks project-scoped and
        # is not. It records database-health findings (zero-scan indexes and
        # the like) with a null workspace_id and no report_id column at all.
        # Deleting it would erase operational findings, not stale content.
        #
        # Left behind by a project delete, and the reason the nightly
        # integrity sweep re-ingests deleted files: a manifest row with no
        # report looks exactly like a failed ingest awaiting recovery.
        "bronze.manifest": """
            NOT EXISTS (SELECT 1 FROM silver.reports r
                         WHERE r.source_file_sha256 = bronze.manifest.sha256
                           AND r.workspace_id = bronze.manifest.workspace_id)
              AND NOT EXISTS (SELECT 1 FROM silver.ingest_progress p
                               WHERE p.workspace_id = bronze.manifest.workspace_id
                                 AND p.minio_key = bronze.manifest.file_key)
        """,
    }

    for table, predicate in targets.items():
        try:
            total = await conn.fetchval(f"SELECT count(*) FROM {table}")
            doomed = await conn.fetchval(f"SELECT count(*) FROM {table} WHERE {predicate}")
        except asyncpg.PostgresError as exc:
            plan.detail.append(f"{table}: SKIPPED ({type(exc).__name__})")
            continue

        plan.total += total
        plan.doomed += doomed
        plan.detail.append(f"{table}: {doomed} of {total}")
        if doomed:
            statements[table] = f"DELETE FROM {table} WHERE {predicate}"

    return plan, statements


async def plan_qdrant(conn: asyncpg.Connection) -> tuple[Plan, list[str]]:
    """Points whose report_id is absent from silver.reports.

    Scrolled rather than counted server-side: Qdrant cannot join against
    Postgres, so the live report ids come from the database and the decision is
    made per point here.
    """
    import httpx

    plan = Plan(store="qdrant/georag_chunks")
    live = {str(r["report_id"]) for r in await conn.fetch("SELECT report_id FROM silver.reports")}

    base = f"https://{os.environ['QDRANT_HOST']}:{os.environ['QDRANT_PORT']}"
    headers = {"api-key": os.environ["QDRANT_API_KEY"]}
    doomed: list[str] = []

    async with httpx.AsyncClient(timeout=90, headers=headers) as http:
        plan.total = (
            await http.post(f"{base}/collections/georag_chunks/points/count", json={"exact": True})
        ).json()["result"]["count"]

        offset: Any = None
        while True:
            body: dict[str, Any] = {"limit": 1000, "with_payload": ["report_id"], "with_vector": False}
            if offset is not None:
                body["offset"] = offset
            page = (
                await http.post(f"{base}/collections/georag_chunks/points/scroll", json=body)
            ).json()["result"]

            for point in page["points"]:
                if str((point.get("payload") or {}).get("report_id")) not in live:
                    doomed.append(point["id"])

            offset = page.get("next_page_offset")
            if offset is None:
                break

    plan.doomed = len(doomed)
    plan.detail.append(f"{len(live)} live report ids; points citing anything else are unreachable")
    return plan, doomed


async def plan_blobs(conn: asyncpg.Connection) -> tuple[Plan, dict[str, list[str]]]:
    """Objects no live ingest run or report points at."""
    from azure.identity.aio import DefaultAzureCredential
    from azure.storage.blob.aio import BlobServiceClient

    plan = Plan(store="blob storage")
    keys = {
        r["minio_key"]
        for r in await conn.fetch(
            "SELECT minio_key FROM silver.ingest_progress WHERE minio_key IS NOT NULL"
        )
    }
    doomed: dict[str, list[str]] = {}

    credential = DefaultAzureCredential()
    service = BlobServiceClient(os.environ["AZURE_STORAGE_ACCOUNT_URL"], credential=credential)
    try:
        async with service:
            for container in BLOB_CONTAINERS:
                client = service.get_container_client(container)
                names = [b.name async for b in client.list_blobs()]
                stranded = [n for n in names if n not in keys]
                plan.total += len(names)
                plan.doomed += len(stranded)
                plan.detail.append(f"{container}: {len(stranded)} of {len(names)}")
                if stranded:
                    doomed[container] = stranded
    finally:
        await credential.close()

    return plan, doomed


async def apply_postgres(conn: asyncpg.Connection, statements: dict[str, str]) -> None:
    async with conn.transaction():
        for table, sql in statements.items():
            result = await conn.execute(sql)
            print(f"      {table}: {result}")


async def apply_qdrant(ids: list[str]) -> None:
    import httpx

    base = f"https://{os.environ['QDRANT_HOST']}:{os.environ['QDRANT_PORT']}"
    headers = {"api-key": os.environ["QDRANT_API_KEY"]}
    async with httpx.AsyncClient(timeout=120, headers=headers) as http:
        for start in range(0, len(ids), 1000):
            chunk = ids[start : start + 1000]
            response = await http.post(
                f"{base}/collections/georag_chunks/points/delete?wait=true",
                json={"points": chunk},
            )
            response.raise_for_status()
            print(f"      deleted {start + len(chunk)}/{len(ids)}")


async def apply_blobs(doomed: dict[str, list[str]]) -> None:
    from azure.identity.aio import DefaultAzureCredential
    from azure.storage.blob.aio import BlobServiceClient

    credential = DefaultAzureCredential()
    service = BlobServiceClient(os.environ["AZURE_STORAGE_ACCOUNT_URL"], credential=credential)
    try:
        async with service:
            for container, names in doomed.items():
                client = service.get_container_client(container)
                for index, name in enumerate(names, start=1):
                    await client.delete_blob(name, delete_snapshots="include")
                    if index % 100 == 0 or index == len(names):
                        print(f"      {container}: {index}/{len(names)}")
    finally:
        await credential.close()


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--delete", action="store_true", help="actually delete (default: plan only)")
    parser.add_argument("--only", default="postgres,qdrant,blobs", help="comma-separated stores")
    parser.add_argument("--max-fraction", type=float, default=0.98)
    parser.add_argument("--force", action="store_true", help="ignore --max-fraction")
    args = parser.parse_args()

    stores = {s.strip() for s in args.only.split(",") if s.strip()}
    conn = await asyncpg.connect(build_dsn())

    projects = await conn.fetchval("SELECT count(*) FROM silver.projects")
    reports = await conn.fetchval("SELECT count(*) FROM silver.reports")
    print(f"### live: {projects} projects, {reports} reports\n")

    plans: list[Plan] = []
    pg_statements: dict[str, str] = {}
    qdrant_ids: list[str] = []
    blob_names: dict[str, list[str]] = {}

    if "postgres" in stores:
        plan, pg_statements = await plan_postgres(conn)
        plans.append(plan)
    if "qdrant" in stores:
        plan, qdrant_ids = await plan_qdrant(conn)
        plans.append(plan)
    if "blobs" in stores:
        plan, blob_names = await plan_blobs(conn)
        plans.append(plan)

    print("### PLAN — unreachable artefacts")
    for plan in plans:
        print(plan.render())

    over = [p for p in plans if p.fraction > args.max_fraction and p.doomed]
    if over and not args.force:
        print(
            "\n### REFUSING: "
            + ", ".join(f"{p.store} would lose {p.fraction * 100:.1f}%" for p in over)
            + f"\n    Above --max-fraction={args.max_fraction}. A sweep that deletes everything "
            "looks the same as a sweep with a broken predicate. Re-run with --force if this "
            "is genuinely intended."
        )
        await conn.close()
        return 2

    if not args.delete:
        print("\n### DRY RUN — nothing deleted. Re-run with --delete to act.")
        await conn.close()
        return 0

    print("\n### DELETING")
    if pg_statements:
        await apply_postgres(conn, pg_statements)
    if qdrant_ids:
        await apply_qdrant(qdrant_ids)
    if blob_names:
        await apply_blobs(blob_names)

    print("\n### DONE")
    await conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
