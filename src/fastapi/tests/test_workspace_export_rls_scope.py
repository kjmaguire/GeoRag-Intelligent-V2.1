"""SEC-4 — workspace_export binds the tenant before it reads.

In production the export connects as ``georag_app`` (NOSUPERUSER,
NOBYPASSRLS). It used to run ``SELECT … WHERE workspace_id = $1`` with no
``app.workspace_id`` bound, and on the tables whose policy is fail-closed —
silver.hypotheses, silver.decision_records, silver.document_passages,
targeting.target_recommendations — that returns zero rows. The export then
reported success with those tables silently empty, and a restore from it
"succeeded" with the data missing.

This drives ``run_export`` itself (S3, audit and admin broadcast stubbed)
through a throwaway NOBYPASSRLS login role that inherits georag_app's
grants, and asserts the seeded fail-closed row is in the uploaded bundle.

Needs a Postgres with the migration chain applied; ``PG_DSN`` must be a
superuser DSN (it creates and drops the probe role). Skips otherwise.
"""
from __future__ import annotations

import gzip
import importlib
import json
import pathlib
import os
import secrets
import sys
import types
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest

PG_DSN = os.environ.get("PG_DSN") or (
    "postgresql://{u}:{p}@{h}:{port}/{db}".format(
        u=os.environ.get("POSTGRES_USER", "georag"),
        p=os.environ.get("POSTGRES_PASSWORD", "georag_dev_password"),
        h=os.environ.get("POSTGRES_DIRECT_HOST", os.environ.get("POSTGRES_HOST", "localhost")),
        port=os.environ.get("POSTGRES_DIRECT_PORT", os.environ.get("POSTGRES_PORT", "5432")),
        db=os.environ.get("POSTGRES_DB", "georag"),
    )
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def _import_workspace_export() -> types.ModuleType:
    """Import the workflow module, stubbing the Hatchet client when no
    HATCHET_CLIENT_TOKEN is configured (the package builds a client at
    import). The stub only needs to make ``@workflow.task`` hand back an
    object with ``.fn``, which is how the real Task exposes the function."""
    try:
        return importlib.import_module("app.hatchet_workflows.workspace_export")
    except Exception:  # noqa: BLE001 — no token / no engine: stub below
        pass

    class _Workflow:
        def task(self, *_a: Any, **_k: Any) -> Any:
            return lambda fn: types.SimpleNamespace(fn=fn)

        def __getattr__(self, _name: str) -> Any:
            return lambda *a, **k: (lambda fn: types.SimpleNamespace(fn=fn))

    class _Hatchet:
        def workflow(self, *_a: Any, **_k: Any) -> _Workflow:
            return _Workflow()

        def __getattr__(self, _name: str) -> Any:
            return lambda *a, **k: _Workflow()

    pkg_dir = Path(__file__).resolve().parents[1] / "app" / "hatchet_workflows"
    stub = types.ModuleType("app.hatchet_workflows")
    stub.__path__ = [str(pkg_dir)]  # type: ignore[attr-defined]
    stub.hatchet = _Hatchet()  # type: ignore[attr-defined]
    saved = {k: v for k, v in sys.modules.items() if k.startswith("app.hatchet_workflows")}
    for k in saved:
        del sys.modules[k]
    sys.modules["app.hatchet_workflows"] = stub
    try:
        return importlib.import_module("app.hatchet_workflows.workspace_export")
    finally:
        # Leave no stub behind for other tests in the session.
        for k in [k for k in sys.modules if k.startswith("app.hatchet_workflows")]:
            del sys.modules[k]
        sys.modules.update(saved)


def _dsn_for(role: str, password: str) -> str:
    parts = urlsplit(PG_DSN)
    host = parts.hostname or "localhost"
    port = f":{parts.port}" if parts.port else ""
    return urlunsplit((parts.scheme, f"{role}:{password}@{host}{port}", parts.path, "", ""))


@pytest.fixture
async def admin() -> Any:
    try:
        conn = await asyncpg.connect(PG_DSN, timeout=5)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no Postgres at PG_DSN: {exc}")
    try:
        ok = await conn.fetchval(
            "SELECT to_regclass('silver.hypotheses') IS NOT NULL "
            "AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'georag_app' AND NOT rolbypassrls) "
            "AND (SELECT rolsuper FROM pg_roles WHERE rolname = current_user)"
        )
        if not ok:
            pytest.skip("needs the migrated schema, a georag_app role, and a superuser PG_DSN")
        yield conn
    finally:
        await conn.close()


async def test_export_reads_fail_closed_tables_under_a_nobypassrls_role(
    admin: asyncpg.Connection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    we = _import_workspace_export()

    workspace_id = str(uuid.uuid4())
    role = f"sec4_probe_{uuid.uuid4().hex[:8]}"
    password = secrets.token_hex(16)

    await admin.execute(
        f"CREATE ROLE {role} LOGIN NOSUPERUSER NOBYPASSRLS INHERIT PASSWORD '{password}'"
    )
    await admin.execute(f"GRANT georag_app TO {role}")
    await admin.execute(
        "INSERT INTO silver.workspaces (workspace_id, name, slug, created_at, updated_at) "
        "VALUES ($1::uuid, 'SEC-4 probe', $2, now(), now())",
        workspace_id, f"sec4-{workspace_id[:8]}",
    )
    await admin.execute(
        "INSERT INTO silver.hypotheses (workspace_id, parent_question, label, description) "
        "VALUES ($1::uuid, 'SEC-4 probe question', 'H1', 'fail-closed probe row')",
        workspace_id,
    )

    try:
        probe_dsn = _dsn_for(role, password)

        # The defect, reproduced: the same filtered read the export used to
        # issue, as a NOBYPASSRLS role with no GUC bound, sees nothing.
        bare = await asyncpg.connect(probe_dsn)
        try:
            unbound = await bare.fetchval(
                "SELECT count(*) FROM silver.hypotheses WHERE workspace_id = $1::uuid",
                workspace_id,
            )
        finally:
            await bare.close()
        assert unbound == 0, "precondition: silver.hypotheses should be fail-closed"

        uploaded: dict[str, bytes] = {}

        # The export streams to a temp file and hands it to _upload_file_s3
        # (2026-10-04); read it before run_export removes the spool.
        async def _fake_put(bucket: str, key: str, path: str) -> None:
            uploaded[key] = pathlib.Path(path).read_bytes()

        async def _no_audit(*_a: Any, **_k: Any) -> None:
            return None

        monkeypatch.setattr(we, "_build_dsn", lambda: probe_dsn)
        monkeypatch.setattr(we, "_upload_file_s3", _fake_put)
        monkeypatch.setattr(we, "emit_audit", _no_audit)
        import app.services.laravel_bridge as bridge

        monkeypatch.setattr(bridge, "post_admin_surface_updated", _no_audit)

        out = await we.run_export.fn(
            we.WorkspaceExportInput(
                workspace_id=workspace_id,
                include_neo4j=False,
                include_qdrant=False,
                include_redis=False,
            ),
            types.SimpleNamespace(workflow_run_id="sec4-test"),
        )

        assert out.per_table["silver_hypotheses"] == 1, out.per_table
        assert out.per_table["silver_workspaces"] == 1, out.per_table

        (body,) = uploaded.values()
        lines = gzip.decompress(body).decode().splitlines()
        hypothesis_rows = [
            json.loads(line) for line in lines
            if '"silver_hypotheses"' in line and "fail-closed probe row" in line
        ]
        assert hypothesis_rows, "the fail-closed row is missing from the uploaded bundle"
    finally:
        await admin.execute(
            "DELETE FROM silver.workspaces WHERE workspace_id = $1::uuid", workspace_id,
        )
        await admin.execute(f"DROP OWNED BY {role}")
        await admin.execute(f"DROP ROLE IF EXISTS {role}")
