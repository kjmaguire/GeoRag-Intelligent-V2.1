"""§11.3 wave 1 — workspace_export + restore round-trip tests.

Three test surfaces:
  - Unit: serialisation invariants (manifest shape, JSONL streaming,
    row dict normalisation)
  - Unit, scripted connection: how a restore classifies a row (inserted /
    already present / rejected), retries a foreign-key miss, and what the
    workflow does with a restore that did not complete
  - Integration: run_export -> archive -> restore_postgres_from_export against
    a real Postgres, asserting EXACT row counts per table and the restored
    values -- not ">= 0". The first version of this test asserted
    ``total_rows_inserted >= 0`` and called ``_export_one_table`` with a stale
    three-argument signature, so it could neither fail on a restore that wrote
    nothing nor run at all; the restore did, in fact, write nothing
    (``ON CONFLICT (id)`` on tables whose key is not ``id``).
"""
from __future__ import annotations

import gzip
import io
import json
import logging
import os
import pathlib
import secrets
import types
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest

from app.hatchet_workflows import _restore_pg_from_export as rp
from app.hatchet_workflows import workspace_export as we


# ---------------------------------------------------------------------------
# Workflow registration
# ---------------------------------------------------------------------------
def test_workflow_registered() -> None:
    assert we.workspace_export is not None
    assert we.workspace_export.name == "workspace_export"


def test_workflow_in_ai_pool() -> None:
    from app.hatchet_workflows.worker import POOLS
    names = {w.name for w in POOLS["ai"]}
    assert "workspace_export" in names


# ---------------------------------------------------------------------------
# Manifest schema contract
# ---------------------------------------------------------------------------
def test_manifest_shape() -> None:
    m = we._build_manifest(
        workspace_id="11111111-1111-1111-1111-111111111111",
        run_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        per_table_rows={
            "silver_workspaces": [{"workspace_id": "x"}],
            "silver_hypotheses": [],
        },
    )
    assert m["manifest_version"] == "2.0"
    assert m["format"] == "workspace_export"
    assert m["workspace_id"] == "11111111-1111-1111-1111-111111111111"
    assert m["table_row_counts"] == {"silver_workspaces": 1, "silver_hypotheses": 0}
    assert "captured_at" in m


def test_serialise_jsonl_gz_has_manifest_then_rows() -> None:
    manifest = we._build_manifest(
        workspace_id="x", run_id="r",
        per_table_rows={"silver_hypotheses": [{"id": "1", "text": "h1"}, {"id": "2", "text": "h2"}]},
    )
    body = we._serialise_jsonl_gz(
        manifest,
        {"silver_hypotheses": [{"id": "1", "text": "h1"}, {"id": "2", "text": "h2"}]},
    )
    # gzipped
    assert body[:2] == b"\x1f\x8b"
    # parse back
    with gzip.GzipFile(fileobj=io.BytesIO(body), mode="rb") as gz:
        lines = gz.read().decode().splitlines()
    assert len(lines) == 3  # manifest + 2 rows
    first = json.loads(lines[0])
    assert first["format"] == "workspace_export"
    second = json.loads(lines[1])
    assert second["table"] == "silver_hypotheses"
    assert second["row"]["id"] == "1"


def test_row_to_dict_handles_uuid_bytes_datetime() -> None:
    """asyncpg returns native UUID / bytes / datetime; the export must
    coerce them to JSON-safe primitives."""
    import uuid as _u
    class _FakeRow:
        def __init__(self, d): self._d = d
        def items(self): return self._d.items()

    row = _FakeRow({
        "id": _u.UUID("11111111-1111-1111-1111-111111111111"),
        "hash": b"\x01\x02\x03",
        "created_at": datetime(2026, 5, 16, 12, 0, 0, tzinfo=UTC),
        "name": "demo",
    })
    out = we._row_to_dict(row)
    assert out["id"] == "11111111-1111-1111-1111-111111111111"
    assert out["hash"] == "010203"
    assert out["created_at"] == "2026-05-16T12:00:00+00:00"
    assert out["name"] == "demo"


def test_the_audit_ledger_is_exported_oldest_first() -> None:
    """The ledger's BEFORE INSERT trigger rebuilds the hash chain from the
    newest existing row, so a restore reproduces the chain only if rows replay
    oldest-first. The export's SELECT must therefore say so."""
    import asyncio

    class _Conn:
        def __init__(self) -> None:
            self.queries: list[str] = []

        async def fetch(self, _sql: str, *_a: Any) -> list[dict[str, str]]:
            return [{"column_name": c} for c in ("id", "workspace_id", "created_at")]

        def cursor(self, query: str, *_a: Any, **_k: Any):  # noqa: ANN201
            self.queries.append(query)

            async def _g():  # noqa: ANN202
                return
                yield  # pragma: no cover

            return _g()

    async def _run() -> list[str]:
        conn = _Conn()
        for table in ("audit.audit_ledger", "silver.hypotheses"):
            async for _ in we._stream_table(conn, table, "w"):  # type: ignore[arg-type]
                pass
        return conn.queries

    ledger_q, other_q = asyncio.run(_run())
    assert ledger_q.endswith("ORDER BY created_at, id")
    assert "ORDER BY" not in other_q


# ---------------------------------------------------------------------------
# Restore helper — manifest parse + cross-workspace refusal
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_fetch_manifest_bytes_file_scheme(tmp_path: Path) -> None:
    body = b"\x1f\x8btest"  # not real gzip, just bytes
    p = tmp_path / "export.jsonl.gz"
    p.write_bytes(body)
    got = await rp._fetch_manifest_bytes(f"file://{p}")
    assert got == body


@pytest.mark.asyncio
async def test_fetch_manifest_bytes_rejects_unknown_scheme() -> None:
    with pytest.raises(ValueError, match="unsupported manifest_uri scheme"):
        await rp._fetch_manifest_bytes("http://example.com/x")


def test_parse_jsonl_gz_round_trip() -> None:
    manifest = {"format": "workspace_export", "workspace_id": "ws"}
    rows_body = [
        {"table": "silver_hypotheses", "row": {"id": "1"}},
        {"table": "silver_hypotheses", "row": {"id": "2"}},
    ]
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
        gz.write(json.dumps(manifest).encode() + b"\n")
        for e in rows_body:
            gz.write(json.dumps(e).encode() + b"\n")
    parsed_manifest, parsed_rows = rp._parse_jsonl_gz(buf.getvalue())
    assert parsed_manifest["format"] == "workspace_export"
    assert len(parsed_rows) == 2
    assert parsed_rows[0] == ("silver_hypotheses", {"id": "1"})


def _gz(*objs: dict[str, Any]) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
        for o in objs:
            gz.write(json.dumps(o).encode() + b"\n")
    return buf.getvalue()


@pytest.mark.asyncio
async def test_restore_refuses_cross_workspace(tmp_path: Path) -> None:
    """A manifest for workspace A cannot restore into workspace B —
    catches operator typos that would otherwise corrupt tenant data."""
    manifest = {
        "manifest_version": "1.0",
        "format": "workspace_export",
        "workspace_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "tables": [],
        "table_row_counts": {},
    }
    p = tmp_path / "export.jsonl.gz"
    p.write_bytes(_gz(manifest))

    with pytest.raises(ValueError, match="does not match target"):
        await rp.restore_postgres_from_export(
            workspace_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            manifest_uri=f"file://{p}",
        )


@pytest.mark.asyncio
async def test_restore_refuses_an_export_that_says_it_is_partial() -> None:
    body = _gz({
        "format": "workspace_export", "workspace_id": WS,
        "skipped_tables": {"silver_hypotheses": "permission denied"},
    })
    with pytest.raises(ValueError, match="export is partial"):
        await rp.restore_postgres_from_export(WS, "file:///unused", body=body)


# ---------------------------------------------------------------------------
# What a row becomes on the way in (pure functions)
# ---------------------------------------------------------------------------
def test_record_for_undoes_the_exporters_bytea_and_json_encoding() -> None:
    info = rp._TableInfo(
        columns={"id": "uuid", "hash": "bytea", "payload": "jsonb", "n": "numeric", "note": "text"},
        pk=("id",),
    )
    record = rp._record_for(info, {
        "id": "11111111-1111-1111-1111-111111111111",
        "hash": "deadbeef",                       # bytes.hex(), no prefix
        "payload": '{"a": [1, 2], "b": null}',    # asyncpg hands jsonb back as TEXT
        "n": "0.9500",                            # Decimal -> str
        "note": '{"looks": "like json"}',         # a text column must be left alone
        "dropped_since_export": "x",
    })
    assert record == {
        "id": "11111111-1111-1111-1111-111111111111",
        "hash": "\\xdeadbeef",
        "payload": {"a": [1, 2], "b": None},
        "n": "0.9500",
        "note": '{"looks": "like json"}',
    }
    # Already prefixed / already a document: left as they are.
    again = rp._record_for(info, {"hash": "\\xdeadbeef", "payload": {"a": 1}})
    assert again == {"hash": "\\xdeadbeef", "payload": {"a": 1}}


def test_the_conflict_target_is_the_tables_own_primary_key() -> None:
    single = rp._TableInfo(columns={"hypothesis_id": "uuid", "label": "varchar"}, pk=("hypothesis_id",))
    composite = rp._TableInfo(columns={"id": "uuid", "created_at": "timestamptz"}, pk=("id", "created_at"))
    keyless = rp._TableInfo(columns={"x": "int4"}, pk=())

    assert rp._insert_sql("silver.hypotheses", single, ["hypothesis_id", "label"]).endswith(
        'ON CONFLICT ("hypothesis_id") DO NOTHING'
    )
    assert rp._insert_sql("targeting.scores", composite, ["id", "created_at"]).endswith(
        'ON CONFLICT ("id", "created_at") DO NOTHING'
    )
    assert rp._insert_sql("x.y", keyless, ["x"]).endswith("ON CONFLICT DO NOTHING")
    sql = rp._insert_sql("silver.hypotheses", single, ["hypothesis_id", "label"])
    assert "ON CONFLICT (id)" not in sql
    assert "jsonb_populate_record(NULL::silver.hypotheses, $1::jsonb)" in sql
    assert sql.startswith('INSERT INTO silver.hypotheses ("hypothesis_id", "label") SELECT "hypothesis_id", "label" FROM')


def test_a_ledger_row_is_present_when_its_id_is() -> None:
    """The ledger's key is (id, created_at), but the hash trigger stamps
    created_at itself, so a replayed row never matches on the key. Keyed on
    the key, a second restore appended every ledger row again."""
    composite = rp._TableInfo(columns={"id": "uuid", "created_at": "timestamptz"}, pk=("id", "created_at"))

    sql = rp._insert_sql("audit.audit_ledger", composite, ["id", "created_at"])

    assert "ON CONFLICT" not in sql
    assert sql.endswith('WHERE NOT EXISTS (SELECT 1 FROM audit.audit_ledger AS t WHERE t."id" = r."id")')
    assert "jsonb_populate_record(NULL::audit.audit_ledger, $1::jsonb) AS r" in sql


# ---------------------------------------------------------------------------
# Restore against a scripted connection
# ---------------------------------------------------------------------------
WS = "11111111-1111-4111-8111-111111111111"
OTHER_WS = "22222222-2222-4222-8222-222222222222"


class _Err:
    """Constructors for the asyncpg exceptions a real INSERT would raise."""

    @staticmethod
    def fk() -> Exception:
        return asyncpg.exceptions.ForeignKeyViolationError("violates foreign key constraint")

    @staticmethod
    def check() -> Exception:
        return asyncpg.exceptions.CheckViolationError("violates check constraint")

    @staticmethod
    def gone() -> Exception:
        return asyncpg.exceptions.ConnectionDoesNotExistError("connection was closed")


class _ScriptedConn:
    """asyncpg stand-in. ``script(table, record) -> 'INSERT 0 1' | 'INSERT 0 0' | Exception``."""

    def __init__(self, script: Any, *, tables: dict[str, rp._TableInfo]) -> None:
        self.script = script
        self.tables = tables
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.closed = False
        self.txn_open = False

    async def fetch(self, sql: str, qualified: str) -> list[dict[str, str]]:
        info = self.tables[qualified]
        if sql == rp._COLUMNS_SQL:
            return [{"name": n, "type": t} for n, t in info.columns.items()]
        assert sql == rp._PK_SQL
        return [{"name": n} for n in info.pk]

    async def execute(self, sql: str, payload: str) -> str:
        assert self.txn_open, "every row must run inside its own transaction"
        table = sql.split("INSERT INTO ", 1)[1].split(" ", 1)[0]
        record = json.loads(payload)
        self.sent.append((table, record))
        outcome = self.script(table, record)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def transaction(self):  # noqa: ANN201
        conn = self

        @asynccontextmanager
        async def _tx():  # noqa: ANN202
            conn.txn_open = True
            try:
                yield
            finally:
                conn.txn_open = False

        return _tx()

    async def close(self) -> None:
        self.closed = True


def _tables() -> dict[str, rp._TableInfo]:
    return {
        "silver.hypotheses": rp._TableInfo(
            columns={"hypothesis_id": "uuid", "workspace_id": "uuid", "label": "varchar"},
            pk=("hypothesis_id",),
        ),
        "silver.evidence_items": rp._TableInfo(
            columns={"evidence_id": "uuid", "workspace_id": "uuid", "passage_id": "uuid"},
            pk=("evidence_id",),
        ),
        "silver.document_passages": rp._TableInfo(
            columns={"passage_id": "uuid", "workspace_id": "uuid", "parent_chunk_id": "uuid"},
            pk=("passage_id",),
        ),
    }


def _archive(rows: list[tuple[str, dict[str, Any]]], **manifest_extra: Any) -> bytes:
    counts: dict[str, int] = {}
    for key, _ in rows:
        counts[key] = counts.get(key, 0) + 1
    manifest = {
        "manifest_version": "2.0", "format": "workspace_export", "workspace_id": WS,
        "table_row_counts": counts, **manifest_extra,
    }
    return _gz(manifest, *({"table": k, "row": r} for k, r in rows))


@pytest.fixture
def scripted(monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    holder: dict[str, _ScriptedConn] = {}

    def _install(script: Any) -> _ScriptedConn:
        conn = _ScriptedConn(script, tables=_tables())
        holder["conn"] = conn

        async def _connect(*_a: Any, **_k: Any) -> _ScriptedConn:
            return conn

        async def _bind(*_a: Any, **_k: Any) -> None:
            return None

        monkeypatch.setattr(rp.asyncpg, "connect", _connect)
        monkeypatch.setattr(rp, "bind_workspace_scope", _bind)
        monkeypatch.setattr(rp, "_build_dsn", lambda: "postgres://unused")
        return conn

    return _install


def _hyp(i: int, ws: str = WS) -> dict[str, Any]:
    return {"hypothesis_id": f"00000000-0000-4000-8000-{i:012d}", "workspace_id": ws, "label": "A"}


@pytest.mark.asyncio
async def test_inserted_and_already_present_rows_are_counted_apart(scripted) -> None:  # noqa: ANN001
    conn = scripted(lambda _t, rec: "INSERT 0 0" if rec["label"] == "dup" else "INSERT 0 1")
    rows = [("silver_hypotheses", _hyp(1)), ("silver_hypotheses", {**_hyp(2), "label": "dup"})]
    out = await rp.restore_postgres_from_export(WS, "unused", body=_archive(rows))

    assert out["rows_in_export"] == 2
    assert out["rows_inserted"] == {"silver.hypotheses": 1}
    assert out["rows_already_present"] == {"silver.hypotheses": 1}
    assert out["rows_rejected"] == {}
    assert (out["total_rows_inserted"], out["total_rows_already_present"], out["total_rows_rejected"]) == (1, 1, 0)
    assert rp.restore_shortfall(out) is None
    assert conn.closed


@pytest.mark.asyncio
async def test_a_rerun_over_rows_that_are_all_there_is_a_complete_restore(scripted) -> None:  # noqa: ANN001
    scripted(lambda _t, _r: "INSERT 0 0")
    out = await rp.restore_postgres_from_export(
        WS, "unused", body=_archive([("silver_hypotheses", _hyp(1)), ("silver_hypotheses", _hyp(2))]),
    )
    assert out["total_rows_inserted"] == 0
    assert out["total_rows_already_present"] == 2
    assert rp.restore_shortfall(out) is None


@pytest.mark.asyncio
async def test_a_rejected_row_is_counted_sampled_and_logged_at_warning(
    scripted, caplog: pytest.LogCaptureFixture,  # noqa: ANN001
) -> None:
    conn = scripted(lambda _t, rec: _Err.check() if rec["label"] == "bad" else "INSERT 0 1")
    rows = [("silver_hypotheses", _hyp(1)), ("silver_hypotheses", {**_hyp(2), "label": "bad"})]
    with caplog.at_level(logging.DEBUG, logger="georag.hatchet._restore_pg_from_export"):
        out = await rp.restore_postgres_from_export(WS, "unused", body=_archive(rows))

    assert out["rows_inserted"] == {"silver.hypotheses": 1}
    assert out["rows_rejected"] == {"silver.hypotheses": 1}
    (sample,) = out["rejected_samples"]
    assert sample["table"] == "silver.hypotheses"
    assert sample["sqlstate"] == "23514"
    assert sample["key"] == {"hypothesis_id": _hyp(2)["hypothesis_id"]}
    shortfall = rp.restore_shortfall(out)
    assert shortfall is not None and "1 of 2 exported rows were rejected" in shortfall
    assert "23514" in shortfall

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "rejected" in r.getMessage()]
    assert warnings, "a rejected row must be logged above DEBUG"
    # A non-FK failure is final: it was tried once, not retried.
    assert [t for t, rec in conn.sent if rec["label"] == "bad"] == ["silver.hypotheses"]


@pytest.mark.asyncio
async def test_a_foreign_key_miss_is_retried_after_its_parent_lands(scripted) -> None:  # noqa: ANN001
    """Export order is not dependency order: evidence precedes the passage it
    cites, and a passage can precede its parent chunk."""
    parent, child = "00000000-0000-4000-8000-00000000000a", "00000000-0000-4000-8000-00000000000b"
    placed: set[str] = set()

    def script(table: str, rec: dict[str, Any]):  # noqa: ANN202
        if table == "silver.evidence_items" and rec["passage_id"] not in placed:
            return _Err.fk()
        if table == "silver.document_passages" and rec.get("parent_chunk_id") and rec["parent_chunk_id"] not in placed:
            return _Err.fk()
        placed.add(rec.get("passage_id") or rec["evidence_id"])
        return "INSERT 0 1"

    scripted(script)
    rows = [
        ("silver_evidence_items", {"evidence_id": "00000000-0000-4000-8000-0000000000e1", "workspace_id": WS, "passage_id": child}),
        ("silver_document_passages", {"passage_id": child, "workspace_id": WS, "parent_chunk_id": parent}),
        ("silver_document_passages", {"passage_id": parent, "workspace_id": WS, "parent_chunk_id": None}),
    ]
    out = await rp.restore_postgres_from_export(WS, "unused", body=_archive(rows))

    assert out["rows_rejected"] == {}
    assert out["rows_inserted"] == {"silver.evidence_items": 1, "silver.document_passages": 2}
    assert rp.restore_shortfall(out) is None


@pytest.mark.asyncio
async def test_a_foreign_key_miss_that_never_resolves_is_rejected_with_the_databases_words(scripted) -> None:  # noqa: ANN001
    conn = scripted(lambda _t, _r: _Err.fk())
    out = await rp.restore_postgres_from_export(WS, "unused", body=_archive([("silver_hypotheses", _hyp(1))]))

    assert out["rows_rejected"] == {"silver.hypotheses": 1}
    assert out["rejected_samples"][0]["sqlstate"] == "23503"
    # tried in the main pass and once more in the retry pass -- then it stops
    assert len(conn.sent) == 2
    assert rp.restore_shortfall(out) is not None


@pytest.mark.asyncio
async def test_a_dead_connection_aborts_the_restore_instead_of_rejecting_every_row(scripted) -> None:  # noqa: ANN001
    scripted(lambda _t, _r: _Err.gone())
    with pytest.raises(asyncpg.exceptions.ConnectionDoesNotExistError):
        await rp.restore_postgres_from_export(WS, "unused", body=_archive([("silver_hypotheses", _hyp(1))]))


@pytest.mark.asyncio
async def test_a_row_for_another_workspace_never_reaches_the_database(scripted) -> None:  # noqa: ANN001
    conn = scripted(lambda _t, _r: "INSERT 0 1")
    rows = [("silver_hypotheses", _hyp(1)), ("silver_hypotheses", _hyp(2, ws=OTHER_WS)),
            ("silver_hypotheses", _hyp(3, ws=None))]  # type: ignore[arg-type]
    out = await rp.restore_postgres_from_export(WS, "unused", body=_archive(rows))

    assert out["total_rows_inserted"] == 1
    assert out["rows_rejected"] == {"silver.hypotheses": 2}
    assert len(conn.sent) == 1
    assert "not the restore target" in out["rejected_samples"][0]["reason"]


@pytest.mark.asyncio
async def test_a_row_for_an_unknown_table_is_rejected_not_skipped_quietly(scripted) -> None:  # noqa: ANN001
    scripted(lambda _t, _r: "INSERT 0 1")
    out = await rp.restore_postgres_from_export(
        WS, "unused", body=_archive([("silver_collars", {"collar_id": "x"})]),
    )
    assert out["rows_rejected"] == {"silver_collars": 1}
    assert rp.restore_shortfall(out) is not None


@pytest.mark.asyncio
async def test_a_file_shorter_than_its_manifest_is_not_a_complete_restore(scripted) -> None:  # noqa: ANN001
    scripted(lambda _t, _r: "INSERT 0 1")
    body = _gz(
        {"format": "workspace_export", "workspace_id": WS, "table_row_counts": {"silver_hypotheses": 3}},
        {"table": "silver_hypotheses", "row": _hyp(1)},
    )
    out = await rp.restore_postgres_from_export(WS, "unused", body=body)
    assert out["count_mismatches"] == {"silver_hypotheses": {"manifest": 3, "file": 1}}
    assert "manifest" in (rp.restore_shortfall(out) or "")


@pytest.mark.asyncio
async def test_extra_store_sections_are_collected_in_the_same_pass(scripted) -> None:  # noqa: ANN001
    scripted(lambda _t, _r: "INSERT 0 1")
    body = _gz(
        {"format": "workspace_export", "workspace_id": WS, "manifest_version": "2.0"},
        {"table": "silver_hypotheses", "row": _hyp(1)},
        {"section": "qdrant_points", "row": {"id": "p1"}},
        {"section": "redis_keys", "row": {"key": "k"}},
    )
    sections: dict[str, list[dict[str, Any]]] = {}
    out = await rp.restore_postgres_from_export(WS, "unused", body=body, sections_out=sections)
    assert sections == {"qdrant_points": [{"id": "p1"}], "redis_keys": [{"key": "k"}]}
    assert out["rows_in_export"] == 1  # sections are not table rows
    assert out["manifest_version"] == "2.0"


def test_a_counting_bug_cannot_turn_nothing_written_back_into_success() -> None:
    assert rp.restore_shortfall({"rows_in_export": 5, "total_rows_inserted": 0,
                                 "total_rows_already_present": 0, "total_rows_rejected": 0}) is not None
    assert rp.restore_shortfall({"rows_in_export": 0, "total_rows_inserted": 0,
                                 "total_rows_already_present": 0, "total_rows_rejected": 0}) is None


# ---------------------------------------------------------------------------
# The workflow's verdict
# ---------------------------------------------------------------------------
def _restore_input(uri: str = "s3://workspace-exports/x/y.jsonl.gz"):  # noqa: ANN202
    from app.hatchet_workflows.restore_workspace import RestoreWorkspaceInput

    return RestoreWorkspaceInput(
        workspace_id=uuid.UUID(WS), snapshot_manifest_uri=uri, initiated_by_user_id=1,
        restore_request_id=uuid.uuid4(), dry_run=False,
    )


def _pg_result(**over: Any) -> dict[str, Any]:
    base = {
        "manifest_workspace_id": WS, "manifest_version": "2.0", "tables": ["silver.hypotheses"],
        "rows_in_export": 2, "rows_inserted": {"silver.hypotheses": 2}, "rows_already_present": {},
        "rows_rejected": {}, "rejected_samples": [], "count_mismatches": {},
        "total_rows_inserted": 2, "total_rows_already_present": 0, "total_rows_rejected": 0,
    }
    base.update(over)
    return base


@pytest.fixture
def workflow_stubs(monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    """restore_workspace.execute with the archive fetch, PG pass and extras faked."""
    calls: dict[str, Any] = {"qdrant": 0, "redis": 0}

    def _install(pg_result: dict[str, Any], sections: dict[str, list[dict[str, Any]]] | None = None) -> dict[str, Any]:
        async def _fetch(_uri: str) -> bytes:
            return b"unused"

        async def _restore(*, workspace_id: str, manifest_uri: str, body: bytes, sections_out: dict) -> dict:
            assert body == b"unused", "the workflow must hand the one fetched archive to the PG pass"
            sections_out.update(sections or {})
            return pg_result

        async def _qdrant(_ws: str, points: list) -> dict[str, Any]:
            calls["qdrant"] += 1
            return {"points_upserted": len(points), "error": None}

        async def _redis(_ws: str, keys: list) -> dict[str, Any]:
            calls["redis"] += 1
            return {"keys_restored": len(keys), "error": None}

        monkeypatch.setattr(rp, "_fetch_manifest_bytes", _fetch)
        monkeypatch.setattr(rp, "restore_postgres_from_export", _restore)
        import app.hatchet_workflows._restore_extras as extras

        monkeypatch.setattr(extras, "restore_qdrant", _qdrant)
        monkeypatch.setattr(extras, "restore_redis", _redis)
        return calls

    return _install


@pytest.mark.asyncio
async def test_workflow_reports_failure_when_rows_were_rejected(workflow_stubs) -> None:  # noqa: ANN001
    from app.hatchet_workflows.restore_workspace import execute

    calls = workflow_stubs(
        _pg_result(
            rows_inserted={"silver.hypotheses": 1}, total_rows_inserted=1,
            rows_rejected={"silver.hypotheses": 1}, total_rows_rejected=1,
            rejected_samples=[{"table": "silver.hypotheses", "key": {}, "sqlstate": "23503", "reason": "fk"}],
        ),
        sections={"qdrant_points": [{"id": "p"}], "redis_keys": [{"key": "k"}]},
    )
    out = await execute.aio_mock_run(_restore_input())

    assert out.success is False
    assert out.failure_stage == "pg_restore"
    assert "1 of 2 exported rows were rejected" in (out.failure_reason or "")
    assert out.stores_restored == []
    assert out.consistency_check_results["rows_rejected"] == {"silver.hypotheses": 1}
    # Qdrant / Redis point at Postgres rows that are not all there: untouched.
    assert calls == {"qdrant": 0, "redis": 0}


@pytest.mark.asyncio
async def test_workflow_reports_failure_when_nothing_landed(workflow_stubs) -> None:  # noqa: ANN001
    from app.hatchet_workflows.restore_workspace import execute

    workflow_stubs(_pg_result(rows_inserted={}, total_rows_inserted=0))
    out = await execute.aio_mock_run(_restore_input())
    assert out.success is False
    assert out.failure_stage == "pg_restore"
    assert "none of the 2 exported rows landed" in (out.failure_reason or "")


@pytest.mark.asyncio
async def test_workflow_succeeds_on_a_complete_restore_and_reuses_the_one_decode(workflow_stubs) -> None:  # noqa: ANN001
    from app.hatchet_workflows.restore_workspace import execute

    calls = workflow_stubs(
        _pg_result(), sections={"qdrant_points": [{"id": "p"}], "redis_keys": [{"key": "k"}]},
    )
    out = await execute.aio_mock_run(_restore_input())

    assert out.success is True
    assert out.stores_restored == ["postgres", "qdrant", "redis"]
    assert calls == {"qdrant": 1, "redis": 1}
    results = out.consistency_check_results
    assert results["rows_inserted"] == {"silver.hypotheses": 2}
    assert results["rows_rejected"] == {}
    assert results["rows_in_export"] == 2


@pytest.mark.asyncio
async def test_workflow_reports_an_unreadable_archive_as_a_pg_restore_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.hatchet_workflows.restore_workspace import execute

    async def _boom(_uri: str) -> bytes:
        raise FileNotFoundError("no such export")

    monkeypatch.setattr(rp, "_fetch_manifest_bytes", _boom)
    out = await execute.aio_mock_run(_restore_input())
    assert out.success is False and out.failure_stage == "pg_restore"
    assert "no such export" in (out.failure_reason or "")


# ===========================================================================
# Integration — live stack round-trip
# ===========================================================================
PG_DSN = os.environ.get("PG_DSN") or (
    "postgresql://{u}:{p}@{h}:{port}/{db}".format(
        u=os.environ.get("POSTGRES_USER", "georag"),
        p=os.environ.get("POSTGRES_PASSWORD", "georag_dev_password"),
        h=os.environ.get("POSTGRES_DIRECT_HOST", os.environ.get("POSTGRES_HOST", "localhost")),
        port=os.environ.get("POSTGRES_DIRECT_PORT", os.environ.get("POSTGRES_PORT", "5432")),
        db=os.environ.get("POSTGRES_DB", "georag"),
    )
)

#: output_key -> exact row count seeded for the test workspace
EXPECTED = {
    "silver_workspaces": 1,
    "silver_hypotheses": 2,
    "silver_decision_records": 1,
    "silver_answer_runs": 2,
    "silver_evidence_items": 2,
    "silver_document_passages": 2,
    "audit_ledger_anchors": 3,
    "targeting_target_recommendations": 1,
    "ops_support_tickets": 1,
}


@pytest.fixture
async def admin():
    """A superuser connection to a migrated database, or skip."""
    try:
        conn = await asyncpg.connect(PG_DSN, timeout=5)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no Postgres at PG_DSN: {exc}")
    try:
        ok = await conn.fetchval(
            "SELECT to_regclass('targeting.target_recommendations') IS NOT NULL "
            "AND to_regclass('audit.audit_ledger') IS NOT NULL "
            "AND (SELECT rolsuper FROM pg_roles WHERE rolname = current_user)"
        )
        if not ok:
            pytest.skip("needs the migrated schema and a superuser PG_DSN")
        yield conn
    finally:
        await conn.close()


class _Seeded(types.SimpleNamespace):
    """ids and the original rows of the seeded workspace."""


async def _seed(admin: asyncpg.Connection) -> _Seeded:
    s = _Seeded()
    s.ws = str(uuid.uuid4())
    s.other_ws = str(uuid.uuid4())
    s.project = str(uuid.uuid4())
    s.parent = str(uuid.uuid4())
    s.child = str(uuid.uuid4())
    s.tag = uuid.uuid4().hex[:10]
    s.user = await admin.fetchval(
        "INSERT INTO users (name, email, password) VALUES ($1, $2, 'x') RETURNING id",
        f"restore-{s.tag}", f"restore-{s.tag}@example.test",
    )
    for ws, label in ((s.ws, "restore test"), (s.other_ws, "restore bystander")):
        await admin.execute(
            "INSERT INTO silver.workspaces (workspace_id, name, slug, created_at, updated_at) "
            "VALUES ($1::uuid, $2, $3, now(), now())",
            ws, label, f"restore-{ws[:8]}",
        )
    await admin.execute(
        "INSERT INTO silver.projects (project_id, project_name, slug, workspace_id) "
        "VALUES ($1::uuid, 'restore project', $2, $3::uuid)",
        s.project, f"restore-p-{s.tag}", s.ws,
    )
    for label in ("A", "B"):
        await admin.execute(
            "INSERT INTO silver.hypotheses (workspace_id, parent_question, label, description, "
            "confidence, reviewed_by_user_id, reviewed_at) "
            "VALUES ($1::uuid, 'where is it?', $2, 'because', 0.7500, $3, now())",
            s.ws, label, s.user,
        )
    await admin.execute(
        "INSERT INTO silver.decision_records (workspace_id, decision_type, recommendation, "
        "human_decision, reason, uncertainty, decided_by_user_id, hash) "
        "VALUES ($1::uuid, 'crs_decision', 'use 26904', 'agreed', 'datum', 0.2500, $2, '\\xdeadbeef'::bytea)",
        s.ws, s.user,
    )
    for q in ("first question", "second question"):
        await admin.execute(
            "INSERT INTO silver.answer_runs (workspace_id, project_id, user_id, query_text, "
            "query_class, workspace_data_version_at_query, backend_chain, "
            "speculative_acceptance_rate_sample, partial_failure_details, "
            "hallucination_guard_results, confidence, latency_ms) "
            "VALUES ($1::uuid, $2::uuid, $3, $4, 'factual', 7, ARRAY['cohere','bedrock'], "
            "0.8125, '{\"a\": [1, 2]}'::jsonb, '{\"layer1\": \"ok\"}'::jsonb, 0.9, 1234)",
            s.ws, s.project, s.user, q,
        )
    await admin.execute(
        "INSERT INTO silver.document_passages (passage_id, workspace_id, revision_number, text, "
        "text_hash, ordinal, created_at, updated_at, parser_confidence, chunk_kind) "
        "VALUES ($1::uuid, $2::uuid, 1, 'parent text', $3, 0, now(), now(), 0.9876, 'section')",
        s.parent, s.ws, "a" * 64,
    )
    await admin.execute(
        "INSERT INTO silver.document_passages (passage_id, workspace_id, revision_number, text, "
        "text_hash, ordinal, parent_chunk_id, created_at, updated_at, chunk_kind) "
        "VALUES ($1::uuid, $2::uuid, 1, 'child text', $3, 1, $4::uuid, now(), now(), 'paragraph')",
        s.child, s.ws, "b" * 64, s.parent,
    )
    await admin.execute(
        "INSERT INTO silver.evidence_items (workspace_id, evidence_type, passage_id, source_uri, source_date) "
        "VALUES ($1::uuid, 'document_passage', $2::uuid, 's3://bronze/a.pdf', '2026-01-02')",
        s.ws, s.child,
    )
    await admin.execute(
        "INSERT INTO silver.evidence_items (workspace_id, evidence_type, structured_ref, source_uri) "
        "VALUES ($1::uuid, 'structured_record', '{\"table\": \"silver.collars\", \"id\": 1}'::jsonb, 'pg://collars/1')",
        s.ws,
    )
    for action in ("restore.test.one", "restore.test.two", "restore.test.three"):
        await admin.execute(
            "INSERT INTO audit.audit_ledger (workspace_id, actor_kind, action_type, payload) "
            "VALUES ($1::uuid, 'workflow', $2, '{\"n\": 1}'::jsonb)",
            s.ws, action,
        )
    await admin.execute(  # a bystander: must never appear in the export
        "INSERT INTO audit.audit_ledger (workspace_id, actor_kind, action_type) "
        "VALUES ($1::uuid, 'workflow', 'restore.test.bystander')", s.other_ws,
    )
    model = await admin.fetchval(
        "INSERT INTO targeting.target_models (slug, display_name, commodity_primary) "
        "VALUES ($1, 'restore model', 'U') RETURNING target_model_id", f"restore_{s.tag}",
    )
    s.model = str(model)
    version = await admin.fetchval(
        "INSERT INTO targeting.target_model_versions (target_model_id, version, scoring_kind) "
        "VALUES ($1::uuid, 1, 'weighted') RETURNING version_id", s.model,
    )
    s.zone = str(await admin.fetchval(
        "INSERT INTO targeting.target_candidate_zones (workspace_id, project_id, target_model_id, "
        "run_id, zone_geom) VALUES ($1::uuid, $2::uuid, $3::uuid, $4::uuid, "
        "ST_GeomFromText('POLYGON((0 0,1 0,1 1,0 1,0 0))', 4326)) RETURNING zone_id",
        s.ws, s.project, s.model, str(uuid.uuid4()),
    ))
    score = await admin.fetchval(
        "INSERT INTO targeting.target_scores (zone_id, workspace_id, model_version_id, aggregate_score) "
        "VALUES ($1::uuid, $2::uuid, $3::uuid, 0.5) RETURNING score_id", s.zone, s.ws, version,
    )
    await admin.execute(
        "INSERT INTO targeting.target_recommendations (workspace_id, project_id, run_id, zone_id, "
        "score_id, rank, explanation_markdown) VALUES ($1::uuid, $2::uuid, $3::uuid, $4::uuid, "
        "$5::uuid, 1, 'drill here')",
        s.ws, s.project, str(uuid.uuid4()), s.zone, score,
    )
    await admin.execute(
        "INSERT INTO ops.support_tickets (workspace_id, reported_by_user_id, channel, category, description) "
        "VALUES ($1::uuid, $2, 'email', 'other', 'it broke')", s.ws, s.user,
    )
    return s


async def _snapshot(admin: asyncpg.Connection, ws: str) -> dict[str, list[dict[str, Any]]]:
    """Every exported table's rows for ``ws``, canonicalised and ordered."""
    out: dict[str, list[dict[str, Any]]] = {}
    for key, table in we._WORKSPACE_TABLES:
        cols = await we._table_columns(admin, table)
        col_list = ", ".join(we._quote_ident(c) for c in cols)
        where = "workspace_id = $1::uuid"
        rows = await admin.fetch(f"SELECT {col_list} FROM {table} WHERE {where} ORDER BY 1", ws)
        out[key] = sorted((we._row_to_dict(r) for r in rows), key=lambda r: json.dumps(r, sort_keys=True, default=str))
    return out


#: Ledger columns the BEFORE INSERT trigger owns. A restore replays each row's
#: content oldest-first; the trigger stamps created_at after the chain lock
#: (2026_10_10_100100) and rebuilds previous_hash/hash from it, so after a
#: restore these three are the restore's, not the export's.
_TRIGGER_OWNED = frozenset({"created_at", "previous_hash", "hash"})


def _restorable(snapshot: dict[str, list[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    """The snapshot without the columns a restore cannot carry over."""
    out = dict(snapshot)
    out["audit_ledger_anchors"] = sorted(
        ({k: v for k, v in r.items() if k not in _TRIGGER_OWNED} for r in snapshot["audit_ledger_anchors"]),
        key=lambda r: json.dumps(r, sort_keys=True, default=str),
    )
    return out


def _ledger_order(snapshot: dict[str, list[dict[str, Any]]]) -> list[str]:
    """Ledger row ids in chain order, (created_at, id), as the trigger links them."""
    rows = snapshot["audit_ledger_anchors"]
    return [str(r["id"]) for r in sorted(rows, key=lambda r: (str(r["created_at"]), str(r["id"])))]


async def _assert_ledger_restored(
    admin: asyncpg.Connection, ws: str,
    before: dict[str, list[dict[str, Any]]], after: dict[str, list[dict[str, Any]]],
) -> None:
    """Every row back, in its original order, on a chain the verifier accepts."""
    assert _restorable(after) == _restorable(before)
    assert _ledger_order(after) == _ledger_order(before)
    breaks = await admin.fetch(
        "SELECT audit_id FROM audit.verify_hash_chain("
        "  (SELECT min(created_at) FROM audit.audit_ledger WHERE workspace_id = $1::uuid),"
        "  (SELECT max(created_at) FROM audit.audit_ledger WHERE workspace_id = $1::uuid)"
        "    + interval '1 microsecond'"
        ") WHERE workspace_id = $1::uuid",
        ws,
    )
    assert breaks == [], "the restored rows must form a chain the nightly verifier accepts"


async def _forget(admin: asyncpg.Connection, s: _Seeded, *, drop_targeting_parents: bool = False) -> None:
    """Remove the eight restorable tables' rows (not the workspace itself)."""
    await admin.execute("DELETE FROM targeting.target_recommendations WHERE workspace_id = $1::uuid", s.ws)
    for table in ("silver.hypotheses", "silver.decision_records", "silver.answer_runs",
                  "silver.evidence_items", "ops.support_tickets"):
        await admin.execute(f"DELETE FROM {table} WHERE workspace_id = $1::uuid", s.ws)
    await admin.execute("DELETE FROM silver.document_passages WHERE workspace_id = $1::uuid", s.ws)
    async with admin.transaction():
        # The ledger is append-only; this is the test database's own rows.
        await admin.execute("SET LOCAL session_replication_role = replica")
        await admin.execute("DELETE FROM audit.audit_ledger WHERE workspace_id = $1::uuid", s.ws)
    if drop_targeting_parents:
        await admin.execute("DELETE FROM targeting.target_candidate_zones WHERE zone_id = $1::uuid", s.zone)


async def _teardown(admin: asyncpg.Connection, s: _Seeded) -> None:
    await admin.execute("DELETE FROM ops.support_tickets WHERE workspace_id = $1::uuid", s.ws)
    await admin.execute("DELETE FROM silver.workspaces WHERE workspace_id = ANY($1::uuid[])", [s.ws, s.other_ws])
    async with admin.transaction():
        await admin.execute("SET LOCAL session_replication_role = replica")
        await admin.execute(
            "DELETE FROM audit.audit_ledger WHERE workspace_id = ANY($1::uuid[])", [s.ws, s.other_ws],
        )
    await admin.execute("DELETE FROM silver.projects WHERE project_id = $1::uuid", s.project)
    await admin.execute("DELETE FROM targeting.target_models WHERE target_model_id = $1::uuid", s.model)
    await admin.execute("DELETE FROM users WHERE id = $1", s.user)


async def _export(
    monkeypatch: pytest.MonkeyPatch, s: _Seeded, tmp_path: Path, *, reorder: bool = True,
) -> Path:
    """Run the real exporter and write the archive it uploads to ``tmp_path``.

    ``reorder`` puts the archive's rows in the order a restore finds hardest:
    the evidence rows (they cite a passage) first, and the child passage ahead
    of the parent it points at.
    """
    uploaded: dict[str, bytes] = {}

    async def _fake_put(bucket: str, key: str, path: str) -> None:
        uploaded[key] = pathlib.Path(path).read_bytes()

    async def _no_audit(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(we, "_build_dsn", lambda: PG_DSN)
    monkeypatch.setattr(we, "_upload_file_s3", _fake_put)
    monkeypatch.setattr(we, "emit_audit", _no_audit)
    import app.services.laravel_bridge as bridge

    monkeypatch.setattr(bridge, "post_admin_surface_updated", _no_audit)

    out = await we.run_export.fn(
        we.WorkspaceExportInput(workspace_id=s.ws, include_qdrant=False, include_redis=False),
        types.SimpleNamespace(workflow_run_id="restore-test"),
    )
    assert out.per_table == EXPECTED, out.per_table
    (body,) = uploaded.values()

    lines = gzip.decompress(body).decode().splitlines()
    manifest, entries = lines[0], [json.loads(line) for line in lines[1:]]
    if reorder:
        def rank(e: dict[str, Any]) -> tuple[int, int]:
            if e["table"] == "silver_evidence_items":
                return (0, 0)
            if e["table"] == "silver_document_passages":
                return (1, 0 if e["row"]["passage_id"] == s.child else 1)
            return (2, 0)

        entries.sort(key=rank)  # stable: the rest keep export order
    path = tmp_path / "export.jsonl.gz"
    path.write_bytes(gzip.compress(
        "\n".join([manifest, *(json.dumps(e, sort_keys=True) for e in entries)]).encode() + b"\n",
    ))
    return path


@pytest.fixture
async def seeded(admin: asyncpg.Connection):  # noqa: ANN201
    s = await _seed(admin)
    try:
        yield s
    finally:
        await _teardown(admin, s)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_export_one_table_walks_silver_workspaces(
    admin: asyncpg.Connection, seeded: _Seeded, tmp_path: Path,
) -> None:
    """The silver.workspaces special-case returns 1 row per workspace_id."""
    spool = we._GzipSpool(str(tmp_path / "w.gz"))
    try:
        n = await we._export_one_table(admin, "silver.workspaces", seeded.ws, "silver_workspaces", spool)
    finally:
        spool.close()
    assert n == 1
    assert spool.lines == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_full_export_restore_round_trip(
    admin: asyncpg.Connection, seeded: _Seeded, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Export a seeded workspace, remove its rows, restore, and compare.

    Exact counts, not '>= 0': every one of the 14 rows that was removed comes
    back (the workspace row itself was never removed, so it is reported as
    already present), nothing is rejected, and the values -- bytea hashes,
    jsonb documents, arrays, numerics, both kinds of timestamp -- are what
    they were. The audit ledger's rows come back with their content and in
    their order, on a chain the verifier accepts; their created_at, and so
    previous_hash/hash, are the restore's (_TRIGGER_OWNED).
    """
    path = await _export(monkeypatch, seeded, tmp_path)
    before = await _snapshot(admin, seeded.ws)
    assert {k: len(v) for k, v in before.items()} == EXPECTED
    await _forget(admin, seeded)
    mid = await _snapshot(admin, seeded.ws)
    assert {k: len(v) for k, v in mid.items()} == {**{k: 0 for k in EXPECTED}, "silver_workspaces": 1}

    monkeypatch.setattr(rp, "_build_dsn", lambda: PG_DSN)
    result = await rp.restore_postgres_from_export(seeded.ws, f"file://{path}")

    assert result["manifest_workspace_id"] == seeded.ws
    assert result["rows_in_export"] == sum(EXPECTED.values()) == 15
    assert result["rows_inserted"] == {
        "silver.hypotheses": 2,
        "silver.decision_records": 1,
        "silver.answer_runs": 2,
        "silver.evidence_items": 2,
        "silver.document_passages": 2,
        "audit.audit_ledger": 3,
        "targeting.target_recommendations": 1,
        "ops.support_tickets": 1,
    }
    assert result["rows_already_present"] == {"silver.workspaces": 1}
    assert result["rows_rejected"] == {}
    assert result["total_rows_inserted"] == 14
    assert rp.restore_shortfall(result) is None

    after = await _snapshot(admin, seeded.ws)
    assert {k: len(v) for k, v in after.items()} == EXPECTED
    await _assert_ledger_restored(admin, seeded.ws, before, after)

    # Running it again changes nothing, and says so.
    again = await rp.restore_postgres_from_export(seeded.ws, f"file://{path}")
    assert again["total_rows_inserted"] == 0
    assert again["total_rows_already_present"] == 15
    assert again["total_rows_rejected"] == 0
    assert rp.restore_shortfall(again) is None
    # Exact, ledger included: the second pass must not append the ledger rows
    # again (their key holds a created_at the trigger re-stamps on insert).
    assert await _snapshot(admin, seeded.ws) == after


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_restore_that_the_database_refuses_is_reported_not_swallowed(
    admin: asyncpg.Connection, seeded: _Seeded, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The target recommendation's zone and score are not part of the export.
    Without them the row cannot be placed, and the run has to say so."""
    path = await _export(monkeypatch, seeded, tmp_path)
    await _forget(admin, seeded, drop_targeting_parents=True)
    monkeypatch.setattr(rp, "_build_dsn", lambda: PG_DSN)

    result = await rp.restore_postgres_from_export(seeded.ws, f"file://{path}")

    assert result["rows_rejected"] == {"targeting.target_recommendations": 1}
    assert result["total_rows_inserted"] == 13
    (sample,) = result["rejected_samples"]
    assert sample["sqlstate"] == "23503"
    assert "foreign key" in sample["reason"]
    assert rp.restore_shortfall(result) is not None

    # ... and through the workflow: success=False, nothing else touched.
    from app.hatchet_workflows.restore_workspace import RestoreWorkspaceInput, execute

    out = await execute.aio_mock_run(RestoreWorkspaceInput(
        workspace_id=uuid.UUID(seeded.ws), snapshot_manifest_uri=f"file://{path}",
        initiated_by_user_id=1, restore_request_id=uuid.uuid4(), dry_run=False,
    ))
    assert out.success is False
    assert out.failure_stage == "pg_restore"
    assert out.stores_restored == []
    assert "targeting.target_recommendations" in (out.failure_reason or "")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_restore_under_a_nobypassrls_role_lands_every_row(
    admin: asyncpg.Connection, seeded: _Seeded, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production connects as georag_app. Every table's policy has to admit the
    restore once the target workspace is bound, and nothing else."""
    if not await admin.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'georag_app' AND NOT rolbypassrls)"
    ):
        pytest.skip("needs a georag_app role")
    path = await _export(monkeypatch, seeded, tmp_path)
    before = await _snapshot(admin, seeded.ws)
    await _forget(admin, seeded)

    role = f"restore_probe_{uuid.uuid4().hex[:8]}"
    password = secrets.token_hex(16)
    await admin.execute(f"CREATE ROLE {role} LOGIN NOSUPERUSER NOBYPASSRLS INHERIT PASSWORD '{password}'")
    await admin.execute(f"GRANT georag_app TO {role}")
    try:
        parts = urlsplit(PG_DSN)
        port = f":{parts.port}" if parts.port else ""
        probe_dsn = urlunsplit((parts.scheme, f"{role}:{password}@{parts.hostname}{port}", parts.path, "", ""))
        monkeypatch.setattr(rp, "_build_dsn", lambda: probe_dsn)

        result = await rp.restore_postgres_from_export(seeded.ws, f"file://{path}")

        assert result["rows_rejected"] == {}, result["rejected_samples"]
        assert result["total_rows_inserted"] == 14
        await _assert_ledger_restored(admin, seeded.ws, before, await _snapshot(admin, seeded.ws))
    finally:
        await admin.execute(f"DROP OWNED BY {role}")
        await admin.execute(f"DROP ROLE IF EXISTS {role}")
