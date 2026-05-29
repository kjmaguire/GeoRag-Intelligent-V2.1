"""§11.5 — Tenant Isolation Auditor (Phase H4).

Programmatic guard that EVERY table in silver / gold / ops / audit /
workflow / targeting / public_geoscience schemas carries:

  1. A ``workspace_id`` column (or is on the audit-public namespace
     exemption list).
  2. A foreign-key reference to ``silver.workspaces(workspace_id)``.
  3. Row-Level Security enabled.
  4. At least one RLS policy that filters on
     ``current_setting('app.workspace_id', ...)::UUID``.
  5. An index covering ``workspace_id`` (B-tree, GIN, or partial — any
     index that includes the column is acceptable).

Runs on every PR via pytest. A regression here is a cross-tenant
data-leak primitive in waiting, so the auditor refuses to be skipped
unless the live DB is unreachable.

Per §11.5 "deployment topologies + tenant isolation" the auditor
acts as a CI gate. The reciprocal check at runtime is the
``MULTI_TENANT_ENFORCEMENT_ENABLED`` config + pydantic model_validator
in app/config.py that refuses to start in an unsafe configuration.
"""
from __future__ import annotations

import os

import asyncpg
import pytest

pytestmark = pytest.mark.asyncio


# Schemas that MUST be tenant-isolated. Skipping one here is a major
# decision — every operational + analytical + audit surface must
# enforce workspace boundaries.
_TENANT_SCHEMAS: tuple[str, ...] = (
    "silver",
    "gold",
    "audit",
    "ops",
    "workflow",
    "targeting",
    # public_geoscience is EXEMPT by design (Block 4 decision, 2026-05-15).
    # That schema holds Crown-copyright open-data reference tables
    # (NRCan / BC Geological Survey / SK Geological Survey / etc.) that
    # are shared globally across all workspaces. Access control is at
    # the GRANT level (georag_app has CRUD via the role grants in
    # database/migrations; non-georag_app roles get SELECT only). No
    # workspace_id column or RLS policy is appropriate — adding one
    # would force operators to duplicate the open dataset per workspace
    # for no security gain.
    #
    # See docs/audits/tenant_isolation_findings_2026_05_15.md §Block 4
    # for the full SME decision + rationale.
)


# Tables exempt from the workspace_id column requirement:
#
#  - silver.workspaces / silver.users / silver.user_workspace_grants —
#    the workspace registry itself + cross-workspace identity
#  - audit.audit_ledger — has workspace_id but stored as nullable text
#    (some action_types are infrastructure-wide; the RLS policy still
#    filters when the value is set)
#  - public_geoscience.pg_sources / pg_jurisdictions / pg_commodities —
#    shared reference data (Crown copyright / open data); read-only to
#    every workspace; not tenant-scoped by design
#  - workflow.workflow_runs — has workspace_id but the unified view
#    cross-references all workspaces for the ops dashboard
_WORKSPACE_ID_EXEMPT: set[tuple[str, str]] = {
    ("silver", "workspaces"),
    ("silver", "users"),
    ("silver", "user_workspace_grants"),
    # Shared reference data — geological ontology + KG aliases are
    # workspace-agnostic catalogues. Block 2 EXEMPT (2026-05-15).
    ("silver", "geological_ontology_terms"),
    ("silver", "geological_ontology_synonyms"),
    # Block 3 EXEMPT (2026-05-15) — platform-wide infra:
    ("workflow", "flow_jwt_keys"),       # per-flow signing keys
    ("workflow", "flow_registry"),       # available flows manifest
    # SME-curated global model catalogue (per-workspace overrides land
    # via a future silver.workspace_target_model_overrides table):
    ("targeting", "target_models"),
    ("targeting", "target_model_versions"),
    ("public_geoscience", "pg_sources"),
    ("public_geoscience", "pg_jurisdictions"),
    ("public_geoscience", "pg_commodities"),
    # Phase H4 EXEMPT (2026-05-15):
    # QP credentials — Qualified Persons sign off across workspaces
    # per §29.6; the registry is cross-workspace by design. Access is
    # gated at the Laravel admin Gate, not RLS.
    ("silver", "qp_credentials"),
    # Activepieces webhook channel registry — workflow.* is platform-
    # level outbox dispatcher infra. Access is gated at the Laravel
    # admin Gate, not RLS.
    ("workflow", "activepieces_channels"),
    # §6.6 EXEMPT (2026-05-16, kickoff-locked):
    # h3 density aggregation of public-geoscience mineral data —
    # cross-tenant by design. Public geoscience is shared
    # infrastructure with no workspace scoping.
    ("gold", "h3_density_mineral"),
}


# Tables exempt from the RLS-required check. Limited to the
# workspace registry itself + a couple of operational tables where
# the app layer enforces scope instead (e.g., ops.dry_run_records
# carries an actor_user_id and is filtered application-side).
_RLS_EXEMPT: set[tuple[str, str]] = {
    ("silver", "workspaces"),
    ("silver", "users"),
    # Shared reference data — see _WORKSPACE_ID_EXEMPT rationale.
    ("silver", "geological_ontology_terms"),
    ("silver", "geological_ontology_synonyms"),
    # Block 3 — platform-wide infra (see _WORKSPACE_ID_EXEMPT):
    ("workflow", "flow_jwt_keys"),
    ("workflow", "flow_registry"),
    ("targeting", "target_models"),
    ("targeting", "target_model_versions"),
    # Phase H4 — cross-workspace registries, admin-gated:
    ("silver", "qp_credentials"),
    ("workflow", "activepieces_channels"),
    # §6.6 — h3 density choropleth, cross-tenant shared aggregation:
    ("gold", "h3_density_mineral"),
}


def _dsn() -> str:
    user = os.environ.get("POSTGRES_USER", "georag")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


async def _pool():
    """Connect or skip — Tenant Isolation Auditor needs the live DB."""
    if not os.environ.get("POSTGRES_PASSWORD"):
        pytest.skip("POSTGRES_PASSWORD not set — Tenant Isolation Auditor needs live DB")
    try:
        return await asyncpg.create_pool(_dsn(), min_size=1, max_size=2)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"DB unreachable: {type(exc).__name__}: {exc}")


async def _tables(pool) -> list[tuple[str, str]]:
    """Return (schema, table) pairs across the tenant schemas."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT n.nspname AS schema_name, c.relname AS table_name
              FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE c.relkind = 'r'
               AND n.nspname = ANY($1::text[])
             ORDER BY n.nspname, c.relname
            """,
            list(_TENANT_SCHEMAS),
        )
    return [(r["schema_name"], r["table_name"]) for r in rows]


# ──────────────────── Gate 1 — workspace_id column ─────────────────────


async def test_every_tenant_table_carries_workspace_id_column():
    pool = await _pool()
    try:
        tables = await _tables(pool)
        offenders: list[tuple[str, str]] = []
        async with pool.acquire() as conn:
            for schema, table in tables:
                if (schema, table) in _WORKSPACE_ID_EXEMPT:
                    continue
                col = await conn.fetchval(
                    """
                    SELECT column_name FROM information_schema.columns
                     WHERE table_schema = $1
                       AND table_name   = $2
                       AND column_name  = 'workspace_id'
                    """,
                    schema, table,
                )
                if col is None:
                    offenders.append((schema, table))
        assert not offenders, (
            f"Tenant Isolation Auditor — {len(offenders)} table(s) missing "
            f"workspace_id column:\n  "
            + "\n  ".join(f"{s}.{t}" for s, t in offenders)
            + "\nFix: ALTER TABLE add column + backfill, OR add to "
              "_WORKSPACE_ID_EXEMPT with a comment."
        )
    finally:
        await pool.close()


# ──────────────────── Gate 2 — workspace_id FK ─────────────────────────


async def test_workspace_id_columns_have_fk_to_silver_workspaces():
    pool = await _pool()
    try:
        tables = await _tables(pool)
        offenders: list[tuple[str, str]] = []
        async with pool.acquire() as conn:
            for schema, table in tables:
                if (schema, table) in _WORKSPACE_ID_EXEMPT:
                    continue
                # FK may exist on the table OR on a partitioned parent
                # it inherits from. Walk pg_inherits up the tree, using
                # pg_catalog so it works regardless of the connecting
                # role's information_schema visibility.
                fk_count = await conn.fetchval(
                    """
                    WITH RECURSIVE parents AS (
                        SELECT ($1 || '.' || $2)::regclass AS oid
                        UNION ALL
                        SELECT i.inhparent
                          FROM pg_inherits i
                          JOIN parents p ON p.oid = i.inhrelid
                    )
                    SELECT count(*)
                      FROM pg_constraint c
                      JOIN parents p ON p.oid = c.conrelid
                     WHERE c.contype = 'f'
                       -- FK column is workspace_id
                       AND EXISTS (
                           SELECT 1 FROM pg_attribute a
                            WHERE a.attrelid = c.conrelid
                              AND a.attname = 'workspace_id'
                              AND a.attnum = ANY(c.conkey)
                       )
                       -- references silver.workspaces
                       AND c.confrelid = 'silver.workspaces'::regclass
                    """,
                    schema, table,
                )
                if (fk_count or 0) == 0:
                    offenders.append((schema, table))
        assert not offenders, (
            f"Tenant Isolation Auditor — {len(offenders)} table(s) have "
            f"workspace_id but no FK to silver.workspaces:\n  "
            + "\n  ".join(f"{s}.{t}" for s, t in offenders)
            + "\nFix: ALTER TABLE add FK constraint."
        )
    finally:
        await pool.close()


# ──────────────────── Gate 3 — RLS enabled ─────────────────────────────


async def test_every_tenant_table_has_rls_enabled():
    pool = await _pool()
    try:
        tables = await _tables(pool)
        offenders: list[tuple[str, str]] = []
        async with pool.acquire() as conn:
            for schema, table in tables:
                if (schema, table) in _RLS_EXEMPT:
                    continue
                rls = await conn.fetchval(
                    """
                    SELECT c.relrowsecurity
                      FROM pg_class c
                      JOIN pg_namespace n ON n.oid = c.relnamespace
                     WHERE n.nspname = $1 AND c.relname = $2
                    """,
                    schema, table,
                )
                if not rls:
                    offenders.append((schema, table))
        assert not offenders, (
            f"Tenant Isolation Auditor — {len(offenders)} table(s) without "
            f"RLS enabled:\n  "
            + "\n  ".join(f"{s}.{t}" for s, t in offenders)
            + "\nFix: ALTER TABLE ENABLE ROW LEVEL SECURITY + add a "
              "workspace-scoped policy."
        )
    finally:
        await pool.close()


# ──────────────────── Gate 4 — at least one workspace_id RLS policy ────


async def test_every_rls_table_has_workspace_filtering_policy():
    pool = await _pool()
    try:
        tables = await _tables(pool)
        offenders: list[tuple[str, str]] = []
        async with pool.acquire() as conn:
            for schema, table in tables:
                if (schema, table) in _RLS_EXEMPT:
                    continue
                # A workspace_id policy may be defined on either the
                # table itself OR on a partitioned parent it inherits
                # from. Walk pg_inherits up the chain to detect either.
                policy_count = await conn.fetchval(
                    """
                    WITH RECURSIVE parents AS (
                        SELECT ($1 || '.' || $2)::regclass AS oid
                        UNION ALL
                        SELECT i.inhparent
                          FROM pg_inherits i
                          JOIN parents p ON p.oid = i.inhrelid
                    )
                    SELECT count(*)
                      FROM pg_policies pp
                      JOIN parents p
                        ON (pp.schemaname || '.' || pp.tablename)::regclass = p.oid
                     WHERE pp.qual ILIKE '%workspace_id%'
                        OR pp.with_check ILIKE '%workspace_id%'
                    """,
                    schema, table,
                )
                if (policy_count or 0) == 0:
                    offenders.append((schema, table))
        assert not offenders, (
            f"Tenant Isolation Auditor — {len(offenders)} table(s) with "
            f"RLS enabled but no workspace_id-filtering policy:\n  "
            + "\n  ".join(f"{s}.{t}" for s, t in offenders)
            + "\nFix: CREATE POLICY ... USING (workspace_id = "
              "current_setting('app.workspace_id', TRUE)::UUID)"
        )
    finally:
        await pool.close()


# ──────────────────── Gate 5 — workspace_id index coverage ─────────────


async def test_every_tenant_table_has_workspace_id_indexed():
    pool = await _pool()
    try:
        tables = await _tables(pool)
        offenders: list[tuple[str, str]] = []
        async with pool.acquire() as conn:
            for schema, table in tables:
                if (schema, table) in _WORKSPACE_ID_EXEMPT:
                    continue
                idx_count = await conn.fetchval(
                    """
                    SELECT count(*) FROM pg_indexes
                     WHERE schemaname = $1
                       AND tablename  = $2
                       AND indexdef ILIKE '%workspace_id%'
                    """,
                    schema, table,
                )
                if (idx_count or 0) == 0:
                    offenders.append((schema, table))
        assert not offenders, (
            f"Tenant Isolation Auditor — {len(offenders)} table(s) with "
            f"workspace_id column but no covering index:\n  "
            + "\n  ".join(f"{s}.{t}" for s, t in offenders)
            + "\nFix: CREATE INDEX idx_<table>_workspace_id ON "
              "<schema>.<table> (workspace_id) [+ other columns]."
        )
    finally:
        await pool.close()


# ──────────────────── Gate 6 — SINGLE_TENANT_MODE / multi-tenant flag ──


async def test_settings_refuse_unsafe_tenant_configuration():
    """The pydantic model_validator in app/config.py rejects the
    combination MULTI_TENANT_ENFORCEMENT_ENABLED=False AND
    SINGLE_TENANT_MODE=False at startup. This test exercises the same
    validator path in-process."""
    import pydantic

    from app.config import Settings  # noqa: PLC0415

    base = {k: v for k, v in os.environ.items() if k.startswith(("POSTGRES_", "REDIS_", "NEO4J_", "QDRANT_", "FASTAPI_", "SEAWEEDFS_"))}
    # Force the unsafe combo
    base["MULTI_TENANT_ENFORCEMENT_ENABLED"] = "false"
    base["SINGLE_TENANT_MODE"] = "false"

    with pytest.raises(pydantic.ValidationError):
        Settings(**{k.lower(): v for k, v in base.items()})


# ──────────────────── Gate 7 — `app.workspace_id` GUC enforcement ──────


async def test_rls_actually_blocks_cross_workspace_select():
    """The most important check: with RLS enabled + workspace GUC set
    to workspace A, a SELECT against silver.collars must NOT see
    workspace B's rows. Probes a real silver table end-to-end."""
    pool = await _pool()
    try:
        async with pool.acquire() as conn:
            # Use two synthetic workspace ids that don't exist; the
            # assertion is on RLS behaviour, not on the test data
            # itself. If the table has rows for either ws, the RLS
            # filter scopes them off.
            ws_a = "11111111-1111-1111-1111-111111111111"
            ws_b = "22222222-2222-2222-2222-222222222222"
            await conn.execute(
                "SELECT set_config('app.workspace_id', $1, false)", ws_a,
            )
            count_a = await conn.fetchval(
                "SELECT count(*) FROM silver.collars"
            )
            await conn.execute(
                "SELECT set_config('app.workspace_id', $1, false)", ws_b,
            )
            count_b = await conn.fetchval(
                "SELECT count(*) FROM silver.collars"
            )
            # Both should see 0 rows because neither ws exists in the
            # data. If both saw the SAME non-zero count, that's
            # cross-tenant leakage (RLS off).
            assert count_a == 0, (
                f"RLS BREACH: workspace A saw {count_a} collars for a "
                f"non-existent workspace_id"
            )
            assert count_b == 0, (
                f"RLS BREACH: workspace B saw {count_b} collars for a "
                f"non-existent workspace_id"
            )
    finally:
        await pool.close()
