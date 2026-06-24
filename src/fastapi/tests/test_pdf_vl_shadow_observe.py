"""ADR-0015 step-3 dual-write — PdfVlService.shadow_observe_section tests.

No DB, no model, no serving: a {"kind":"page"} section_ref resolves without the
pool, shadow_observe_section never persists, and the VL backend is mocked. The
render service is mocked too so we can assert the section is rendered ONCE and
the identical images feed both model versions.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, Mock

import pytest

from app.services.pdf_vl import PdfVlService, VlBackendError


def _claim() -> dict:
    return {"claim_text": "grade is 1.2 g/t Au", "page": 1, "bbox": [0, 0, 1, 1], "confidence": 0.9}


def _valid_content(summary: str, n_claims: int) -> str:
    return json.dumps({"summary": summary, "claims": [_claim() for _ in range(n_claims)]})


def _make_service() -> tuple[PdfVlService, Mock]:
    render = Mock()
    render.render_page = AsyncMock(return_value=b"pngbytes")
    svc = PdfVlService(pool=Mock(), render_service=render, http_client=None)
    return svc, render


_PAGE_REF = {"kind": "page", "page": 1}


def test_both_models_valid_renders_once(monkeypatch: pytest.MonkeyPatch) -> None:
    svc, render = _make_service()

    async def fake_backend(png_list, pages, model_id=None):
        # Same images feed both models.
        assert png_list == [b"pngbytes"]
        if model_id == "model-v3":
            return _valid_content("v3 summary", 3), {}
        return _valid_content("v2 summary", 2), {}

    monkeypatch.setattr(svc, "_call_vl_backend", fake_backend)

    obs = asyncio.run(
        svc.shadow_observe_section(
            b"pdf", "pdfid123", _PAGE_REF, v2_model_id="model-v2", v3_model_id="model-v3"
        )
    )

    assert obs.page_count == 1
    assert obs.v2_schema_valid is True and obs.v3_schema_valid is True
    assert obs.v2_grounded_claims == 2 and obs.v3_grounded_claims == 3
    assert obs.v2_has_grounded_output is True and obs.v3_has_grounded_output is True
    assert obs.v2_latency_ms is not None and obs.v3_latency_ms is not None
    # Rendered ONCE — not once per model.
    assert render.render_page.call_count == 1


def test_v3_backend_error_recorded_as_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    svc, _ = _make_service()

    async def fake_backend(png_list, pages, model_id=None):
        if model_id == "model-v3":
            raise VlBackendError(502, "vl backend down")
        return _valid_content("v2 only", 1), {}

    monkeypatch.setattr(svc, "_call_vl_backend", fake_backend)

    obs = asyncio.run(
        svc.shadow_observe_section(
            b"pdf", "pdfid", _PAGE_REF, v2_model_id="model-v2", v3_model_id="model-v3"
        )
    )

    assert obs.v2_schema_valid is True
    assert obs.v3_schema_valid is False
    assert obs.v3_grounded_claims == 0
    assert obs.v3_has_grounded_output is False
    # Latency is still recorded for the failed version (it was attempted).
    assert obs.v3_latency_ms is not None


def test_v3_malformed_output_recorded_as_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    svc, _ = _make_service()

    async def fake_backend(png_list, pages, model_id=None):
        if model_id == "model-v3":
            return "this is not json", {}  # _parse_and_validate → VlOutputShapeError
        return _valid_content("v2 ok", 2), {}

    monkeypatch.setattr(svc, "_call_vl_backend", fake_backend)

    obs = asyncio.run(
        svc.shadow_observe_section(
            b"pdf", "pdfid", _PAGE_REF, v2_model_id="model-v2", v3_model_id="model-v3"
        )
    )

    assert obs.v2_schema_valid is True
    assert obs.v3_schema_valid is False


def test_defaults_use_v2_v3_env_model_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    svc, _ = _make_service()
    seen: list[str] = []

    async def fake_backend(png_list, pages, model_id=None):
        seen.append(model_id)
        return _valid_content("ok", 1), {}

    monkeypatch.setattr(svc, "_call_vl_backend", fake_backend)
    monkeypatch.setenv("PDF_VL_MODEL_ID_V2", "env-v2")
    monkeypatch.setenv("PDF_VL_MODEL_ID_V3", "env-v3")

    asyncio.run(svc.shadow_observe_section(b"pdf", "pdfid", _PAGE_REF))

    assert seen == ["env-v2", "env-v3"]
