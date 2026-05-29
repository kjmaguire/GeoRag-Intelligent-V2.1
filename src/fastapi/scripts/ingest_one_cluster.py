"""One-off CLI to ingest a single extracted PLSS-section cluster.

Used by the 2026-05-17 Wyoming catch-up to add projects beyond the
original Shirley Basin (028N079W36) ingest. Runs the standard
`ingest_cluster` pipeline (LAS + .log header coords) then the derivation
pipeline (`derive_intervals`).

Usage:
    python -m scripts.ingest_one_cluster \\
        --cluster-dir /data/033N089W28 \\
        --section-key 033N089W28 \\
        --project-name "Gas Hills Uranium (033N089W28)" \\
        --project-slug "gas-hills-033n089w28" \\
        --company "Pathfinder/Cameco" \\
        --region "Fremont, WY"
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

import asyncpg

from app.services.ingest.cluster_runner import ingest_cluster
from app.services.ingest.derive_intervals import derive_project

log = logging.getLogger("georag.scripts.ingest_one_cluster")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


async def _main(
    *,
    cluster_dir: str,
    section_key: str,
    project_name: str,
    project_slug: str,
    company: str,
    region: str,
    workspace_id: str,
) -> int:
    summary = await ingest_cluster(
        cluster_dir,
        workspace_id=workspace_id,
        plss_section_key=section_key,
        project_name=project_name,
        project_slug=project_slug,
        project_company=company,
        project_region=region,
    )
    log.info("cluster_runner.summary %s", summary)

    # Find the project_id we created/upserted.
    user = os.environ["POSTGRES_USER"]
    password = os.environ["POSTGRES_PASSWORD"]
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    conn = await asyncpg.connect(
        f"postgres://{user}:{password}@{host}:{port}/{db}",
        statement_cache_size=0,
    )
    try:
        await conn.execute("SELECT set_config('app.workspace_id', $1, false)", workspace_id)
        project_id = await conn.fetchval(
            "SELECT project_id::text FROM silver.projects WHERE slug = $1",
            project_slug,
        )
    finally:
        await conn.close()

    if not project_id:
        log.error("could not resolve project_id for slug=%s", project_slug)
        return 1

    log.info("running derivation for project_id=%s", project_id)
    derive_summary = await derive_project(project_id)
    log.info("derive_intervals.summary %s", derive_summary)
    return 0


def _cli() -> int:
    p = argparse.ArgumentParser(description="Ingest a single cluster + derive intervals")
    p.add_argument("--cluster-dir", required=True)
    p.add_argument("--section-key", required=True, help="e.g. 033N089W28")
    p.add_argument("--project-name", required=True)
    p.add_argument("--project-slug", required=True)
    p.add_argument("--company", default="Unknown Operator")
    p.add_argument("--region", default="Wyoming")
    p.add_argument("--workspace-id", default="a0000000-0000-0000-0000-000000000001")
    args = p.parse_args()
    return asyncio.run(_main(
        cluster_dir=args.cluster_dir,
        section_key=args.section_key,
        project_name=args.project_name,
        project_slug=args.project_slug,
        company=args.company,
        region=args.region,
        workspace_id=args.workspace_id,
    ))


if __name__ == "__main__":
    raise SystemExit(_cli())
