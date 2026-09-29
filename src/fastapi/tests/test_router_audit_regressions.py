"""Unit regressions for the 2026-09-29 FastAPI audit (API-1, 3, 4, 8, 10, 13).

These replace the coverage ``tests/test_demo_ready_surfaces.py`` was meant to
give: that file is ``integration``-marked, hits a live FastAPI on
localhost:8000, and is not in ``integration_ci_manifest.txt`` — so it ran
nowhere, and every bug below shipped behind it. Everything here runs in the
ordinary unit job: no Postgres, no network, no Settings credentials (auth
dependencies are overridden and workspace resolution is stubbed).
"""

from __future__ import annotations

import datetime
import io
import zipfile
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.services.auth import UserContext, extract_user_context, verify_service_key
from tests._stub_pool import FailingPool, StubConn, StubPool

WS = UUID("a0000000-0000-0000-0000-000000000001")
PROJECT = UUID("019d74a1-fba8-7165-9ae6-a5bf93eef97d")


def _app(router: Any, pool: Any, user: UserContext | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.state.pg_pool = pool
    app.state.redis_client = None
    app.dependency_overrides[verify_service_key] = lambda: None
    app.dependency_overrides[extract_user_context] = lambda: (
        user or UserContext(user_id="1", workspace_id=str(WS))
    )
    return app


async def _resolve_ws(*_a: Any, **_k: Any) -> UUID:
    return WS


# ---------------------------------------------------------------------------
# API-1 — trust summary
# ---------------------------------------------------------------------------


def _trust_conn(answer_run_id: UUID) -> StubConn:
    return StubConn(
        fetchrow=[
            ("SELECT workspace_id FROM silver.answer_runs", {"workspace_id": WS}),
            (
                "citation_lifecycle_state",
                {
                    "answer_run_id": str(answer_run_id),
                    "query_text": "What is the grade at PLS-22-001?",
                    "query_class": "factual",
                    "model_name": "command-a-plus-05-2026",
                    "citation_lifecycle_state": "resolved",
                    "citation_mode": "posthoc_span_resolution",
                    "partial_resolution_rate": 1.0,
                    "rejection_reason": None,
                    "created_at": datetime.datetime(2026, 9, 29, tzinfo=datetime.UTC),
                    "evidence_truncated_count": 0,
                    "workspace_data_version_at_query": 3,
                },
            ),
            (
                "silver.claim_ledger",
                {"total": 2, "verified": 2, "failed": 0, "pending": 0, "insufficient": 0},
            ),
        ],
        fetch=[
            (
                "GROUP BY source_store, state",
                [
                    {"source_store": "qdrant", "state": "accepted", "n": 3},
                    {"source_store": "qdrant", "state": "rejected", "n": 1},
                ],
            ),
            ("silver.answer_retrieval_items", []),
            ("LIMIT 25", []),
            (
                "silver.message_feedback",
                [
                    {
                        "polarity": "down",
                        "category": "wrong_facts",
                        "note": "grade is off",
                        "created_at": "2026-09-29T00:00:00Z",
                    }
                ],
            ),
        ],
    )


def test_trust_summary_does_not_500_on_cited_answers(monkeypatch) -> None:
    """Every cited answer used to 500: the handler read ``lifecycle_state``
    off rows whose query only selects ``source_store, state, n``."""
    import app.services.workspace_resolution as wr
    from app.routers.answer_runs import router

    monkeypatch.setattr(wr, "resolve_workspace_id", _resolve_ws)
    run_id = uuid4()
    conn = _trust_conn(run_id)
    client = TestClient(_app(router, StubPool(conn)))

    resp = client.get(f"/v1/answer_runs/{run_id}/trust-summary")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["citations"]["total"] == 4
    assert body["citations"]["resolved"] == 3
    assert body["citations"]["resolution_pct"] == 75.0


def test_trust_summary_reads_feedback_from_message_feedback(monkeypatch) -> None:
    """Feedback is written to silver.message_feedback; the summary used to
    read silver.answer_run_feedback (no migration creates it) behind a bare
    ``except``, so the section was always empty."""
    import app.services.workspace_resolution as wr
    from app.routers.answer_runs import router

    monkeypatch.setattr(wr, "resolve_workspace_id", _resolve_ws)
    run_id = uuid4()
    conn = _trust_conn(run_id)
    client = TestClient(_app(router, StubPool(conn)))

    body = client.get(f"/v1/answer_runs/{run_id}/trust-summary").json()

    assert body["feedback"][0]["category"] == "wrong_facts"
    assert not conn.sql_for("answer_run_feedback")
    fb_calls = [c for c in conn.calls if "silver.message_feedback" in c[1]]
    assert fb_calls and fb_calls[0][2] == (run_id, WS)


# ---------------------------------------------------------------------------
# API-3 — evidence document_passage branch
# ---------------------------------------------------------------------------


def test_evidence_passage_uses_live_columns_under_workspace_scope(monkeypatch) -> None:
    import app.routers.evidence as evidence

    monkeypatch.setattr(evidence, "resolve_workspace_id", _resolve_ws)
    evidence_id, passage_id, doc_id = uuid4(), uuid4(), uuid4()
    conn = StubConn(
        fetchrow=[
            (
                "FROM silver.evidence_items",
                {
                    "evidence_id": evidence_id,
                    "workspace_id": WS,
                    "evidence_type": "document_passage",
                    "passage_id": passage_id,
                    "structured_ref": None,
                    "graph_edge_ref": None,
                    "map_feature_ref": None,
                    "source_uri": None,
                    "source_date": None,
                    "linked_node_ids": None,
                    "created_at": None,
                },
            ),
            (
                "FROM silver.document_passages dp",
                {
                    "passage_id": passage_id,
                    "document_id": doc_id,
                    "revision_number": 1,
                    "ordinal": 5,
                    "text": "Hole PLS-22-001 intersected 12.5 m at 3.2% U3O8.",
                    "page_first": 42,
                    "source_object_key": "reports/ws/20260901_120000_ni43101.pdf",
                    "filing_date": datetime.date(2024, 3, 1),
                },
            ),
        ],
        fetch=[
            (
                "ordinal IN",
                [{"ordinal": 4, "text": "before"}, {"ordinal": 6, "text": "after"}],
            ),
        ],
    )
    client = TestClient(_app(evidence.router, StubPool(conn)))

    resp = client.get(f"/v1/evidence/{evidence_id}")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["passage_text"].startswith("Hole PLS-22-001")
    assert body["page"] == 42
    assert body["document_id"] == str(doc_id)
    assert body["context_before"] == "before"
    assert body["context_after"] == "after"
    assert body["source_date"] == "2024-03-01"
    passage_sql = "\n".join(conn.sql_for("silver.document_passages"))
    for dead in ("document_revision_id", "passage_text", "page_number", "document_revisions"):
        assert dead not in passage_sql
    # document_passages is FORCE RLS fail-closed: the GUC must be bound.
    assert conn.workspace_guc_values() == [str(WS)]


# ---------------------------------------------------------------------------
# API-4 — exports
# ---------------------------------------------------------------------------


def _export_conn(rows: list[dict[str, Any]]) -> StubConn:
    return StubConn(
        fetchval=[("FROM silver.projects", str(WS))],
        fetch=[("FROM silver.collars", rows)],
    )


@pytest.mark.parametrize("path", ["/internal/exports/shapefile", "/internal/exports/geopackage"])
def test_export_of_empty_project_is_a_404_not_a_json_file(path: str) -> None:
    """Used to be ``{"error": ...}`` with HTTP 200, which Laravel saved as the
    ``.zip`` / ``.gpkg`` the user downloaded."""
    from app.routers.exports import router

    client = TestClient(_app(router, StubPool(_export_conn([]))))

    resp = client.post(path, json={"project_id": str(PROJECT)})

    assert resp.status_code == 404
    assert resp.json()["detail"] == "No collar data found for this project"


_COLLAR = {
    "collar_id": "12345678-1234-5678-1234-567812345678",
    "hole_id": "PLS-22-001",
    "total_depth": 350.5,
    "elevation": 520.0,
    "azimuth": 225.0,
    "dip": -60.0,
    "hole_type": "Diamond",
    "status": "Completed",
    "drill_date": "2022-06-15",
    "easting": 448200.0,
    "northing": 6174300.0,
    "epsg": 32613,
    "longitude": -106.5,
    "latitude": 55.7,
}


def test_shapefile_export_carries_3d_attributes_within_dbf_limits(tmp_path) -> None:
    import pyogrio

    from app.routers.exports import router

    client = TestClient(_app(router, StubPool(_export_conn([_COLLAR]))))

    resp = client.post("/internal/exports/shapefile", json={"project_id": str(PROJECT)})

    assert resp.status_code == 200, resp.text
    zipfile.ZipFile(io.BytesIO(resp.content)).extractall(tmp_path)
    info = pyogrio.read_info(tmp_path / "georag_collars.shp")
    fields = list(info["fields"])
    for required in ("elevation", "azimuth", "dip", "tot_depth", "easting", "northing", "epsg"):
        assert required in fields
    # DBF caps names at 10 chars; GDAL used to truncate total_depth silently.
    assert "total_dept" not in fields
    assert all(len(f) <= 10 for f in fields)
    df = pyogrio.read_dataframe(tmp_path / "georag_collars.shp")
    assert df.loc[0, "tot_depth"] == pytest.approx(350.5)
    assert df.loc[0, "dip"] == pytest.approx(-60.0)


def test_geopackage_export_keeps_full_column_names(tmp_path) -> None:
    import pyogrio

    from app.routers.exports import router

    client = TestClient(_app(router, StubPool(_export_conn([_COLLAR]))))

    resp = client.post("/internal/exports/geopackage", json={"project_id": str(PROJECT)})

    assert resp.status_code == 200, resp.text
    gpkg = tmp_path / "out.gpkg"
    gpkg.write_bytes(resp.content)
    fields = list(pyogrio.read_info(gpkg, layer="collars")["fields"])
    for required in ("total_depth", "elevation", "azimuth", "dip", "easting", "northing", "epsg"):
        assert required in fields


# ---------------------------------------------------------------------------
# API-8 — chunked bodies are capped too
# ---------------------------------------------------------------------------


def _body_app(max_bytes: int) -> FastAPI:
    from app.middleware import BodySizeLimitMiddleware

    app = FastAPI()

    @app.post("/echo")
    async def echo(payload: dict) -> dict:  # type: ignore[type-arg]
        return {"keys": len(payload)}

    app.add_middleware(BodySizeLimitMiddleware, max_bytes=max_bytes)
    return app


def test_content_length_over_cap_is_413() -> None:
    client = TestClient(_body_app(64))
    resp = client.post("/echo", content=b"{" + b" " * 200 + b"}", headers={"content-type": "application/json"})
    assert resp.status_code == 413


def test_chunked_body_over_cap_is_413() -> None:
    """No Content-Length: the old middleware let this through to be
    buffered in full before auth ran."""

    def chunks():  # type: ignore[no-untyped-def]
        yield b'{"a": "'
        for _ in range(64):
            yield b"x" * 64
        yield b'"}'

    client = TestClient(_body_app(1024))
    resp = client.post("/echo", content=chunks(), headers={"content-type": "application/json"})
    assert resp.status_code == 413
    assert resp.json() == {"detail": "Request body too large"}


def test_chunked_body_under_cap_passes() -> None:
    def chunks():  # type: ignore[no-untyped-def]
        yield b'{"a": 1,'
        yield b' "b": 2}'

    client = TestClient(_body_app(1024))
    resp = client.post("/echo", content=chunks(), headers={"content-type": "application/json"})
    assert resp.status_code == 200
    assert resp.json() == {"keys": 2}


# ---------------------------------------------------------------------------
# API-10 — coverage density is tenant-scoped
# ---------------------------------------------------------------------------


def _coverage_conn() -> StubConn:
    return StubConn(
        fetch=[
            (
                "silver.coverage_density",
                [
                    {
                        "geom_json": {"type": "Polygon", "coordinates": []},
                        "record_count": 4,
                        "bias_warning": False,
                    }
                ],
            )
        ]
    )


def test_coverage_rejects_a_project_other_than_the_jwts(monkeypatch) -> None:
    import app.routers.coverage as coverage

    monkeypatch.setattr(coverage, "resolve_workspace_id", _resolve_ws)
    conn = _coverage_conn()
    user = UserContext(user_id="1", project_id=str(uuid4()), workspace_id=str(WS))
    client = TestClient(_app(coverage.router, StubPool(conn), user))

    resp = client.get("/coverage/density", params={"project_id": str(PROJECT)})

    assert resp.status_code == 403
    assert not conn.calls


def test_coverage_runs_with_the_workspace_guc_bound(monkeypatch) -> None:
    import app.routers.coverage as coverage

    monkeypatch.setattr(coverage, "resolve_workspace_id", _resolve_ws)
    conn = _coverage_conn()
    user = UserContext(user_id="1", project_id=str(PROJECT), workspace_id=str(WS))
    client = TestClient(_app(coverage.router, StubPool(conn), user))

    resp = client.get("/coverage/density", params={"project_id": str(PROJECT)})

    assert resp.status_code == 200, resp.text
    assert resp.json()["feature_count"] == 1
    assert conn.workspace_guc_values() == [str(WS)]


# ---------------------------------------------------------------------------
# API-13 — a DB outage is a 503, not an authorization failure
# ---------------------------------------------------------------------------


async def test_workspace_lookup_db_failure_is_503() -> None:
    from app.services.workspace_resolution import _lookup_workspace_for_project

    with pytest.raises(HTTPException) as exc_info:
        await _lookup_workspace_for_project(
            str(PROJECT), FailingPool(OSError("connection refused")), None
        )
    assert exc_info.value.status_code == 503


async def test_workspace_lookup_missing_project_is_still_none() -> None:
    from app.services.workspace_resolution import _lookup_workspace_for_project

    conn = StubConn(fetchrow=[("FROM silver.projects", None)])
    assert await _lookup_workspace_for_project(str(PROJECT), StubPool(conn), None) is None
