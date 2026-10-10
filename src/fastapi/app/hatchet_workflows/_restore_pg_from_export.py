"""§11.3 wave 1 helper — restore PG rows from a workspace_export manifest.

Reads a JSONL.gz object produced by ``app.hatchet_workflows.workspace_export``
and INSERTs the rows back into their original PG tables under the target
workspace's RLS scope. Supports two URI schemes:

  - file:// — for local testing (the integration test uses this)
  - s3://   — for production (workspace_export writes to SeaweedFS S3)

How a row is written
--------------------

Each row goes to the SERVER as one JSON document and is typed there::

    INSERT INTO t (cols)
    SELECT cols FROM jsonb_populate_record(NULL::t, $1::jsonb)
    ON CONFLICT (<t's primary key, from the catalog>) DO NOTHING

Two things the first version got wrong, and why this shape:

  * ``ON CONFLICT (id)`` was written for every table. Eight of the nine have a
    different primary key (``workspace_id``, ``hypothesis_id`` ...) and
    ``audit.audit_ledger`` has ``(id, created_at)``, so Postgres rejected the
    statement before it looked at a single row. The conflict target is now read
    from ``pg_index`` for each table.
  * The exporter writes timestamps as ISO strings and numerics as strings,
    which asyncpg's ``timestamptz`` / ``numeric`` parameter encoders refuse.
    Handing the server a JSON document lets Postgres parse every value with the
    column's own input function. Two column types need help first, because the
    exporter wrote them in a form the JSON route would misread: ``bytea`` (hex
    without the ``\\x`` prefix) and ``json``/``jsonb`` (asyncpg returns those
    as text, so the export holds a JSON *string*, not the document).

What "restored" means
---------------------

Every exported row ends in exactly one of three buckets, and the result says
how many are in each:

  * ``inserted``         -- the row is now in the target;
  * ``already_present``  -- its primary key was already there. ``DO NOTHING``
    makes a re-run safe and will not overwrite a row edited since the export;
  * ``rejected``         -- the database refused it (foreign key, check, RLS ...)
    or it could not be applied. Counted, sampled in the result and logged at
    WARNING; the caller turns any rejection into a failed restore. Before this
    a rejected row was logged at DEBUG and dropped, and the workflow reported
    success having inserted nothing.

Row order in the file is not dependency order (evidence items are exported
before the passages they point at; a passage can point at its parent chunk),
so a row that fails on a foreign key is retried after the rest of the file
has been applied, for as long as a pass makes progress.

For production "atomic restore" semantics (drop + insert vs. merge),
the operator runs the restore against a fresh target database, not the
source DB.
"""

from __future__ import annotations

import gzip
import io
import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import asyncpg

from app.db import bind_workspace_scope
from app.db.dsn import build_dsn

log = logging.getLogger("georag.hatchet._restore_pg_from_export")


_TABLE_KEY_TO_QUALIFIED = {
    "silver_workspaces":                "silver.workspaces",
    "silver_hypotheses":                "silver.hypotheses",
    "silver_decision_records":          "silver.decision_records",
    "silver_answer_runs":               "silver.answer_runs",
    "silver_evidence_items":            "silver.evidence_items",
    "silver_document_passages":         "silver.document_passages",
    "audit_ledger_anchors":             "audit.audit_ledger",
    "targeting_target_recommendations": "targeting.target_recommendations",
    "ops_support_tickets":              "ops.support_tickets",
}

#: Rejections described individually in the result and at WARNING. The rest
#: are only counted -- a restore into the wrong database can reject a million
#: rows, and one line each helps nobody.
_MAX_REJECT_SAMPLES = 20


# One DSN builder for the whole service — see app/db/dsn.py for why
# sixty copies of this existed and what the drift cost.
_build_dsn = build_dsn


async def _fetch_manifest_bytes(manifest_uri: str) -> bytes:
    """Resolve file:// or s3:// scheme + return gzipped bytes."""
    parsed = urlparse(manifest_uri)
    if parsed.scheme == "file":
        with open(parsed.path, "rb") as f:
            return f.read()
    if parsed.scheme == "s3":
        # bucket comes straight out of the s3:// URI's netloc — genuinely
        # dynamic, not one of georag_object_storage's four fixed logical
        # Bucket members, so this uses the raw-client escape hatch
        # (async_client_kwargs) rather than the higher-level
        # AsyncObjectStorage interface.
        import aioboto3
        from georag_object_storage import StorageConfig, async_client_kwargs

        bucket = parsed.netloc
        key = parsed.path.lstrip("/")
        session = aioboto3.Session()
        async with session.client("s3", **async_client_kwargs(StorageConfig.from_env())) as s3:
            resp = await s3.get_object(Bucket=bucket, Key=key)
            return await resp["Body"].read()
    raise ValueError(f"unsupported manifest_uri scheme: {parsed.scheme!r}")


def _iter_export_lines(body: bytes) -> Iterator[dict[str, Any]]:
    """Decode the export one JSON line at a time.

    The archive used to be inflated to one string, split into a list of lines
    and parsed into a list of rows -- the whole export, three times over --
    and then parsed AGAIN by ``_restore_extras`` for the Qdrant/Redis
    sections. Iterating the gzip stream keeps one line in memory.
    """
    with gzip.GzipFile(fileobj=io.BytesIO(body), mode="rb") as gz:
        for raw in gz:
            if raw.strip():
                yield json.loads(raw)


def _parse_jsonl_gz(body: bytes) -> tuple[dict[str, Any], list[tuple[str, dict[str, Any]]]]:
    """Return (manifest_dict, [(table_key, row_dict), ...]).
    The first JSONL line is the manifest; subsequent lines are tagged rows.

    Materialises every row -- ``restore_postgres_from_export`` streams through
    ``_iter_export_lines`` instead; this stays for callers (and tests) that
    want the whole list.
    """
    lines = _iter_export_lines(body)
    try:
        manifest = next(lines)
    except StopIteration:
        raise ValueError("empty manifest body") from None
    rows: list[tuple[str, dict[str, Any]]] = []
    for entry in lines:
        rows.append((entry["table"], entry["row"]))
    return manifest, rows


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


@dataclass(frozen=True)
class _TableInfo:
    """What the live catalog says about a restore target."""

    #: Insertable columns (not generated, not GENERATED ALWAYS identity) -> type name.
    columns: dict[str, str]
    #: Primary-key column names; empty if the table has none.
    pk: tuple[str, ...]


_COLUMNS_SQL = (
    "SELECT a.attname AS name, t.typname AS type"
    "  FROM pg_catalog.pg_attribute a"
    "  JOIN pg_catalog.pg_type t ON t.oid = a.atttypid"
    " WHERE a.attrelid = pg_catalog.to_regclass($1)"
    "   AND a.attnum > 0"
    "   AND NOT a.attisdropped"
    "   AND a.attgenerated = ''"
    "   AND a.attidentity <> 'a'"
    " ORDER BY a.attnum"
)

_PK_SQL = (
    "SELECT a.attname AS name"
    "  FROM pg_catalog.pg_index i"
    "  JOIN pg_catalog.pg_attribute a"
    "    ON a.attrelid = i.indrelid AND a.attnum = ANY (i.indkey)"
    " WHERE i.indrelid = pg_catalog.to_regclass($1)"
    "   AND i.indisprimary"
    " ORDER BY a.attnum"
)


async def _load_table_info(conn: asyncpg.Connection, qualified_table: str) -> _TableInfo:
    columns = {r["name"]: r["type"] for r in await conn.fetch(_COLUMNS_SQL, qualified_table)}
    if not columns:
        raise LookupError(f"{qualified_table} does not exist on the restore target")
    pk = tuple(r["name"] for r in await conn.fetch(_PK_SQL, qualified_table))
    return _TableInfo(columns=columns, pk=pk)


def _record_for(info: _TableInfo, row: dict[str, Any]) -> dict[str, Any]:
    """The exported row narrowed to what the target accepts, ready for JSON.

    Columns the target no longer has are dropped (an export from a slightly
    older schema); columns the export lacks are simply not named in the INSERT,
    so they take their DEFAULT. ``bytea`` and ``json``/``jsonb`` values are
    converted back from the form ``workspace_export._row_to_dict`` wrote.
    """
    record: dict[str, Any] = {}
    for name, value in row.items():
        pg_type = info.columns.get(name)
        if pg_type is None:
            continue
        if isinstance(value, str):
            if pg_type == "bytea":
                # Exported as bytes.hex() with no prefix; bytea input would
                # read bare hex as its escape format, i.e. as ASCII text.
                value = value if value.startswith("\\x") else "\\x" + value
            elif pg_type in ("json", "jsonb"):
                # asyncpg returns json/jsonb as TEXT, so the export holds a
                # JSON string. Embedded as-is it would become a jsonb string
                # scalar rather than the document it describes.
                value = json.loads(value)
        record[name] = value
    return record


def _insert_sql(qualified_table: str, info: _TableInfo, columns: list[str]) -> str:
    col_list = ", ".join(_quote_ident(c) for c in columns)
    if info.pk:
        conflict = "ON CONFLICT (" + ", ".join(_quote_ident(c) for c in info.pk) + ") DO NOTHING"
    else:
        conflict = "ON CONFLICT DO NOTHING"
    return (
        f"INSERT INTO {qualified_table} ({col_list}) "
        f"SELECT {col_list} FROM jsonb_populate_record(NULL::{qualified_table}, $1::jsonb) "
        f"{conflict}"
    )


_INSERTED = "inserted"
_PRESENT = "already_present"


class RowRejected(Exception):
    """A row the restore refuses before it reaches the database."""


async def _insert_row(
    conn: asyncpg.Connection, qualified_table: str, info: _TableInfo, row: dict[str, Any],
    *, workspace_id: str | None = None,
) -> str:
    """Insert one row in its own transaction.

    Returns ``"inserted"`` or ``"already_present"`` (the primary key was
    there; nothing was written). Raises on anything else and leaves the
    connection usable: the caller classifies the error -- a foreign-key miss
    is retried later, everything else is a rejected row.

    One transaction per row, deliberately: a failing statement aborts its
    transaction, so a retry or a "fallback" statement inside the same one dies
    with 25P02 and says nothing about the row.
    """
    # Defence in depth on top of RLS: a row for another workspace is refused
    # here even where a policy would let it through (audit_ledger admits
    # NULL-workspace rows). The manifest-level check only reads line 1.
    if workspace_id is not None and "workspace_id" in info.columns and "workspace_id" in row:
        row_ws = row["workspace_id"]
        if row_ws is None or str(row_ws).lower() != workspace_id.lower():
            raise RowRejected(
                f"row belongs to workspace {row_ws!r}, not the restore target {workspace_id}"
            )
    record = _record_for(info, row)
    if not record:
        raise RowRejected("none of the exported columns exist on the target table")
    sql = _insert_sql(qualified_table, info, list(record))
    async with conn.transaction():
        status = await conn.execute(sql, json.dumps(record, default=str))
    return _INSERTED if status.rsplit(" ", 1)[-1] != "0" else _PRESENT


@dataclass
class _Tally:
    inserted: dict[str, int] = field(default_factory=dict)
    already_present: dict[str, int] = field(default_factory=dict)
    rejected: dict[str, int] = field(default_factory=dict)
    samples: list[dict[str, Any]] = field(default_factory=list)
    #: rows read from the file, by export table key (for the manifest check)
    seen_by_key: dict[str, int] = field(default_factory=dict)
    tables: list[str] = field(default_factory=list)

    def landed(self, table: str, outcome: str) -> None:
        bucket = self.inserted if outcome == _INSERTED else self.already_present
        bucket[table] = bucket.get(table, 0) + 1

    def reject(
        self, table: str, info: _TableInfo | None, row: dict[str, Any], exc: BaseException,
    ) -> None:
        self.rejected[table] = self.rejected.get(table, 0) + 1
        sqlstate = getattr(exc, "sqlstate", None)
        reason = getattr(exc, "message", None) or str(exc) or type(exc).__name__
        key = {c: str(row.get(c)) for c in (info.pk if info else ())}
        if len(self.samples) < _MAX_REJECT_SAMPLES:
            sample = {"table": table, "key": key, "sqlstate": sqlstate, "reason": reason}
            self.samples.append(sample)
            log.warning(
                "restore_postgres_from_export: %s row rejected key=%s sqlstate=%s: %s",
                table, key, sqlstate, reason,
            )

    @property
    def total_inserted(self) -> int:
        return sum(self.inserted.values())

    @property
    def total_already_present(self) -> int:
        return sum(self.already_present.values())

    @property
    def total_rejected(self) -> int:
        return sum(self.rejected.values())


def _is_connection_failure(exc: BaseException) -> bool:
    """The connection died or was never usable -- not a verdict on one row."""
    return isinstance(
        exc,
        (
            asyncpg.exceptions.PostgresConnectionError,
            asyncpg.exceptions.InterfaceError,
            ConnectionError,
        ),
    )


async def restore_postgres_from_export(
    workspace_id: str,
    manifest_uri: str,
    *,
    body: bytes | None = None,
    sections_out: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Top-level PG restore. Returns a dict with per-table row counts.

    ``body`` is the already-fetched archive (``restore_workspace`` fetches it
    once and shares it); without it the archive is read from ``manifest_uri``.
    ``sections_out``, when given, is filled with the §11.3-v2 extra-store lines
    (``qdrant_points`` / ``redis_keys``) met on the way, so the caller does not
    decode the archive a second time to find them.

    Raises ValueError if the manifest's workspace_id doesn't match the
    target workspace (refuses cross-workspace restore — operator must
    re-export with the correct workspace_id if the intent is clone).

    A rejected row does NOT raise: it is counted in ``total_rows_rejected`` and
    the caller decides (``restore_workspace`` fails the run on any).
    """
    if body is None:
        body = await _fetch_manifest_bytes(manifest_uri)
    entries = _iter_export_lines(body)
    try:
        manifest = next(entries)
    except StopIteration:
        raise ValueError("empty manifest body") from None

    if manifest.get("format") != "workspace_export":
        raise ValueError(
            f"manifest is not a workspace_export "
            f"(format={manifest.get('format')!r})"
        )
    mani_ws = manifest.get("workspace_id")
    if mani_ws and mani_ws != workspace_id:
        raise ValueError(
            f"manifest workspace_id={mani_ws} does not match target "
            f"workspace_id={workspace_id} (refusing cross-workspace restore)"
        )
    if manifest.get("skipped_tables"):
        raise ValueError(
            "export is partial -- these tables could not be read when it was "
            f"taken: {sorted(manifest['skipped_tables'])}; refusing to restore "
            "from it (re-run workspace_export)"
        )

    tally = _Tally()
    infos: dict[str, _TableInfo] = {}
    #: (table, row) for rows that hit a foreign key before their parent
    deferred: list[tuple[str, dict[str, Any]]] = []

    conn = await asyncpg.connect(_build_dsn(), statement_cache_size=0)
    try:
        # Set RLS scope so writes land under the target workspace.
        #
        # is_local=False: this is a dedicated asyncpg.connect() (above)
        # with no wrapping transaction -- each row below gets its own
        # transaction. SET LOCAL would be discarded here, so the scope
        # these writes depend on was never actually applied.
        await bind_workspace_scope(
            conn, workspace_id=workspace_id,
            site="hatchet.restore_pg_from_export", is_local=False,
        )

        async def _apply(qualified: str, row: dict[str, Any]) -> BaseException | None:
            """Apply one row; the error if it must be deferred or rejected."""
            try:
                outcome = await _insert_row(
                    conn, qualified, infos[qualified], row, workspace_id=workspace_id,
                )
            except Exception as exc:  # noqa: BLE001
                if _is_connection_failure(exc):
                    raise
                return exc
            tally.landed(qualified, outcome)
            return None

        for entry in entries:
            if "section" in entry:
                if sections_out is not None:
                    sections_out.setdefault(entry["section"], []).append(entry.get("row"))
                continue
            table_key = entry.get("table")
            row = entry.get("row")
            tally.seen_by_key[table_key] = tally.seen_by_key.get(table_key, 0) + 1
            qualified = _TABLE_KEY_TO_QUALIFIED.get(table_key)
            if qualified is None:
                tally.reject(
                    str(table_key), None, row or {},
                    RowRejected(f"unknown table key {table_key!r}; this restore cannot place it"),
                )
                continue
            if qualified not in tally.tables:
                tally.tables.append(qualified)
            if qualified not in infos:
                try:
                    infos[qualified] = await _load_table_info(conn, qualified)
                except LookupError as exc:
                    # Same fate for every row of this table.
                    infos[qualified] = _TableInfo(columns={}, pk=())
                    tally.reject(qualified, None, row or {}, exc)
                    continue
            if not infos[qualified].columns:
                tally.reject(
                    qualified, None, row or {},
                    LookupError(f"{qualified} does not exist on the restore target"),
                )
                continue
            if not isinstance(row, dict):
                tally.reject(qualified, infos[qualified], {}, RowRejected("row is not an object"))
                continue
            err = await _apply(qualified, row)
            if err is None:
                continue
            if isinstance(err, asyncpg.exceptions.ForeignKeyViolationError):
                deferred.append((qualified, row))
            else:
                tally.reject(qualified, infos[qualified], row, err)

        # Rows that pointed at something not yet restored: go again until a
        # pass places nothing new. Whatever is left has a parent that is not
        # in the export or the target, and is rejected with the database's
        # own words.
        while deferred:
            remaining: list[tuple[str, dict[str, Any]]] = []
            stuck: list[tuple[str, dict[str, Any], BaseException]] = []
            for qualified, row in deferred:
                err = await _apply(qualified, row)
                if err is None:
                    continue
                if isinstance(err, asyncpg.exceptions.ForeignKeyViolationError):
                    remaining.append((qualified, row))
                    stuck.append((qualified, row, err))
                else:
                    tally.reject(qualified, infos[qualified], row, err)
            if len(remaining) == len(deferred):
                for qualified, row, err in stuck:
                    tally.reject(qualified, infos[qualified], row, err)
                break
            deferred = remaining
    finally:
        await conn.close()

    # The file must hold what its own manifest says it holds.
    count_mismatches: dict[str, dict[str, int]] = {}
    for key, claimed in (manifest.get("table_row_counts") or {}).items():
        found = tally.seen_by_key.get(key, 0)
        if found != claimed:
            count_mismatches[key] = {"manifest": int(claimed), "file": found}

    rows_in_export = sum(tally.seen_by_key.values())
    if tally.total_rejected or count_mismatches:
        log.warning(
            "restore_postgres_from_export ws=%s INCOMPLETE rows_in_export=%d "
            "inserted=%d already_present=%d rejected=%d by_table=%s count_mismatches=%s",
            workspace_id, rows_in_export, tally.total_inserted,
            tally.total_already_present, tally.total_rejected,
            tally.rejected, count_mismatches,
        )
    else:
        log.info(
            "restore_postgres_from_export ws=%s tables=%d rows_in_export=%d "
            "inserted=%d already_present=%d",
            workspace_id, len(tally.tables), rows_in_export,
            tally.total_inserted, tally.total_already_present,
        )
    return {
        "manifest_workspace_id":      mani_ws,
        "manifest_version":           manifest.get("manifest_version", "1.0"),
        "tables":                     tally.tables,
        "rows_in_export":             rows_in_export,
        "rows_inserted":              tally.inserted,
        "rows_already_present":       tally.already_present,
        "rows_rejected":              tally.rejected,
        "rejected_samples":           tally.samples,
        "count_mismatches":           count_mismatches,
        "total_rows_inserted":        tally.total_inserted,
        "total_rows_already_present": tally.total_already_present,
        "total_rows_rejected":        tally.total_rejected,
    }


def restore_shortfall(result: dict[str, Any]) -> str | None:
    """Why a ``restore_postgres_from_export`` result is not a restore, or None.

    A restore is complete when no row was rejected, the file held what its
    manifest said, and -- if the export had any rows at all -- at least one
    landed (inserted now, or already there from an earlier run). The last
    clause is redundant with the first two while the counts are right; it is
    here so that a counting bug can never turn "nothing was written" back
    into success.
    """
    rejected = int(result.get("total_rows_rejected", 0))
    mismatches = result.get("count_mismatches") or {}
    in_export = int(result.get("rows_in_export", 0))
    landed = int(result.get("total_rows_inserted", 0)) + int(
        result.get("total_rows_already_present", 0)
    )
    problems: list[str] = []
    if rejected:
        by_table = ", ".join(f"{t}={n}" for t, n in sorted(result.get("rows_rejected", {}).items()))
        first = (result.get("rejected_samples") or [{}])[0]
        problems.append(
            f"{rejected} of {in_export} exported rows were rejected ({by_table}); "
            f"first: {first.get('table')} sqlstate={first.get('sqlstate')} {first.get('reason')}"
        )
    if mismatches:
        problems.append(f"the file does not hold the rows its manifest lists: {mismatches}")
    if in_export > 0 and landed == 0 and not rejected:
        problems.append(f"none of the {in_export} exported rows landed")
    return "; ".join(problems) or None


__all__ = ["restore_postgres_from_export", "restore_shortfall"]
