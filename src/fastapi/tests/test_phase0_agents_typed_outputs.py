"""Regression tests for the typed-output rollout on the 10 Phase 0
Hatchet agent workflows (§B.7.1 untyped-gap closure, 2026-05-29).

Each workflow's task function previously returned ``dict``; it now
returns a typed Pydantic v2 BaseModel. These tests pin:

  1. The output model exists and is a Pydantic BaseModel.
  2. The task function's declared return annotation matches.
  3. ``model_validate({})`` succeeds (no required-field landmine that
     would crash on the rare empty/None agent return path).
  4. ``model_dump()`` round-trips back to a dict with the same core
     keys — preserving downstream backward compatibility for any
     caller that still wants dict semantics.
  5. ``extra="allow"`` lets agent-specific conditional keys through
     (e.g. ``kestra_error``, ``fatal``) without a validation error.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest
from pydantic import BaseModel

from app.hatchet_workflows import phase0_agents as p0


# (workflow_attr, task_callable_attr, output_model_cls, sample_extra_kw)
# The sample_extra_kw verifies extra="allow" on each model — these are
# real conditional keys the agents emit on specific code paths.
WORKFLOW_TYPING = [
    (
        "tenant_isolation_audit",
        "_run_tenant_isolation",
        p0.TenantIsolationAuditOutput,
        {"kestra_error": "kestra http 503"},
    ),
    (
        "lineage_walk",
        "_run_lineage_walk",
        p0.LineageWalkOutput,
        {"requested_target_type": "report"},
    ),
    (
        "storage_tiering_run",
        "_run_storage_tiering",
        p0.StorageTieringRunOutput,
        {"fatal": "aioboto3 not installed: ..."},
    ),
    (
        "index_health_check",
        "_run_index_health",
        p0.IndexHealthCheckOutput,
        {"some_future_metric": 42},
    ),
    (
        "store_reconciliation_run",
        "_run_store_recon",
        p0.StoreReconciliationRunOutput,
        {"propagation_lag_seconds": 12.3},
    ),
    (
        "model_upgrade_watch_run",
        "_run_model_upgrade_watch",
        p0.ModelUpgradeWatchRunOutput,
        {"checked_at": "2026-05-29T00:00:00Z"},
    ),
    (
        "vllm_security_check_run",
        "_run_vllm_security",
        p0.VllmSecurityCheckRunOutput,
        {"http_status": 502, "note": "VLLM_VERSION env unset"},
    ),
    (
        "model_cost_summary_run",
        "_run_model_cost_summary",
        p0.ModelCostSummaryRunOutput,
        {"oldest_unrolled_day": "2026-05-27"},
    ),
    (
        "llm_incident_diagnosis_run",
        "_run_llm_incident",
        p0.LlmIncidentDiagnosisRunOutput,
        {"latency_ms": 1900},
    ),
    (
        "support_packet_assemble",
        "_run_support_packet",
        p0.SupportPacketAssembleOutput,
        {"checksum_sha256": "deadbeef"},
    ),
]


@pytest.mark.parametrize(
    "workflow_attr,task_attr,model_cls,extra_kw",
    WORKFLOW_TYPING,
    ids=[t[0] for t in WORKFLOW_TYPING],
)
def test_phase0_workflow_output_is_typed(
    workflow_attr: str,
    task_attr: str,
    model_cls: type[BaseModel],
    extra_kw: dict[str, Any],
) -> None:
    # 1. Model exists & is a BaseModel subclass.
    assert issubclass(model_cls, BaseModel), (
        f"{model_cls.__name__} must subclass pydantic.BaseModel"
    )

    # 2. The Hatchet Task wrapper resolved the typed annotation into
    # an output_validator_type matching the model. (Hatchet decorates
    # the task body into a Task object, so we can't call
    # inspect.signature directly — we read the validator instead.)
    task_obj = getattr(p0, task_attr)
    assert task_obj.output_validator_type is model_cls, (
        f"{task_attr}.output_validator_type is "
        f"{task_obj.output_validator_type!r} but expected {model_cls!r}"
    )

    # 3. Empty-payload validation works (the r.value or {} fallback).
    empty = model_cls.model_validate({})
    assert isinstance(empty, model_cls)

    # 4. model_dump round-trip preserves declared (core) keys for
    # downstream callers that still want dict semantics.
    dumped = empty.model_dump()
    assert isinstance(dumped, dict)
    declared = set(model_cls.model_fields.keys())
    assert declared.issubset(dumped.keys()), (
        f"{model_cls.__name__}.model_dump() dropped declared keys: "
        f"{declared - set(dumped.keys())}"
    )

    # 5. extra="allow" — agent-specific conditional keys round-trip.
    with_extras = model_cls.model_validate({**dumped, **extra_kw})
    dumped_extras = with_extras.model_dump()
    for k, v in extra_kw.items():
        assert dumped_extras.get(k) == v, (
            f"{model_cls.__name__} lost extra key {k!r}={v!r} on round-trip"
        )


def test_phase0_typed_output_models_exported() -> None:
    """All 10 typed outputs are listed in ``phase0_agents.__all__``."""
    expected = {model.__name__ for _, _, model, _ in WORKFLOW_TYPING}
    missing = expected - set(p0.__all__)
    assert not missing, f"missing from __all__: {missing}"


def test_phase0_no_more_dict_return_annotations() -> None:
    """Belt-and-braces: none of the 10 task wrappers fell back to a
    ``dict`` validator. Guards against regression if a future hand-edit
    accidentally reverts one — the Task object's output_validator_type
    must be a BaseModel subclass, not dict / None."""
    for workflow_attr, task_attr, _, _ in WORKFLOW_TYPING:
        task_obj = getattr(p0, task_attr)
        vt = task_obj.output_validator_type
        assert vt is not None, (
            f"{task_attr} ({workflow_attr}) has no output_validator_type "
            "— missing typed return annotation?"
        )
        assert vt is not dict, (
            f"{task_attr} ({workflow_attr}) regressed to bare dict return"
        )
        assert inspect.isclass(vt) and issubclass(vt, BaseModel), (
            f"{task_attr} ({workflow_attr}) output_validator_type "
            f"{vt!r} is not a BaseModel subclass"
        )
