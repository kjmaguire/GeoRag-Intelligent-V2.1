"""RLS layering and the audit grants, on a database built the production way.

WHY THIS FILE EXISTS
    The ECS migrate task runs ``php artisan migrate`` and THEN ``php artisan
    db:apply-raw``. The raw files (database/raw/phase0/95-99) therefore have the
    last word on every policy they touch, and a test that only sees the
    migrate-only state (tests/Feature/Tenancy/WorkspaceRlsCoverageTest.php runs
    on RefreshDatabase, i.e. migrations alone) cannot see what they do. Database
    audit 2026-10 found phase0/95 DROPping the fail-closed ``tenant_isolation``
    that the migration chain installs on five tables and re-creating it
    fail-open on every deploy -- invisible to every existing test, and, worse,
    to the assertion written to catch it (it grepped for ``IS NULL OR``, which
    PostgreSQL never emits: it deparses ``(... IS NULL) OR (...)``).

    This module runs where CI builds the database that way: the
    ``cron-sweeps-app-role`` job (migrate, db:apply-raw, then pytest as a login
    that can ``SET ROLE georag_app``). It also covers the grants and the ledger
    changes of the same audit, because "what georag_app can do" is only true
    after both layers have run.

WHAT IT PINS
    * the five tables whose policy belongs to the migration chain
      (workspace.workspace_memberships / workspace_agent_config / dry_run_outputs,
      outbox.pending_propagations / propagation_attempts) carry exactly one
      policy, with no unbound-GUC branch, FORCEd -- and behave that way as
      ``georag_app``: an unbound session reads no tenant's rows;
    * the outbox keeps its PLATFORM rows (workspace_id NULL) for an unbound
      session and nobody else -- the cleared-scope pass of outbox_dispatcher
      depends on it;
    * of 2026_08_14_030000's verified subset, exactly the two usage tables are
      still open after raw (a documented gap, see phase0/95): the set is compared
      for EQUALITY, so fixing them without updating KNOWN_OPEN_PENDING_BINDING,
      or opening another, both fail;
    * retention_sweep's real purge SQL runs as georag_app;
    * audit.audit_ledger is append-only for georag_app, its hash trigger works
      without UPDATE, and the owner is stopped by the trigger.

HOW TO RUN
    Needs a database where ``php artisan migrate`` AND ``php artisan
    db:apply-raw`` have both run, and a PG_DSN login that is a superuser or can
    ``SET ROLE georag_app``. Skips cleanly otherwise.

        PG_DSN=postgresql://georag:...@localhost:5432/georag \\
            pytest -m integration tests/test_rls_after_raw.py
"""
from __future__ import annotations

import contextlib
import os
import re
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import asyncpg
import pytest

pytestmark = pytest.mark.integration

PG_DSN = os.environ.get(
    "PG_DSN", "postgresql://georag:georag_dev_password@localhost:5432/georag",
)
APP_ROLE = os.environ.get("PG_APP_ROLE", "georag_app")

#: Policy owned by the migration chain alone; phase0/95 must leave it be.
FIXED_TABLES = (
    "workspace.workspace_memberships",
    "workspace.workspace_agent_config",
    "workspace.dry_run_outputs",
    "outbox.pending_propagations",
    "outbox.propagation_attempts",
)

#: workspace_id NULL means "platform row" on these; the policy must keep that.
PLATFORM_AWARE_TABLES = ("outbox.pending_propagations", "outbox.propagation_attempts")

#: 2026_08_14_030000's TABLES, verbatim. Tables a given cluster lacks are skipped.
VERIFIED_SUBSET = (
    *FIXED_TABLES,
    "usage.usage_events",
    "usage.workspace_cost_ceilings",
    "silver.kg_formation_aliases",
    "silver.kg_mineral_aliases",
    "silver.kg_report_aliases",
    "silver.kg_sample_aliases",
    "silver.collaboration_audit_log",
    "silver.collaboration_comments",
    "silver.agent_conversation_messages",
    "silver.agent_conversations",
    "silver.pdf_coordinates",
    "silver.pdf_layout_regions",
    "silver.pdf_ocr_results",
    "silver.pdf_table_cells",
    "silver.pdf_text_blocks",
    "silver.pdf_vl_summaries",
    "silver.review_audit_log",
    "silver.assay_events",
    "silver.ingest_extractions",
    "silver.ingest_layouts",
    "silver.ingest_ocr_results",
    "silver.collab_anchors",
    "silver.tier3_unlock_requests",
)

#: Still open after raw, on purpose, until three unbound code paths bind a
#: workspace (phase0/95 lists them). Compared for equality: it can only shrink.
KNOWN_OPEN_PENDING_BINDING = frozenset({"usage.usage_events", "usage.workspace_cost_ceilings"})

_UNBOUND_BRANCH = re.compile(r"\bis\s+null\b", re.IGNORECASE)

_REQUIRED_RELATIONS = (
    "silver.workspaces", "outbox.pending_propagations", "outbox.propagation_attempts",
    "workspace.workspace_memberships", "workspace.workspace_agent_config",
    "workspace.dry_run_outputs", "workspace.workspace_roles", "audit.audit_ledger",
    "audit.query_audit_log", "audit.audit_ledger_verification_runs", "public.users",
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
        raw_applied = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = 'silver' "
            "AND tablename = 'collars' AND policyname = 'collars_workspace_isolation')",
        )
        if not raw_applied:
            pytest.skip("database/raw has not been applied (phase0/96 policy absent); run db:apply-raw")
        if not await conn.fetchval("SELECT pg_has_role(current_user, $1, 'MEMBER')", APP_ROLE):
            pytest.skip(f"the PG_DSN login cannot SET ROLE {APP_ROLE}")
        yield conn
    finally:
        await conn.close()


async def _as_app_role(conn: asyncpg.Connection) -> None:
    await conn.execute(f"SET ROLE {APP_ROLE}")


@pytest.fixture
async def app_pool(owner_conn) -> AsyncIterator[asyncpg.Pool]:
    pool = await asyncpg.create_pool(
        PG_DSN, min_size=1, max_size=4, statement_cache_size=0, init=_as_app_role,
    )
    async with pool.acquire() as conn:
        flags = await conn.fetchrow(
            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user",
        )
    # If the pool bypassed RLS every assertion below would pass for the wrong reason.
    assert flags is not None and not flags["rolsuper"] and not flags["rolbypassrls"]
    try:
        yield pool
    finally:
        await pool.close()


@contextlib.asynccontextmanager
async def _scoped(conn: asyncpg.Connection, workspace_id: str | None):
    """One transaction with app.workspace_id bound (None -> cleared, i.e. unbound)."""
    async with conn.transaction():
        await conn.execute(
            "SELECT set_config('app.workspace_id', $1, true)", workspace_id or "",
        )
        yield conn


class _Rollback(Exception):
    """Raised inside a transaction block to roll a probe row back."""


async def _policies(conn: asyncpg.Connection, qualified: str) -> list[asyncpg.Record]:
    schema, table = qualified.split(".")
    return await conn.fetch(
        "SELECT policyname, permissive, cmd, coalesce(qual, '') AS qual, "
        "coalesce(with_check, '') AS with_check FROM pg_policies "
        "WHERE schemaname = $1 AND tablename = $2 ORDER BY policyname",
        schema, table,
    )


# ---------------------------------------------------------------------------
# The catalog: what policies exist after migrate + db:apply-raw
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("table", FIXED_TABLES)
async def test_fixed_table_carries_one_fail_closed_policy_after_raw(owner_conn, table: str) -> None:
    policies = await _policies(owner_conn, table)

    assert [p["policyname"] for p in policies] == ["tenant_isolation"], (
        f"{table} must carry exactly one policy after migrate + db:apply-raw; permissive policies "
        f"OR together, so a second one can only widen access. Found: {[p['policyname'] for p in policies]}"
    )
    (policy,) = policies
    for clause in ("qual", "with_check"):
        assert not _UNBOUND_BRANCH.search(policy[clause]), (
            f"{table}.tenant_isolation has an unbound-GUC (IS NULL) branch in {clause}: {policy[clause]}"
        )

    forced = await owner_conn.fetchval(
        "SELECT relforcerowsecurity AND relrowsecurity FROM pg_class WHERE oid = $1::regclass", table,
    )
    assert forced, f"{table} is not ENABLEd and FORCEd; the owner role bypasses its policy"


@pytest.mark.parametrize("table", PLATFORM_AWARE_TABLES)
async def test_outbox_policy_keeps_platform_rows_for_an_unbound_session_only(owner_conn, table: str) -> None:
    (policy,) = await _policies(owner_conn, table)
    expression = " ".join(policy["qual"].split())

    # IS NOT DISTINCT FROM: NULL matches NULL (the unbound platform pass) and a
    # uuid matches only itself. A plain `=` could match no NULL row at all.
    assert "IS DISTINCT FROM" in expression and "NOT" in expression, expression
    assert policy["with_check"], f"{table}: WITH CHECK must be explicit, not inherited from USING"


async def test_open_verified_subset_tables_after_raw_are_exactly_the_known_gap(owner_conn) -> None:
    """The ratchet: nothing new opens, and fixing the two known ones is noticed."""
    open_tables: set[str] = set()
    for table in VERIFIED_SUBSET:
        if not await owner_conn.fetchval("SELECT to_regclass($1) IS NOT NULL", table):
            continue
        for policy in await _policies(owner_conn, table):
            if _UNBOUND_BRANCH.search(policy["qual"]) or _UNBOUND_BRANCH.search(policy["with_check"]):
                open_tables.add(table)

    assert open_tables == KNOWN_OPEN_PENDING_BINDING, (
        f"verified-subset tables with an unbound-GUC branch after migrate + db:apply-raw: {sorted(open_tables)}; "
        f"expected exactly {sorted(KNOWN_OPEN_PENDING_BINDING)}. A table ADDED here is a regression (a raw file "
        "re-opened it). A table REMOVED is the usage binding fix landing: update KNOWN_OPEN_PENDING_BINDING."
    )


# ---------------------------------------------------------------------------
# The behaviour: what georag_app can actually read and write
# ---------------------------------------------------------------------------
class _Tenants:
    def __init__(self) -> None:
        self.a = str(uuid.uuid4())
        self.b = str(uuid.uuid4())
        self.marker = f"rls-after-raw-{uuid.uuid4().hex[:10]}"
        self.user_id: int | None = None
        self.platform_propagation: str | None = None
        self.propagations: dict[str, str] = {}


@pytest.fixture
async def tenants(owner_conn) -> AsyncIterator[_Tenants]:
    """Two workspaces with one row of every fixed table each, plus a platform outbox row."""
    t = _Tenants()
    role_id = await owner_conn.fetchval("SELECT id FROM workspace.workspace_roles ORDER BY name LIMIT 1")
    assert role_id is not None, "workspace.workspace_roles has no seed row"
    t.user_id = await owner_conn.fetchval(
        "INSERT INTO public.users (name, email, password) VALUES ($1, $2, 'x') RETURNING id",
        t.marker, f"{t.marker}@example.test",
    )

    for ws in (t.a, t.b):
        async with _scoped(owner_conn, ws) as c:
            await c.execute(
                "INSERT INTO silver.workspaces (workspace_id, name, slug) VALUES ($1::uuid, $2, $2)",
                ws, f"{t.marker}-{ws[:6]}",
            )
            await c.execute(
                "INSERT INTO workspace.workspace_agent_config (workspace_id, agent_name) VALUES ($1::uuid, $2)",
                ws, t.marker,
            )
            await c.execute(
                "INSERT INTO workspace.dry_run_outputs (invocation_id, workspace_id, agent_name, target, payload) "
                "VALUES (gen_random_uuid(), $1::uuid, $2, 't', '{}'::jsonb)",
                ws, t.marker,
            )
            await c.execute(
                "INSERT INTO workspace.workspace_memberships (user_id, workspace_id, role_id) "
                "VALUES ($1, $2::uuid, $3::uuid)",
                t.user_id, ws, role_id,
            )
            t.propagations[ws] = str(await c.fetchval(
                "INSERT INTO outbox.pending_propagations (workspace_id, source_schema, source_table, source_id, "
                "target_store, operation, idempotency_key) VALUES ($1::uuid, 'silver', 't', $2, "
                "'external_webhook', 'upsert', $3) RETURNING id",
                ws, t.marker, f"{t.marker}-{ws[:6]}",
            ))
            await c.execute(
                "INSERT INTO outbox.propagation_attempts (propagation_id, workspace_id, attempt_no, status) "
                "VALUES ($1::uuid, $2::uuid, 1, 'success')",
                t.propagations[ws], ws,
            )

    async with _scoped(owner_conn, None) as c:
        t.platform_propagation = str(await c.fetchval(
            "INSERT INTO outbox.pending_propagations (workspace_id, source_schema, source_table, source_id, "
            "target_store, operation, idempotency_key) VALUES (NULL, 'audit', 't', $1, "
            "'external_webhook', 'upsert', $2) RETURNING id",
            t.marker, f"{t.marker}-platform",
        ))
        await c.execute(
            "INSERT INTO outbox.propagation_attempts (propagation_id, workspace_id, attempt_no, status) "
            "VALUES ($1::uuid, NULL, 1, 'success')",
            t.platform_propagation,
        )
    try:
        yield t
    finally:
        async with _scoped(owner_conn, None) as c:
            await c.execute(
                "DELETE FROM outbox.pending_propagations WHERE source_id = $1", t.marker,
            )
            await c.execute("DELETE FROM silver.workspaces WHERE workspace_id = ANY($1::uuid[])", [t.a, t.b])
            await c.execute("DELETE FROM public.users WHERE id = $1", t.user_id)


_TENANT_ROW_COUNT_SQL = {
    "workspace.workspace_memberships": "SELECT count(*) FROM workspace.workspace_memberships WHERE workspace_id = ANY($1::uuid[])",
    "workspace.workspace_agent_config": "SELECT count(*) FROM workspace.workspace_agent_config WHERE workspace_id = ANY($1::uuid[])",
    "workspace.dry_run_outputs": "SELECT count(*) FROM workspace.dry_run_outputs WHERE workspace_id = ANY($1::uuid[])",
    "outbox.pending_propagations": "SELECT count(*) FROM outbox.pending_propagations WHERE workspace_id = ANY($1::uuid[])",
    "outbox.propagation_attempts": "SELECT count(*) FROM outbox.propagation_attempts WHERE workspace_id = ANY($1::uuid[])",
}


@pytest.mark.parametrize("table", FIXED_TABLES)
async def test_an_unbound_app_session_reads_no_tenant_rows(app_pool, tenants, table: str) -> None:
    async with app_pool.acquire() as conn:
        for scope in (None, ""):
            async with conn.transaction():
                if scope is not None:
                    await conn.execute("SELECT set_config('app.workspace_id', $1, true)", scope)
                n = await conn.fetchval(_TENANT_ROW_COUNT_SQL[table], [tenants.a, tenants.b])
            assert n == 0, (
                f"{table}: an unbound georag_app session (GUC {'unset' if scope is None else repr(scope)}) "
                f"read {n} tenant row(s). This is the cross-tenant read the fail-open policy allowed."
            )


@pytest.mark.parametrize("table", FIXED_TABLES)
async def test_a_bound_app_session_reads_only_its_own_workspace(app_pool, tenants, table: str) -> None:
    async with app_pool.acquire() as conn:
        for own, other in ((tenants.a, tenants.b), (tenants.b, tenants.a)):
            async with _scoped(conn, own):
                mine = await conn.fetchval(_TENANT_ROW_COUNT_SQL[table], [own])
                theirs = await conn.fetchval(_TENANT_ROW_COUNT_SQL[table], [other])
            assert (mine, theirs) == (1, 0), f"{table}: bound to {own[:8]} saw own={mine} other={theirs}"


async def test_the_outbox_platform_row_is_visible_to_an_unbound_session_and_nobody_else(app_pool, tenants) -> None:
    sql = "SELECT count(*) FROM outbox.pending_propagations WHERE id = $1::uuid"
    async with app_pool.acquire() as conn:
        async with _scoped(conn, None):
            assert await conn.fetchval(sql, tenants.platform_propagation) == 1
            assert await conn.fetchval(sql, tenants.propagations[tenants.a]) == 0
        async with _scoped(conn, tenants.a):
            assert await conn.fetchval(sql, tenants.platform_propagation) == 0, (
                "a tenant-bound session must not see platform rows"
            )
            assert await conn.fetchval(sql, tenants.propagations[tenants.a]) == 1


async def test_outbox_writes_are_fenced_the_same_way(app_pool, tenants) -> None:
    insert = (
        "INSERT INTO outbox.pending_propagations (workspace_id, source_schema, source_table, source_id, "
        "target_store, operation, idempotency_key) VALUES ($1::uuid, 'silver', 't', $2, "
        "'external_webhook', 'upsert', $3)"
    )

    async def refused(scope: str | None, row_ws: str | None) -> bool:
        async with app_pool.acquire() as conn:
            try:
                async with _scoped(conn, scope):
                    await conn.execute(insert, row_ws, tenants.marker, f"{tenants.marker}-{uuid.uuid4().hex[:6]}")
                    raise _Rollback
            except _Rollback:
                return False
            except asyncpg.InsufficientPrivilegeError as exc:
                assert "row-level security" in str(exc), exc
                return True

    assert await refused(None, tenants.a), "an unbound session wrote a TENANT outbox row"
    assert await refused(tenants.a, tenants.b), "a session bound to A wrote a row for B"
    assert await refused(tenants.a, None), "a tenant-bound session wrote a PLATFORM row"
    assert not await refused(tenants.a, tenants.a), "a session bound to A could not write A's own row"
    assert not await refused(None, None), "the unbound platform pass could not write a platform row"


@pytest.mark.parametrize(
    ("table", "insert"),
    [
        (
            "workspace.workspace_agent_config",
            "INSERT INTO workspace.workspace_agent_config (workspace_id, agent_name) VALUES ($1::uuid, 'probe')",
        ),
        (
            "workspace.dry_run_outputs",
            "INSERT INTO workspace.dry_run_outputs (invocation_id, workspace_id, agent_name, target, payload) "
            "VALUES (gen_random_uuid(), $1::uuid, 'probe', 't', '{}'::jsonb)",
        ),
    ],
)
async def test_workspace_tables_refuse_an_unbound_write(app_pool, tenants, table: str, insert: str) -> None:
    async with app_pool.acquire() as conn:
        with pytest.raises(asyncpg.InsufficientPrivilegeError, match="row-level security"):
            async with _scoped(conn, None):
                await conn.execute(insert, tenants.a)
        # Bound to the row's own workspace the same statement is accepted. The
        # row goes with the workspace when the fixture deletes it (ON DELETE CASCADE).
        async with _scoped(conn, tenants.a):
            await conn.execute(insert, tenants.a)


# ---------------------------------------------------------------------------
# Finding 2: retention_sweep can purge audit.query_audit_log as georag_app
# ---------------------------------------------------------------------------
async def test_retention_sweep_purge_runs_as_the_app_role(app_pool, owner_conn) -> None:
    # The sweep's real SQL, not a copy of it.
    from app.hatchet_workflows.retention_sweep import _QUERY_AUDIT_BATCH_SQL

    query_id = f"ret-{uuid.uuid4().hex[:12]}"
    await owner_conn.execute(
        "INSERT INTO audit.query_audit_log (query_id, query_text, created_at) "
        "VALUES ($1, 'q', now() - interval '400 days')",
        query_id,
    )
    expected = await owner_conn.fetchval(
        "SELECT count(*) FROM audit.query_audit_log WHERE created_at < now() - interval '399 days'",
    )
    assert expected >= 1

    async with app_pool.acquire() as conn:
        deleted = await conn.fetchval(_QUERY_AUDIT_BATCH_SQL, 399, 5000)

    assert deleted == expected, "permission denied here is the AWS failure: georag_app held no DELETE on the table"
    assert await owner_conn.fetchval("SELECT count(*) FROM audit.query_audit_log WHERE query_id = $1", query_id) == 0


# ---------------------------------------------------------------------------
# Findings 3 and 4: the ledger is append-only and its trigger works without UPDATE
# ---------------------------------------------------------------------------
@contextlib.asynccontextmanager
async def _ledger_rows(owner_conn: asyncpg.Connection, action: str) -> AsyncIterator[None]:
    try:
        yield
    finally:
        # The ledger refuses DELETE for everyone; a superuser fixture removes its
        # own probe rows with ordinary triggers off for this one transaction.
        async with owner_conn.transaction():
            await owner_conn.execute("SET LOCAL session_replication_role = replica")
            await owner_conn.execute("DELETE FROM audit.audit_ledger WHERE action_type = $1", action)


async def test_the_app_role_can_append_to_the_ledger_but_not_rewrite_it(app_pool, owner_conn) -> None:
    action = f"rls.after_raw.{uuid.uuid4().hex[:8]}"
    ws = str(uuid.uuid4())
    async with _ledger_rows(owner_conn, action):
        async with app_pool.acquire() as conn:
            async with _scoped(conn, None):
                platform = await conn.fetchrow(
                    "INSERT INTO audit.audit_ledger (workspace_id, actor_kind, action_type, payload) "
                    "VALUES (NULL, 'system', $1, '{}'::jsonb) RETURNING id, length(hash) AS n", action,
                )
            async with _scoped(conn, ws):
                tenant = await conn.fetchrow(
                    "INSERT INTO audit.audit_ledger (workspace_id, actor_kind, action_type, payload) "
                    "VALUES ($1::uuid, 'system', $2, '{}'::jsonb) RETURNING id, length(hash) AS n", ws, action,
                )
            assert platform["n"] == 32 and tenant["n"] == 32, (
                "the hash trigger must work for the app role without the UPDATE privilege"
            )

            for statement in (
                "UPDATE audit.audit_ledger SET payload = '{}'::jsonb WHERE id = $1::uuid",
                "DELETE FROM audit.audit_ledger WHERE id = $1::uuid",
            ):
                with pytest.raises(asyncpg.InsufficientPrivilegeError, match="permission denied"):
                    async with _scoped(conn, None):
                        await conn.execute(statement, str(platform["id"]))

        # The privilege matrix after both layers, for the roles that matter.
        privileges = {
            priv: await owner_conn.fetchval(
                "SELECT has_table_privilege($1, 'audit.audit_ledger', $2)", APP_ROLE, priv,
            )
            for priv in ("INSERT", "SELECT", "UPDATE", "DELETE")
        }
        assert privileges == {"INSERT": True, "SELECT": True, "UPDATE": False, "DELETE": False}
        assert await owner_conn.fetchval(
            "SELECT has_table_privilege($1, 'audit.audit_ledger_verification_runs', 'UPDATE')", APP_ROLE,
        ), "audit.run_verification() UPDATEs its own run row; the app role must keep that"


async def test_the_trigger_stops_the_owner_too_and_the_row_survives(owner_conn) -> None:
    action = f"rls.after_raw.{uuid.uuid4().hex[:8]}"
    async with _ledger_rows(owner_conn, action):
        row_id = await owner_conn.fetchval(
            "INSERT INTO audit.audit_ledger (workspace_id, actor_kind, action_type, payload) "
            "VALUES (NULL, 'system', $1, '{}'::jsonb) RETURNING id", action,
        )
        for statement in (
            "UPDATE audit.audit_ledger SET hash = decode('00', 'hex') WHERE id = $1",
            "DELETE FROM audit.audit_ledger WHERE id = $1",
        ):
            with pytest.raises(asyncpg.RestrictViolationError, match="audit.audit_ledger is append-only"):
                await owner_conn.execute(statement, row_id)
        assert await owner_conn.fetchval("SELECT count(*) FROM audit.audit_ledger WHERE id = $1", row_id) == 1


async def test_a_stale_timestamp_cannot_fork_the_chain_the_verifier_walks(app_pool, owner_conn) -> None:
    """created_at is taken inside the trigger, after the chain lock (finding 4).

    Writer B arrives after writer A but brings a timestamp from before A's: what a
    writer that waited on the lock looks like when created_at comes from the DEFAULT.
    """
    action = f"rls.after_raw.{uuid.uuid4().hex[:8]}"
    ws = str(uuid.uuid4())
    async with _ledger_rows(owner_conn, action):
        async with app_pool.acquire() as conn:
            ids = []
            for stale in (datetime(2099, 1, 1, tzinfo=UTC), datetime(2000, 1, 1, tzinfo=UTC)):
                async with _scoped(conn, ws):
                    ids.append(await conn.fetchval(
                        "INSERT INTO audit.audit_ledger (workspace_id, actor_kind, action_type, payload, created_at) "
                        "VALUES ($1::uuid, 'system', $2, '{}'::jsonb, $3) RETURNING id",
                        ws, action, stale,
                    ))

        a_created = await owner_conn.fetchval("SELECT created_at FROM audit.audit_ledger WHERE id = $1", ids[0])
        b_created = await owner_conn.fetchval("SELECT created_at FROM audit.audit_ledger WHERE id = $1", ids[1])
        assert b_created > a_created, "a child row carries an older created_at than its parent"

        breaks = await owner_conn.fetch(
            "SELECT audit_id FROM audit.verify_hash_chain('1999-01-01'::timestamptz, '2100-01-01'::timestamptz) "
            "WHERE workspace_id = $1::uuid", ws,
        )
        assert breaks == [], "verify_hash_chain reports a break on a chain nobody tampered with"
