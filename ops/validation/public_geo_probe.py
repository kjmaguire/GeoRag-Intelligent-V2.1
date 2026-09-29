"""Probe every public-geo registry feed from where the sync really runs.

Why: the registry's addressing (Saskatchewan ArcGIS REST layers on
gis.saskatchewan.ca — British Columbia is not synced, see the registry
docstring) is only proven by fetching it from the network the sync uses. A
sandbox without egress cannot tell a wrong layer number from a blocked host,
so this checks each feed from inside AWS. It is verification only — nothing
here is on the sync's runtime path, and it writes nothing.

For every feed it prints:

  source_id  OK/FAIL/SKIP  feature count  first feature's property keys

using the layer's own name (``?f=json``), a returnCountOnly count and one
GeoJSON feature's keys. Parent rows (a MapServer root with no layer index)
are SKIP. Read-only calls only.

Exit status is 1 if any feed FAILs.

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
syncs from.)
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


@dataclass
class Feed:
    source_id: str
    jurisdiction_code: str
    canonical_type: str
    service_url: str
    layer_index: int | None = None


def load_feeds(registry_path: str | None) -> tuple[list[Feed], str]:
    """An explicit --registry wins (a local venv may import some other checkout's app)."""
    fields = Feed.__dataclass_fields__
    if not registry_path:
        try:
            from app.services.public_geo.registry import SOURCES  # type: ignore[import-not-found]

            feeds = [Feed(**{k: getattr(s, k) for k in fields}) for s in SOURCES]
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
        out["keys"] = sorted((feats[0].get("properties") or {}).keys())
    return out


def verdict(r: dict[str, Any]) -> str:
    if "skip" in r:
        return "SKIP"
    return "FAIL" if "error" in r or not r.get("count") else "OK"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--registry", help="registry.py path when app is not importable")
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--json", action="store_true", help="also print one JSON line per feed")
    args = ap.parse_args(argv)

    feeds, origin = load_feeds(args.registry)
    print(f"registry: {origin} — {len(feeds)} feed(s)", flush=True)

    failures = 0
    with httpx.Client(timeout=args.timeout, follow_redirects=True,
                      headers={"User-Agent": "GeoRAG-public-geo-probe/1.0"}) as client:
        for f in sorted(feeds, key=lambda x: (x.jurisdiction_code, x.source_id)):
            r = probe_arcgis(client, f)
            v = verdict(r)
            failures += v == "FAIL"
            print(f"\n{f.source_id:<44} {v:<4} count={r.get('count')}", flush=True)
            for k in ("layer_name", "error", "skip"):
                if k in r:
                    print(f"    {k}: {r[k]}", flush=True)
            if r.get("keys"):
                print(f"    keys: {', '.join(r['keys'])}", flush=True)
            if args.json:
                print("JSON " + json.dumps({"source_id": f.source_id, "verdict": v, **r},
                                           default=str), flush=True)

    print(f"\n{'ALL OK' if not failures else f'{failures} problem(s)'}", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
