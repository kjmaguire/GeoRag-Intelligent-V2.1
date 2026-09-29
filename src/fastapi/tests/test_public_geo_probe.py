"""ops/validation/public_geo_probe.py against fake services.

The probe is the only way the BC WFS feeds get verified (from inside AWS), so
it is tested here the way the rerank/Cohere probes are: the real script,
fake upstreams (``httpx.MockTransport``), no network.
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


def _healthy(req: httpx.Request, *, catalogue_object: str | None = None) -> httpx.Response:
    url = str(req.url)
    p = req.url.params
    if "catalogue.data.gov.bc.ca" in url:
        slug = p["id"]
        obj = catalogue_object or {
            "minfile-mineral-occurrence-database": "WHSE_MINERAL_TENURE.MINFIL_MINERAL_OCCURRENCE",
            "mta-mineral-placer-and-coal-tenure-spatial-view": "WHSE_MINERAL_TENURE.MTA_ACQUIRED_TENURE_SVW",
            "bedrock-geology": "WHSE_MINERAL_TENURE.GEOL_BEDROCK_UNIT_POLY_SVW",
        }[slug]
        return httpx.Response(200, json={"success": True, "result": {"object_name": obj}})
    if p.get("REQUEST") == "GetCapabilities":
        names = "".join(
            f"<Name>pub:{n}</Name>"
            for n in (
                "WHSE_MINERAL_TENURE.MINFIL_MINERAL_OCCURRENCE",
                "WHSE_MINERAL_TENURE.MTA_ACQUIRED_TENURE_SVW",
                "WHSE_MINERAL_TENURE.GEOL_BEDROCK_UNIT_POLY_SVW",
            )
        )
        return httpx.Response(200, text=f"<WFS_Capabilities>{names}</WFS_Capabilities>",
                              headers={"content-type": "application/xml"})
    if p.get("REQUEST") == "GetFeature":
        props = {p["sortBy"]: "X1", "NAME": "n"}
        return httpx.Response(200, json={
            "type": "FeatureCollection", "numberMatched": 12,
            "features": [{"type": "Feature", "properties": props,
                          "geometry": {"type": "Point", "coordinates": [-121.0, 50.0]}}],
        })
    if p.get("returnCountOnly") == "true":
        return httpx.Response(200, json={"count": 99})
    if url.endswith("/query") or "/query?" in url:
        return httpx.Response(200, json={"type": "FeatureCollection", "features": [
            {"type": "Feature", "id": 1, "properties": {"MINFILE_NUMBER": "1"}, "geometry": None}]})
    return httpx.Response(200, json={"name": "MINFILE Mineral Occurrence"})


def test_ast_registry_matches_the_imported_one(probe) -> None:
    from app.services.public_geo.registry import SOURCES

    feeds, origin = probe.load_feeds(str(REGISTRY_PATH))
    assert "(ast)" in origin
    assert {f.source_id for f in feeds} == {s.source_id for s in SOURCES}
    by_id = {f.source_id: f for f in feeds}
    assert by_id["CA-BC-MTA-TENURE"].protocol == "wfs"
    assert by_id["CA-BC-MTA-TENURE"].catalogue_slug == "mta-mineral-placer-and-coal-tenure-spatial-view"


def test_healthy_bc_feeds_pass(probe, monkeypatch, capsys) -> None:
    seen = _install(monkeypatch, probe, _healthy)
    rc = probe.main(["--registry", str(REGISTRY_PATH), "--jurisdiction", "CA-BC"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "ALL OK" in out
    assert "in_capabilities: True" in out
    assert out.count("MATCH") == 4
    # Capabilities fetched exactly once, whatever the number of WFS feeds.
    assert sum(1 for r in seen if r.url.params.get("REQUEST") == "GetCapabilities") == 1
    gf = [r for r in seen if r.url.params.get("REQUEST") == "GetFeature"]
    assert {r.url.params["typeNames"] for r in gf} == {
        "pub:WHSE_MINERAL_TENURE.MINFIL_MINERAL_OCCURRENCE",
        "pub:WHSE_MINERAL_TENURE.MTA_ACQUIRED_TENURE_SVW",
        "pub:WHSE_MINERAL_TENURE.GEOL_BEDROCK_UNIT_POLY_SVW",
    }
    assert all(r.url.params["count"] == "1" for r in gf)


def test_catalogue_mismatch_fails(probe, monkeypatch, capsys) -> None:
    _install(monkeypatch, probe, lambda r: _healthy(r, catalogue_object="WHSE_SOMETHING.ELSE"))
    rc = probe.main(["--registry", str(REGISTRY_PATH), "--jurisdiction", "CA-BC"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "MISMATCH" in out


def test_missing_type_and_swapped_axes_fail(probe, monkeypatch, capsys) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.params.get("REQUEST") == "GetCapabilities":
            return httpx.Response(200, text="<WFS_Capabilities></WFS_Capabilities>")
        if req.url.params.get("REQUEST") == "GetFeature":
            return httpx.Response(200, json={"type": "FeatureCollection", "numberMatched": 3, "features": [
                {"type": "Feature", "properties": {"OTHER": 1},
                 "geometry": {"type": "Point", "coordinates": [50.0, -121.0]}}]})
        return _healthy(req)

    _install(monkeypatch, probe, handler)
    rc = probe.main(["--registry", str(REGISTRY_PATH), "--jurisdiction", "CA-BC"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "in_capabilities: False" in out
    assert "id_field_present: False" in out
    assert "lon_lat_ok: False" in out


def test_ogc_exception_is_a_fail(probe, monkeypatch, capsys) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.params.get("REQUEST") == "GetFeature":
            return httpx.Response(400, text="<ows:ExceptionReport>bad</ows:ExceptionReport>")
        return _healthy(req)

    _install(monkeypatch, probe, handler)
    assert probe.main(["--registry", str(REGISTRY_PATH), "--jurisdiction", "CA-BC"]) == 1
    assert "HTTP 400" in capsys.readouterr().out


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
    assert mod.__doc__ and mod.__doc__.startswith("Probe every BC/SK")
    assert callable(mod.__dict__["main"])
