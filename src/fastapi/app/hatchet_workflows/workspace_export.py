"""§11.3 wave 1 — per-workspace logical export to cold tier.

Complement to the §11.1 full-store backup crons. Where §11.1 dumps each
store wholesale (pg_dump / qdrant snapshot / Redis RDB /
SeaweedFS bucket clone), this workflow walks **one workspace** across
the tenant-scoped Postgres tables and writes a JSONL.gz export to
SeaweedFS that ``restore_workspace.dry_run=False`` can consume.

Why both?
=========

Full-store backups (§11.1) are the production DR primitive: a single
restore brings the platform back from a node-level outage. But they
can't be restored selectively — pg_restore is per-database, not
per-workspace.

Workspace exports (this module) are the operator primitive: ship one
workspace to a new cluster, clone a workspace for an investigation,
recover from a workspace-scoped tenant-isolation incident.

Scope (v1)
==========

Postgres only. Every tenant-scoped table in `_WORKSPACE_TABLES` is
walked under the target workspace's RLS scope (SET app.workspace_id),
serialised to JSONL, gzipped, and uploaded to the configured EXPORTS bucket
(``AWS_BUCKET_EXPORTS``: ``georag-exports-<account>`` on AWS, ``exports`` in
compose) under
``workspace-exports/<workspace_id>/<timestamp>-<run_id>.jsonl.gz``.

The bucket used to be a bare ``workspace-exports``, which Terraform never
creates or grants (it provisions bronze, bronze-raster, exports and backups),
so on AWS every export ended in NoSuchBucket. ``workspace-exports/`` is now a
key prefix inside the EXPORTS bucket, and a restore manifest URI is
``s3://<exports bucket>/workspace-exports/<workspace_id>/<file>``.

Qdrant and Redis sections were added in manifest v2.0 (Qdrant: scroll API
with a workspace_id payload filter; Redis: SCAN over the workspace-prefixed
keys). There is no Neo4j section: Neo4j was removed from the stack on
2026-07-28 and the export carries no graph data.

Memory and format (database audit 2026-10)
==========================================

The export used to ``SELECT *`` every table into one Python list, hold every
Qdrant vector in another, and gzip the lot into a ``BytesIO`` before one
``put_object`` -- peak memory was roughly the uncompressed archive, several
times over. It now streams:

  * each table is read through a server-side cursor (``conn.cursor``,
    ``prefetch=_PG_CURSOR_PREFETCH``) under ONE ``REPEATABLE READ, READ ONLY``
    transaction, so the tables are a consistent snapshot of each other (the
    old autocommit reads were each a different instant) -- with a savepoint
    per table, so a table that cannot be read is named and the run fails after
    every table has been tried (it used to be skipped, which shipped an empty
    section that ``restore_workspace`` restored as a gap);
  * the column list is explicit: read from ``pg_attribute`` at run time and
    quoted into the SELECT, so the SQL no longer says ``SELECT *`` and a
    dropped column can never be selected, while a column added later is still
    exported (a hard-coded list would silently stop exporting it);
  * Qdrant is scrolled page by page straight into the archive;
  * rows are gzipped as they arrive into temp files (one gzip member per
    section), the manifest -- which must be line 1 and needs the final row
    counts -- is written last as its own member and placed FIRST, and the
    result is handed to ``upload_file``, which is a multipart upload past
    8 MiB. The archive is several concatenated gzip members; every reader in
    this repo (``gzip.GzipFile``, ``zcat``) decodes that as one stream and
    the decoded JSONL is byte-for-byte the format below.

Triggering
==========

No cron. Since 2026-09-29 (HAT-13) an admin who belongs to the workspace
starts it with Laravel
``POST /api/v1/admin/workspaces/{workspace}/workflows/workspace_export``,
which calls FastAPI ``POST /internal/v1/workflows/workspace_export/trigger``.
That route only ever writes to the configured EXPORTS bucket. The Hatchet
UI still works for an operator, with ``{"workspace_id": "<uuid>"}``. Output
run_id is logged + audit-row anchored.
"""

from __future__ import annotations

import asyncio
import gzip
import io
import json
import logging
import os
import shutil
import tempfile
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import aioboto3
import asyncpg
from georag_object_storage import Bucket, StorageConfig, async_client_kwargs
from hatchet_sdk import Context
from pydantic import BaseModel, Field

from app.audit import emit_audit
from app.db import bind_workspace_scope
from app.db.dsn import build_dsn
from app.hatchet_workflows import hatchet

log = logging.getLogger("georag.hatchet.workspace_export")


# Tenant-scoped tables walked by the export. Kept in sync with the
# restore_workspace consistency-check baseline (`_PG_BASELINE_TABLES`)
# so the export and the dry-run reporter agree on what counts as
# "this workspace's PG footprint". Adding a tenant table here requires
# matching it in restore_workspace + the §11.2 cross-store reporter.
_WORKSPACE_TABLES: list[tuple[str, str]] = [
    # (output_key, qualified_table)
    ("silver_workspaces",                 "silver.workspaces"),
    ("silver_hypotheses",                 "silver.hypotheses"),
    ("silver_decision_records",           "silver.decision_records"),
    ("silver_answer_runs",                "silver.answer_runs"),
    ("silver_evidence_items",             "silver.evidence_items"),
    ("silver_document_passages",          "silver.document_passages"),
    ("audit_ledger_anchors",              "audit.audit_ledger"),
    ("targeting_target_recommendations",  "targeting.target_recommendations"),
    ("ops_support_tickets",               "ops.support_tickets"),
]


#: Key prefix of every workspace export inside the EXPORTS bucket. The restore
#: trigger route and Laravel's WorkflowTriggerController validate manifest URIs
#: against it, so change all three together.
EXPORT_KEY_PREFIX = "workspace-exports"


def exports_bucket() -> str:
    """The bucket exports are written to: the configured EXPORTS bucket
    (``AWS_BUCKET_EXPORTS``; ``georag-exports-<account>`` on AWS)."""
    return StorageConfig.from_env().bucket_name(Bucket.EXPORTS)


class WorkspaceExportInput(BaseModel):
    workspace_id: str = Field(..., description="UUID of the workspace to export.")
    bucket: str | None = Field(
        default=None,
        description="Bucket receiving the export object. Default: the configured "
                    "EXPORTS bucket (AWS_BUCKET_EXPORTS), resolved when the run starts.",
    )
    include_qdrant: bool = Field(
        default=True,
        description="§11.3-v2 — include Qdrant points (vectors + payload) filtered by workspace_id.",
    )
    include_redis: bool = Field(
        default=True,
        description="§11.3-v2 — include Redis keys matching georag:ws:<uuid>:* prefix (cache only).",
    )

    # Defence-in-depth UUID guard — the workspace_id is interpolated
    # into f-string SQL at workspace_export.py:192 (`SELECT * FROM
    # {qualified_table} WHERE workspace_id = $1::uuid`). $1 binding
    # is safe for the value, but the qualified_table comes from a
    # static allowlist and the workspace_id flows into other paths
    # (S3 key prefix, Redis key prefix) that don't bind. A malformed
    # workspace_id here could create arbitrarily-named SeaweedFS
    # objects. 2026-06-03 audit — see AUDIT_AND_FIX_REPORT.md
    # Theme G + workflow input sweep.
    from pydantic import field_validator as _fv

    @_fv("workspace_id")
    @classmethod
    def _validate_workspace_id_uuid(cls, v: str) -> str:
        import re as _re
        if not _re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            v,
            _re.IGNORECASE,
        ):
            raise ValueError(
                "WorkspaceExportInput.workspace_id must be a UUID (canonical 8-4-4-4-12 form)."
            )
        return v


class WorkspaceExportOutput(BaseModel):
    run_id: str
    workspace_id: str
    bucket: str
    object_key: str
    bytes: int
    rows_exported: int
    per_table: dict[str, int]
    # §11.3-v2 — per-store extra counts + partial-store failure reasons
    qdrant_point_count: int = 0
    redis_key_count: int = 0
    partial_stores: dict[str, str] = Field(default_factory=dict)
    started_at: datetime
    completed_at: datetime


workspace_export = hatchet.workflow(
    name="workspace_export",
    input_validator=WorkspaceExportInput,
)


# One DSN builder for the whole service — see app/db/dsn.py for why
# sixty copies of this existed and what the drift cost.
_build_dsn = build_dsn


def _build_object_key(workspace_id: str, run_id: str, when: datetime) -> str:
    return (
        f"{EXPORT_KEY_PREFIX}/{workspace_id}/"
        f"{when.year:04d}-{when.month:02d}-{when.day:02d}T"
        f"{when.hour:02d}{when.minute:02d}{when.second:02d}-{run_id}.jsonl.gz"
    )


def _row_to_dict(row: asyncpg.Record) -> dict[str, Any]:
    """JSON-safe serialisation. bytes → hex, datetime → ISO-8601,
    UUID → str, everything else passes through."""
    import uuid as _u
    out: dict[str, Any] = {}
    for k, v in row.items():
        if isinstance(v, (bytes, bytearray, memoryview)):
            out[k] = bytes(v).hex()
        elif isinstance(v, datetime):
            out[k] = v.isoformat()
        elif isinstance(v, _u.UUID):
            out[k] = str(v)
        else:
            out[k] = v
    return out


#: Rows the server hands back per cursor round trip. Bounds the client-side
#: buffer to this many rows; 1000 passages is a few MB.
_PG_CURSOR_PREFETCH = 1000


class _GzipSpool:
    """One gzip member of JSONL lines, written to a temp file as it arrives.

    The archive is assembled from these (see ``_assemble_archive``), so no
    section is ever held in memory. ``lines`` is the count the manifest needs.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self.lines = 0
        self._open()

    def _open(self) -> None:
        self._raw = open(self.path, "wb")  # noqa: SIM115 - closed in close()
        self._gz = gzip.GzipFile(fileobj=self._raw, mode="wb", compresslevel=6)

    def write_json(self, obj: dict[str, Any]) -> None:
        # Same serialisation as _serialise_jsonl_gz, line for line.
        self._gz.write(json.dumps(obj, sort_keys=True, default=str).encode("utf-8"))
        self._gz.write(b"\n")
        self.lines += 1

    def reset(self) -> None:
        """Discard everything written (a section that failed part-way)."""
        self.close()
        self.lines = 0
        self._open()

    def close(self) -> None:
        if not self._gz.closed:
            self._gz.close()
        if not self._raw.closed:
            self._raw.close()


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


async def _table_columns(conn: asyncpg.Connection, qualified_table: str) -> list[str]:
    """Live column names of a table, in attnum order; [] if it does not exist.

    ``qualified_table`` comes from the static ``_WORKSPACE_TABLES`` allowlist,
    and is passed to ``to_regclass`` as a bound value regardless.
    """
    rows = await conn.fetch(
        "SELECT a.attname AS column_name"
        "  FROM pg_catalog.pg_attribute a"
        " WHERE a.attrelid = pg_catalog.to_regclass($1)"
        "   AND a.attnum > 0"
        "   AND NOT a.attisdropped"
        " ORDER BY a.attnum",
        qualified_table,
    )
    return [r["column_name"] for r in rows]


async def _stream_table(
    conn: asyncpg.Connection, qualified_table: str, workspace_id: str,
) -> AsyncIterator[dict[str, Any]]:
    """Yield one tenant table's rows for the target workspace, a page at a time.

    The caller has already bound ``app.workspace_id`` for this workspace on
    ``conn`` (session scope, dedicated direct connection — see run_export),
    so RLS scopes every read here; the explicit ``WHERE workspace_id`` is
    belt and braces. Tables without that column fall back to the RLS-scoped
    read alone. Must run inside a transaction (server-side cursor).
    """
    columns = await _table_columns(conn, qualified_table)
    if not columns:
        raise LookupError(f"{qualified_table} does not exist")
    select_list = ", ".join(_quote_ident(c) for c in columns)

    if qualified_table == "silver.workspaces" or "workspace_id" in columns:
        # silver.workspaces is keyed on workspace_id (its PK).
        query = f"SELECT {select_list} FROM {qualified_table} WHERE workspace_id = $1::uuid"
        if qualified_table == "audit.audit_ledger":
            # The ledger is hash-chained by a BEFORE INSERT trigger that rebuilds
            # previous_hash/hash from the newest existing row, so a restore
            # reproduces the chain only if rows are replayed oldest-first -- the
            # same (created_at, id) order the trigger and the verifier use.
            query += " ORDER BY created_at, id"
        args: tuple[Any, ...] = (workspace_id,)
    else:
        query = f"SELECT {select_list} FROM {qualified_table}"
        args = ()

    async for record in conn.cursor(query, *args, prefetch=_PG_CURSOR_PREFETCH):
        yield _row_to_dict(record)


class ExportTableUnreadable(RuntimeError):
    """A table the export lists could not be read at all (missing, no privilege).

    Not a "skip". An empty section in the archive is indistinguishable from a
    workspace that has no rows there, and ``restore_workspace`` would restore
    the gap without a word -- so the run fails and names the table instead.
    """

    def __init__(self, output_key: str, qualified_table: str, cause: BaseException) -> None:
        self.output_key = output_key
        self.qualified_table = qualified_table
        self.reason = f"{type(cause).__name__}: {cause}"
        super().__init__(f"{qualified_table} could not be read ({self.reason})")


async def _export_one_table(
    conn: asyncpg.Connection,
    qualified_table: str,
    workspace_id: str,
    output_key: str,
    spool: _GzipSpool,
) -> int:
    """Stream one table into ``spool`` as ``{"table": key, "row": ...}`` lines.

    Returns the rows written. A table that cannot be read AT ALL (missing, no
    privilege) raises ``ExportTableUnreadable``: it used to be logged and
    counted as 0 rows, which shipped an empty section as if it were data.
    The savepoint keeps the failure from aborting the surrounding snapshot
    transaction, so ``run_export`` can go on and name every unreadable table
    in one go. A failure after rows have already been written re-raises as it
    was: silently shipping a truncated table would be worse than failing.
    """
    written = 0
    try:
        async with conn.transaction():  # savepoint inside run_export's snapshot
            async for row in _stream_table(conn, qualified_table, workspace_id):
                spool.write_json({"table": output_key, "row": row})
                written += 1
    except Exception as exc:  # noqa: BLE001
        if written:
            raise
        log.error(
            "workspace_export: %s could not be read (err=%r)",
            qualified_table, exc,
        )
        raise ExportTableUnreadable(output_key, qualified_table, exc) from exc
    return written


def _build_manifest_from_counts(
    workspace_id: str,
    run_id: str,
    table_row_counts: dict[str, int],
    *,
    qdrant_point_count: int = 0,
    redis_key_count: int = 0,
    partial_stores: dict[str, str] | None = None,
    skipped_tables: dict[str, str] | None = None,
) -> dict[str, Any]:
    """The manifest is the first JSONL line; subsequent lines are
    `{"table": <output_key>, "row": <row_dict>}` for PG tables and
    `{"section": <qdrant_points|redis_keys>,
       "row": <dict>}` for the §11.3-v2 extra stores.

    restore_workspace reads the manifest line first to validate target
    workspace + section list, then streams rows.

    Manifest version bumped from 1.0 to 2.0 with §11.3-v2.

    ``skipped_tables`` names any listed table that could not be read, with the
    reason. ``run_export`` fails rather than upload such an archive, so it is
    ``{}`` in everything it writes; the restore refuses a manifest where it is
    not, so a partial archive can never be mistaken for a complete one.

    Built from COUNTS rather than the rows themselves, because the rows are
    streamed to disk and never held; ``_build_manifest`` is the same thing
    over in-memory lists.
    """
    return {
        "manifest_version":   "2.0",
        "format":             "workspace_export",
        "workspace_id":       workspace_id,
        "run_id":             run_id,
        "captured_at":        datetime.now(tz=UTC).isoformat(),
        "table_row_counts":   dict(table_row_counts),
        "tables":             list(table_row_counts.keys()),
        "skipped_tables":     dict(skipped_tables or {}),
        # §11.3-v2 extras
        "qdrant_point_count": qdrant_point_count,
        "redis_key_count":    redis_key_count,
        "partial_stores":     dict(partial_stores or {}),
    }


def _build_manifest(
    workspace_id: str,
    run_id: str,
    per_table_rows: dict[str, list[dict[str, Any]]],
    *,
    qdrant_points: list[dict[str, Any]] | None = None,
    redis_keys: list[dict[str, Any]] | None = None,
    partial_stores: dict[str, str] | None = None,
) -> dict[str, Any]:
    return _build_manifest_from_counts(
        workspace_id, run_id,
        {k: len(v) for k, v in per_table_rows.items()},
        qdrant_point_count=len(qdrant_points or []),
        redis_key_count=len(redis_keys or []),
        partial_stores=partial_stores,
    )


def _serialise_jsonl_gz(
    manifest: dict[str, Any],
    per_table_rows: dict[str, list[dict[str, Any]]],
    *,
    qdrant_points: list[dict[str, Any]] | None = None,
    redis_keys: list[dict[str, Any]] | None = None,
) -> bytes:
    """Manifest as line 1, then PG rows (table-tagged), then §11.3-v2
    extra-store rows (section-tagged: qdrant_points / redis_keys).

    The in-memory REFERENCE serialiser. ``run_export`` no longer calls it --
    it streams through ``_GzipSpool`` / ``_assemble_archive`` -- but the
    streamed archive must decode to exactly this, and the tests hold it to
    that.
    """
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=6) as gz:
        gz.write(json.dumps(manifest, sort_keys=True, default=str).encode("utf-8"))
        gz.write(b"\n")
        for output_key, rows in per_table_rows.items():
            for row in rows:
                line = json.dumps(
                    {"table": output_key, "row": row},
                    sort_keys=True, default=str,
                ).encode("utf-8")
                gz.write(line)
                gz.write(b"\n")
        # §11.3-v2 extras — each tagged with `section`
        for section, rows in (
            ("qdrant_points", qdrant_points or []),
            ("redis_keys",    redis_keys or []),
        ):
            for row in rows:
                line = json.dumps(
                    {"section": section, "row": row},
                    sort_keys=True, default=str,
                ).encode("utf-8")
                gz.write(line)
                gz.write(b"\n")
    return buf.getvalue()


def _assemble_archive(
    path: str, manifest: dict[str, Any], spools: list[_GzipSpool],
) -> int:
    """Write the final archive: the manifest member, then each spool's member.

    Blocking file I/O -- call through ``asyncio.to_thread``. Each spool file
    is deleted as soon as it has been copied so peak disk is the archive plus
    the spools still waiting, not twice the whole export. Returns the
    archive's size in bytes.
    """
    with open(path, "wb") as out:
        with gzip.GzipFile(fileobj=out, mode="wb", compresslevel=6) as gz:
            gz.write(json.dumps(manifest, sort_keys=True, default=str).encode("utf-8"))
            gz.write(b"\n")
        for spool in spools:
            spool.close()
            if spool.lines:
                with open(spool.path, "rb") as src:
                    shutil.copyfileobj(src, out, 1024 * 1024)
            os.unlink(spool.path)
    return os.path.getsize(path)


async def _upload_file_s3(bucket: str, key: str, path: str) -> None:
    # bucket is the resolved physical name (the configured EXPORTS bucket,
    # or an operator override -- see run_export below), so this uses the
    # raw-client escape hatch (async_client_kwargs) rather than the
    # higher-level AsyncObjectStorage interface, which takes a logical Bucket.
    #
    # upload_file, not put_object: it streams from disk and switches to a
    # multipart upload past 8 MiB, so the archive is never in memory (and is
    # not subject to put_object's 5 GiB single-request ceiling).
    session = aioboto3.Session()
    async with session.client("s3", **async_client_kwargs(StorageConfig.from_env())) as s3:
        await s3.upload_file(Filename=path, Bucket=bucket, Key=key)


@workspace_export.task(execution_timeout="30m")
async def run_export(
    input: WorkspaceExportInput, ctx: Context,
) -> WorkspaceExportOutput:
    started_at = datetime.now(tz=UTC)
    workspace_id = str(input.workspace_id)
    # The configured EXPORTS bucket unless the operator named one (the trigger
    # route pins it to the configured one).
    bucket = input.bucket or exports_bucket()

    conn = await asyncpg.connect(_build_dsn(), statement_cache_size=0)
    try:
        # SEC-4: bind the tenant BEFORE the first read. This connection is
        # georag_app (NOBYPASSRLS) in production, and four of the tables
        # below — silver.hypotheses, silver.decision_records,
        # silver.document_passages, targeting.target_recommendations — have
        # fail-closed policies, so without the GUC they returned zero rows
        # and the export reported success with them silently empty.
        # Session scope (is_local=False) is right here and only here: a
        # dedicated connection to the DIRECT host (build_dsn defaults to
        # direct=True), autocommit statements, closed in the finally below.
        await bind_workspace_scope(
            conn,
            workspace_id=workspace_id,
            site="hatchet.workspace_export",
            is_local=False,
        )

        # Verify workspace exists.
        ws_row = await conn.fetchrow(
            "SELECT workspace_id::text AS id FROM silver.workspaces "
            "WHERE workspace_id = $1::uuid",
            workspace_id,
        )
        if ws_row is None:
            raise RuntimeError(f"workspace_id {workspace_id} not found in silver.workspaces")

        from uuid import uuid4
        run_id = str(uuid4())
        object_key = _build_object_key(workspace_id, run_id, started_at)
        partial_stores: dict[str, str] = {}

        # Everything below is spooled to disk, never held: see the module
        # docstring. The directory (and any spool a failure leaves behind) is
        # removed on the way out.
        with tempfile.TemporaryDirectory(prefix="ws-export-") as tmp:
            pg_spool = _GzipSpool(os.path.join(tmp, "pg.jsonl.gz"))
            qdrant_spool = _GzipSpool(os.path.join(tmp, "qdrant.jsonl.gz"))
            redis_spool = _GzipSpool(os.path.join(tmp, "redis.jsonl.gz"))
            spools = [pg_spool, qdrant_spool, redis_spool]
            try:
                # Walk each tenant table, as one consistent snapshot. Opened
                # and closed here so it does not pin the xmin horizon while
                # the (slow, non-transactional) extra stores are read.
                table_row_counts: dict[str, int] = {}
                skipped_tables: dict[str, str] = {}
                async with conn.transaction(isolation="repeatable_read", readonly=True):
                    for output_key, qualified_table in _WORKSPACE_TABLES:
                        try:
                            table_row_counts[output_key] = await _export_one_table(
                                conn, qualified_table, workspace_id, output_key, pg_spool,
                            )
                        except ExportTableUnreadable as exc:
                            # Keep going so one run names EVERY unreadable
                            # table; each had its own savepoint, so the
                            # snapshot is still sound.
                            skipped_tables[output_key] = exc.reason
                if skipped_tables:
                    # An archive with a silently empty section restores as a
                    # workspace missing that data, and nothing says so. Fail
                    # before anything is uploaded.
                    raise RuntimeError(
                        f"workspace_export {workspace_id}: {len(skipped_tables)} listed "
                        f"table(s) could not be read, so no archive was written: "
                        + "; ".join(f"{k}: {v}" for k, v in sorted(skipped_tables.items()))
                    )

                # §11.3-v2 — walk the 2 extra stores. Each failure is
                # recorded in partial_stores but does NOT fail the export (PG
                # already ran successfully + that's the must-preserve store).
                if input.include_qdrant:
                    from app.hatchet_workflows._export_extras import stream_qdrant_workspace
                    _, q_err = await stream_qdrant_workspace(
                        workspace_id,
                        lambda point: qdrant_spool.write_json(
                            {"section": "qdrant_points", "row": point},
                        ),
                    )
                    if q_err:
                        # A half-scrolled section is not a usable section.
                        partial_stores["qdrant"] = q_err
                        qdrant_spool.reset()
                if input.include_redis:
                    from app.hatchet_workflows._export_extras import export_redis_workspace
                    redis_keys, r_err = await export_redis_workspace(workspace_id)
                    if r_err:
                        partial_stores["redis"] = r_err
                    for row in redis_keys:
                        redis_spool.write_json({"section": "redis_keys", "row": row})

                # Manifest (needs the final counts) + assemble + upload.
                manifest = _build_manifest_from_counts(
                    workspace_id, run_id, table_row_counts,
                    qdrant_point_count=qdrant_spool.lines,
                    redis_key_count=redis_spool.lines,
                    partial_stores=partial_stores,
                    skipped_tables=skipped_tables,
                )
                archive_path = os.path.join(tmp, "archive.jsonl.gz")
                archive_bytes = await asyncio.to_thread(
                    _assemble_archive, archive_path, manifest, spools,
                )
                await _upload_file_s3(bucket, object_key, archive_path)
            finally:
                for spool in spools:
                    spool.close()

        qdrant_point_count = manifest["qdrant_point_count"]
        redis_key_count = manifest["redis_key_count"]

        completed_at = datetime.now(tz=UTC)
        rows_exported = sum(table_row_counts.values())
        per_table_counts = manifest["table_row_counts"]

        # Audit anchor.
        await emit_audit(
            conn,
            action_type="workspace.export.completed",
            workspace_id=workspace_id,
            actor_id=None,
            actor_kind="workflow",
            target_schema="silver",
            target_table="workspaces",
            target_id=workspace_id,
            payload={
                "run_id":            run_id,
                "bucket":            bucket,
                "object_key":        object_key,
                "bytes":             archive_bytes,
                "rows_exported":     rows_exported,
                "table_row_counts":  per_table_counts,
                "duration_s":        (completed_at - started_at).total_seconds(),
            },
        )

        log.info(
            "workspace_export OK ws=%s rows=%d bytes=%s key=%s",
            workspace_id, rows_exported, archive_bytes, object_key,
        )

        # Phase 5 admin surface push — drives Admin/ExportGate.
        try:
            from app.services.laravel_bridge import post_admin_surface_updated
            admin_payload = {
                "workflow_kind": "workspace_export",
                "run_id": str(run_id),
                "workspace_id": str(workspace_id),
                "rows_exported": rows_exported,
                "bytes": archive_bytes,
                "status": "success",
            }
            await post_admin_surface_updated(
                surface="workflow-runs",
                affected_props=["workflow_runs"],
                payload=admin_payload,
            )
            await post_admin_surface_updated(
                surface="export-gate",
                affected_props=["results"],
                payload=admin_payload,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "workspace_export: admin surface broadcasts failed run_id=%s err=%s",
                run_id, exc,
            )

        return WorkspaceExportOutput(
            run_id=run_id,
            workspace_id=workspace_id,
            bucket=bucket,
            object_key=object_key,
            bytes=archive_bytes,
            rows_exported=rows_exported,
            per_table=per_table_counts,
            qdrant_point_count=qdrant_point_count,
            redis_key_count=redis_key_count,
            partial_stores=partial_stores,
            started_at=started_at,
            completed_at=completed_at,
        )
    finally:
        await conn.close()


__all__ = [
    "workspace_export",
    "WorkspaceExportInput",
    "WorkspaceExportOutput",
]
