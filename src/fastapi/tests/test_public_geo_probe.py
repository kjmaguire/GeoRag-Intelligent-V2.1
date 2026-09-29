"""ops/validation/public_geo_probe.py against fake services.

The probe is how the registry's SK ArcGIS addressing gets verified from inside
AWS, so it is tested here the way the rerank/Cohere probes are: the real
script, fake upstreams (``httpx.MockTransport``), no network.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
import types
import zlib
from pathlib import Path
from typing import Any

import httpx
import pytest

REPO = Path(__file__).resolve().parents[3]
PROBE_PATH = REPO / "ops/validation/public_geo_probe.py"
REGISTRY_PATH = REPO / "src/fastapi/app/services/public_geo/registry.py"


@pytest.fixture
def probe(monkeypatch):
    spec = importlib.util.spec_from_file_location("public_geo_probe", PROBE_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    # dataclasses resolves annotations through sys.modules[cls.__module__].
    monkeypatch.setitem(sys.modules, spec.name, mod)
    spec.loader.exec_module(mod)
    return mod


def _install(monkeypatch, probe, handler) -> list[httpx.Request]:
    seen: list[httpx.Request] = []
    real_client = httpx.Client

    def _client(**kw: Any) -> httpx.Client:
        kw.pop("follow_redirects", None)

        def _h(req: httpx.Request) -> httpx.Response:
            seen.append(req)
            return handler(req)

        return real_client(transport=httpx.MockTransport(_h), **kw)

    monkeypatch.setattr(probe.httpx, "Client", _client)
    return seen


def _healthy(req: httpx.Request) -> httpx.Response:
    url = str(req.url)
    if req.url.params.get("returnCountOnly") == "true":
        return httpx.Response(200, json={"count": 99})
    if url.endswith("/query") or "/query?" in url:
        return httpx.Response(200, json={"type": "FeatureCollection", "features": [
            {"type": "Feature", "id": 1, "properties": {"NAME": "n", "SMDI": "1"}, "geometry": None}]})
    return httpx.Response(200, json={"name": "Some SK layer"})


def test_ast_registry_matches_the_imported_one(probe) -> None:
    from app.services.public_geo.registry import SOURCES

    feeds, origin = probe.load_feeds(str(REGISTRY_PATH))
    assert "(ast)" in origin
    assert {f.source_id for f in feeds} == {s.source_id for s in SOURCES}
    by_id = {f.source_id: f for f in feeds}
    assert by_id["CA-SK-SMDI"].service_url.endswith("/Mineral_Exploration/MapServer/5")
    assert by_id["CA-SK-RESOURCE-POTENTIAL"].layer_index is None


def test_healthy_feeds_pass_and_parents_skip(probe, monkeypatch, capsys) -> None:
    seen = _install(monkeypatch, probe, _healthy)
    rc = probe.main(["--registry", str(REGISTRY_PATH)])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "ALL OK" in out
    assert "keys: NAME, SMDI" in out
    assert out.count(" SKIP ") == 3  # the three MapServer-root parent rows
    # Only Saskatchewan is contacted — BC is not synced, so it is not probed.
    assert {r.url.host for r in seen} == {"gis.saskatchewan.ca"}


def test_http_error_fails(probe, monkeypatch, capsys) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if "/Mineral_Exploration/MapServer/5" in str(req.url):
            return httpx.Response(403, text="Forbidden")
        return _healthy(req)

    _install(monkeypatch, probe, handler)
    assert probe.main(["--registry", str(REGISTRY_PATH)]) == 1
    out = capsys.readouterr().out
    assert "HTTP 403" in out
    assert "1 problem(s)" in out


def test_in_band_arcgis_error_and_zero_count_fail(probe, monkeypatch, capsys) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if "/Mineral_Exploration/MapServer/1" in str(req.url) and req.url.params.get("f") == "json" \
                and "query" not in str(req.url):
            return httpx.Response(200, json={"error": {"code": 400, "message": "Invalid layer"}})
        if "/Mineral_Exploration/MapServer/4/query" in str(req.url) \
                and req.url.params.get("returnCountOnly") == "true":
            return httpx.Response(200, json={"count": 0})
        return _healthy(req)

    _install(monkeypatch, probe, handler)
    assert probe.main(["--registry", str(REGISTRY_PATH)]) == 1
    out = capsys.readouterr().out
    assert "Invalid layer" in out
    assert "2 problem(s)" in out


def test_fits_an_ecs_run_task_override(monkeypatch) -> None:
    """The documented CloudShell invocation ships the script zlib+base64 in
    `python -c`; ECS rejects overrides over 8192 characters."""
    b64 = base64.b64encode(zlib.compress(PROBE_PATH.read_bytes(), 9)).decode()
    py = f"import base64,zlib;exec(zlib.decompress(base64.b64decode('{b64}')))"
    ovr = json.dumps({"containerOverrides": [{"name": "hatchet-worker", "command": ["python", "-c", py]}]})
    assert len(ovr) < 8192, len(ovr)
    # And the round trip still compiles as a module with its docstring.
    code = compile(zlib.decompress(base64.b64decode(b64)), "<probe>", "exec")
    mod = types.ModuleType("public_geo_probe_roundtrip")
    monkeypatch.setitem(sys.modules, mod.__name__, mod)
    exec(code, mod.__dict__)  # noqa: S102 — our own file
    assert mod.__doc__ and mod.__doc__.startswith("Probe every public-geo registry feed")
    assert callable(mod.__dict__["main"])
