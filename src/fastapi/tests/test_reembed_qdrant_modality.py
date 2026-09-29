"""scripts/reembed_qdrant.py — the documented dimension-mismatch recovery.

VEN-4 (2026-09-29): it re-embedded every point from ``payload["text"]``,
including page-image points whose text is only a placeholder caption, so a
run silently replaced every image vector with a caption vector.

Found alongside it: it wrote with ``upsert``, which REPLACES the point, so on
``georag_chunks`` (dense '' + sparse 'text') every re-embedded point lost its
SPLADE++ vector. It now uses ``update_vectors`` on the dense slot only.

No Qdrant, no model: both are fakes.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "reembed_qdrant.py"


@pytest.fixture(scope="module")
def reembed():
    spec = importlib.util.spec_from_file_location("reembed_qdrant_under_test", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Model:
    def __init__(self) -> None:
        self.seen: list[list[str]] = []

    def encode(self, texts: list[str], **_kw: Any) -> np.ndarray:
        self.seen.append(list(texts))
        return np.ones((len(texts), 1024), dtype=np.float32)


class _Client:
    def __init__(self, points: list[Any], vectors_cfg: Any) -> None:
        self._points = points
        self._vectors_cfg = vectors_cfg
        self.updates: list[Any] = []
        self.upserts: list[Any] = []

    def get_collection(self, _name: str) -> Any:
        return SimpleNamespace(
            points_count=len(self._points),
            config=SimpleNamespace(params=SimpleNamespace(vectors=self._vectors_cfg)),
        )

    def scroll(self, **_kw: Any) -> tuple[list[Any], None]:
        return self._points, None

    def update_vectors(self, *, collection_name: str, points: list[Any], wait: bool) -> None:
        self.updates.extend(points)

    def upsert(self, **kw: Any) -> None:  # pragma: no cover -- must not be called
        self.upserts.append(kw)


def _points() -> list[Any]:
    return [
        SimpleNamespace(id=1, payload={"text": "chalcopyrite veins", "modality": "text"}),
        SimpleNamespace(id=2, payload={"text": "page image", "modality": "image", "image_object_key": "k"}),
        SimpleNamespace(id=3, payload={"text": "no modality field at all"}),
    ]


def test_image_points_are_not_given_a_caption_vector(reembed, caplog) -> None:
    client = _Client(_points(), {"": SimpleNamespace(size=1024)})
    model = _Model()

    with caplog.at_level("WARNING"):
        count, skipped = reembed._reembed_collection(client, model, "georag_chunks")

    assert (count, skipped) == (2, False)
    assert model.seen == [["chalcopyrite veins", "no modality field at all"]]
    assert [p.id for p in client.updates] == [1, 3]
    assert any("page-image point" in r.getMessage() for r in caplog.records)


def test_only_the_dense_slot_is_written_so_sparse_vectors_survive(reembed) -> None:
    client = _Client(_points(), {"": SimpleNamespace(size=1024)})
    reembed._reembed_collection(client, _Model(), "georag_chunks")

    assert client.upserts == [], "upsert replaces the point and drops the SPLADE++ vector"
    assert all(set(p.vector) == {""} for p in client.updates)


def test_an_unnamed_legacy_collection_gets_a_plain_vector(reembed) -> None:
    client = _Client(_points(), SimpleNamespace(size=1024))
    reembed._reembed_collection(client, _Model(), "georag_reports")
    assert all(isinstance(p.vector, list) for p in client.updates)
