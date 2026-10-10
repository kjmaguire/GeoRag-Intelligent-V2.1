"""workspace_export: streaming, explicit columns, identical archive format.

Database audit 2026-10: ``run_export`` did ``SELECT *`` on every table into
Python lists, held every Qdrant vector in another, gzipped into a ``BytesIO``
and ``put_object``-ed it. It now streams through server-side cursors into
spooled gzip members and uploads from disk.

The contract these tests hold: the decoded archive is EXACTLY what the
reference in-memory serialiser (``_serialise_jsonl_gz``) produces, so
``restore_workspace`` (``gzip.GzipFile`` readers) is unaffected.
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import os
import pathlib
import uuid
from contextlib import asynccontextmanager
from typing import Any

import pytest

from app.hatchet_workflows import workspace_export as we

WS = "11111111-1111-4111-8111-111111111111"


class _FakeConn:
    """asyncpg stand-in: catalogue columns, cursors, savepoint-aware transactions."""

    def __init__(
        self,
        tables: dict[str, tuple[list[str], list[dict[str, Any]]]],
        *,
        fail_after: dict[str, int] | None = None,
        unreadable: set[str] | None = None,
    ) -> None:
        self.tables = tables
        self.fail_after = fail_after or {}
        self.unreadable = unreadable or set()
        self.queries: list[str] = []
        self.query_args: list[tuple[Any, ...]] = []
        self.prefetches: list[int | None] = []
        self.txn_calls: list[dict[str, Any]] = []
        self.txn_depth = 0
        self.max_txn_depth = 0

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        assert "pg_attribute" in sql, sql
        cols, _ = self.tables.get(args[0], ([], []))
        return [{"column_name": c} for c in cols]

    def cursor(self, query: str, *args: Any, prefetch: int | None = None):  # noqa: ANN201
        self.queries.append(query)
        self.query_args.append(args)
        self.prefetches.append(prefetch)
        table = query.split(" FROM ", 1)[1].split()[0]
        _, rows = self.tables[table]
        limit = self.fail_after.get(table)
        unreadable = table in self.unreadable

        async def _gen():  # noqa: ANN202
            if unreadable:
                raise PermissionError(f"permission denied for table {table}")
            for i, row in enumerate(rows):
                if limit is not None and i >= limit:
                    raise ConnectionResetError("lost the connection mid-table")
                yield row

        return _gen()

    def transaction(self, **kwargs: Any):  # noqa: ANN201
        self.txn_calls.append(kwargs)
        conn = self

        @asynccontextmanager
        async def _tx():  # noqa: ANN202
            conn.txn_depth += 1
            conn.max_txn_depth = max(conn.max_txn_depth, conn.txn_depth)
            try:
                yield conn
            finally:
                conn.txn_depth -= 1

        return _tx()


def _rows() -> dict[str, tuple[list[str], list[dict[str, Any]]]]:
    when = dt.datetime(2026, 10, 4, 12, 0, tzinfo=dt.UTC)
    return {
        "silver.workspaces": (
            ["workspace_id", "name"], [{"workspace_id": uuid.UUID(WS), "name": "W"}],
        ),
        "silver.hypotheses": (
            ["id", "workspace_id", "created_at", "blob"],
            [
                {"id": uuid.uuid4(), "workspace_id": uuid.UUID(WS), "created_at": when, "blob": b"\x00\xff"},
                {"id": uuid.uuid4(), "workspace_id": uuid.UUID(WS), "created_at": when, "blob": None},
            ],
        ),
        # No workspace_id column: read through RLS alone, as before.
        "ops.support_tickets": (["id", "subject"], [{"id": 1, "subject": "x"}]),
        # Exists, has no rows for this workspace: a legitimately empty section.
        "targeting.target_recommendations": (["recommendation_id", "workspace_id"], []),
    }


def _spool(tmp_path, name: str = "s.jsonl.gz") -> we._GzipSpool:  # noqa: ANN001
    return we._GzipSpool(str(tmp_path / name))


def _decode(path: str) -> bytes:
    with gzip.GzipFile(path, "rb") as gz:
        return gz.read()


# ---------------------------------------------------------------------------
# The query
# ---------------------------------------------------------------------------


async def test_the_select_has_an_explicit_quoted_column_list_not_a_star(tmp_path) -> None:  # noqa: ANN001
    conn = _FakeConn(_rows())
    sp = _spool(tmp_path)
    n = await we._export_one_table(conn, "silver.hypotheses", WS, "silver_hypotheses", sp)  # type: ignore[arg-type]
    sp.close()

    assert n == 2
    (query,) = conn.queries
    assert "*" not in query
    assert query.startswith('SELECT "id", "workspace_id", "created_at", "blob" FROM silver.hypotheses')
    assert "WHERE workspace_id = $1::uuid" in query
    assert conn.query_args == [(WS,)]


async def test_the_workspace_row_is_keyed_on_its_pk_and_a_table_without_the_column_is_unfiltered(
    tmp_path,  # noqa: ANN001
) -> None:
    conn = _FakeConn(_rows())
    sp = _spool(tmp_path)
    await we._export_one_table(conn, "silver.workspaces", WS, "silver_workspaces", sp)  # type: ignore[arg-type]
    await we._export_one_table(conn, "ops.support_tickets", WS, "ops_support_tickets", sp)  # type: ignore[arg-type]
    sp.close()

    assert "WHERE workspace_id = $1::uuid" in conn.queries[0]
    assert "WHERE" not in conn.queries[1]
    assert conn.query_args[1] == ()


async def test_the_cursor_is_paged(tmp_path) -> None:  # noqa: ANN001
    conn = _FakeConn(_rows())
    sp = _spool(tmp_path)
    await we._export_one_table(conn, "silver.hypotheses", WS, "k", sp)  # type: ignore[arg-type]
    sp.close()
    assert conn.prefetches == [we._PG_CURSOR_PREFETCH]


def test_a_column_name_is_quoted_not_interpolated() -> None:
    assert we._quote_ident('a"b') == '"a""b"'


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


async def test_a_missing_table_is_reported_not_exported_as_an_empty_section(tmp_path) -> None:  # noqa: ANN001
    conn = _FakeConn({})
    sp = _spool(tmp_path)
    with pytest.raises(we.ExportTableUnreadable, match="does not exist") as err:
        await we._export_one_table(conn, "targeting.target_recommendations", WS, "k", sp)  # type: ignore[arg-type]
    sp.close()
    assert sp.lines == 0
    assert (err.value.output_key, err.value.qualified_table) == ("k", "targeting.target_recommendations")


async def test_an_unreadable_table_is_reported_not_exported_as_an_empty_section(tmp_path) -> None:  # noqa: ANN001
    conn = _FakeConn(_rows(), unreadable={"silver.hypotheses"})
    sp = _spool(tmp_path)
    with pytest.raises(we.ExportTableUnreadable, match="permission denied"):
        await we._export_one_table(conn, "silver.hypotheses", WS, "k", sp)  # type: ignore[arg-type]
    sp.close()
    assert sp.lines == 0


async def test_a_table_that_is_merely_empty_is_still_exported_as_zero_rows(tmp_path) -> None:  # noqa: ANN001
    conn = _FakeConn(_rows())
    sp = _spool(tmp_path)
    assert await we._export_one_table(  # type: ignore[arg-type]
        conn, "targeting.target_recommendations", WS, "k", sp,
    ) == 0
    sp.close()


async def test_a_failure_after_rows_were_written_is_not_swallowed(tmp_path) -> None:  # noqa: ANN001
    conn = _FakeConn(_rows(), fail_after={"silver.hypotheses": 1})
    sp = _spool(tmp_path)
    with pytest.raises(ConnectionResetError):
        await we._export_one_table(conn, "silver.hypotheses", WS, "k", sp)  # type: ignore[arg-type]
    sp.close()


# ---------------------------------------------------------------------------
# The archive is byte-for-byte the old format once decoded
# ---------------------------------------------------------------------------


async def test_streamed_archive_decodes_to_the_reference_serialisation(tmp_path) -> None:  # noqa: ANN001
    tables = _rows()
    conn = _FakeConn(tables)
    pg = _spool(tmp_path, "pg.gz")
    neo = _spool(tmp_path, "neo.gz")
    qd = _spool(tmp_path, "qd.gz")
    rd = _spool(tmp_path, "rd.gz")

    keyed = [
        ("silver_workspaces", "silver.workspaces"),
        ("silver_hypotheses", "silver.hypotheses"),
        ("ops_support_tickets", "ops.support_tickets"),
    ]
    counts: dict[str, int] = {}
    reference_rows: dict[str, list[dict[str, Any]]] = {}
    for key, table in keyed:
        counts[key] = await we._export_one_table(conn, table, WS, key, pg)  # type: ignore[arg-type]
        reference_rows[key] = [we._row_to_dict(r) for r in tables[table][1]]

    points = [{"id": "p1", "vector": [0.1, 0.2], "payload": {"workspace_id": WS}},
              {"id": 7, "vector": None, "payload": {}}]
    keys = [{"key": f"georag:ws:{WS}:a", "type": "string", "ttl_s": 30, "value_b64": "AA=="}]
    for p in points:
        qd.write_json({"section": "qdrant_points", "row": p})
    for k in keys:
        rd.write_json({"section": "redis_keys", "row": k})

    manifest = we._build_manifest_from_counts(
        WS, "run-1", counts, qdrant_point_count=qd.lines, redis_key_count=rd.lines,
    )
    reference_manifest = we._build_manifest(
        WS, "run-1", reference_rows, qdrant_points=points, redis_keys=keys,
    )
    # captured_at is wall-clock; everything else must agree.
    reference_manifest["captured_at"] = manifest["captured_at"]
    assert manifest == reference_manifest

    out = str(tmp_path / "archive.gz")
    size = we._assemble_archive(out, manifest, [pg, neo, qd, rd])
    assert size == os.path.getsize(out)

    expected = we._serialise_jsonl_gz(
        reference_manifest, reference_rows, qdrant_points=points, redis_keys=keys,
    )
    assert _decode(out) == gzip.decompress(expected)
    # Spools are gone once copied (peak disk is bounded).
    assert not any(os.path.exists(s.path) for s in (pg, neo, qd, rd))


async def test_the_restore_readers_accept_the_streamed_archive(tmp_path) -> None:  # noqa: ANN001
    from app.hatchet_workflows import _restore_extras, _restore_pg_from_export

    conn = _FakeConn(_rows())
    pg = _spool(tmp_path, "pg.gz")
    qd = _spool(tmp_path, "qd.gz")
    counts = {"silver_hypotheses": await we._export_one_table(
        conn, "silver.hypotheses", WS, "silver_hypotheses", pg,  # type: ignore[arg-type]
    )}
    qd.write_json({"section": "qdrant_points", "row": {"id": "p", "vector": [1.0], "payload": {}}})

    # PG-only archive: the PG reader (which has only ever understood table
    # lines) takes it, across the manifest-member / body-member boundary.
    out_pg = str(tmp_path / "pg_only.gz")
    pg_only = we._build_manifest_from_counts(WS, "r", counts)
    pg2 = _spool(tmp_path, "pg2.gz")
    await we._export_one_table(conn, "silver.hypotheses", WS, "silver_hypotheses", pg2)  # type: ignore[arg-type]
    we._assemble_archive(out_pg, pg_only, [pg2])
    m1, rows = _restore_pg_from_export._parse_jsonl_gz(pathlib.Path(out_pg).read_bytes())
    assert m1["table_row_counts"] == {"silver_hypotheses": 2}
    assert [t for t, _ in rows] == ["silver_hypotheses"] * 2

    # Full archive: the extras reader takes tables and sections alike.
    out = str(tmp_path / "full.gz")
    manifest = we._build_manifest_from_counts(WS, "r", counts, qdrant_point_count=1)
    we._assemble_archive(out, manifest, [pg, qd])
    m2, tables, sections = _restore_extras.parse_export_jsonl_gz(pathlib.Path(out).read_bytes())
    assert len(tables["silver_hypotheses"]) == 2
    assert sections["qdrant_points"][0]["id"] == "p"
    assert m2["qdrant_point_count"] == 1


def test_an_empty_spool_contributes_nothing(tmp_path) -> None:  # noqa: ANN001
    sp = _spool(tmp_path)
    manifest = we._build_manifest_from_counts(WS, "r", {})
    out = str(tmp_path / "a.gz")
    we._assemble_archive(out, manifest, [sp])
    lines = _decode(out).splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["format"] == "workspace_export"


def test_a_reset_spool_forgets_what_it_held(tmp_path) -> None:  # noqa: ANN001
    sp = _spool(tmp_path)
    sp.write_json({"a": 1})
    sp.reset()
    assert sp.lines == 0
    sp.write_json({"b": 2})
    sp.close()
    assert json.loads(_decode(sp.path)) == {"b": 2}


# ---------------------------------------------------------------------------
# Qdrant: paged, not accumulated
# ---------------------------------------------------------------------------


class _Pt:
    def __init__(self, i: int) -> None:
        self.id = f"p{i}"
        self.vector = [float(i)] * 3
        self.payload = {"workspace_id": WS}


class _FakeQdrant:
    pages: list[list[_Pt]] = []
    calls: list[dict[str, Any]] = []
    fail_on_page: int | None = None

    def __init__(self, **_k: Any) -> None:
        pass

    async def scroll(self, **kw: Any):  # noqa: ANN201
        type(self).calls.append(kw)
        n = len(type(self).calls)
        if type(self).fail_on_page == n:
            raise RuntimeError("qdrant fell over")
        i = n - 1
        batch = type(self).pages[i] if i < len(type(self).pages) else []
        nxt = "next" if i + 1 < len(type(self).pages) else None
        return batch, nxt

    async def close(self) -> None:
        return None


@pytest.fixture
def fake_qdrant(monkeypatch):  # noqa: ANN001, ANN201
    import qdrant_client

    _FakeQdrant.pages = [[_Pt(0), _Pt(1)], [_Pt(2)]]
    _FakeQdrant.calls = []
    _FakeQdrant.fail_on_page = None
    monkeypatch.setattr(qdrant_client, "AsyncQdrantClient", _FakeQdrant)
    return _FakeQdrant


async def test_qdrant_is_scrolled_page_by_page_into_the_sink(fake_qdrant) -> None:  # noqa: ANN001
    from app.hatchet_workflows import _export_extras as ex

    seen: list[dict[str, Any]] = []
    n, err = await ex.stream_qdrant_workspace(WS, seen.append)
    assert (n, err) == (3, None)
    assert [p["id"] for p in seen] == ["p0", "p1", "p2"]
    assert len(fake_qdrant.calls) == 2
    assert all(c["limit"] == ex._QDRANT_SCROLL_PAGE and c["with_vectors"] for c in fake_qdrant.calls)
    assert fake_qdrant.calls[1]["offset"] == "next"


async def test_the_list_returning_export_keeps_its_old_contract(fake_qdrant) -> None:  # noqa: ANN001
    from app.hatchet_workflows import _export_extras as ex

    points, err = await ex.export_qdrant_workspace(WS)
    assert err is None and [p["id"] for p in points] == ["p0", "p1", "p2"]

    fake_qdrant.calls = []
    fake_qdrant.fail_on_page = 2
    points, err = await ex.export_qdrant_workspace(WS)
    assert points == [] and err is not None and "qdrant_export_failed" in err


# ---------------------------------------------------------------------------
# run_export, end to end against fakes
# ---------------------------------------------------------------------------


class _RunConn(_FakeConn):
    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any]:
        return {"id": WS}

    async def close(self) -> None:
        return None


async def _run(  # noqa: ANN202
    monkeypatch, tmp_path, conn, *, qdrant_error: str | None = None,  # noqa: ANN001
    uploaded: dict[str, Any] | None = None,
):
    from app.hatchet_workflows import _export_extras as ex

    uploaded = {} if uploaded is None else uploaded

    async def _connect(*_a: Any, **_k: Any) -> _RunConn:
        return conn

    async def _noop(*_a: Any, **_k: Any) -> None:
        return None

    async def _upload(bucket: str, key: str, path: str) -> None:
        uploaded.update(bucket=bucket, key=key, body=pathlib.Path(path).read_bytes())

    async def _stream(workspace_id: str, emit, collection_name: str = "x"):  # noqa: ANN001, ANN202
        for i in range(3):
            emit({"id": f"p{i}", "vector": [0.5], "payload": {"workspace_id": workspace_id}})
        return 3, qdrant_error

    async def _redis(_ws: str):  # noqa: ANN202
        return [], None

    monkeypatch.setattr(we.asyncpg, "connect", _connect)
    monkeypatch.setattr(we, "bind_workspace_scope", _noop)
    monkeypatch.setattr(we, "emit_audit", _noop)
    monkeypatch.setattr(we, "_upload_file_s3", _upload)
    monkeypatch.setattr(we, "_build_dsn", lambda *a, **k: "postgres://x/y")
    monkeypatch.setattr(ex, "stream_qdrant_workspace", _stream)
    monkeypatch.setattr(ex, "export_redis_workspace", _redis)
    monkeypatch.setattr(
        we, "_WORKSPACE_TABLES",
        [("silver_workspaces", "silver.workspaces"),
         ("silver_hypotheses", "silver.hypotheses"),
         ("ops_support_tickets", "ops.support_tickets"),
         ("targeting_target_recommendations", "targeting.target_recommendations")],
    )
    out = await we.run_export.fn(we.WorkspaceExportInput(workspace_id=WS), None)  # type: ignore[arg-type]
    return out, uploaded


async def test_run_export_streams_uploads_from_disk_and_reports_counts(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    # What Terraform sets (config.tf): the real EXPORTS bucket, not a bare
    # "workspace-exports" that the account never creates.
    monkeypatch.setenv("AWS_BUCKET_EXPORTS", "georag-exports-123456789012")
    conn = _RunConn(_rows())
    out, uploaded = await _run(monkeypatch, tmp_path, conn)

    # One consistent read-only snapshot for the PG walk.
    assert {"isolation": "repeatable_read", "readonly": True} in conn.txn_calls
    # Each table got its own savepoint inside it.
    assert conn.max_txn_depth == 2

    assert uploaded["bucket"] == out.bucket == "georag-exports-123456789012"
    assert uploaded["key"] == out.object_key
    assert uploaded["key"].startswith(f"workspace-exports/{WS}/"), "the old bucket name is the key prefix now"
    assert out.bytes == len(uploaded["body"])
    assert out.per_table == {
        "silver_workspaces": 1, "silver_hypotheses": 2,
        "ops_support_tickets": 1, "targeting_target_recommendations": 0,
    }
    assert out.rows_exported == 4
    assert out.qdrant_point_count == 3 and out.redis_key_count == 0
    # Neo4j is gone from the stack: a clean export is NOT reported partial.
    assert out.partial_stores == {}

    lines = gzip.decompress(uploaded["body"]).decode().splitlines()
    manifest = json.loads(lines[0])
    assert manifest["manifest_version"] == "2.0"
    assert manifest["qdrant_point_count"] == 3
    assert manifest["skipped_tables"] == {}
    assert manifest["tables"] == list(out.per_table)
    kinds = [("table" if "table" in json.loads(ln) else "section") for ln in lines[1:]]
    assert kinds == ["table"] * 4 + ["section"] * 3, "PG rows first, then extras, as before"
    # Spool directory is gone.
    assert not [p for p in os.listdir(tempfile_dir()) if p.startswith("ws-export-")]


def tempfile_dir() -> str:
    import tempfile

    return tempfile.gettempdir()


async def test_run_export_fails_naming_every_unreadable_table_and_uploads_nothing(
    monkeypatch, tmp_path,  # noqa: ANN001
) -> None:
    """An unreadable table used to count 0 and ship an empty section; restore
    then restored the gap without a word."""
    rows = _rows()
    del rows["silver.hypotheses"]                      # missing
    conn = _RunConn(rows, unreadable={"ops.support_tickets"})  # present, but not readable
    uploaded: dict[str, Any] = {}
    with pytest.raises(RuntimeError) as err:
        await _run(monkeypatch, tmp_path, conn, uploaded=uploaded)
    text = str(err.value)
    assert "2 listed table(s) could not be read" in text
    assert "silver_hypotheses" in text and "ops_support_tickets" in text
    assert "no archive was written" in text
    assert uploaded == {}, "nothing may be uploaded for a partial export"
    # ... and the spool directory does not outlive the failure.
    assert not [p for p in os.listdir(tempfile_dir()) if p.startswith("ws-export-")]


async def test_a_failed_qdrant_section_is_discarded_and_reported(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    conn = _RunConn(_rows())
    out, uploaded = await _run(monkeypatch, tmp_path, conn, qdrant_error="qdrant_export_failed: boom")

    assert out.qdrant_point_count == 0
    assert out.partial_stores["qdrant"] == "qdrant_export_failed: boom"
    lines = gzip.decompress(uploaded["body"]).decode().splitlines()
    assert not any('"qdrant_points"' in ln for ln in lines[1:])
    assert json.loads(lines[0])["partial_stores"]["qdrant"] == "qdrant_export_failed: boom"


def test_nothing_in_the_module_reads_a_whole_table_into_memory() -> None:
    import inspect

    assert "SELECT *" not in inspect.getsource(we._stream_table)
    assert "conn.fetch(f" not in inspect.getsource(we._export_one_table)
    run_src = inspect.getsource(we.run_export.fn)
    assert "BytesIO" not in run_src and "put_object" not in run_src
    assert not hasattr(we, "_put_s3")


# ---------------------------------------------------------------------------
# Qdrant section: collection + named-vector round trip
# ---------------------------------------------------------------------------


def test_the_qdrant_helpers_default_to_the_collection_ingest_writes() -> None:
    """They defaulted to the legacy ``georag_reports``, which no writer has
    touched since ADR-0010: every export carried zero points or a failure."""
    import inspect

    from app.hatchet_workflows import _export_extras as ex
    from app.hatchet_workflows import _restore_extras as rx

    assert ex._QDRANT_COLLECTION == "georag_chunks"
    for fn in (ex.stream_qdrant_workspace, ex.export_qdrant_workspace, rx.restore_qdrant):
        assert inspect.signature(fn).parameters["collection_name"].default == "georag_chunks"


def test_a_named_dense_plus_sparse_vector_survives_export_and_restore() -> None:
    """georag_chunks points carry {"": dense, "text": SparseVector}. The export
    used ``list(p.vector)``, which turned that mapping into its key names."""
    from qdrant_client.models import SparseVector

    from app.hatchet_workflows._export_extras import _vector_to_json
    from app.hatchet_workflows._restore_extras import _vector_from_json

    original = {"": [0.1, 0.2], "text": SparseVector(indices=[3, 9], values=[0.5, 0.25])}
    exported = _vector_to_json(original)
    assert exported == {"": [0.1, 0.2], "text": {"indices": [3, 9], "values": [0.5, 0.25]}}
    json.dumps(exported)  # JSON-serialisable, unlike a SparseVector

    restored = _vector_from_json(exported)
    assert restored[""] == [0.1, 0.2]
    assert isinstance(restored["text"], SparseVector)
    assert list(restored["text"].indices) == [3, 9]
    # An unnamed vector (older shape) passes straight through.
    assert _vector_to_json([0.5]) == [0.5] and _vector_from_json([0.5]) == [0.5]
