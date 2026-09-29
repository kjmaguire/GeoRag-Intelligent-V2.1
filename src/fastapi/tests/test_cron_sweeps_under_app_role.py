"""Cron sweeps and dispatch claims, run as the AWS worker role (HAT-1/2/6/7/11).

WHY THIS EXISTS
    On AWS the Hatchet worker connects as ``georag_app``: NOSUPERUSER,
    NOBYPASSRLS (deploy/aws/bootstrap.sql). Compose and CI connect as the
    superuser ``georag``, which bypasses row-level security, so every sweep
    that read "all the data" on a bare connection looked fine everywhere
    except production. There, a fail-CLOSED table (``silver.document_passages``
    after ``migrate`` + ``db:apply-raw``) reads as empty with
    ``app.workspace_id`` unset, and the embed / enrich / verbalize fan-outs,
    the orphan-passage sweep, Tier 2's Qdrant spot-check and the stale sweep's
    "fully embedded?" test all concluded there was nothing to do.

    The fix (Kyle, 2026-09-29) is per-workspace iteration with the scope
    bound, not a BYPASSRLS role. This module runs the real sweep code as the
    real role against a database with the raw RLS layer applied.

HOW TO RUN
    Needs a Postgres where ``php artisan migrate`` AND ``php artisan
    db:apply-raw`` have both run (the ci.yml job "Cron sweeps under
    georag_app"), and a login in ``PG_DSN`` that can ``SET ROLE georag_app``.
    Skips cleanly without one.

        PG_DSN=postgresql://georag:...@localhost:5432/georag \\
            pytest -m integration tests/test_cron_sweeps_under_app_role.py
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import uuid
from collections.abc import AsyncIterator

import asyncpg
import pytest

from app.db import fetch_per_workspace, list_workspace_ids

pytestmark = pytest.mark.integration

PG_DSN = os.environ.get(
    "PG_DSN", "postgresql://georag:georag_dev_password@localhost:5432/georag",
)
APP_ROLE = os.environ.get("PG_APP_ROLE", "georag_app")
PLATFORM_WORKSPACE = "a0000000-0000-0000-0000-000000000001"

_REQUIRED_RELATIONS = (
    "silver.workspaces", "silver.projects", "silver.reports",
    "silver.document_passages", "silver.ingest_progress", "bronze.manifest",
    "outbox.pending_propagations", "audit.audit_ledger_verification_runs",
)


async def _connect_or_skip() -> asyncpg.Connection:
    try:
        return await asyncpg.connect(PG_DSN, timeout=5)
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"no Postgres at PG_DSN ({exc})")


@pytest.fixture
async def owner_conn() -> AsyncIterator[asyncpg.Connection]:
    conn = await _connect_or_skip()
    try:
        for rel in _REQUIRED_RELATIONS:
            if not await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", rel):
                pytest.skip(f"{rel} missing; run migrate + db:apply-raw first")
        if not await conn.fetchval(
            "SELECT pg_has_role(current_user, $1, 'MEMBER')", APP_ROLE,
        ):
            pytest.skip(f"the PG_DSN login cannot SET ROLE {APP_ROLE}")
        yield conn
    finally:
        await conn.close()


async def _as_app_role(conn: asyncpg.Connection) -> None:
    await conn.execute(f"SET ROLE {APP_ROLE}")


@pytest.fixture
async def app_pool(owner_conn) -> AsyncIterator[asyncpg.Pool]:
    pool = await asyncpg.create_pool(
        PG_DSN, min_size=1, max_size=6, statement_cache_size=0, init=_as_app_role,
    )
    async with pool.acquire() as conn:
        flags = await conn.fetchrow(
            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user",
        )
    # The whole point: if the pool somehow bypasses RLS, every assertion
    # below passes for the wrong reason.
    assert flags is not None and not flags["rolsuper"] and not flags["rolbypassrls"]
    try:
        yield pool
    finally:
        await pool.close()


@contextlib.asynccontextmanager
async def _scoped(conn: asyncpg.Connection, workspace_id: str):
    async with conn.transaction():
        await conn.execute(
            "SELECT set_config('app.workspace_id', $1, true)", workspace_id,
        )
        yield conn


class _Seed:
    def __init__(self) -> None:
        self.workspaces = [str(uuid.uuid4()), str(uuid.uuid4())]
        self.projects = [str(uuid.uuid4()), str(uuid.uuid4())]
        self.reports = [str(uuid.uuid4()), str(uuid.uuid4())]


@pytest.fixture
async def seed(owner_conn) -> AsyncIterator[_Seed]:
    """Two workspaces, each with a project, a report and an unembedded passage."""
    s = _Seed()
    for i, (ws, pj, rp) in enumerate(zip(s.workspaces, s.projects, s.reports, strict=True)):
        async with _scoped(owner_conn, ws) as c:
            await c.execute(
                "INSERT INTO silver.workspaces (workspace_id, name, slug) "
                "VALUES ($1::uuid, $2, $2)",
                ws, f"hat-{ws[:8]}",
            )
            await c.execute(
                "INSERT INTO silver.projects (project_id, project_name, "
                "orientation_reference, slug, workspace_id) "
                "VALUES ($1::uuid, $2, 'true_north', $2, $3::uuid)",
                pj, f"hat-{pj[:8]}", ws,
            )
            await c.execute(
                "INSERT INTO silver.reports (report_id, title, workspace_id, "
                "project_id, source_file_sha256) "
                "VALUES ($1::uuid, 'r', $2::uuid, $3::uuid, $4)",
                rp, ws, pj, f"{i:x}" * 64,
            )
            await c.execute(
                "INSERT INTO silver.document_passages (workspace_id, document_id, "
                "revision_number, text, text_hash, ordinal, created_at) "
                "VALUES ($1::uuid, $2::uuid, 1, 'x', $3, 0, now() - interval '1 hour')",
                ws, rp, uuid.uuid4().hex + uuid.uuid4().hex,
            )
    try:
        yield s
    finally:
        for ws in s.workspaces:
            async with _scoped(owner_conn, ws) as c:
                for sql in (
                    "DELETE FROM outbox.pending_propagations WHERE workspace_id = $1::uuid",
                    "DELETE FROM silver.ingest_progress WHERE workspace_id = $1::uuid",
                    "DELETE FROM bronze.manifest WHERE workspace_id = $1::uuid",
                    "DELETE FROM silver.document_passages WHERE workspace_id = $1::uuid",
                    "DELETE FROM silver.reports WHERE workspace_id = $1::uuid",
                    "DELETE FROM silver.projects WHERE workspace_id = $1::uuid",
                    "DELETE FROM silver.workspaces WHERE workspace_id = $1::uuid",
                ):
                    await c.execute(sql, ws)


# ---------------------------------------------------------------------------
# The premise, and the helper
# ---------------------------------------------------------------------------
async def test_the_app_role_sees_no_passages_unscoped(app_pool, seed) -> None:
    """The failure the sweeps were built on. If this starts returning rows,
    silver.document_passages has gone fail-open, which is its own incident."""
    async with app_pool.acquire() as conn:
        n = await conn.fetchval(
            "SELECT count(*) FROM silver.document_passages "
            "WHERE workspace_id = ANY($1::uuid[])",
            seed.workspaces,
        )
    assert n == 0


async def test_workspaces_can_be_enumerated_as_the_app_role(app_pool, seed) -> None:
    """No SECURITY DEFINER function needed: silver.workspaces is readable
    unscoped by design (2026_05_20_010000). Pinned so tightening it cannot
    silently turn every per-workspace sweep into a no-op."""
    async with app_pool.acquire() as conn:
        ids = await list_workspace_ids(conn, site="test")
    assert set(seed.workspaces) <= set(ids)


async def test_a_bound_session_scope_does_not_narrow_enumeration(app_pool, seed) -> None:
    async with app_pool.acquire() as conn:
        await conn.execute(
            "SELECT set_config('app.workspace_id', $1, false)", seed.workspaces[0],
        )
        try:
            ids = await list_workspace_ids(conn, site="test")
        finally:
            await conn.execute("RESET app.workspace_id")
    assert set(seed.workspaces) <= set(ids)


async def test_fetch_per_workspace_reaches_every_workspace(app_pool, seed) -> None:
    from app.hatchet_workflows.embed_pending_passages import _FANOUT_TARGETS_SQL

    async with app_pool.acquire() as conn:
        rows = await fetch_per_workspace(conn, _FANOUT_TARGETS_SQL, site="test")
    targets = {(r["wid"], r["pid"]) for r in rows}
    assert set(zip(seed.workspaces, seed.projects, strict=True)) <= targets


async def test_the_helper_refuses_to_nest_inside_a_caller_transaction(app_pool) -> None:
    async with app_pool.acquire() as conn, conn.transaction():
        with pytest.raises(RuntimeError, match="no open transaction"):
            await fetch_per_workspace(conn, "SELECT 1", site="test")


# ---------------------------------------------------------------------------
# The sweeps that read fail-closed passages
# ---------------------------------------------------------------------------
async def test_the_orphan_passage_sweep_finds_orphans(app_pool, seed) -> None:
    from app.services.ingest.orphan_sweep import select_orphan_documents

    async with app_pool.acquire() as conn:
        orphans = await select_orphan_documents(conn)
    assert set(seed.reports) <= {o.document_id for o in orphans}


async def test_the_stale_sweep_does_not_call_an_unembedded_project_done(
    app_pool, seed, owner_conn,
) -> None:
    from app.hatchet_workflows.stale_run_detector import _project_is_fully_embedded

    ws, pj = seed.workspaces[0], seed.projects[0]
    assert not await _project_is_fully_embedded(app_pool, pj, workspace_id=ws)
    assert not await _project_is_fully_embedded(app_pool, pj, workspace_id=None)

    async with _scoped(owner_conn, ws) as c:
        await c.execute(
            "UPDATE silver.document_passages SET embedding_id = gen_random_uuid()::text "
            "WHERE workspace_id = $1::uuid", ws,
        )
    assert await _project_is_fully_embedded(app_pool, pj, workspace_id=ws)


async def test_tier_2_samples_every_workspace(app_pool, seed, owner_conn, monkeypatch) -> None:
    from app.hatchet_workflows import nightly_ingestion_integrity as nii

    for ws in seed.workspaces:
        async with _scoped(owner_conn, ws) as c:
            await c.execute(
                "UPDATE silver.document_passages SET embedding_id = gen_random_uuid()::text "
                "WHERE workspace_id = $1::uuid", ws,
            )

    async def _no_misses(ids: list[str]) -> int:
        return 0

    monkeypatch.setattr(nii, "_qdrant_count_misses", _no_misses)
    rates = await nii._tier_2_qdrant_spotcheck(app_pool)
    assert set(seed.workspaces) <= set(rates)


# ---------------------------------------------------------------------------
# Tier 1 never re-bills a PDF (HAT-1)
# ---------------------------------------------------------------------------
async def test_tier_1_dispatches_only_true_orphans(app_pool, seed, owner_conn, monkeypatch) -> None:
    from app.hatchet_workflows import nightly_ingestion_integrity as nii

    ws, pj = seed.workspaces[0], seed.projects[0]
    landed = f"reports/{pj}/20260101_000000_landed.pdf"
    completed_no_report = f"reports/{pj}/20260101_000000_ran.pdf"
    failed_only = f"reports/{pj}/20260101_000000_failed.pdf"
    never_ran = f"reports/{pj}/20260101_000000_never.pdf"
    async with _scoped(owner_conn, ws) as c:
        for key, sha in (
            (landed, "0" * 64),  # seed report 0's sha: a silver.reports row exists
            (completed_no_report, "e" * 64),
            (failed_only, "f" * 64),
            (never_ran, "d" * 64),
        ):
            await c.execute(
                "INSERT INTO bronze.manifest (file_key, workspace_id, sha256, "
                "document_type, uploaded_at) VALUES ($1, $2::uuid, $3, 'pdf', "
                "now() - interval '2 hours')",
                key, ws, sha,
            )
        for key, status in ((completed_no_report, "completed"), (failed_only, "failed")):
            await c.execute(
                "INSERT INTO silver.ingest_progress (run_id, workspace_id, project_id, "
                "minio_key, filename, status, current_step, step_index, total_steps, "
                "triggered_by, started_at, updated_at) VALUES (gen_random_uuid(), "
                "$1::uuid, $2::uuid, $3, 'f.pdf', $4, 'persist', 3, 5, 'upload', "
                "now(), now())",
                ws, pj, key, status,
            )

    dispatched: list[str] = []

    async def _record(**kwargs):
        dispatched.append(kwargs["minio_key"])
        return "dispatched", "wf"

    monkeypatch.setattr(nii, "_dispatch_recovery", _record)
    await nii._tier_1_bronze(app_pool)

    mine = {k for k in dispatched if k.startswith(f"reports/{pj}/")}
    assert mine == {failed_only, never_ran}, (
        "Tier 1 must re-dispatch a PDF only when no run for it landed, is in "
        "flight or was completed; a completed run with an invisible report "
        "row is exactly the re-billing HAT-1 found"
    )


# ---------------------------------------------------------------------------
# HAT-2: the verifier can record its run
# ---------------------------------------------------------------------------
async def test_the_hash_chain_verifier_records_a_run_as_the_app_role(app_pool, seed) -> None:
    async with app_pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "SELECT set_config('app.workspace_id', $1, true)", seed.workspaces[0],
        )
        run_id = await conn.fetchval(
            "SELECT audit.run_verification(now() - interval '1 day', now(), NULL)",
        )
        scope_after = await conn.fetchval(
            "SELECT current_setting('app.workspace_id', true)",
        )
        await conn.execute(
            "SELECT set_config('app.workspace_id', $1, true)", PLATFORM_WORKSPACE,
        )
        row = await conn.fetchrow(
            "SELECT workspace_id::text AS ws, status FROM "
            "audit.audit_ledger_verification_runs WHERE id = $1",
            run_id,
        )
    assert scope_after == seed.workspaces[0], "the caller's scope must be restored"
    assert row is not None
    assert row["ws"] == PLATFORM_WORKSPACE
    assert row["status"] in ("clean", "break")


# ---------------------------------------------------------------------------
# HAT-6/12: the dispatch claim
# ---------------------------------------------------------------------------
async def test_concurrent_claims_for_one_file_admit_exactly_one(
    app_pool, seed, monkeypatch,
) -> None:
    from app.hatchet_workflows import _progress

    async def _pool():
        return app_pool

    monkeypatch.setattr(_progress, "get_pool", _pool)
    ws, pj = seed.workspaces[0], seed.projects[0]
    key = f"lithology/{pj}/20260929_120000_lith.csv"

    claims = await asyncio.gather(*(
        _progress.claim_dispatch(
            workspace_id=ws, project_id=pj, minio_key=key, run_id=str(uuid.uuid4()),
        )
        for _ in range(5)
    ))
    winners = [c for c in claims if c.claimed]
    assert len(winners) == 1
    assert {c.run_id for c in claims if not c.claimed} == {winners[0].run_id}


async def test_a_reused_run_id_is_a_duplicate_even_after_it_finished(
    app_pool, seed, monkeypatch,
) -> None:
    from app.hatchet_workflows import _progress

    async def _pool():
        return app_pool

    monkeypatch.setattr(_progress, "get_pool", _pool)
    ws, pj = seed.workspaces[0], seed.projects[0]
    key = f"spatial/{pj}/20260929_120000_faults.zip"
    run_id = str(uuid.uuid4())

    first = await _progress.claim_dispatch(
        workspace_id=ws, project_id=pj, minio_key=key, run_id=run_id,
    )
    assert first.claimed
    await _progress.stamp_workflow_run_id(run_id=run_id, workflow_run_id="wf-1")
    async with app_pool.acquire() as conn, _scoped(conn, ws):
        await conn.execute(
            "UPDATE silver.ingest_progress SET status = 'completed' "
            "WHERE run_id = $1::uuid", run_id,
        )
    again = await _progress.claim_dispatch(
        workspace_id=ws, project_id=pj, minio_key=key, run_id=run_id,
    )
    assert not again.claimed
    assert again.workflow_run_id == "wf-1"

    # A NEW upload of the same key (fresh run_id) after completion is allowed.
    fresh = await _progress.claim_dispatch(
        workspace_id=ws, project_id=pj, minio_key=key, run_id=str(uuid.uuid4()),
    )
    assert fresh.claimed


async def test_an_undispatched_claim_is_released(app_pool, seed, monkeypatch) -> None:
    from app.hatchet_workflows import _progress

    async def _pool():
        return app_pool

    monkeypatch.setattr(_progress, "get_pool", _pool)
    ws, pj = seed.workspaces[0], seed.projects[0]
    key = f"reports/{pj}/20260929_120000_x.pdf"
    claim = await _progress.claim_dispatch(workspace_id=ws, project_id=pj, minio_key=key)
    assert claim.claimed
    await _progress.release_undispatched(run_id=claim.run_id)
    retry = await _progress.claim_dispatch(workspace_id=ws, project_id=pj, minio_key=key)
    assert retry.claimed, "a released claim must not block the retry"


# ---------------------------------------------------------------------------
# HAT-1/11: the outbox
# ---------------------------------------------------------------------------
async def _outbox_row(conn, ws: str | None, status: str = "pending", age_min: int = 0) -> str:
    return str(await conn.fetchval(
        "INSERT INTO outbox.pending_propagations (workspace_id, source_schema, "
        "source_table, source_id, target_store, operation, idempotency_key, "
        "status, last_attempted_at) VALUES ($1::uuid, 'silver', 't', $2, "
        "'external_webhook', 'upsert', $2, $3, now() - make_interval(mins => $4)) "
        "RETURNING id",
        ws, f"hat-{uuid.uuid4()}", status, age_min,
    ))


async def test_the_outbox_claims_tenant_and_platform_rows(app_pool, seed, owner_conn) -> None:
    from app.hatchet_workflows.outbox_dispatcher import _CLAIM_SQL, _per_scope

    ws = seed.workspaces[0]
    async with _scoped(owner_conn, ws) as c:
        tenant_row = await _outbox_row(c, ws)
    async with _scoped(owner_conn, "") as c:
        platform_row = await _outbox_row(c, None)
    try:
        only_platform, _ = await _per_scope(
            app_pool, [None], _CLAIM_SQL, fetch=True, limit_budget=500,
        )
        assert tenant_row not in {str(r["id"]) for r in only_platform}, (
            "the platform pass must not sweep up tenant rows"
        )
        assert platform_row in {str(r["id"]) for r in only_platform}

        tenant, _ = await _per_scope(
            app_pool, [ws], _CLAIM_SQL, fetch=True, limit_budget=500,
        )
        assert {str(r["id"]) for r in tenant} == {tenant_row}
    finally:
        async with _scoped(owner_conn, "") as c:
            await c.execute(
                "DELETE FROM outbox.pending_propagations WHERE id = $1::uuid", platform_row,
            )


async def test_a_stale_in_flight_row_is_reclaimed(app_pool, seed, owner_conn) -> None:
    from app.hatchet_workflows.outbox_dispatcher import _RECLAIM_SQL, _per_scope

    ws = seed.workspaces[0]
    async with _scoped(owner_conn, ws) as c:
        stuck = await _outbox_row(c, ws, status="in_flight", age_min=10)
        live = await _outbox_row(c, ws, status="in_flight", age_min=1)
    _, reclaimed = await _per_scope(app_pool, [ws], _RECLAIM_SQL, fetch=False)
    assert reclaimed == 1
    async with _scoped(owner_conn, ws) as c:
        states = {
            str(r["id"]): r["status"]
            for r in await c.fetch(
                "SELECT id, status FROM outbox.pending_propagations "
                "WHERE id = ANY($1::uuid[])", [stuck, live],
            )
        }
    assert states == {stuck: "pending", live: "in_flight"}


# ---------------------------------------------------------------------------
# HAT-7: Pass 2 bumps both counters
# ---------------------------------------------------------------------------
async def test_pass_2_bumps_the_workspace_and_the_project(app_pool, seed, owner_conn) -> None:
    from app.hatchet_workflows import nightly_ingestion_integrity as nii

    ws, pj = seed.workspaces[1], seed.projects[1]
    async with _scoped(owner_conn, ws) as c:
        await c.execute(
            "INSERT INTO silver.ingest_progress (run_id, workspace_id, project_id, "
            "minio_key, filename, status, current_step, step_index, total_steps, "
            "triggered_by, started_at, updated_at) VALUES (gen_random_uuid(), "
            "$1::uuid, $2::uuid, 'reports/x.pdf', 'x.pdf', 'queued', 'queued', 0, 5, "
            "'nightly_integrity_sweep', now(), now())",
            ws, pj,
        )
        before = await c.fetchrow(
            "SELECT w.data_version AS w, p.data_version AS p FROM silver.workspaces w "
            "JOIN silver.projects p ON p.workspace_id = w.workspace_id "
            "WHERE p.project_id = $1::uuid", pj,
        )

    bumped = await nii._bump_data_version_for_recovered_workspaces(app_pool)
    assert ws in bumped

    async with _scoped(owner_conn, ws) as c:
        after = await c.fetchrow(
            "SELECT w.data_version AS w, p.data_version AS p FROM silver.workspaces w "
            "JOIN silver.projects p ON p.workspace_id = w.workspace_id "
            "WHERE p.project_id = $1::uuid", pj,
        )
    assert after["w"] == before["w"] + 1
    assert after["p"] == before["p"] + 1, "the MVT tiles read the PROJECT counter"
