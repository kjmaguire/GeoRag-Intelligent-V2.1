"""LAS files that arrived before their collar: keep them, attach them later.

A LAS well log attaches curves to a collar. When the collar is not in the
project yet and the LAS header has no coordinates of its own, ``las_ingester``
refuses it (``las_collar_unlocated``) -- it never invents a location, and
silver.collars.easting / northing are NOT NULL. Refusing used to mean the
curves were lost until someone noticed and re-uploaded. Now:

  * ``record_pending`` notes the file (it is already in bronze) in
    silver.las_pending_collar: which well, which key, which project;
  * ``attach_pending_las`` is called wherever collars have just been written
    (the end of an ingest_tabular run that wrote collars, the ZIP archive's
    dependent phase). It finds pending wells whose hole id -- exact, then
    canonical -- now has a collar and ingests them.

Idempotency: a row moves pending -> attaching -> attached only by a
conditional UPDATE, so two workers cannot both ingest one file and an attached
row is never picked up again. A worker that dies mid-attach leaves
'attaching', reclaimed after ``_RECLAIM_AFTER``. The curve writer is
ON CONFLICT (collar_id, curve_name) DO UPDATE, so even a repeated attach
replaces rather than duplicates.

``attach_pending_las`` never raises: it is a hook on other workflows' success
paths and must not fail them.
"""
from __future__ import annotations

import asyncio
import logging
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from georag_object_storage import Bucket

log = logging.getLogger("georag.ingest.las_pending")

#: How long an 'attaching' claim is honoured before another worker may take it.
_RECLAIM_AFTER = "30 minutes"

#: Same rule as las_ingester / csv parser: strip separators, uppercase.
_SEPARATORS = re.compile(r"[ \-_./]+")


def canonical_hole_id(hole_id: str) -> str | None:
    return _SEPARATORS.sub("", (hole_id or "").strip()).upper() or None


@dataclass
class AttachSummary:
    attached: list[str] = field(default_factory=list)   # source names
    still_pending: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


async def record_pending(
    conn: Any,
    *,
    workspace_id: str,
    project_id: str,
    hole_id: str,
    bronze_key: str,
    source_name: str,
) -> None:
    """Note that ``bronze_key`` (a LAS already in bronze) is waiting for ``hole_id``.

    Idempotent on (project_id, bronze_key). A row already 'attached' is left
    alone; a waiting one is refreshed.
    """
    await conn.execute(
        """
        INSERT INTO silver.las_pending_collar
            (pending_id, workspace_id, project_id, hole_id, hole_id_canonical,
             bronze_key, source_name, status, created_at, updated_at)
        VALUES (gen_random_uuid(), $1::uuid, $2::uuid, $3, $4, $5, $6, 'pending', NOW(), NOW())
        ON CONFLICT (project_id, bronze_key) DO UPDATE
           SET hole_id = EXCLUDED.hole_id,
               hole_id_canonical = EXCLUDED.hole_id_canonical,
               source_name = EXCLUDED.source_name,
               updated_at = NOW()
         WHERE silver.las_pending_collar.status <> 'attached'
        """,
        workspace_id, project_id, hole_id, canonical_hole_id(hole_id),
        bronze_key, source_name[:255],
    )


_CANDIDATES_SQL = f"""
SELECT p.pending_id::text AS pending_id, p.hole_id, p.bronze_key, p.source_name
  FROM silver.las_pending_collar p
 WHERE p.project_id = $1::uuid
   AND (p.status = 'pending'
        OR (p.status = 'attaching' AND p.updated_at < now() - interval '{_RECLAIM_AFTER}'))
   AND EXISTS (
        SELECT 1 FROM silver.collars c
         WHERE c.project_id = p.project_id
           AND (c.hole_id = p.hole_id
                OR (p.hole_id_canonical IS NOT NULL
                    AND c.hole_id_canonical = p.hole_id_canonical)))
 ORDER BY p.created_at
"""

_CLAIM_SQL = f"""
UPDATE silver.las_pending_collar
   SET status = 'attaching', attempts = attempts + 1, updated_at = NOW()
 WHERE pending_id = $1::uuid
   AND (status = 'pending'
        OR (status = 'attaching' AND updated_at < now() - interval '{_RECLAIM_AFTER}'))
RETURNING 1
"""


async def attach_pending_las(
    conn: Any,
    *,
    store: Any,
    workspace_id: str,
    project_id: str,
) -> AttachSummary:
    """Ingest every pending LAS in the project whose collar now exists.

    ``conn`` must already be bound to the workspace's RLS scope. Never raises.
    """
    summary = AttachSummary()
    try:
        candidates = await conn.fetch(_CANDIDATES_SQL, project_id)
    except Exception as exc:  # noqa: BLE001 — a hook on other workflows' success paths
        log.warning("las_pending.lookup_failed project=%s err=%s", project_id, exc)
        summary.errors.append(f"lookup: {exc}")
        return summary
    if not candidates:
        return summary

    from app.services.ingest.las_ingester import ingest_las_file  # noqa: PLC0415

    for row in candidates:
        try:
            pending_id, key, name = row["pending_id"], row["bronze_key"], row["source_name"]
            hole_id = row["hole_id"]
        except (KeyError, TypeError) as exc:
            log.warning("las_pending.bad_candidate_row project=%s err=%s", project_id, exc)
            summary.errors.append(f"candidate row: {exc}")
            continue
        try:
            claimed = await conn.fetchval(_CLAIM_SQL, pending_id)
            if not claimed:
                continue  # another worker has it, or it was attached meanwhile
            with tempfile.TemporaryDirectory(prefix="georag_las_pending_") as tmp:
                local = str(Path(tmp) / (Path(name).name or "pending.las"))
                await asyncio.to_thread(store.get_file, Bucket.BRONZE, key, local)
                async with conn.transaction():
                    result = await ingest_las_file(
                        conn, local, workspace_id=workspace_id,
                        project_id_override=project_id,
                        hole_id_override=hole_id,
                    )
            if result.skipped or not result.collar_id:
                reason = result.skipped_reason or "not_ingested"
                await _release(conn, pending_id, reason)
                summary.still_pending.append(name)
                log.info("las_pending.still_pending file=%s reason=%s", name, reason)
                continue
            await conn.execute(
                """
                UPDATE silver.las_pending_collar
                   SET status = 'attached', collar_id = $2::uuid, last_error = NULL,
                       attached_at = NOW(), updated_at = NOW()
                 WHERE pending_id = $1::uuid
                """,
                pending_id, result.collar_id,
            )
            summary.attached.append(name)
            log.info(
                "las_pending.attached file=%s collar=%s curves=%d",
                name, result.collar_id, result.curves_inserted,
            )
        except Exception as exc:  # noqa: BLE001 — one bad file must not stop the rest
            log.warning("las_pending.attach_failed file=%s err=%s", name, exc)
            summary.errors.append(f"{name}: {exc}")
            await _release(conn, pending_id, f"{type(exc).__name__}: {exc}"[:500])
    return summary


async def _release(conn: Any, pending_id: str, reason: str) -> None:
    """Put a claimed row back to 'pending' with the reason it did not attach."""
    try:
        await conn.execute(
            """
            UPDATE silver.las_pending_collar
               SET status = 'pending', last_error = $2, updated_at = NOW()
             WHERE pending_id = $1::uuid AND status = 'attaching'
            """,
            pending_id, reason[:500],
        )
    except Exception as exc:  # noqa: BLE001 — reclaimed after _RECLAIM_AFTER anyway
        log.warning("las_pending.release_failed id=%s err=%s", pending_id, exc)
