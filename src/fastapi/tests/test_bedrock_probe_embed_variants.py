"""ops/validation/bedrock_probe.py after VEN-18 (2026-09-29).

The Bedrock probe never exercised ``input_type="search_query"``, a batch
larger than one, or an image embed -- so ``EMBED_IMAGE`` stayed unobserved
although ingest relies on it. It still carried a dead Parse section on the
retired object-form ``image_url``, and reported a hard-coded 8 s reranker
budget when the real one is 19 s.

The new variants CANNOT be run from this container (no AWS). These tests run
them against a fake bedrock-runtime client, so what they prove is the
probe's own logic -- which variant goes where, which image shape it tries
first, how the report reads -- not anything about Bedrock.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "ops" / "validation"))

import bedrock_probe  # noqa: E402


class _Body:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._raw = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._raw


def _validation_error() -> Exception:
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": "ValidationException", "Message": "bad body"}}, "InvokeModel")


class _Runtime:
    def __init__(self, *, reject_images_shape: bool = False) -> None:
        self.bodies: list[dict[str, Any]] = []
        self.reject_images_shape = reject_images_shape

    def invoke_model(self, **kwargs: Any) -> dict[str, Any]:
        body = json.loads(kwargs["body"])
        self.bodies.append(body)
        if "images" in body and self.reject_images_shape:
            raise _validation_error()
        n = len(body.get("texts") or body.get("images") or body.get("inputs") or [])
        return {"body": _Body({"embeddings": {"float": [[0.0] * 1024] * n}, "id": "x", "response_type": "embeddings_by_type"})}


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch):
    fake = _Runtime()
    monkeypatch.setattr(bedrock_probe, "_client", lambda _service: fake)
    return fake


def test_the_parse_section_is_gone() -> None:
    assert not hasattr(bedrock_probe, "probe_parse")
    assert "parse" not in bedrock_probe._EVIDENCE_SECTIONS


def test_every_embed_variant_runs_and_keeps_the_old_evidence_path(runtime) -> None:
    out = bedrock_probe.probe_embed(None, 1)

    # Same top-level evidence path as the committed 2026-09-16 report.
    assert out["top_level_keys"] == ["embeddings", "id", "response_type"]
    assert out["dimension_honoured"] is True
    assert out["query"]["dimension"] == 1024
    assert out["batch"] == {**out["batch"], "texts_sent": 96, "vectors_back": 96}
    assert out["image"]["shape_accepted"] == "images"

    sent_types = [b.get("input_type") for b in runtime.bodies]
    assert sent_types == ["search_document", "search_query", "search_document", "image"]
    assert len(runtime.bodies[2]["texts"]) == 96


def test_the_image_falls_back_to_inputs_only_on_a_validation_error(monkeypatch) -> None:
    fake = _Runtime(reject_images_shape=True)
    monkeypatch.setattr(bedrock_probe, "_client", lambda _service: fake)

    out = bedrock_probe.probe_embed(None, 1)

    assert out["image"]["shape_accepted"] == "inputs"
    assert out["image"]["rejected_first"]["images"]["code"] == "ValidationException"


def test_the_image_embed_evidence_reaches_the_wire_contract(runtime) -> None:
    from app.services.bedrock_wire import diff_report

    diff = diff_report({"embed": bedrock_probe.probe_embed(None, 1)})
    assert diff["calls"]["embed_image"]["status"] == "observed"
    assert diff["calls"]["embed_image"]["confirmed"] == ["embeddings"]
    assert diff["calls"]["embed_text"]["confirmed"] == ["embeddings"]


def test_the_verdict_counts_an_embed_section_with_a_failed_variant(monkeypatch) -> None:
    class _NoImages(_Runtime):
        def invoke_model(self, **kwargs: Any) -> dict[str, Any]:
            if kwargs["body"] and "image" in json.loads(kwargs["body"]).get("input_type", ""):
                raise RuntimeError("image path down")
            return super().invoke_model(**kwargs)

    monkeypatch.setattr(bedrock_probe, "_client", lambda _service: _NoImages())
    section = bedrock_probe.probe_embed(None, 1)
    assert "error" in section["image"]
    v = bedrock_probe.verdict({"embed": section})
    assert "embed" in v["sections_ok"]


def test_the_latency_section_reports_the_real_reranker_budget(monkeypatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "TIMEOUT_RERANKER_S", 20.0)
    budget = bedrock_probe._reranker_budget()
    assert budget["budget_s"] == pytest.approx(19.0)
    assert budget["max_attempts"] * budget["read_timeout_s"] < 20.0


def test_a_synthetic_image_is_used_without_a_pdf() -> None:
    rendered = bedrock_probe._probe_image_png(None, 1)
    assert rendered is not None
    png, source = rendered
    assert source.startswith("synthetic")
    from PIL import Image

    image = Image.open(io.BytesIO(png))
    assert image.width * image.height <= 2_000_000
