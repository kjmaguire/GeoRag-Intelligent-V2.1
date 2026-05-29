"""Bronze backfill from a pre-staged dir of LAS curve files.

Companion to bronze_backfill_xlsx_dir.py — same per-row INSERT pattern,
but the input is a directory of LAS (Log ASCII Standard) files instead
of xlsx. The 186 GB Uranium_Logs_ALL.zip is dominated by LAS — every
inner per-TRS zip contains gamma / SP / resistivity curves for each
drillhole. The xlsx backfill only got the coordinate tables; this
script handles the actual logging data.

LAS dialect notes for the 2008-2013 Wyoming uranium archive
-----------------------------------------------------------
* Most files are LAS 2.0 (a few LAS 1.2 from the State Lease era).
* Standard curve names in this dataset:
    - DEPTH (m or ft, ~Curve section .UNIT field disambiguates)
    - GR    (gamma in API or counts/sec)
    - eU    (computed equivalent uranium %eU, Murray-formula derived)
    - SP    (spontaneous potential, mV)
    - RES   (resistivity, ohm-m)
    - CAL   (caliper, in)
* Hole id usually appears in the ~Well section as `WELL` or `UWI`.
* Tool name is in `~Parameter section TOOL` or `CONTRACTOR`.

The script writes one bronze.raw_geophysical_runs row per LAS file
(header-level), with raw_header carrying the full ~Well + ~Parameter
JSON for downstream curve parsing in Phase 1.

Run
---
    docker exec georag-fastapi sh -c "cd /app && python \\
      scripts/bronze_backfill_las_dir.py \\
        --dir /tmp/uranium_las_staged \\
        --workspace-id a0000000-0000-0000-0000-000000000001"

Data extraction is a separate operator step:
  1. Re-extract the 186 GB outer ZIP targeting *.las members (use the
     same shutil.copyfileobj streaming pattern as extract_targeted_v2.py)
  2. Stage in /home/georag/uranium_las_staged/<TRS>/<file>.las
  3. docker cp into the container
  4. Run this loader
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import sys
from typing import Any

import asyncpg

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("bronze_las_dir")


def _parse_las_header(path: str) -> dict[str, Any]:
    """Parse just the LAS metadata sections via lasio. Returns:
        {
          'hole_id': str | None,
          'tool_name': str | None,
          'contractor': str | None,
          'depth_from': float | None,
          'depth_to': float | None,
          'sample_interval': float | None,
          'curves': [{'mnemonic': str, 'unit': str, 'desc': str}],
          'well_section': {<mnemonic>: value, ...},
          'param_section': {<mnemonic>: value, ...},
        }
    """
    import lasio  # noqa: PLC0415

    las = lasio.read(path, ignore_header_errors=True)
    well = {item.mnemonic: str(item.value) for item in las.well}
    params = {item.mnemonic: str(item.value) for item in las.params}
    curves = [
        {
            "mnemonic": c.mnemonic,
            "unit": c.unit or "",
            "desc": c.descr or "",
        }
        for c in las.curves
    ]
    hole_id = well.get("WELL") or well.get("UWI") or well.get("API")
    return {
        "hole_id": hole_id.strip() if hole_id else None,
        "tool_name": params.get("TOOL") or params.get("LOGTOOL"),
        "contractor": params.get("SRVC") or params.get("CONTRACTOR")
            or well.get("SRVC"),
        "depth_from": float(las.well.STRT.value) if las.well.STRT.value is not None else None,
        "depth_to": float(las.well.STOP.value) if las.well.STOP.value is not None else None,
        "sample_interval": float(las.well.STEP.value) if las.well.STEP.value is not None else None,
        "curves": curves,
        "well_section": well,
        "param_section": params,
    }


async def _ensure_source_file(
    conn: asyncpg.Connection, workspace_id: str,
    seaweedfs_key: str, original_filename: str, sha: str,
    size_bytes: int,
) -> str:
    row = await conn.fetchrow(
        """
        INSERT INTO bronze.source_files
          (workspace_id, seaweedfs_key, original_filename, file_sha256,
           file_size_bytes, source_type, data_type)
        VALUES ($1::uuid, $2, $3, $4, $5, 'las', 'geophysics')
        ON CONFLICT (workspace_id, file_sha256) DO UPDATE
           SET seaweedfs_key = EXCLUDED.seaweedfs_key
        RETURNING id::text
        """,
        workspace_id, seaweedfs_key, original_filename, sha, size_bytes,
    )
    return row["id"]


async def _ingest_las(
    conn: asyncpg.Connection, workspace_id: str, source_file_id: str,
    seaweedfs_key: str, hdr: dict[str, Any],
) -> bool:
    if not hdr.get("hole_id"):
        return False
    await conn.execute(
        """
        INSERT INTO bronze.raw_geophysical_runs
          (workspace_id, hole_id, run_type, tool_name, contractor,
           seaweedfs_key, file_format, depth_from, depth_to, sample_interval,
           raw_header)
        VALUES ($1::uuid, $2, 'wireline_log', $3, $4,
                $5, 'LAS', $6, $7, $8, $9::jsonb)
        """,
        workspace_id, hdr["hole_id"], hdr["tool_name"], hdr["contractor"],
        seaweedfs_key, hdr["depth_from"], hdr["depth_to"], hdr["sample_interval"],
        json.dumps({
            "well": hdr["well_section"],
            "params": hdr["param_section"],
            "curves": hdr["curves"],
        }),
    )
    return True


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--workspace-id", required=True)
    args = ap.parse_args()

    if not os.path.isdir(args.dir):
        logger.error("not a directory: %s", args.dir)
        return 2

    user = os.environ["POSTGRES_USER"]
    pwd  = os.environ["POSTGRES_PASSWORD"]
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db   = os.environ["POSTGRES_DB"]
    pg = await asyncpg.connect(f"postgres://{user}:{pwd}@{host}:{port}/{db}")

    counts = {
        "las_files_seen": 0,
        "runs_inserted": 0,
        "header_errors": 0,
        "no_hole_id": 0,
    }

    try:
        for root, _, files in os.walk(args.dir):
            for fname in files:
                if not fname.lower().endswith(".las"):
                    continue
                path = os.path.join(root, fname)
                size = os.path.getsize(path)
                counts["las_files_seen"] += 1

                # File-level SHA for source_files dedup.
                h = hashlib.sha256()
                with open(path, "rb") as f:
                    for chunk in iter(lambda: f.read(64 * 1024), b""):
                        h.update(chunk)
                sha = h.hexdigest()

                try:
                    hdr = _parse_las_header(path)
                except Exception as exc:
                    counts["header_errors"] += 1
                    logger.warning("LAS parse failed on %s: %s", path, exc)
                    continue

                sf_id = await _ensure_source_file(
                    pg, workspace_id=args.workspace_id,
                    seaweedfs_key=f"uranium-logs-las/{os.path.basename(root)}/{fname}",
                    original_filename=fname,
                    sha=sha,
                    size_bytes=size,
                )

                ok = await _ingest_las(
                    pg, args.workspace_id, sf_id,
                    f"uranium-logs-las/{os.path.basename(root)}/{fname}",
                    hdr,
                )
                if ok:
                    counts["runs_inserted"] += 1
                else:
                    counts["no_hole_id"] += 1

                if counts["las_files_seen"] % 100 == 0:
                    logger.info("progress: %s", json.dumps(counts))
    finally:
        await pg.close()

    logger.info("done: %s", json.dumps(counts, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
