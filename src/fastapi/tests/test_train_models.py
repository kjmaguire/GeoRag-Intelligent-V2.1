"""§12.3 + §12.7 training workflow graduations (Phase H4 final)."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import asyncpg
import pytest

from app.hatchet_workflows.train_source_trust import (
    TrainSourceTrustInput,
    _compute_trust,
    _recency_factor,
    execute as train_source_trust_execute,
)
from app.hatchet_workflows.train_target_model import (
    TrainTargetModelInput,
    _fit_linear_weights,
    execute as train_target_model_execute,
)


def _dsn() -> str:
    user = os.environ.get("POSTGRES_USER", "georag")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


def _live_db() -> bool:
    return bool(os.environ.get("POSTGRES_PASSWORD"))


# ──────────────── train_target_model._fit_linear_weights ───────────────


class _Row(dict):
    def __getitem__(self, k):
        return super().__getitem__(k)


def test_fit_linear_weights_discriminating_factor() -> None:
    rows = [
        _Row(factor_name="alteration", factor_value=0.9, hit_or_miss="hit"),
        _Row(factor_name="alteration", factor_value=0.8, hit_or_miss="hit"),
        _Row(factor_name="alteration", factor_value=0.2, hit_or_miss="miss"),
        _Row(factor_name="alteration", factor_value=0.1, hit_or_miss="miss"),
        _Row(factor_name="noise",      factor_value=0.5, hit_or_miss="hit"),
        _Row(factor_name="noise",      factor_value=0.5, hit_or_miss="miss"),
    ]
    weights, metrics = _fit_linear_weights(rows)
    # alteration is much more discriminating than noise
    assert weights["alteration"] > weights["noise"]
    assert sum(weights.values()) == pytest.approx(1.0, abs=1e-6)
    assert metrics["n_hit_outcomes"] == 3
    assert metrics["n_miss_outcomes"] == 3


def test_fit_linear_weights_no_signal_returns_uniform() -> None:
    rows = [
        _Row(factor_name="a", factor_value=0.5, hit_or_miss="hit"),
        _Row(factor_name="a", factor_value=0.5, hit_or_miss="miss"),
    ]
    weights, _ = _fit_linear_weights(rows)
    assert weights["a"] == pytest.approx(1.0)


def test_fit_linear_weights_empty_returns_uniform_default() -> None:
    weights, metrics = _fit_linear_weights([])
    assert "proximity_to_known_occurrence" in weights
    assert metrics["n_hit_outcomes"] == 0


# ──────────────── train_source_trust._compute_trust ────────────────────


def test_compute_trust_high_citation_recent_peer_reviewed() -> None:
    score, components = _compute_trust(
        citations_total=20, citations_validated=18,
        filing_date=datetime.now(timezone.utc) - timedelta(days=180),
        doctype="peer_reviewed",
    )
    assert 0.7 < score <= 1.0
    assert components["citation_rate"] == pytest.approx(0.9)


def test_compute_trust_zero_citations_neutral() -> None:
    score, _ = _compute_trust(
        citations_total=0, citations_validated=0,
        filing_date=datetime.now(timezone.utc), doctype="unknown",
    )
    assert 0.3 < score < 0.7  # neutral-ish


def test_compute_trust_old_source_decays() -> None:
    score_new, _ = _compute_trust(
        citations_total=10, citations_validated=10,
        filing_date=datetime.now(timezone.utc), doctype="ni_43_101",
    )
    score_old, _ = _compute_trust(
        citations_total=10, citations_validated=10,
        filing_date=datetime.now(timezone.utc) - timedelta(days=365 * 15),
        doctype="ni_43_101",
    )
    assert score_new > score_old


def test_recency_factor_no_date_is_neutral() -> None:
    assert _recency_factor(None) == 0.5


def test_recency_factor_just_now_is_one() -> None:
    assert _recency_factor(datetime.now(timezone.utc)) == pytest.approx(1.0, abs=0.01)


# ──────────────── train_target_model end-to-end (live DB) ──────────────


@pytest.mark.asyncio
async def test_train_target_model_emits_version_against_real_db() -> None:
    """Smoke: the workflow runs end-to-end against the live DB and
    creates a new target_model_versions row."""
    if not _live_db():
        pytest.skip("POSTGRES_PASSWORD not set")

    conn = await asyncpg.connect(_dsn(), statement_cache_size=0)
    try:
        existing_model = await conn.fetchval(
            "SELECT target_model_id::text FROM targeting.target_models LIMIT 1"
        )
    finally:
        await conn.close()
    if existing_model is None:
        pytest.skip("No target_models rows in this DB")

    inp = TrainTargetModelInput(
        target_model_id=existing_model,
        initiated_by_user_id=1,
        activate_on_success=False,
        train_request_id=uuid4(),
    )
    out = await train_target_model_execute.aio_mock_run(inp)
    assert out.success is True
    assert out.new_version_id is not None
    assert out.training_metrics["method"] == "deterministic_linear_baseline"


# ──────────────── train_source_trust end-to-end (live DB) ──────────────


@pytest.mark.asyncio
async def test_train_source_trust_runs_against_real_db() -> None:
    if not _live_db():
        pytest.skip("POSTGRES_PASSWORD not set")

    conn = await asyncpg.connect(_dsn(), statement_cache_size=0)
    try:
        ws_id = await conn.fetchval(
            "SELECT workspace_id::text FROM silver.workspaces "
            "WHERE workspace_id = '00000000-acce-ed30-cccc-000000000030'::uuid"
            " OR workspace_id = 'a0000000-0000-0000-0000-000000000001'::uuid "
            "LIMIT 1"
        )
    finally:
        await conn.close()
    if ws_id is None:
        pytest.skip("No suitable workspace in this DB")

    inp = TrainSourceTrustInput(
        workspace_id=ws_id,
        initiated_by_user_id=1,
        min_citations_per_source=1,
        model_version=f"test_v_{uuid4().hex[:6]}",
        train_request_id=uuid4(),
    )
    out = await train_source_trust_execute.aio_mock_run(inp)
    assert out.success is True
    assert out.training_metrics["method"] == "deterministic_weighted_blend"
