"""Probe every BC/SK public-geo registry feed from where the sync really runs.

Why: the BC feeds added 2026-09-29 (DataBC WFS, addressed by BC Geographic
Warehouse object name) and CA-BC-MINFILE's ArcGIS layer number were written
from a sandbox that could not reach openmaps.gov.bc.ca, delivery.maps.gov.bc.ca,
catalogue.data.gov.bc.ca or gis.saskatchewan.ca. This checks each one from
inside AWS. It is verification only — nothing here is on the sync's runtime
path, and it writes nothing.

For every feed (default jurisdictions CA-BC and CA-SK) it prints:

  source_id  OK/FAIL/SKIP  feature count  first feature's property keys

  * WFS feeds: one GetCapabilities fetch, asserting each registry typeName
    (``pub:<BCGW object>``) is PRESENT; then a count=1 GetFeature with the
    feed's own sortBy / CQL_FILTER, reporting numberMatched, the property
    keys, whether the declared id_field exists, and a lon/lat sanity check.
  * ArcGIS feeds: the layer's own name (``?f=json``), a returnCountOnly
    count, and one GeoJSON feature's keys. Parent rows with no layer are SKIP.
  * BC catalogue check: ``package_show?id=<catalogue_slug>`` on the keyless
    CKAN read API, printing the returned ``object_name`` next to the
    registry's ``bcgw_object_name`` — MATCH or MISMATCH. Read-only calls only.

Exit status is 1 if any feed FAILs or any catalogue object_name mismatches.

Registry source: imports ``app.services.public_geo.registry`` when run inside
the fastapi / hatchet-worker image (cwd /app); otherwise parses the registry
file with ``ast`` (``--registry PATH``), so no repo import is needed.

Dependencies: stdlib + httpx (present in the image).

Local run (from a repo checkout, where the hosts are reachable)::

    python ops/validation/public_geo_probe.py --registry src/fastapi/app/services/public_geo/registry.py

From CloudShell — run it in a one-off hatchet-worker task (same image, same
egress as the sync). The script travels zlib+base64 inside ``python -c`` so
nothing needs to be copied into the image; ECS caps overrides at 8192 chars
and this compresses to well under that::

    CLUSTER=georag
    NET=$(aws ecs describe-services --cluster $CLUSTER --services fastapi \\
      --query 'services[0].networkConfiguration.awsvpcConfiguration' --output json)
    SUBNETS=$(echo "$NET" | jq -r '.subnets | join(",")')
    SG=$(echo "$NET" | jq -r '.securityGroups | join(",")')
    B64=$(python3 -c "import base64,zlib;print(base64.b64encode(zlib.compress(open('ops/validation/public_geo_probe.py','rb').read(),9)).decode())")
    OVR=$(jq -n --arg py "import base64,zlib;exec(zlib.decompress(base64.b64decode('$B64')))" \\
      '{containerOverrides:[{name:"hatchet-worker",command:["python","-c",$py]}]}')
    echo "overrides: ${#OVR}/8192 chars"
    ARN=$(aws ecs run-task --cluster $CLUSTER --task-definition georag-hatchet-worker \\
      --launch-type FARGATE \\
      --network-configuration "awsvpcConfiguration={subnets=[$SUBNETS],securityGroups=[$SG],assignPublicIp=DISABLED}" \\
      --overrides "$OVR" --query 'tasks[0].taskArn' --output text)
    aws ecs wait tasks-stopped --cluster $CLUSTER --tasks "$ARN"
    aws ecs describe-tasks --cluster $CLUSTER --tasks "$ARN" --query 'tasks[0].containers[0].exitCode'
    aws logs filter-log-events --log-group-name /ecs/georag \\
      --log-stream-names "hatchet-worker/hatchet-worker/${ARN##*/}" \\
      --query 'events[].message' --output text | tr '\\t' '\\n'

(Inside the task the working directory is /app, so the registry import path
is taken and the probe checks exactly the addressing the deployed image
syncs from.) Once a feed passes, flip its ``verified=False`` to True in the
registry and drop the [UNVERIFIED] markers.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

WFS_URL = "https://openmaps.gov.bc.ca/geo/ows"
CKAN_URL = "https://catalogue.data.gov.bc.ca/api/3/action/package_show"
BC_LON, BC_LAT = (-140.0, -113.0), (47.0, 61.0)


@dataclass
class Feed:
    source_id: str
    jurisdiction_code: str
    canonical_type: str
    service_url: str
    layer_index: int | None = None
    protocol: str = "arcgis"
    bcgw_object_name: str | None = None
    catalogue_slug: str | None = None
    id_field: str | None = None
    cql_filter: str | None = None
    verified: bool = True


def load_feeds(registry_path: str | None) -> tuple[list[Feed], str]:
    """An explicit --registry wins (a local venv may import some other checkout's app)."""
    fields = Feed.__dataclass_fields__
    if not registry_path:
        try:
            from app.services.public_geo.registry import SOURCES  # type: ignore[import-not-found]

            feeds = [Feed(**{k: getattr(s, k) for k in fields if hasattr(s, k)}) for s in SOURCES]
            return feeds, "app.services.public_geo.registry (import)"
        except ImportError:
            pass
    path = Path(registry_path or Path(__file__).resolve().parents[2]
                / "src/fastapi/app/services/public_geo/registry.py")
    feeds = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "PublicGeoSource":
            kw = {k.arg: ast.literal_eval(k.value) for k in node.keywords if k.arg in fields}
            feeds.append(Feed(**kw))
    return feeds, f"{path} (ast)"


def _get(client: httpx.Client, url: str, params: dict[str, Any]) -> tuple[Any, str | None]:
    """(parsed body, error) — body is dict for JSON, str otherwise."""
    try:
        r = client.get(url, params=params)
    except httpx.HTTPError as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if r.status_code >= 400:
        return None, f"HTTP {r.status_code} {r.text[:160]!r}"
    try:
        return r.json(), None
    except ValueError:
        return r.text, None


def _keys(feature: dict[str, Any]) -> list[str]:
    return sorted((feature.get("properties") or {}).keys())


def _first_xy(geom: Any) -> tuple[float, float] | None:
    c = (geom or {}).get("coordinates")
    while isinstance(c, list) and c and isinstance(c[0], list):
        c = c[0]
    return (float(c[0]), float(c[1])) if isinstance(c, list) and len(c) >= 2 else None


def probe_wfs(client: httpx.Client, f: Feed, caps: str | None) -> dict[str, Any]:
    obj = f.bcgw_object_name or ""
    tn = obj if ":" in obj else f"pub:{obj}"
    out: dict[str, Any] = {"type_name": tn}
    if caps is not None:
        out["in_capabilities"] = (f">{tn}<" in caps) or (f">{obj}<" in caps)
    params = {"SERVICE": "WFS", "VERSION": "2.0.0", "REQUEST": "GetFeature", "typeNames": tn,
              "outputFormat": "application/json", "srsName": "EPSG:4326", "count": 1}
    if f.id_field:
        params["sortBy"] = f.id_field
    if f.cql_filter:
        params["CQL_FILTER"] = f.cql_filter
    body, err = _get(client, f.service_url or WFS_URL, params)
    if err or not isinstance(body, dict):
        out["error"] = err or f"not JSON: {str(body)[:200]!r}"
        return out
    feats = body.get("features") or []
    out["count"] = body.get("numberMatched", body.get("totalFeatures"))
    if feats:
        keys = _keys(feats[0])
        out["keys"] = keys
        if f.id_field:
            out["id_field_present"] = f.id_field.lower() in {k.lower() for k in keys}
        xy = _first_xy(feats[0].get("geometry"))
        out["first_xy"] = xy
        if xy and f.jurisdiction_code == "CA-BC":
            out["lon_lat_ok"] = BC_LON[0] <= xy[0] <= BC_LON[1] and BC_LAT[0] <= xy[1] <= BC_LAT[1]
    return out


def probe_arcgis(client: httpx.Client, f: Feed) -> dict[str, Any]:
    base = f.service_url.rstrip("/")
    if not base.rsplit("/", 1)[-1].isdigit():
        if f.layer_index is None:
            return {"skip": "MapServer root with no layer index (parent row)"}
        base = f"{base}/{f.layer_index}"
    out: dict[str, Any] = {}
    meta, err = _get(client, base, {"f": "json"})
    if err or not isinstance(meta, dict) or meta.get("error"):
        out["error"] = err or str((meta or {}).get("error") if isinstance(meta, dict) else meta)[:200]
        return out
    out["layer_name"] = meta.get("name")
    cnt, err = _get(client, f"{base}/query", {"where": "1=1", "returnCountOnly": "true", "f": "json"})
    if isinstance(cnt, dict):
        out["count"] = cnt.get("count")
    one, err = _get(client, f"{base}/query", {"where": "1=1", "outFields": "*", "outSR": 4326,
                                             "f": "geojson", "resultRecordCount": 1})
    if err or not isinstance(one, dict) or one.get("error"):
        out["error"] = err or str(one.get("error") if isinstance(one, dict) else one)[:200]
        return out
    feats = one.get("features") or []
    if feats:
        out["keys"] = _keys(feats[0])
    return out


def probe_catalogue(client: httpx.Client, f: Feed) -> dict[str, Any]:
    body, err = _get(client, CKAN_URL, {"id": f.catalogue_slug})
    if err or not isinstance(body, dict) or not body.get("success"):
        return {"error": err or f"CKAN: {str(body)[:200]}"}
    obj = (body.get("result") or {}).get("object_name")
    return {"object_name": obj, "match": bool(obj) and obj == f.bcgw_object_name}


def verdict(f: Feed, r: dict[str, Any]) -> str:
    if "skip" in r:
        return "SKIP"
    bad = (
        "error" in r
        or not r.get("count")
        or r.get("in_capabilities") is False
        or r.get("id_field_present") is False
        or r.get("lon_lat_ok") is False
    )
    return "FAIL" if bad else "OK"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--registry", help="registry.py path when app is not importable")
    ap.add_argument("--jurisdiction", action="append", help="default: CA-BC and CA-SK")
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--json", action="store_true", help="also print one JSON line per feed")
    args = ap.parse_args(argv)

    feeds, origin = load_feeds(args.registry)
    wanted = set(args.jurisdiction or ["CA-BC", "CA-SK"])
    feeds = [f for f in feeds if f.jurisdiction_code in wanted]
    print(f"registry: {origin} — {len(feeds)} feed(s) in {sorted(wanted)}", flush=True)

    failures = 0
    with httpx.Client(timeout=args.timeout, follow_redirects=True,
                      headers={"User-Agent": "GeoRAG-public-geo-probe/1.0"}) as client:
        caps: str | None = None
        if any(f.protocol == "wfs" for f in feeds):
            body, err = _get(client, WFS_URL, {"SERVICE": "WFS", "VERSION": "2.0.0",
                                               "REQUEST": "GetCapabilities"})
            if err:
                print(f"WFS GetCapabilities FAILED: {err}", flush=True)
            else:
                caps = body if isinstance(body, str) else json.dumps(body)
                print(f"WFS GetCapabilities OK ({len(caps):,} bytes)", flush=True)

        for f in sorted(feeds, key=lambda x: (x.jurisdiction_code, x.source_id)):
            r = probe_wfs(client, f, caps) if f.protocol == "wfs" else probe_arcgis(client, f)
            v = verdict(f, r)
            failures += v == "FAIL"
            flag = "" if f.verified else " [UNVERIFIED in registry]"
            print(f"\n{f.source_id:<44} {v:<4} count={r.get('count')} {f.protocol}{flag}", flush=True)
            for k in ("type_name", "in_capabilities", "layer_name", "id_field_present",
                      "first_xy", "lon_lat_ok", "error", "skip"):
                if k in r:
                    print(f"    {k}: {r[k]}", flush=True)
            if r.get("keys"):
                print(f"    keys: {', '.join(r['keys'])}", flush=True)
            if f.catalogue_slug:
                c = probe_catalogue(client, f)
                if "error" in c:
                    failures += 1  # unconfirmed is not confirmed
                    print(f"    catalogue[{f.catalogue_slug}]: ERROR {c['error']}", flush=True)
                else:
                    tag = "MATCH" if c["match"] else "MISMATCH"
                    failures += not c["match"]
                    print(f"    catalogue[{f.catalogue_slug}]: object_name={c['object_name']} "
                          f"registry={f.bcgw_object_name} {tag}", flush=True)
                r["catalogue"] = c
            if args.json:
                print("JSON " + json.dumps({"source_id": f.source_id, "verdict": v, **r},
                                           default=str), flush=True)

    print(f"\n{'ALL OK' if not failures else f'{failures} problem(s)'}", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
