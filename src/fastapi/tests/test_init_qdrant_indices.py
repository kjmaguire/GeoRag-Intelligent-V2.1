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
        self.put_params: list[tuple[str, dict | None]] = []

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

    async def put(self, url: str, json: dict | None = None, params: dict | None = None):
        self.puts.append((url, json or {}))
        self.put_params.append((url, params))
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


@pytest.mark.asyncio
async def test_index_puts_do_not_wait_for_the_build(init_qdrant, monkeypatch) -> None:
    """The default (wait=true) holds the response until the index is built,
    which on a live collection outlasts the client's 30 s read timeout."""
    client = await _run_bootstrap(init_qdrant, monkeypatch, existing={})
    index_calls = [(u, p) for u, p in client.put_params if u.endswith("/index")]
    assert index_calls, "a fresh bootstrap creates indices"
    assert all(p == {"wait": "false"} for _, p in index_calls)


class _FailingGet:
    """An existing collection whose schema read fails (5xx, or gone mid-run)."""

    def __init__(self, status: int = 503, body: object = None, bad_json: bool = False) -> None:
        self.status, self.body, self.bad_json = status, body, bad_json
        self.puts: list[str] = []

    async def get(self, url: str):
        if url == "/healthz":
            return _Resp()
        if url.startswith("/collections/georag_chunks"):
            # the existence check sees 200, the schema read sees the failure
            self.calls = getattr(self, "calls", 0) + 1
            if self.calls == 1:
                return _Resp(200, {"result": {"payload_schema": {}}})
            if self.bad_json:
                r = _Resp(200)
                r.json = lambda: (_ for _ in ()).throw(ValueError("not json"))  # type: ignore[method-assign]
                return r
            r = _Resp(self.status)
            r.text = "service unavailable"
            return r
        return _Resp(404)

    async def put(self, url: str, json: dict | None = None, params: dict | None = None):
        self.puts.append(url)
        return _Resp()


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [{"status": 503}, {"status": 404}, {"bad_json": True}])
async def test_an_unreadable_schema_stops_the_bootstrap_instead_of_reindexing(
    init_qdrant, monkeypatch, kwargs,
) -> None:
    """An empty set would have re-PUT workspace_id with is_tenant on a live collection."""
    client = _FailingGet(**kwargs)
    monkeypatch.setattr(init_qdrant.httpx, "AsyncClient", lambda **kw: _ClientCM(client))  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="payload schema of 'georag_chunks'"):
        await init_qdrant.bootstrap()
    assert [u for u in client.puts if u.endswith("/index")] == []


@pytest.mark.asyncio
async def test_existing_payload_fields_reads_the_schema_when_it_can(init_qdrant) -> None:
    client = _Client({"georag_chunks": {"workspace_id", "chunk_kind"}})
    assert await init_qdrant._existing_payload_fields(client, "georag_chunks") == {
        "workspace_id", "chunk_kind",
    }


# ---------------------------------------------------------------------------
# --adopt-tenant-index
# ---------------------------------------------------------------------------

_CHUNK_FIELDS = ("workspace_id", "project_id", "report_id", "section_number", "chunk_kind")
_TENANT_PARAMS = {"type": "keyword", "is_tenant": True}
_NO_TENANT_PARAMS = {"type": "keyword"}


class _TenantClient:
    """Existing collections with a controllable ``workspace_id`` schema entry.

    ``ws_entry`` is the raw ``payload_schema["workspace_id"]`` Qdrant reports for
    georag_chunks; every other index is a plain keyword. Records every mutating
    call in order so the DELETE-then-PUT sequence is assertable.
    """

    def __init__(self, ws_entry: dict, put_status: int = 200) -> None:
        self.ws_entry = ws_entry
        self.put_status = put_status
        self.calls: list[tuple[str, str, dict | None, dict | None]] = []

    async def get(self, url: str):
        if url == "/healthz":
            return _Resp()
        if url == "/collections":
            return _Resp(payload={"result": {"collections": [
                {"name": n} for n in ("georag_chunks", "georag_reports")
            ]}})
        if url == "/collections/georag_chunks":
            schema = {f: {"data_type": "keyword", "points": 7} for f in _CHUNK_FIELDS}
            schema["workspace_id"] = self.ws_entry
            return _Resp(payload={"result": {"payload_schema": schema}})
        if url == "/collections/georag_reports":
            return _Resp(payload={"result": {"payload_schema": {
                "workspace_id": {"data_type": "keyword"},
                "report_id": {"data_type": "keyword"},
                "section_number": {"data_type": "integer"},
                "commodity": {"data_type": "keyword"},
            }}})
        return _Resp(404)

    async def put(self, url: str, json: dict | None = None, params: dict | None = None):
        self.calls.append(("PUT", url, json, params))
        return _Resp(self.put_status)

    async def delete(self, url: str, params: dict | None = None):
        self.calls.append(("DELETE", url, None, params))
        return _Resp()

    @property
    def mutations(self) -> list[tuple[str, str, dict | None, dict | None]]:
        return self.calls


async def _run_adopt(init_qdrant, monkeypatch, client, *, adopt: bool) -> None:
    monkeypatch.setattr(init_qdrant.httpx, "AsyncClient", lambda **kw: _ClientCM(client))
    await init_qdrant.bootstrap(adopt_tenant_index=adopt)


@pytest.mark.asyncio
@pytest.mark.parametrize("adopt", [False, True])
async def test_an_index_that_is_already_tenant_is_never_touched(
    init_qdrant, monkeypatch, capsys, adopt,
) -> None:
    client = _TenantClient({"data_type": "keyword", "params": _TENANT_PARAMS, "points": 7})
    await _run_adopt(init_qdrant, monkeypatch, client, adopt=adopt)
    assert client.mutations == []
    assert "'workspace_id' (is_tenant) — left as is" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_missing_is_tenant_without_the_flag_logs_one_line_and_changes_nothing(
    init_qdrant, monkeypatch, capsys,
) -> None:
    client = _TenantClient({"data_type": "keyword", "params": _NO_TENANT_PARAMS, "points": 7})
    await _run_adopt(init_qdrant, monkeypatch, client, adopt=False)
    assert client.mutations == []
    lines = [
        ln for ln in capsys.readouterr().out.splitlines()
        if "lacks is_tenant" in ln
    ]
    assert len(lines) == 1
    assert "--adopt-tenant-index" in lines[0]


@pytest.mark.asyncio
async def test_with_the_flag_the_index_is_deleted_then_recreated_with_is_tenant(
    init_qdrant, monkeypatch, capsys,
) -> None:
    client = _TenantClient({"data_type": "keyword", "params": _NO_TENANT_PARAMS, "points": 7})
    await _run_adopt(init_qdrant, monkeypatch, client, adopt=True)
    assert client.mutations == [
        ("DELETE", "/collections/georag_chunks/index/workspace_id", None, {"wait": "true"}),
        (
            "PUT",
            "/collections/georag_chunks/index",
            {"field_name": "workspace_id",
             "field_schema": {"type": "keyword", "is_tenant": True}},
            {"wait": "false"},
        ),
    ]
    out = capsys.readouterr().out
    assert "BEFORE" in out and "AFTER" in out


@pytest.mark.asyncio
async def test_the_frozen_reports_collection_is_never_adopted(
    init_qdrant, monkeypatch,
) -> None:
    client = _TenantClient({"data_type": "keyword", "params": _NO_TENANT_PARAMS})
    await _run_adopt(init_qdrant, monkeypatch, client, adopt=True)
    assert not any("georag_reports" in url for _m, url, _b, _p in client.mutations)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entry",
    [
        {"points": 7},                                   # no data_type at all
        {"data_type": "integer", "params": None},       # indexed as another type
        "keyword",                                       # not an object at all
    ],
)
@pytest.mark.parametrize("adopt", [False, True])
async def test_a_schema_that_does_not_expose_params_is_unknown_and_not_acted_on(
    init_qdrant, monkeypatch, capsys, entry, adopt,
) -> None:
    client = _TenantClient(entry)  # type: ignore[arg-type]
    await _run_adopt(init_qdrant, monkeypatch, client, adopt=adopt)
    assert client.mutations == []
    out = capsys.readouterr().out
    assert "is_tenant UNKNOWN" in out
    assert "lacks is_tenant" not in out


@pytest.mark.asyncio
async def test_the_bare_keyword_bootstrap_form_is_adopted_with_the_flag(
    init_qdrant, monkeypatch,
) -> None:
    """The live georag_chunks index was created as plain "keyword", which
    Qdrant reports with params null - the one shape the flag must act on."""
    client = _TenantClient({"data_type": "keyword", "params": None, "points": 3})
    await _run_adopt(init_qdrant, monkeypatch, client, adopt=True)
    assert [m for m, *_ in client.mutations] == ["DELETE", "PUT"]


@pytest.mark.asyncio
async def test_a_failed_recreate_after_the_delete_says_the_index_is_gone(
    init_qdrant, monkeypatch,
) -> None:
    client = _TenantClient(
        {"data_type": "keyword", "params": _NO_TENANT_PARAMS}, put_status=500,
    )
    with pytest.raises(RuntimeError, match="DELETED and NOT re-created"):
        await _run_adopt(init_qdrant, monkeypatch, client, adopt=True)
    assert [m for m, *_ in client.mutations] == ["DELETE", "PUT"]


def test_tenant_state_reads_only_params_is_tenant(init_qdrant) -> None:
    state = init_qdrant.tenant_state
    assert state({"params": {"type": "keyword", "is_tenant": True}}) == "tenant"
    assert state({"params": {"type": "keyword", "is_tenant": False}}) == "not_tenant"
    assert state({"params": {"type": "keyword"}}) == "not_tenant"
    # The bare-"keyword" bootstrap form: Qdrant reports params null.
    assert state({"data_type": "keyword", "params": None, "points": 3}) == "not_tenant"
    assert state({"data_type": "keyword"}) == "not_tenant"
    assert state({"data_type": "integer", "params": None}) == "unknown"
    assert state({"points": 3}) == "unknown"
    assert state(None) == "unknown"


def test_the_cli_flag_defaults_off(init_qdrant) -> None:
    assert init_qdrant._parse_args([]).adopt_tenant_index is False
    assert init_qdrant._parse_args(["--adopt-tenant-index"]).adopt_tenant_index is True
