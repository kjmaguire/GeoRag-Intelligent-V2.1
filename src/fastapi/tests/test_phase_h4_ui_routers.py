"""Phase H4 UI router smoke tests.

Verifies that the remaining admin routers register cleanly + their
non-DB endpoints (e.g. ml_training request models) return well-shaped
responses. Live-DB endpoints (list runs / training runs) are exercised
via separate live tests guarded on POSTGRES_PASSWORD.

report_builder / target_recommendation_cockpit coverage removed 2026-07-28
(task #31) — both routers were deleted (zero Laravel-side callers; the
admin pages that reached them were gone since the reader-core trim).
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


def test_ml_training_router_mounted() -> None:
    assert ml_training_router.router.prefix == "/api/v1/admin/ml"


def test_citation_feedback_router_mounted() -> None:
    assert citation_feedback_router.router.prefix == "/api/v1/citations"


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
