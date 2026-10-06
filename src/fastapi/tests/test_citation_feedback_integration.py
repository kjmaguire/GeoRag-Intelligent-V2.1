"""Live-Postgres test for the §12.8 citation-feedback writer (audit PG-7).

The writer used to INSERT columns ``silver.source_trust_features`` does not
have (source_document_id, recorded_at) and omit the ones it requires
(trust_score_id, feature_name, feature_value), so every call raised
UndefinedColumn. The admin source-trust list's subquery filtered on the same
phantom column. This drives both handlers against a migrated database, as
``georag_app`` with the workspace GUC bound by ``scoped_connection`` — i.e.
through FORCE ROW LEVEL SECURITY, not as a superuser that bypasses it.

Skip-safe module-level guard on POSTGRES_USER, same convention as
test_csv_collar_ingester_integration.py.
"""
from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.integration

if not os.environ.get("POSTGRES_USER"):
    pytest.skip("postgres env not configured", allow_module_level=True)

import json  # noqa: E402
import sys  # noqa: E402
import uuid  # noqa: E402
from types import SimpleNamespace  # noqa: E402

import asyncpg  # noqa: E402

from app.routers.citation_feedback import FeedbackRequest, post_feedback  # noqa: E402
from app.services.source_trust.boost import (  # noqa: E402
    FEEDBACK_ANCHOR_MODEL_VERSION,
    _trust_lookup,
)


def _dsn() -> str:
    user = os.environ["POSTGRES_USER"]
    password = os.environ["POSTGRES_PASSWORD"]
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


async def _as_app_role(conn: asyncpg.Connection) -> None:
    await conn.execute("SET ROLE georag_app")


@pytest.fixture
async def workspace_id():
    ws = str(uuid.uuid4())
    admin = await asyncpg.connect(_dsn(), statement_cache_size=0)
    try:
        await admin.execute(
            "INSERT INTO silver.workspaces (workspace_id, name, slug) VALUES ($1::uuid, $2, $3)",
            ws, "citation-feedback-it", f"citation-feedback-it-{ws[:8]}",
        )
        yield ws
    finally:
        # Cascades to source_trust_scores -> source_trust_features.
        await admin.execute("DELETE FROM silver.workspaces WHERE workspace_id = $1::uuid", ws)
        await admin.close()


@pytest.fixture
async def app_pool(monkeypatch):
    pool = await asyncpg.create_pool(
        _dsn(), min_size=1, max_size=2, statement_cache_size=0, init=_as_app_role,
    )
    # Both handlers do `from app.main import app` for the pool. Importing the
    # real app.main needs Hatchet credentials, so stand in a module that
    # carries only app.state.pg_pool.
    fake_app = SimpleNamespace(state=SimpleNamespace(pg_pool=pool))
    monkeypatch.setitem(sys.modules, "app.main", SimpleNamespace(app=fake_app))
    try:
        yield pool
    finally:
        await pool.close()


def _request(ws: str, source: str, verdict: str) -> FeedbackRequest:
    return FeedbackRequest(
        workspace_id=ws,
        answer_run_id=uuid.uuid4(),
        citation_item_id=uuid.uuid4(),
        source_document_id=source,
        verdict=verdict,
        submitted_by_user_id=None,
    )


async def test_feedback_aggregates_on_one_anchor_row(workspace_id, app_pool) -> None:
    source = str(uuid.uuid4())

    first = await post_feedback(_request(workspace_id, source, "right"))
    second = await post_feedback(_request(workspace_id, source, "wrong"))
    third = await post_feedback(_request(workspace_id, source, "partial"))

    assert [first.cumulative_feedback_for_source,
            second.cumulative_feedback_for_source,
            third.cumulative_feedback_for_source] == [1, 2, 3]
    assert first.feature_id == second.feature_id == third.feature_id

    admin = await asyncpg.connect(_dsn(), statement_cache_size=0)
    try:
        scores = await admin.fetch(
            "SELECT model_version, trust_score::float AS trust_score "
            "FROM silver.source_trust_scores WHERE workspace_id = $1::uuid",
            workspace_id,
        )
        feature = await admin.fetchrow(
            "SELECT feature_name, feature_value::float AS feature_value, "
            "workspace_id::text AS workspace_id, payload "
            "FROM silver.source_trust_features WHERE feature_id = $1::uuid",
            first.feature_id,
        )
    finally:
        await admin.close()

    assert [(r["model_version"], r["trust_score"]) for r in scores] == [
        (FEEDBACK_ANCHOR_MODEL_VERSION, 0.5),
    ]
    assert feature["feature_name"] == "citation_accuracy"
    assert feature["workspace_id"] == workspace_id
    # (1 right + 0.5 x 1 partial) / 3 events
    assert feature["feature_value"] == pytest.approx(0.5)
    payload = json.loads(feature["payload"])
    assert (payload["right"], payload["wrong"], payload["partial"], payload["event_count"]) == (1, 1, 1, 3)
    assert payload["last_event"]["verdict"] == "partial"


async def test_feedback_events_are_counted_and_boost_ignores_anchor(workspace_id, app_pool) -> None:
    source = str(uuid.uuid4())
    await post_feedback(_request(workspace_id, source, "wrong"))
    await post_feedback(_request(workspace_id, source, "wrong"))

    async with app_pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.workspace_id', $1, true)", workspace_id)
        # Feedback is aggregated on the source's anchor row as one
        # citation_accuracy feature (routers/citation_feedback.py).
        event_count = await conn.fetchval(
            """
            SELECT COALESCE(sum((f.payload->>'event_count')::int), 0)
              FROM silver.source_trust_features f
              JOIN silver.source_trust_scores a ON a.trust_score_id = f.trust_score_id
             WHERE a.workspace_id = $1::uuid
               AND a.source_document_id::text = $2
               AND a.model_version = $3
               AND f.feature_name = 'citation_accuracy'
            """,
            workspace_id, source, FEEDBACK_ANCHOR_MODEL_VERSION,
        )
        assert event_count == 2
        trust = await _trust_lookup(conn, {source}, fallback_trust=0.73)
    # The anchor's placeholder 0.5 must not be read as a score.
    assert trust == {source: 0.73}
