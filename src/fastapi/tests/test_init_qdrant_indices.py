"""scripts/init_qdrant.py payload indices (audit 2026-10-04 item 29)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "init_qdrant.py"


@pytest.fixture
def init_qdrant(monkeypatch):
    spec = importlib.util.spec_from_file_location("init_qdrant_indices_under_test", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def _chunks_spec(module):
    return next(c for c in module.COLLECTIONS if c.name == "georag_chunks")


def test_chunk_kind_is_a_keyword_index(init_qdrant) -> None:
    indices = {i.field_name: i for i in _chunks_spec(init_qdrant).payload_indices}
    assert indices["chunk_kind"].field_schema == "keyword"
    # Existing indices are unchanged.
    for name in ("workspace_id", "project_id", "report_id", "section_number"):
        assert indices[name].field_schema == "keyword"


def test_workspace_id_is_the_tenant_index_and_nothing_else_is(init_qdrant) -> None:
    indices = _chunks_spec(init_qdrant).payload_indices
    tenant = [i.field_name for i in indices if i.is_tenant]
    assert tenant == ["workspace_id"]
    # The legacy reports collection is frozen: no new options there.
    reports = next(c for c in init_qdrant.COLLECTIONS if c.name == "georag_reports")
    assert not any(i.is_tenant for i in reports.payload_indices)


def test_field_schema_wire_shape(init_qdrant) -> None:
    ws = init_qdrant.PayloadIndex("workspace_id", "keyword", is_tenant=True)
    plain = init_qdrant.PayloadIndex("chunk_kind", "keyword")
    assert init_qdrant.index_field_schema(ws) == {"type": "keyword", "is_tenant": True}
    assert init_qdrant.index_field_schema(plain) == "keyword"


class _Resp:
    def __init__(self, status: int = 200, payload: dict | None = None) -> None:
        self.status_code = status
        self._payload = payload or {}
        self.text = ""

    def json(self) -> dict:
        return self._payload


class _Client:
    """Records calls; `existing` maps collection -> already-indexed fields."""

    def __init__(self, existing: dict[str, set[str]]) -> None:
        self.existing = existing
        self.puts: list[tuple[str, dict]] = []

    async def get(self, url: str):
        if url == "/healthz":
            return _Resp()
        if url == "/collections":
            return _Resp(payload={"result": {"collections": [
                {"name": n} for n in ("georag_chunks", "georag_reports")
            ]}})
        name = url.rsplit("/", 1)[-1]
        if name in self.existing:
            return _Resp(payload={"result": {"payload_schema": {
                f: {"data_type": "keyword"} for f in self.existing[name]
            }}})
        return _Resp(404)

    async def put(self, url: str, json: dict | None = None):
        self.puts.append((url, json or {}))
        return _Resp()


class _ClientCM:
    def __init__(self, client: _Client) -> None:
        self.client = client

    async def __aenter__(self) -> _Client:
        return self.client

    async def __aexit__(self, *exc: Any) -> bool:
        return False


async def _run_bootstrap(init_qdrant, monkeypatch, existing: dict[str, set[str]]) -> _Client:
    client = _Client(existing)
    monkeypatch.setattr(init_qdrant.httpx, "AsyncClient", lambda **kw: _ClientCM(client))
    await init_qdrant.bootstrap()
    return client


@pytest.mark.asyncio
async def test_a_fresh_collection_gets_every_index_including_tenant_and_chunk_kind(
    init_qdrant, monkeypatch,
) -> None:
    client = await _run_bootstrap(init_qdrant, monkeypatch, existing={})
    index_puts = {
        (url, body["field_name"]): body["field_schema"]
        for url, body in client.puts if url.endswith("/index")
    }
    assert index_puts[("/collections/georag_chunks/index", "workspace_id")] == {
        "type": "keyword", "is_tenant": True,
    }
    assert index_puts[("/collections/georag_chunks/index", "chunk_kind")] == "keyword"


@pytest.mark.asyncio
async def test_an_existing_collection_is_not_reindexed_and_gains_only_missing_indices(
    init_qdrant, monkeypatch,
) -> None:
    client = await _run_bootstrap(
        init_qdrant, monkeypatch,
        existing={
            "georag_chunks": {"workspace_id", "project_id", "report_id", "section_number"},
            "georag_reports": {"workspace_id", "report_id", "section_number", "commodity"},
        },
    )
    chunk_index_puts = [
        body["field_name"] for url, body in client.puts
        if url == "/collections/georag_chunks/index"
    ]
    # Only the missing chunk_kind index is created; workspace_id is NOT
    # re-PUT (that would rebuild it with is_tenant on a live collection).
    assert chunk_index_puts == ["chunk_kind"]
    assert not any(url == "/collections/georag_reports/index" for url, _ in client.puts)
    assert not any(url in ("/collections/georag_chunks", "/collections/georag_reports")
                   for url, _ in client.puts)


@pytest.mark.asyncio
async def test_running_twice_is_idempotent(init_qdrant, monkeypatch) -> None:
    second = await _run_bootstrap(
        init_qdrant, monkeypatch,
        existing={
            "georag_chunks": {"workspace_id", "project_id", "report_id",
                              "section_number", "chunk_kind"},
            "georag_reports": {"workspace_id", "report_id", "section_number", "commodity"},
        },
    )
    assert second.puts == []
