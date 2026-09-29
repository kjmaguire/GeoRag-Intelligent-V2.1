"""VEN-14 (2026-09-29): one rejected text must not strand its whole batch.

``embed_pending_passages`` sweeps oldest-first in batches of 64. A single
text the embedding host rejected failed the entire batch, every sweep, so its
63 good neighbours were never embedded either. ``encode_isolating_rejections``
bisects on a *request rejection* (Bedrock ``ValidationException``, sidecar
400/422) and returns None only for the offending text(s).

``batch_size`` is also bounded at the Embed v4 per-request limit (96,
[ASSUMED] vendor figure). That is checked statically: importing the workflow
module needs HATCHET_CLIENT_TOKEN, which is set in CI only.
"""

from __future__ import annotations

import ast
import pathlib

import httpx
import pytest
from botocore.exceptions import ClientError

from app.services.ingest.passage_embedder import encode_isolating_rejections


def _validation_error() -> ClientError:
    return ClientError(
        {"Error": {"Code": "ValidationException", "Message": "text too long"}}, "InvokeModel"
    )


def _encoder(poison: set[str], calls: list[int], error=_validation_error):
    def encode(texts: list[str]) -> list[list[float]]:
        calls.append(len(texts))
        if poison & set(texts):
            raise error()
        return [[float(len(t))] for t in texts]

    return encode


def test_a_clean_batch_is_one_call() -> None:
    calls: list[int] = []
    out = encode_isolating_rejections(_encoder(set(), calls), ["a", "bb", "ccc"])
    assert out == [[1.0], [2.0], [3.0]]
    assert calls == [3]


def test_one_poison_text_only_loses_itself() -> None:
    calls: list[int] = []
    texts = [f"t{i:02d}" for i in range(64)]
    out = encode_isolating_rejections(_encoder({"t37"}, calls), texts)

    assert out[37] is None
    assert all(v is not None for i, v in enumerate(out) if i != 37)
    assert len(out) == 64
    # O(log n): 1 + 2 per level down to the single text.
    assert len(calls) <= 1 + 2 * 6


def test_two_poison_texts_are_both_isolated_and_order_is_kept() -> None:
    texts = ["ok1", "bad1", "ok22", "ok333", "bad2", "ok4444"]
    out = encode_isolating_rejections(_encoder({"bad1", "bad2"}, []), texts)
    assert out == [[3.0], None, [4.0], [5.0], None, [6.0]]


def test_a_sidecar_422_is_a_rejection_too() -> None:
    def http_422():
        request = httpx.Request("POST", "http://embedding:8000/embed")
        return httpx.HTTPStatusError("422", request=request, response=httpx.Response(422, request=request))

    out = encode_isolating_rejections(_encoder({"bad"}, [], error=http_422), ["good", "bad"])
    assert out == [[4.0], None]


@pytest.mark.parametrize(
    "error",
    [
        lambda: ClientError({"Error": {"Code": "ThrottlingException", "Message": "slow"}}, "InvokeModel"),
        lambda: RuntimeError("network down"),
    ],
)
def test_a_call_level_failure_is_not_bisected(error) -> None:
    """Throttling or a dead network is about the call, not the texts."""
    calls: list[int] = []
    with pytest.raises(Exception):  # noqa: B017 -- either type must propagate unchanged
        encode_isolating_rejections(_encoder({"a"}, calls, error=error), ["a", "b", "c", "d"])
    assert calls == [4]


def test_batch_size_is_bounded_at_the_embed_v4_request_limit() -> None:
    source = (
        pathlib.Path(__file__).resolve().parents[1] / "app" / "hatchet_workflows" / "embed_pending_passages.py"
    ).read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", None) == "batch_size":
            call = node.value
            assert isinstance(call, ast.Call)
            kwargs = {kw.arg: ast.literal_eval(kw.value) for kw in call.keywords}
            assert kwargs.get("ge") == 1
            assert kwargs.get("le") == 96
            assert kwargs.get("default") == 64
            return
    pytest.fail("batch_size field not found in EmbedPendingPassagesInput")
