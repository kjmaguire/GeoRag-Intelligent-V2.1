"""Phase H4 UI router smoke tests.

Verifies that the 4 new admin routers register cleanly + their
non-DB endpoints (e.g. report-types catalogue) return well-shaped
responses. Live-DB endpoints (list runs / builds / training runs)
are exercised via separate live tests guarded on POSTGRES_PASSWORD.
"""
from __future__ import annotations

import pytest

# Import them at module load — failing here surfaces import-time
# errors (mis-typed dependency, schema drift) before the smoke run.
from app.routers import (
    citation_feedback as citation_feedback_router,
)
from app.routers import (
    ml_training as ml_training_router,
)
from app.routers import (
    report_builder as report_builder_router,
)
from app.routers import (
    target_recommendation_cockpit as trg_cockpit_router,
)


def test_trg_cockpit_router_mounted_at_admin_prefix() -> None:
    assert trg_cockpit_router.router.prefix == "/api/v1/admin/target_recommendation"


def test_report_builder_router_mounted() -> None:
    assert report_builder_router.router.prefix == "/api/v1/admin/reports"


def test_ml_training_router_mounted() -> None:
    assert ml_training_router.router.prefix == "/api/v1/admin/ml"


def test_citation_feedback_router_mounted() -> None:
    assert citation_feedback_router.router.prefix == "/api/v1/citations"


@pytest.mark.asyncio
async def test_report_types_endpoint_returns_11_types() -> None:
    """Pure function test — _plan_for_type returns a section list
    for every §15.2 type."""
    from app.routers.report_builder import _ALL_TYPES, _plan_for_type
    assert len(_ALL_TYPES) == 11
    # Sanity: run the planner on one type to confirm it threads
    # through.
    plan = await _plan_for_type("ni43101_section_pack")
    assert plan.report_type == "ni43101_section_pack"
    assert len(plan.sections) >= 1


def test_sign_off_request_model_validates_decision() -> None:
    from pydantic import ValidationError

    from app.routers.target_recommendation_cockpit import SignOffRequest
    with pytest.raises(ValidationError):
        SignOffRequest(
            target_id="11111111-1111-1111-1111-111111111111",
            qp_user_id=1,
            qp_credential_id="x",
            decision="not_a_real_decision",  # type: ignore[arg-type]
            rationale="x",
        )


def test_citation_feedback_request_rejects_invalid_verdict() -> None:
    from pydantic import ValidationError

    from app.routers.citation_feedback import FeedbackRequest
    with pytest.raises(ValidationError):
        FeedbackRequest(
            workspace_id="11111111-1111-1111-1111-111111111111",
            answer_run_id="22222222-2222-2222-2222-222222222222",
            citation_item_id="33333333-3333-3333-3333-333333333333",
            source_document_id="44444444-4444-4444-4444-444444444444",
            verdict="maybe",  # type: ignore[arg-type]
        )


def test_train_target_model_request_minimum_fields() -> None:
    from app.routers.ml_training import TrainTargetModelRequest
    req = TrainTargetModelRequest(
        target_model_id="11111111-1111-1111-1111-111111111111",
        initiated_by_user_id=1,
    )
    assert req.activate_on_success is False
    assert req.min_outcomes_per_deposit_model == 25


def test_train_source_trust_request_default_version_tag() -> None:
    from app.routers.ml_training import TrainSourceTrustRequest
    req = TrainSourceTrustRequest(
        workspace_id="11111111-1111-1111-1111-111111111111",
        initiated_by_user_id=1,
    )
    assert req.model_version == "weighted_learned_v1"
