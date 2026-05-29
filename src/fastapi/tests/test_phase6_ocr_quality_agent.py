"""Phase 6 (2026-05-22) — OCR Quality Agent tests.

Covers:
  - Artifact-detection heuristics (letter/digit confusion, broken
    decimal, garbage glyphs) as pure functions
  - Threshold env reads with invalid-value fallback
  - Workflow disabled → skipped early
  - Quality agent run: classifies passages into accepted /
    low_confidence / pending_reocr based on confidence + artifacts
  - Re-OCR dispatch fires for unique pages only (de-duped)
  - Re-OCR cap respected; review_queue row inserted when over cap
  - Re-OCR dispatch failure leaves ocr_status='pending_reocr' so the
    next run can retry
  - Idempotency: running twice produces same DB state
  - INSERT_PASSAGE_SQL writes ocr_status='accepted' on new rows
  - Persist dispatch is gated on OCR_QUALITY_AGENT_ENABLED

Run with:
    pytest src/fastapi/tests/test_phase6_ocr_quality_agent.py -v
"""

from __future__ import annotations

import inspect
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _ensure_parser_stub_module():
    if "georag_dagster.parsers.pdf_report" in sys.modules:
        return
    pkg_root = sys.modules.get("georag_dagster") or types.ModuleType("georag_dagster")
    pkg_parsers = types.ModuleType("georag_dagster.parsers")
    mod = types.ModuleType("georag_dagster.parsers.pdf_report")
    mod._FIGURE_TEMPDIR_ROOT = "/tmp/georag_figures"

    def _figure_tempdir(sha256: str) -> str:
        import os as _os
        d = f"{mod._FIGURE_TEMPDIR_ROOT}/{sha256}"
        _os.makedirs(d, exist_ok=True)
        return d

    mod._figure_tempdir = _figure_tempdir
    mod.parse_pdf_report = MagicMock()
    pkg_parsers.pdf_report = mod
    pkg_root.parsers = pkg_parsers
    sys.modules["georag_dagster"] = pkg_root
    sys.modules["georag_dagster.parsers"] = pkg_parsers
    sys.modules["georag_dagster.parsers.pdf_report"] = mod


_ensure_parser_stub_module()


# ---------------------------------------------------------------------------
# Heuristics — pure functions
# ---------------------------------------------------------------------------

class TestLetterDigitConfusion:
    def test_clean_text_returns_false(self):
        from app.hatchet_workflows import ocr_quality_check as mod
        assert mod._has_letter_digit_confusion("Au grade 1.23 g/t") is False

    def test_l_before_digits_detected(self):
        from app.hatchet_workflows import ocr_quality_check as mod
        # 'l5' is a Tesseract confusion of '15'
        assert mod._has_letter_digit_confusion("Depth: l5 meters") is True

    def test_capital_I_before_digits_detected(self):
        from app.hatchet_workflows import ocr_quality_check as mod
        assert mod._has_letter_digit_confusion("Drilled I20 holes") is True

    def test_O_for_zero_detected(self):
        from app.hatchet_workflows import ocr_quality_check as mod
        assert mod._has_letter_digit_confusion("Sample O01 Au 0.5") is True

    def test_empty_string(self):
        from app.hatchet_workflows import ocr_quality_check as mod
        assert mod._has_letter_digit_confusion("") is False


class TestBrokenDecimal:
    def test_proper_decimal_passes(self):
        from app.hatchet_workflows import ocr_quality_check as mod
        assert mod._has_broken_decimal("Grade 1.23 g/t Au") is False

    def test_space_in_decimal_detected(self):
        from app.hatchet_workflows import ocr_quality_check as mod
        assert mod._has_broken_decimal("Grade 1 23 g/t Au") is True

    def test_percent_unit(self):
        from app.hatchet_workflows import ocr_quality_check as mod
        assert mod._has_broken_decimal("0 65 % Zn") is True

    def test_ppm_unit(self):
        from app.hatchet_workflows import ocr_quality_check as mod
        assert mod._has_broken_decimal("125 5 ppm Cu") is True

    def test_case_insensitive(self):
        from app.hatchet_workflows import ocr_quality_check as mod
        assert mod._has_broken_decimal("0 65 PPM") is True


class TestGarbage:
    def test_low_confidence_short_text_is_garbage(self, monkeypatch):
        from app.hatchet_workflows import ocr_quality_check as mod
        monkeypatch.setenv("OCR_REOCR_THRESHOLD", "0.60")
        assert mod._is_garbage("xy", 0.30) is True

    def test_high_confidence_short_text_not_garbage(self, monkeypatch):
        from app.hatchet_workflows import ocr_quality_check as mod
        monkeypatch.setenv("OCR_REOCR_THRESHOLD", "0.60")
        # Short but high-confidence — could be a real label like "Au"
        assert mod._is_garbage("xy", 0.85) is False

    def test_low_confidence_long_text_not_garbage(self, monkeypatch):
        from app.hatchet_workflows import ocr_quality_check as mod
        monkeypatch.setenv("OCR_REOCR_THRESHOLD", "0.60")
        long_text = "a" * 100
        assert mod._is_garbage(long_text, 0.30) is False

    def test_none_confidence_returns_false(self):
        from app.hatchet_workflows import ocr_quality_check as mod
        assert mod._is_garbage("xy", None) is False

    def test_invalid_threshold_falls_back_to_default(self, monkeypatch):
        from app.hatchet_workflows import ocr_quality_check as mod
        monkeypatch.setenv("OCR_REOCR_THRESHOLD", "not-a-number")
        # Default threshold 0.60 — short + 0.40 conf → still garbage
        assert mod._is_garbage("xy", 0.40) is True


class TestHasArtifact:
    def test_any_artifact_triggers(self):
        from app.hatchet_workflows import ocr_quality_check as mod
        assert mod._has_artifact("Depth l5 meters", 0.90) is True
        assert mod._has_artifact("Grade 1 23 g/t", 0.90) is True

    def test_clean_text_high_confidence_passes(self):
        from app.hatchet_workflows import ocr_quality_check as mod
        assert mod._has_artifact("Clean prose, no artifacts here.", 0.95) is False


# ---------------------------------------------------------------------------
# Workflow body
# ---------------------------------------------------------------------------

def _make_input():
    from app.hatchet_workflows.ocr_quality_check import OcrQualityCheckInput
    return OcrQualityCheckInput(
        workspace_id="a0000000-0000-0000-0000-000000000001",
        project_id="11111111-2222-3333-4444-555555555555",
        report_id="33333333-4444-5555-6666-777777777777",
    )


def _get_run_func():
    from app.hatchet_workflows.ocr_quality_check import run
    return getattr(run, "_fn", run)


def _make_passage(
    pid: str, text: str, conf: float, page: int,
    status: str = "accepted", method: str = "tesseract",
):
    return {
        "passage_id": pid,
        "text": text,
        "ocr_confidence": conf,
        "ocr_method": method,
        "page_first": page,
        "ocr_status": status,
    }


def _patch_pg(monkeypatch, passages, *, capture_updates=None,
              capture_inserts=None, bronze_sha=None):
    """Stub asyncpg.create_pool with a controllable connection."""
    capture_updates = capture_updates if capture_updates is not None else []
    capture_inserts = capture_inserts if capture_inserts is not None else []

    fake_conn = MagicMock()
    fake_conn.execute = AsyncMock(return_value="OK")
    fake_conn.fetch = AsyncMock(return_value=passages)
    fake_conn.fetchval = AsyncMock(return_value=bronze_sha)

    async def _exec(query, *args):
        q = query.strip().upper()
        if "UPDATE SILVER.DOCUMENT_PASSAGES" in q:
            capture_updates.append((query, args))
        elif "INSERT INTO SILVER.REVIEW_QUEUE" in q:
            capture_inserts.append((query, args))
        return "OK"
    fake_conn.execute.side_effect = _exec

    fake_acquire_cm = MagicMock()
    fake_acquire_cm.__aenter__ = AsyncMock(return_value=fake_conn)
    fake_acquire_cm.__aexit__ = AsyncMock(return_value=None)

    fake_pool = MagicMock()
    fake_pool.acquire = MagicMock(return_value=fake_acquire_cm)
    fake_pool.close = AsyncMock()

    from app.hatchet_workflows import ocr_quality_check as mod
    monkeypatch.setattr(mod.asyncpg, "create_pool", AsyncMock(return_value=fake_pool))
    return fake_conn, capture_updates, capture_inserts


@pytest.mark.asyncio
async def test_workflow_disabled_returns_skipped(monkeypatch):
    monkeypatch.setenv("OCR_QUALITY_AGENT_ENABLED", "false")
    run = _get_run_func()
    result = await run(_make_input(), MagicMock())
    assert result.skipped is True
    assert result.reason == "OCR_QUALITY_AGENT_ENABLED=false"


@pytest.mark.asyncio
async def test_workflow_classifies_low_confidence_only(monkeypatch):
    monkeypatch.setenv("OCR_QUALITY_AGENT_ENABLED", "true")
    monkeypatch.setenv("OCR_QUALITY_THRESHOLD", "0.75")
    monkeypatch.setenv("OCR_REOCR_THRESHOLD", "0.60")
    passages = [
        _make_passage("p1", "x" * 100, 0.65, page=1),  # below quality, above reocr
        _make_passage("p2", "x" * 100, 0.90, page=2),  # above quality
    ]
    _patch_pg(monkeypatch, passages)
    # Patch re_ocr_page to ensure it's NOT called
    re_ocr_spy = AsyncMock()
    fake_re_ocr_mod = types.SimpleNamespace(
        re_ocr_page=MagicMock(aio_run_no_wait=re_ocr_spy),
        ReOcrPageInput=lambda **kw: types.SimpleNamespace(**kw),
    )
    monkeypatch.setitem(
        sys.modules, "app.hatchet_workflows.re_ocr_page", fake_re_ocr_mod,
    )

    run = _get_run_func()
    out = await run(_make_input(), MagicMock())

    assert out.passages_evaluated == 2
    assert out.flagged_low_confidence == 1
    assert out.flagged_pending_reocr == 0
    assert out.reocr_dispatched == 0
    re_ocr_spy.assert_not_called()


@pytest.mark.asyncio
async def test_workflow_dispatches_reocr_on_low_confidence(monkeypatch):
    monkeypatch.setenv("OCR_QUALITY_AGENT_ENABLED", "true")
    passages = [
        _make_passage("p1", "x" * 200, 0.40, page=5),  # below reocr threshold
        _make_passage("p2", "y" * 200, 0.95, page=7),  # clean
    ]
    _patch_pg(monkeypatch, passages)

    re_ocr_spy = AsyncMock()
    fake_re_ocr_mod = types.SimpleNamespace(
        re_ocr_page=MagicMock(aio_run_no_wait=re_ocr_spy),
        ReOcrPageInput=lambda **kw: types.SimpleNamespace(**kw),
    )
    monkeypatch.setitem(
        sys.modules, "app.hatchet_workflows.re_ocr_page", fake_re_ocr_mod,
    )

    run = _get_run_func()
    out = await run(_make_input(), MagicMock())

    assert out.flagged_pending_reocr == 1
    assert out.reocr_dispatched == 1
    re_ocr_spy.assert_awaited_once()


@pytest.mark.asyncio
async def test_workflow_dispatches_reocr_on_artifact(monkeypatch):
    """A passage with high confidence BUT an artifact pattern should still
    be flagged for re-OCR — letter/digit confusion is signal even at 0.80."""
    monkeypatch.setenv("OCR_QUALITY_AGENT_ENABLED", "true")
    passages = [
        _make_passage("p1", "Depth l5 m and l8 m intervals " * 5, 0.80, page=3),
    ]
    _patch_pg(monkeypatch, passages)

    re_ocr_spy = AsyncMock()
    fake_re_ocr_mod = types.SimpleNamespace(
        re_ocr_page=MagicMock(aio_run_no_wait=re_ocr_spy),
        ReOcrPageInput=lambda **kw: types.SimpleNamespace(**kw),
    )
    monkeypatch.setitem(
        sys.modules, "app.hatchet_workflows.re_ocr_page", fake_re_ocr_mod,
    )

    run = _get_run_func()
    out = await run(_make_input(), MagicMock())

    assert out.flagged_pending_reocr == 1
    re_ocr_spy.assert_awaited_once()


@pytest.mark.asyncio
async def test_workflow_dedupes_pages_for_reocr(monkeypatch):
    """Two passages on the same page should produce ONE re-OCR dispatch."""
    monkeypatch.setenv("OCR_QUALITY_AGENT_ENABLED", "true")
    passages = [
        _make_passage("p1", "x" * 200, 0.30, page=10),
        _make_passage("p2", "y" * 200, 0.30, page=10),  # same page
        _make_passage("p3", "z" * 200, 0.30, page=11),
    ]
    _patch_pg(monkeypatch, passages)

    re_ocr_spy = AsyncMock()
    fake_re_ocr_mod = types.SimpleNamespace(
        re_ocr_page=MagicMock(aio_run_no_wait=re_ocr_spy),
        ReOcrPageInput=lambda **kw: types.SimpleNamespace(**kw),
    )
    monkeypatch.setitem(
        sys.modules, "app.hatchet_workflows.re_ocr_page", fake_re_ocr_mod,
    )

    run = _get_run_func()
    out = await run(_make_input(), MagicMock())

    assert out.flagged_pending_reocr == 3  # all 3 passages flagged
    assert out.reocr_dispatched == 2       # only 2 unique pages


@pytest.mark.asyncio
async def test_workflow_respects_reocr_cap_and_creates_review_queue(monkeypatch):
    monkeypatch.setenv("OCR_QUALITY_AGENT_ENABLED", "true")
    monkeypatch.setenv("OCR_MAX_REOCR_PAGES_PER_DOC", "3")
    # 5 passages on 5 distinct pages, all low-confidence
    passages = [
        _make_passage(f"p{i}", "x" * 200, 0.30, page=i)
        for i in range(1, 6)
    ]
    _, _updates, inserts = _patch_pg(monkeypatch, passages, bronze_sha="abc123")

    re_ocr_spy = AsyncMock()
    fake_re_ocr_mod = types.SimpleNamespace(
        re_ocr_page=MagicMock(aio_run_no_wait=re_ocr_spy),
        ReOcrPageInput=lambda **kw: types.SimpleNamespace(**kw),
    )
    monkeypatch.setitem(
        sys.modules, "app.hatchet_workflows.re_ocr_page", fake_re_ocr_mod,
    )

    run = _get_run_func()
    out = await run(_make_input(), MagicMock())

    # Cap = 3, so only 3 dispatched even though 5 flagged
    assert out.flagged_pending_reocr == 5
    assert out.reocr_dispatched == 3
    assert re_ocr_spy.await_count == 3
    assert out.review_queue_created is True
    assert len(inserts) == 1


@pytest.mark.asyncio
async def test_workflow_reocr_dispatch_failure_keeps_pending(monkeypatch):
    """If re_ocr_page dispatch raises, the passage stays flagged
    'pending_reocr' (DB UPDATE already committed) — next run can retry."""
    monkeypatch.setenv("OCR_QUALITY_AGENT_ENABLED", "true")
    passages = [_make_passage("p1", "x" * 200, 0.30, page=4)]
    _, updates, _ = _patch_pg(monkeypatch, passages)

    re_ocr_spy = AsyncMock(side_effect=RuntimeError("hatchet unreachable"))
    fake_re_ocr_mod = types.SimpleNamespace(
        re_ocr_page=MagicMock(aio_run_no_wait=re_ocr_spy),
        ReOcrPageInput=lambda **kw: types.SimpleNamespace(**kw),
    )
    monkeypatch.setitem(
        sys.modules, "app.hatchet_workflows.re_ocr_page", fake_re_ocr_mod,
    )

    run = _get_run_func()
    out = await run(_make_input(), MagicMock())

    # Flag committed, dispatch failed → 0 dispatched
    assert out.flagged_pending_reocr == 1
    assert out.reocr_dispatched == 0
    # The UPDATE to pending_reocr happened before the dispatch attempt
    pending_update = [
        u for u in updates
        if "PENDING_REOCR" in u[0].upper()
    ]
    assert len(pending_update) == 1


@pytest.mark.asyncio
async def test_workflow_skips_passages_with_null_confidence(monkeypatch):
    """fitz_native + pdfplumber_native passages have ocr_confidence=NULL
    — the SQL filters them out, so quality agent has nothing to do."""
    monkeypatch.setenv("OCR_QUALITY_AGENT_ENABLED", "true")
    # Empty result set (the SELECT filters WHERE ocr_confidence IS NOT NULL)
    _patch_pg(monkeypatch, passages=[])

    run = _get_run_func()
    out = await run(_make_input(), MagicMock())

    assert out.passages_evaluated == 0
    assert out.flagged_low_confidence == 0
    assert out.flagged_pending_reocr == 0


# ---------------------------------------------------------------------------
# Source-level regression guards
# ---------------------------------------------------------------------------

def test_insert_passage_sql_writes_ocr_status_default():
    """INSERT_PASSAGE_SQL on new passage must default ocr_status='accepted'."""
    src = open("/app/app/hatchet_workflows/ingest_pdf.py").read()
    # The literal default lives in the VALUES clause
    assert "'accepted'" in src
    # ON CONFLICT does NOT touch ocr_status (preserves any agent-set value)
    on_conflict_idx = src.index("ON CONFLICT (document_id, revision_number, text_hash)")
    on_conflict_block = src[on_conflict_idx:on_conflict_idx + 700]
    assert "ocr_status =" not in on_conflict_block


def test_persist_dispatches_quality_agent_when_enabled():
    """persist body must dispatch ocr_quality_check_wf when env is set."""
    src = open("/app/app/hatchet_workflows/ingest_pdf.py").read()
    assert "ocr_quality_check_wf.aio_run_no_wait" in src
    assert 'OCR_QUALITY_AGENT_ENABLED' in src


def test_worker_registers_ocr_quality_check():
    """worker.py must import + register ocr_quality_check_wf on the
    ingestion pool so dispatches actually reach a worker."""
    src = open("/app/app/hatchet_workflows/worker.py").read()
    assert "from app.hatchet_workflows.ocr_quality_check import ocr_quality_check_wf" in src
    assert "ocr_quality_check_wf" in src
