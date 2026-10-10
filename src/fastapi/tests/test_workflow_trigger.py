"""POST /internal/v1/workflows/{workflow}/trigger (HAT-13, 2026-09-29).

Eight registered workflows used to be reachable only from the Hatchet UI.
This locks the route that replaced that: the service-key gate, input
validation against each workflow's own model, the rule that every resource
the input names lives inside the workspace Laravel authorised, and the
dispatch itself. Hatchet and Postgres are both faked. The scope check is
asserted by the SQL arguments it receives, and nothing may be dispatched
when a check refuses.
"""
from __future__ import annotations

import asyncio
import re
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.config import settings
from app.routers import workflow_trigger as T

_WS = "a0000000-0000-0000-0000-000000000001"
_OTHER_WS = "a0000000-0000-0000-0000-000000000002"
_PROJECT = "b1000000-0000-0000-0000-000000000010"
_TICKET = "c1000000-0000-0000-0000-000000000020"
_AUDIT = "d1000000-0000-0000-0000-000000000030"
_UUID = "e1000000-0000-0000-0000-000000000040"

REPO = Path(__file__).resolve().parents[3]
POLICY = REPO / "app" / "Policies" / "WorkflowTriggerPolicy.php"


class _FakeConn:
    def __init__(self, exists: bool) -> None:
        self.exists = exists
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchval(self, sql: str, *args: Any) -> bool:
        self.calls.append((sql, args))
        return self.exists


@pytest.fixture
def harness(monkeypatch):
    """(client, dispatched, db) with every registered workflow faked."""
    dispatched: list[tuple[str, Any]] = []
    db: dict[str, Any] = {"exists": True, "scopes": [], "conn": None}

    class _Ref:
        def __init__(self, n: int) -> None:
            # The first run keeps the historical id; later ones are distinct, so
            # a test can tell a repeated dispatch from a repeated answer.
            self.workflow_run_id = f"run-{122 + n}"

    for name, spec in T.TRIGGERS.items():
        async def _run_no_wait(inp: Any, _name: str = name) -> _Ref:
            dispatched.append((_name, inp))
            return _Ref(len(dispatched))

        monkeypatch.setattr(spec.workflow, "aio_run_no_wait", _run_no_wait)

    @asynccontextmanager
    async def _scoped(pool: Any, *, workspace_id: str, site: str):
        db["scopes"].append(workspace_id)
        db["conn"] = _FakeConn(db["exists"])
        yield db["conn"]

    monkeypatch.setattr(T, "scoped_connection", _scoped)

    app = FastAPI()
    app.state.pg_pool = object()
    app.include_router(T.router)
    return TestClient(app), dispatched, db


def _post(client: TestClient, workflow: str, body: dict[str, Any], key: str | None = None):
    headers = {"X-Service-Key": key if key is not None else settings.FASTAPI_SERVICE_KEY}
    return client.post(f"/internal/v1/workflows/{workflow}/trigger", json=body, headers=headers)


def _report_input(**over: Any) -> dict[str, Any]:
    return {
        "workspace_id": _WS, "project_id": _PROJECT,
        "report_type": "ingestion_quality", "requested_by_user_id": 7,
        "export_request_id": _UUID, **over,
    }


# ---------------------------------------------------------------------------
# The gate and the registry
# ---------------------------------------------------------------------------
def test_requires_the_service_key(harness) -> None:
    client, dispatched, _ = harness
    r = _post(client, "generate_report", {"workspace_id": _WS, "input": _report_input()}, key="wrong")
    assert r.status_code == 401
    assert dispatched == []


def test_an_unregistered_workflow_is_404(harness) -> None:
    client, dispatched, _ = harness
    for name in ("ingest_pdf", "field_outcome_learning", "nl_summaries", "nope"):
        assert _post(client, name, {"workspace_id": _WS, "input": {}}).status_code == 404
    assert dispatched == []


def test_the_registry_is_exactly_the_hat13_set() -> None:
    assert set(T.TRIGGERS) == {
        "generate_report", "score_targets", "workspace_export", "restore_workspace",
        "support_replay", "lineage_walk", "support_packet_assemble",
        "llm_incident_diagnosis_run",
    }


def test_laravel_policy_names_the_same_workflows() -> None:
    """The Laravel allow-list and this registry must not drift apart."""
    text = POLICY.read_text(encoding="utf-8")
    named = set(re.findall(r"'([a-z_]+)'", " ".join(
        re.findall(r"public const [A-Z_]+_WORKFLOWS = \[(.*?)\];", text, re.S),
    )))
    assert named == set(T.TRIGGERS)


def test_a_workspace_scoped_workflow_without_a_workspace_is_422(harness) -> None:
    client, dispatched, db = harness
    r = _post(client, "workspace_export", {"input": {"workspace_id": _WS}})
    assert r.status_code == 422
    assert dispatched == [] and db["scopes"] == []


def test_invalid_input_is_422_with_serialisable_errors(harness) -> None:
    client, dispatched, _ = harness
    r = _post(client, "generate_report", {
        "workspace_id": _WS, "input": _report_input(report_type="not_a_type"),
    })
    assert r.status_code == 422
    assert r.json()["detail"]["workflow"] == "generate_report"
    assert dispatched == []


# ---------------------------------------------------------------------------
# Project-scoped: generate_report, score_targets
# ---------------------------------------------------------------------------
def test_generate_report_checks_the_project_then_dispatches(harness) -> None:
    client, dispatched, db = harness
    r = _post(client, "generate_report", {
        "workspace_id": _WS, "requested_by": "web:a@b", "input": _report_input(),
    })
    assert r.status_code == 202, r.text
    assert r.json() == {"workflow": "generate_report", "workflow_run_id": "run-123", "workspace_id": _WS}
    assert db["scopes"] == [_WS]
    sql, args = db["conn"].calls[0]
    assert "silver.projects" in sql and args == (_PROJECT, _WS)
    name, inp = dispatched[0]
    assert name == "generate_report" and str(inp.project_id) == _PROJECT


def test_a_project_in_another_workspace_is_404_and_not_dispatched(harness) -> None:
    client, dispatched, db = harness
    db["exists"] = False
    r = _post(client, "score_targets", {"workspace_id": _WS, "input": {
        "workspace_id": _WS, "project_id": _PROJECT, "requested_by_user_id": 7,
        "aoi_geom_wkt": "POLYGON((0 0,1 0,1 1,0 1,0 0))", "score_request_id": _UUID,
        "extra_candidate_zone_wkts": ["POLYGON((0 0,1 0,1 1,0 1,0 0))"],
    }})
    assert r.status_code == 404
    assert dispatched == []


def test_input_workspace_must_match_the_authorised_one(harness) -> None:
    client, dispatched, db = harness
    r = _post(client, "generate_report", {
        "workspace_id": _WS, "input": _report_input(workspace_id=_OTHER_WS),
    })
    assert r.status_code == 422
    assert dispatched == [] and db["scopes"] == []


# ---------------------------------------------------------------------------
# workspace_export / restore_workspace
# ---------------------------------------------------------------------------
def test_workspace_export_dispatches_for_an_existing_workspace(harness) -> None:
    client, dispatched, db = harness
    r = _post(client, "workspace_export", {"workspace_id": _WS, "input": {"workspace_id": _WS}})
    assert r.status_code == 202, r.text
    assert "silver.workspaces" in db["conn"].calls[0][0]
    assert dispatched[0][1].bucket == T.EXPORT_BUCKET


def test_workspace_export_refuses_another_bucket(harness) -> None:
    client, dispatched, _ = harness
    r = _post(client, "workspace_export", {
        "workspace_id": _WS, "input": {"workspace_id": _WS, "bucket": "reports"},
    })
    assert r.status_code == 422
    assert dispatched == []


@pytest.mark.parametrize("uri", [
    f"file:///etc/{_WS}/x.jsonl.gz",
    f"s3://workspace-exports/{_OTHER_WS}/2026-09-29T000000-r.jsonl.gz",
    f"s3://reports/{_WS}/x.jsonl.gz",
    f"s3://workspace-exports/{_WS}/../{_OTHER_WS}/x.jsonl.gz",
    f"s3://workspace-exports/{_WS}/",
])
def test_restore_refuses_a_manifest_outside_the_workspace(harness, uri: str) -> None:
    client, dispatched, _ = harness
    r = _post(client, "restore_workspace", {"workspace_id": _WS, "input": {
        "workspace_id": _WS, "snapshot_manifest_uri": uri,
        "initiated_by_user_id": 7, "restore_request_id": _UUID,
    }})
    assert r.status_code == 422, uri
    assert dispatched == []


def test_restore_dispatches_an_own_workspace_manifest(harness) -> None:
    client, dispatched, _ = harness
    uri = f"s3://workspace-exports/{_WS}/2026-09-29T000000-r.jsonl.gz"
    r = _post(client, "restore_workspace", {"workspace_id": _WS, "input": {
        "workspace_id": _WS, "snapshot_manifest_uri": uri,
        "initiated_by_user_id": 7, "restore_request_id": _UUID, "dry_run": False,
    }})
    assert r.status_code == 202, r.text
    assert dispatched[0][1].dry_run is False
    assert dispatched[0][1].snapshot_manifest_uri == uri


# ---------------------------------------------------------------------------
# support_replay
# ---------------------------------------------------------------------------
def _replay(**over: Any) -> dict[str, Any]:
    return {
        "ticket_id": _TICKET, "original_workflow_run_id": "wf-1",
        "initiated_by_user_id": 7, "replay_request_id": _UUID, **over,
    }


def test_support_replay_is_dry_run_only(harness) -> None:
    client, dispatched, _ = harness
    r = _post(client, "support_replay", {"workspace_id": _WS, "input": _replay(dry_run=False)})
    assert r.status_code == 422
    assert dispatched == []


def test_support_replay_checks_the_ticket_is_in_the_workspace(harness) -> None:
    client, dispatched, db = harness
    db["exists"] = False
    r = _post(client, "support_replay", {"workspace_id": _WS, "input": _replay()})
    assert r.status_code == 404
    sql, args = db["conn"].calls[0]
    assert "ops.support_tickets" in sql and args == (_TICKET, _WS)
    assert dispatched == []

    db["exists"] = True
    assert _post(client, "support_replay", {"workspace_id": _WS, "input": _replay()}).status_code == 202
    assert dispatched[0][1].dry_run is True


def test_support_replay_is_handed_the_workspace_it_was_authorised_for(harness) -> None:
    """The route checks the ticket belongs to the workspace. The worker must not
    rediscover that workspace under the default tenant: ops.support_* is STRICT
    RLS, so as georag_app it could see only a default-tenant ticket and a replay
    for any other workspace died after this route had accepted it (finding 22)."""
    client, dispatched, _ = harness
    r = _post(client, "support_replay", {"workspace_id": _OTHER_WS, "input": _replay()})
    assert r.status_code == 202
    assert str(dispatched[0][1].workspace_id) == _OTHER_WS


def test_support_replay_input_naming_another_workspace_is_refused(harness) -> None:
    client, dispatched, db = harness
    r = _post(client, "support_replay", {
        "workspace_id": _WS, "input": _replay(workspace_id=_OTHER_WS),
    })
    assert r.status_code == 422
    assert dispatched == [] and db["scopes"] == []

    # Naming the authorised workspace itself is fine.
    ok = _post(client, "support_replay", {"workspace_id": _WS, "input": _replay(workspace_id=_WS)})
    assert ok.status_code == 202
    assert str(dispatched[0][1].workspace_id) == _WS


# ---------------------------------------------------------------------------
# Phase 0 agents
# ---------------------------------------------------------------------------
def test_lineage_walk_of_another_workspace_is_refused(harness) -> None:
    client, dispatched, _ = harness
    r = _post(client, "lineage_walk", {"workspace_id": _WS, "input": {
        "workspace_id": _WS, "kwargs": {"target_type": "workspace", "target_id": _OTHER_WS},
    }})
    assert r.status_code == 422
    assert dispatched == []


def test_lineage_walk_rejects_unknown_kwargs(harness) -> None:
    client, dispatched, _ = harness
    r = _post(client, "lineage_walk", {"workspace_id": _WS, "input": {
        "workspace_id": _WS,
        "kwargs": {"target_type": "workspace", "target_id": _WS, "pg_pool": "x"},
    }})
    assert r.status_code == 422
    assert dispatched == []


def test_lineage_walk_scopes_an_audit_entry(harness) -> None:
    client, dispatched, db = harness
    r = _post(client, "lineage_walk", {"workspace_id": _WS, "input": {
        "workspace_id": _WS, "actor_id": 7,
        "kwargs": {"target_type": "audit_ledger_entry", "target_id": _AUDIT, "limit": 50},
    }})
    assert r.status_code == 202, r.text
    sql, args = db["conn"].calls[0]
    assert "audit.audit_ledger" in sql and args == (_AUDIT, _WS)
    assert dispatched[0][1].kwargs == {
        "target_type": "audit_ledger_entry", "target_id": _AUDIT, "limit": 50,
    }

    r = _post(client, "lineage_walk", {"workspace_id": _WS, "input": {
        "workspace_id": _WS,
        "kwargs": {"target_type": "audit_ledger_entry", "target_id": "not-a-uuid"},
    }})
    assert r.status_code == 422


def test_lineage_walk_scopes_a_workflow_run(harness) -> None:
    client, dispatched, db = harness
    db["exists"] = False
    r = _post(client, "lineage_walk", {"workspace_id": _WS, "input": {
        "workspace_id": _WS, "kwargs": {"target_type": "workflow_run", "target_id": "wf-9"},
    }})
    assert r.status_code == 404
    assert "workflow.workflow_runs" in db["conn"].calls[0][0]
    assert dispatched == []


def test_support_packet_dispatches_validated_kwargs(harness) -> None:
    client, dispatched, _ = harness
    r = _post(client, "support_packet_assemble", {"workspace_id": _WS, "input": {
        "workspace_id": _WS, "actor_id": 7,
        "kwargs": {"incident_id": "INC-1", "requested_by": 7},
    }})
    assert r.status_code == 202, r.text
    assert dispatched[0][1].kwargs == {"incident_id": "INC-1", "requested_by": 7}


def test_incident_diagnosis_is_platform_wide_only(harness) -> None:
    client, dispatched, db = harness
    r = _post(client, "llm_incident_diagnosis_run", {"workspace_id": _WS, "input": {
        "workspace_id": _WS, "kwargs": {"alert_label": "HighErrorRate"},
    }})
    assert r.status_code == 422
    assert dispatched == []

    r = _post(client, "llm_incident_diagnosis_run", {"input": {
        "actor_id": 7, "kwargs": {"alert_label": "HighErrorRate", "window_minutes": 30},
    }})
    assert r.status_code == 202, r.text
    assert r.json()["workspace_id"] is None
    assert db["scopes"] == []
    assert dispatched[0][1].kwargs == {"alert_label": "HighErrorRate", "window_minutes": 30}


def test_incident_diagnosis_bounds_the_window(harness) -> None:
    client, dispatched, _ = harness
    r = _post(client, "llm_incident_diagnosis_run", {"input": {
        "kwargs": {"alert_label": "x", "window_minutes": 100_000},
    }})
    assert r.status_code == 422
    assert dispatched == []


# ---------------------------------------------------------------------------
# Request-id dedupe (2026-10 Hatchet audit, finding 8)
# ---------------------------------------------------------------------------
class _FakeRedis:
    """The three calls the route makes, with SET NX and TTL semantics."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int | None] = {}
        self.down = False

    async def set(self, key: str, value: str, ex: int | None = None, nx: bool = False) -> bool | None:
        if self.down:
            raise ConnectionError("redis is down")
        if nx and key in self.store:
            return None
        self.store[key], self.ttls[key] = value, ex
        return True

    async def get(self, key: str) -> str | None:
        if self.down:
            raise ConnectionError("redis is down")
        return self.store.get(key)


@pytest.fixture
def deduping(harness):
    """(client, dispatched, db, redis): the harness with a fake Redis attached."""
    client, dispatched, db = harness
    redis = _FakeRedis()
    client.app.state.redis_client = redis
    return client, dispatched, db, redis


def _report(request_id: str = _UUID, **over: Any) -> dict[str, Any]:
    return {"workspace_id": _WS, "input": _report_input(export_request_id=request_id, **over)}


def test_a_repeated_request_id_returns_the_first_run_and_dispatches_once(deduping) -> None:
    client, dispatched, _, redis = deduping

    first = _post(client, "generate_report", _report())
    second = _post(client, "generate_report", _report())

    assert first.status_code == second.status_code == 202
    assert first.json()["workflow_run_id"] == "run-123"
    assert second.json() == first.json(), "the repeat gets the run the first request started"
    assert len(dispatched) == 1
    (key,) = redis.store
    assert key == f"workflow_trigger:generate_report:{_WS}:{_UUID}"
    assert redis.store[key] == "run-123" and redis.ttls[key] == T.DEDUPE_TTL_SECONDS == 3600


def test_a_new_request_id_is_a_new_run(deduping) -> None:
    client, dispatched, _, _ = deduping

    a = _post(client, "generate_report", _report(_UUID))
    b = _post(client, "generate_report", _report("e1000000-0000-0000-0000-000000000041"))

    assert len(dispatched) == 2
    assert a.json()["workflow_run_id"] != b.json()["workflow_run_id"]


def test_the_same_request_id_in_another_workspace_is_another_request(deduping) -> None:
    """The workspace is part of the key, so one tenant's request id can never
    be answered with a run reference that belongs to another."""
    client, dispatched, _, _ = deduping

    _post(client, "generate_report", _report())
    other = _post(client, "generate_report", {
        "workspace_id": _OTHER_WS, "input": _report_input(workspace_id=_OTHER_WS),
    })

    assert other.status_code == 202
    assert len(dispatched) == 2


def test_each_deduped_workflow_names_a_real_input_field() -> None:
    for workflow, field in T.REQUEST_ID_FIELDS.items():
        assert field in T.TRIGGERS[workflow].input_model.model_fields, workflow


def test_support_replay_and_restore_are_deduped_too(deduping) -> None:
    client, dispatched, _, _ = deduping
    uri = f"s3://workspace-exports/{_WS}/2026-09-29T000000-r.jsonl.gz"
    restore = {"workspace_id": _WS, "input": {
        "workspace_id": _WS, "snapshot_manifest_uri": uri,
        "initiated_by_user_id": 7, "restore_request_id": _UUID,
    }}

    for workflow, body in (
        ("support_replay", {"workspace_id": _WS, "input": _replay()}),
        ("restore_workspace", restore),
    ):
        before = len(dispatched)
        first = _post(client, workflow, body)
        second = _post(client, workflow, body)
        assert second.json() == first.json(), workflow
        assert len(dispatched) == before + 1, workflow


def test_a_workflow_with_no_request_id_is_never_deduped(deduping) -> None:
    client, dispatched, _, redis = deduping
    body = {"workspace_id": _WS, "input": {"workspace_id": _WS}}

    _post(client, "workspace_export", body)
    _post(client, "workspace_export", body)

    assert len(dispatched) == 2
    assert redis.store == {}


def test_a_request_the_scope_check_refused_does_not_claim_its_id(deduping) -> None:
    client, dispatched, db, redis = deduping
    db["exists"] = False
    assert _post(client, "generate_report", _report()).status_code == 404
    assert redis.store == {}

    db["exists"] = True
    assert _post(client, "generate_report", _report()).status_code == 202
    assert len(dispatched) == 1


def test_a_request_id_still_being_dispatched_is_409_not_a_second_run(deduping) -> None:
    client, dispatched, _, redis = deduping
    redis.store[f"workflow_trigger:generate_report:{_WS}:{_UUID}"] = T._IN_PROGRESS

    r = _post(client, "generate_report", _report())

    assert r.status_code == 409
    assert dispatched == []


def test_a_dispatch_that_raises_keeps_the_id_claimed(deduping, monkeypatch) -> None:
    """Whether the engine started the run is unknown after an error (a deadline
    can expire after it did). Releasing the id would let a retry start a second
    run, the one outcome the claim exists to prevent."""
    client, dispatched, _, redis = deduping

    async def _boom(_inp: Any) -> None:
        raise RuntimeError("engine deadline exceeded")

    monkeypatch.setattr(T.TRIGGERS["generate_report"].workflow, "aio_run_no_wait", _boom)
    with pytest.raises(RuntimeError, match="deadline"):
        _post(client, "generate_report", _report())
    assert list(redis.store.values()) == [T._IN_PROGRESS]

    r = _post(client, "generate_report", _report())
    assert r.status_code == 409
    assert dispatched == []


def test_redis_down_still_dispatches(deduping) -> None:
    """The dedupe is a safety net under the Laravel cooldown, not a gate."""
    client, dispatched, _, redis = deduping
    redis.down = True

    r = _post(client, "generate_report", _report())

    assert r.status_code == 202
    assert len(dispatched) == 1


def test_no_redis_client_at_all_still_dispatches(harness) -> None:
    client, dispatched, _ = harness  # the plain harness has no redis_client

    assert _post(client, "generate_report", _report()).status_code == 202
    assert len(dispatched) == 1


async def test_of_two_concurrent_claims_exactly_one_wins() -> None:
    redis = _FakeRedis()
    validated = SimpleNamespace(export_request_id=UUID(_UUID))

    results = await asyncio.gather(
        T._claim_request_id(redis, "generate_report", _WS, validated),
        T._claim_request_id(redis, "generate_report", _WS, validated),
        return_exceptions=True,
    )

    owners = [r for r in results if isinstance(r, str)]
    refused = [r for r in results if isinstance(r, HTTPException)]
    assert len(owners) == 1 and len(refused) == 1 and refused[0].status_code == 409
