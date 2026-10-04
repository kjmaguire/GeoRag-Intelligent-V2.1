"""ADR-0025 Embed 5 cutover, one step per run, from inside the VPC.

Runs as a one-off ECS task on the Hatchet worker's task definition (the Python
image, the Postgres and Qdrant settings, COHERE_API_KEY, the backups bucket),
started by `.github/workflows/embed5-cutover.yml`, which prints what this
writes to stdout. Nothing here needs an operator shell inside AWS, which is the
point: production RDS and Qdrant are in private subnets, ECS Exec is off and
there is no bastion.

Each ``--step`` is one migration step of ADR-0025 ("Migration mechanics"):

  probe      step 1 - POST /v2/embed with the production key: document and
             query input types, both image request shapes, output_dimension
             honoured, the per-request input-count limit, rate-limit headers;
             then the real adapter (``build_cohere_embedding``) end to end.
             Writes nothing. Run with EMBEDDING_BACKEND=cohere in the task's
             environment (the workflow sets it) so the adapter under test is
             the Cohere one even before the Terraform apply.
  questions  step 6's "before" and "after": a fixed question set through
             ``POST /internal/queries`` for the named projects, recording per
             question whether the stream completed, refused, and how many
             citation frames it carried. Answer text is NOT recorded. Run once
             before ``reset`` and once after ``verify`` passes, and compare.
  snapshot   step 3 - a Qdrant snapshot of every collection, uploaded to the
             backups bucket under ``_ops/qdrant-snapshots/<ts>-before-embed5/``.
             The printed prefix is what ``reset`` demands.
  reset      step 4 - refuses unless THIS task's EMBEDDING_BACKEND is
             ``cohere``, the snapshot prefix names objects in the backups
             bucket, and ``--confirm reset-all-embeddings`` was typed. Then
             ``reset_embeddings_for_reencode.py --all``: every Qdrant point
             deleted, ``embedding_id = NULL`` on every passage, workspace by
             workspace under RLS. The embed sweep (``embed_pending_passages``,
             every 10 minutes) re-encodes from there.
  verify     step 6's counts: points in Qdrant, points tagged with the new
             model, points lacking it, image points; passages with and without
             an embedding_id per workspace. Exit 0 when complete and
             consistent, 3 while the sweep is still running, 1 when the two
             stores disagree in a way the sweep cannot fix.

Output contract (the workflow reads these markers, like project_data_diagnostics):

    =====BEGIN EMBED5_CUTOVER_SUMMARY_MD=====   markdown for the step summary
    =====END EMBED5_CUTOVER_SUMMARY_MD=====
    =====BEGIN EMBED5_CUTOVER_JSON=====         the full report
    =====END EMBED5_CUTOVER_JSON=====

Secrets never reach the report: the API key is only ever sent as a header,
and ``probe`` records its length, not its value.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import struct
import sys
import time
import zlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# `python3 /app/scripts/ops/<this file>` puts scripts/ops on sys.path, not /app.
_APP_ROOT = Path(__file__).resolve().parents[2]
if str(_APP_ROOT) not in sys.path:
    sys.path.insert(0, str(_APP_ROOT))

BEGIN_SUM = "=====BEGIN EMBED5_CUTOVER_SUMMARY_MD====="
END_SUM = "=====END EMBED5_CUTOVER_SUMMARY_MD====="
BEGIN_JSON = "=====BEGIN EMBED5_CUTOVER_JSON====="
END_JSON = "=====END EMBED5_CUTOVER_JSON====="

CONFIRM_PHRASE = "reset-all-embeddings"
COLLECTION = "georag_chunks"
SNAPSHOT_ROOT = "_ops/qdrant-snapshots"

#: Exit codes. 3 is "not yet": the sweep is still re-encoding.
EXIT_OK, EXIT_ERROR, EXIT_INCOMPLETE = 0, 1, 3

#: The adapter's chunk size; the probe sends this many and one more.
EMBED_ADAPTER_CHUNK = 96

QUESTIONS = (
    "What commodities are the target of exploration on this project?",
    "Which drill holes returned the highest gold grades, and over what intervals?",
    "Summarize the most recent drilling program: number of holes and total metres.",
    "What is the dominant lithology logged in the drill holes?",
    "What structural controls on mineralization are described?",
    "What QA/QC procedures were applied to the assay results?",
)


def _emit(summary_md: str, report: dict[str, Any]) -> None:
    print(BEGIN_SUM)
    print(summary_md.rstrip())
    print(END_SUM)
    print(BEGIN_JSON)
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    print(END_JSON, flush=True)


def _now() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _err(exc: BaseException) -> dict[str, Any]:
    return {"type": type(exc).__name__, "message": str(exc)[:400]}


# ---------------------------------------------------------------------------
# Postgres under RLS (the task runs as georag_app, NOBYPASSRLS)
# ---------------------------------------------------------------------------


def _pg_dsn() -> str:
    return (
        f"postgresql://{os.environ.get('POSTGRES_USER', 'georag')}:"
        f"{os.environ.get('POSTGRES_PASSWORD', '')}@"
        f"{os.environ.get('POSTGRES_DIRECT_HOST', os.environ.get('POSTGRES_HOST', 'postgresql'))}:"
        f"{os.environ.get('POSTGRES_DIRECT_PORT', os.environ.get('POSTGRES_PORT', '5432'))}/"
        f"{os.environ.get('POSTGRES_DB', 'georag')}"
    )


async def _connect():
    import asyncpg

    return await asyncpg.connect(_pg_dsn(), statement_cache_size=0)


async def _bind(pg, workspace_id: str | None) -> None:
    if workspace_id is None:
        await pg.execute("SELECT set_config('app.workspace_id', '', false)")
        return
    from app.db import bind_workspace_scope

    await bind_workspace_scope(pg, workspace_id=workspace_id, site="embed5_cutover", is_local=False)


async def _scopes(pg) -> list[str | None]:
    """``[None]`` when the role sees passages unscoped, else every workspace id."""
    await _bind(pg, None)
    if await pg.fetchval("SELECT COUNT(*) FROM silver.document_passages"):
        return [None]
    rows = await pg.fetch(
        "SELECT workspace_id::text AS workspace_id FROM silver.workspaces ORDER BY workspace_id"
    )
    return [r["workspace_id"] for r in rows]


_PASSAGE_COUNTS_SQL = """
SELECT COUNT(*)                                                       AS total,
       COUNT(*) FILTER (WHERE embedding_id IS NULL)                   AS unembedded,
       COUNT(*) FILTER (WHERE embedding_id IS NOT NULL)               AS embedded,
       COUNT(*) FILTER (WHERE modality = 'image')                     AS images,
       COUNT(*) FILTER (WHERE modality = 'image' AND embedding_id IS NOT NULL) AS images_embedded
  FROM silver.document_passages
"""


async def passage_counts(pg) -> dict[str, Any]:
    scopes = await _scopes(pg)
    totals = {"total": 0, "unembedded": 0, "embedded": 0, "images": 0, "images_embedded": 0}
    per_scope: dict[str, dict[str, int]] = {}
    for scope in scopes:
        await _bind(pg, scope)
        row = await pg.fetchrow(_PASSAGE_COUNTS_SQL)
        counts = {k: int(row[k]) for k in totals}
        per_scope[scope or "unscoped"] = counts
        for k in totals:
            totals[k] += counts[k]
    return {"scopes": len(scopes), "totals": totals, "per_workspace": per_scope}


# ---------------------------------------------------------------------------
# Qdrant
# ---------------------------------------------------------------------------


def _qdrant():
    from qdrant_client import AsyncQdrantClient

    from app.services.qdrant_conn import qdrant_client_kwargs

    return AsyncQdrantClient(**qdrant_client_kwargs())


async def point_counts(model_tag: str) -> dict[str, Any]:
    from qdrant_client import models as qm

    qc = _qdrant()
    try:
        total = (await qc.count(collection_name=COLLECTION, exact=True)).count
        tagged = (
            await qc.count(
                collection_name=COLLECTION,
                exact=True,
                count_filter=qm.Filter(
                    must=[qm.FieldCondition(key="embed_model", match=qm.MatchValue(value=model_tag))]
                ),
            )
        ).count
        images = (
            await qc.count(
                collection_name=COLLECTION,
                exact=True,
                count_filter=qm.Filter(
                    must=[qm.FieldCondition(key="modality", match=qm.MatchValue(value="image"))]
                ),
            )
        ).count
    finally:
        await qc.close()
    return {"total": total, "tagged_with_model": tagged, "lacking_model_tag": total - tagged, "images": images}


def verdict(points: dict[str, int], passages: dict[str, int]) -> tuple[int, str]:
    """ADR-0025 step 6 as a decision.

    Complete and consistent -> 0. The sweep still has work (passages without
    an embedding_id, and nothing in the old space) -> 3. Anything the sweep
    cannot fix on its own -> 1: a point in the old space, or more points than
    embedded passages (orphans from before the delete), or image passages
    embedded but no image point.
    """
    if points["lacking_model_tag"]:
        return EXIT_ERROR, (
            f"{points['lacking_model_tag']} point(s) lack embed_model={points.get('model')!r}: "
            "the collection holds two vector spaces. Re-run reset before continuing."
        )
    if points["total"] > passages["embedded"]:
        return EXIT_ERROR, (
            f"{points['total']} points but only {passages['embedded']} embedded passages: "
            "orphan points survived the delete."
        )
    if passages["images_embedded"] and not points["images"]:
        return EXIT_ERROR, (
            f"{passages['images_embedded']} image passages are marked embedded but no point "
            "carries modality=image."
        )
    if passages["unembedded"]:
        return EXIT_INCOMPLETE, (
            f"sweep in progress: {passages['unembedded']} of {passages['total']} passages still "
            f"to embed; {points['total']} points so far, all tagged with the new model."
        )
    if points["total"] != passages["embedded"]:
        return EXIT_ERROR, (
            f"every passage is marked embedded ({passages['embedded']}) but Qdrant holds "
            f"{points['total']} points."
        )
    return EXIT_OK, (
        f"complete: {points['total']} points, all tagged with the new model, "
        f"{points['images']} of them images; 0 passages left to embed."
    )


# ---------------------------------------------------------------------------
# probe
# ---------------------------------------------------------------------------


def _generated_png(width: int = 256, height: int = 256) -> bytes:
    """A small non-blank grayscale PNG, stdlib only."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    rows = b"".join(
        b"\x00" + bytes(((x * 3) ^ (y * 5)) & 0xFF for x in range(width)) for y in range(height)
    )
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


class _Refused(Exception):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status
        self.body = body[:300]


def _embed_call(client, url: str, body: dict[str, Any]) -> tuple[dict[str, Any], float, Any]:
    started = time.monotonic()
    response = client.post(url, content=json.dumps(body).encode())
    elapsed = time.monotonic() - started
    if response.status_code >= 300:
        raise _Refused(response.status_code, response.text)
    return response.json(), elapsed, response


def _rate_limit_headers(response) -> dict[str, str]:
    out = {}
    for name, value in response.headers.items():
        lowered = name.lower()
        if "ratelimit" in lowered or "rate-limit" in lowered or lowered == "retry-after":
            out[lowered] = str(value)[:80]
    return out


def _refusal(exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, _Refused):
        return {"status": exc.status, "body": exc.body}
    return _err(exc)


def step_probe() -> tuple[int, str, dict[str, Any]]:
    import httpx

    from app.config import settings
    from app.services import embedding as emb

    key = (settings.COHERE_API_KEY or "").strip()
    if not key:
        return EXIT_ERROR, "COHERE_API_KEY is empty in this task", {"error": "COHERE_API_KEY unset"}

    url = settings.COHERE_BASE_URL.rstrip("/") + "/v2/embed"
    model, dim = emb.COHERE_EMBED_MODEL, emb.COHERE_EMBED_DIMENSION
    report: dict[str, Any] = {
        "model": model,
        "query_model": emb.COHERE_EMBED_QUERY_MODEL,
        "output_dimension_requested": dim,
        "key_present": True,
        "key_length": len(key),
        "embedding_backend_in_task": os.environ.get("EMBEDDING_BACKEND"),
        "raw": {},
        "adapter": {},
    }
    raw = report["raw"]

    def body(input_type: str, **inputs: Any) -> dict[str, Any]:
        return {
            "model": model,
            "input_type": input_type,
            "embedding_types": ["float"],
            "output_dimension": dim,
            **inputs,
        }

    headers = {
        "authorization": f"bearer {key}",
        "content-type": "application/json",
        "accept": "application/json",
    }
    with httpx.Client(headers=headers, timeout=120.0) as client:
        # document
        try:
            payload, elapsed, response = _embed_call(
                client, url, body("search_document", texts=["quartz-carbonate vein hosted gold"])
            )
            rows = payload["embeddings"]["float"]
            raw["document"] = {
                "latency_s": round(elapsed, 3),
                "top_level_keys": sorted(payload),
                "dimension": len(rows[0]),
                "dimension_honoured": len(rows[0]) == dim,
                "rate_limit_headers": _rate_limit_headers(response),
            }
        except Exception as exc:  # noqa: BLE001
            raw["document"] = {"error": _refusal(exc)}
        # query
        try:
            payload, elapsed, _ = _embed_call(client, url, body("search_query", texts=[QUESTIONS[0]]))
            rows = payload["embeddings"]["float"]
            raw["query"] = {"latency_s": round(elapsed, 3), "dimension": len(rows[0])}
        except Exception as exc:  # noqa: BLE001
            raw["query"] = {"error": _refusal(exc)}
        # image, both shapes in the order the adapter tries them
        uri = "data:image/png;base64," + base64.b64encode(_generated_png()).decode("ascii")
        shapes = (
            ("images", body("image", images=[uri])),
            ("inputs", body("image", inputs=[{"content": [{"type": "image_url", "image_url": {"url": uri}}]}])),
        )
        image: dict[str, Any] = {"attempts": {}}
        for name, shape_body in shapes:
            try:
                payload, elapsed, _ = _embed_call(client, url, shape_body)
                rows = payload["embeddings"]["float"]
                image.update(shape_accepted=name, latency_s=round(elapsed, 3), dimension=len(rows[0]))
                break
            except Exception as exc:  # noqa: BLE001
                image["attempts"][name] = _refusal(exc)
                if not (isinstance(exc, _Refused) and exc.status in (400, 422)):
                    break
        raw["image"] = image
        # input-count limit: the adapter's chunk and one more
        limit: dict[str, Any] = {"adapter_chunk_size": EMBED_ADAPTER_CHUNK}
        for count in (EMBED_ADAPTER_CHUNK, EMBED_ADAPTER_CHUNK + 1):
            texts = [f"assay interval {i}: {0.1 * i:.1f} g/t Au over 1.5 m" for i in range(count)]
            try:
                payload, elapsed, _ = _embed_call(client, url, body("search_document", texts=texts))
                limit[str(count)] = {"accepted": True, "vectors_back": len(payload["embeddings"]["float"])}
            except Exception as exc:  # noqa: BLE001
                limit[str(count)] = {"accepted": False, **_refusal(exc)}
        at = limit[str(EMBED_ADAPTER_CHUNK)].get("accepted")
        over = limit[str(EMBED_ADAPTER_CHUNK + 1)].get("accepted")
        limit["adapter_chunk_size_ok"] = bool(at) if at is not None else None
        limit["reading"] = (
            "exact" if at and not over else "conservative" if at and over else "TOO LARGE" if at is False else "undetermined"
        )
        raw["input_limit"] = limit

    # the real adapter, end to end
    adapter = report["adapter"]
    try:
        model_obj = emb.build_cohere_embedding()
        doc = model_obj.encode(["Drill hole MAD-21-003 intersected 12 m of quartz-carbonate veining."])
        query = model_obj.embed_query(QUESTIONS[1])
        img = model_obj.embed_image(_generated_png())
        adapter.update(
            model_name=model_obj.model_name,
            query_model_name=model_obj.query_model_name,
            document_dimension=int(doc.shape[-1]),
            query_dimension=int(query.shape[-1]),
            image_dimension=int(img.shape[-1]),
            reported_dimension=model_obj.get_sentence_embedding_dimension(),
        )
        adapter["ok"] = all(adapter[k] == dim for k in ("document_dimension", "query_dimension", "image_dimension"))
    except Exception as exc:  # noqa: BLE001
        adapter["ok"] = False
        adapter["error"] = _err(exc)

    ok = (
        raw["document"].get("dimension_honoured") is True
        and "dimension" in raw["query"]
        and "shape_accepted" in raw["image"]
        and limit["adapter_chunk_size_ok"] is True
        and adapter.get("ok") is True
    )
    summary = "\n".join(
        [
            "| check | result |",
            "|---|---|",
            f"| `POST /v2/embed` search_document | {raw['document']} |",
            f"| search_query | {raw['query']} |",
            f"| image shape accepted | {raw['image'].get('shape_accepted', 'NONE')} |",
            f"| input-count limit at {EMBED_ADAPTER_CHUNK} | {limit['reading']} |",
            f"| adapter (`build_cohere_embedding`) | {'ok' if adapter.get('ok') else adapter} |",
            "",
            f"**{'PROBE OK' if ok else 'PROBE FAILED'}** - model `{model}`, {dim} dims.",
        ]
    )
    return (EXIT_OK if ok else EXIT_ERROR), summary, report


# ---------------------------------------------------------------------------
# questions (before / after)
# ---------------------------------------------------------------------------

_PROJECT_SQL = """
SELECT p.project_id::text AS project_id, p.workspace_id::text AS workspace_id, p.slug
  FROM silver.projects p
 WHERE p.slug = $1 OR p.slug ~ ('^' || $1 || '-[a-z0-9]{8}$')
 ORDER BY (p.slug = $1) DESC, p.slug
 LIMIT 10
"""


def _mint(project_id: str, workspace_id: str) -> str:
    """Mirror app/Services/FastApiJwtMinter.php (and step5_answer_path.py)."""
    import jwt

    now = int(time.time())
    claims = {
        "iss": "georag-laravel",
        "aud": "georag-fastapi",
        "sub": "0",
        "project_id": project_id,
        "workspace_id": workspace_id,
        "roles": ["member"],
        "iat": now,
        "exp": now + 60,
    }
    return jwt.encode(
        claims,
        os.environ["FASTAPI_SERVICE_KEY"],
        algorithm="HS256",
        headers={"kid": os.environ.get("FASTAPI_SERVICE_KEY_KID", "primary")},
    )


def parse_sse(raw: str) -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = []
    for block in raw.split("\n\n"):
        event, data = None, None
        for line in block.splitlines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data = line[5:].strip()
        if event is None:
            continue
        try:
            out.append((event, json.loads(data) if data else {}))
        except json.JSONDecodeError:
            out.append((event, {"_unparsed": True}))
    return out


def summarize_stream(frames: list[tuple[str, dict]]) -> dict[str, Any]:
    """Counts only: no answer text leaves the task."""
    kinds = [e for e, _ in frames]
    out: dict[str, Any] = {
        "frames": len(frames),
        "deltas": sum(1 for k in kinds if k == "delta"),
        "citations": sum(1 for k in kinds if k == "citation"),
        "terminal": kinds[-1] if kinds else None,
        "refused": False,
        "refusal_code": None,
    }
    for event, data in frames:
        if event == "failed":
            out["refused"] = True
            out["refusal_code"] = data.get("code") or data.get("error") or "failed"
        elif event == "completed" and data.get("refusal_payload"):
            out["refused"] = True
            payload = data["refusal_payload"]
            out["refusal_code"] = payload.get("reason") or payload.get("code") if isinstance(payload, dict) else "refused"
    return out


async def step_questions(slugs: list[str], label: str) -> tuple[int, str, dict[str, Any]]:
    import httpx

    base = os.environ.get("FASTAPI_INTERNAL_URL", "").rstrip("/")
    if not base or "localhost" in base or "127.0.0.1" in base:
        return EXIT_ERROR, f"FASTAPI_INTERNAL_URL is unset or loopback ({base!r})", {"error": "FASTAPI_INTERNAL_URL"}
    if not os.environ.get("FASTAPI_SERVICE_KEY"):
        return EXIT_ERROR, "FASTAPI_SERVICE_KEY is unset", {"error": "FASTAPI_SERVICE_KEY"}

    pg = await _connect()
    try:
        await _bind(pg, None)
        projects = []
        for slug in slugs:
            rows = await pg.fetch(_PROJECT_SQL, slug)
            if rows and (rows[0]["slug"] == slug or len(rows) == 1):
                projects.append(dict(rows[0]))
            else:
                projects.append({"slug": slug, "error": "not found" if not rows else f"ambiguous: {[r['slug'] for r in rows]}"})
    finally:
        await pg.close()

    report: dict[str, Any] = {"label": label, "at": _now(), "projects": []}
    lines = ["| project | question | terminal | refused | citations | deltas | s |", "|---|---|---|---|---|---|---|"]
    rc = EXIT_OK
    with httpx.Client(timeout=240.0) as client:
        for project in projects:
            entry: dict[str, Any] = {"slug": project["slug"], "results": []}
            report["projects"].append(entry)
            if "error" in project:
                entry["error"] = project["error"]
                lines.append(f"| {project['slug']} | - | {project['error']} | | | | |")
                rc = EXIT_ERROR
                continue
            for i, question in enumerate(QUESTIONS):
                started = time.time()
                try:
                    response = client.post(
                        base + "/internal/queries",
                        content=json.dumps({"query": question, "project_id": project["project_id"]}).encode(),
                        headers={
                            "Content-Type": "application/json",
                            "X-Service-Key": os.environ["FASTAPI_SERVICE_KEY"],
                            "Authorization": f"Bearer {_mint(project['project_id'], project['workspace_id'])}",
                            "Accept": "text/event-stream",
                        },
                    )
                    result = {"q": i, "http": response.status_code, **summarize_stream(parse_sse(response.text))}
                except Exception as exc:  # noqa: BLE001
                    result = {"q": i, "error": _err(exc)}
                result["elapsed_s"] = round(time.time() - started, 1)
                entry["results"].append(result)
                lines.append(
                    f"| {project['slug']} | Q{i} | {result.get('terminal') or result.get('error', {}).get('type')} | "
                    f"{result.get('refused')} | {result.get('citations')} | {result.get('deltas')} | {result['elapsed_s']} |"
                )
    return rc, "\n".join(lines), report


# ---------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------


def step_snapshot() -> tuple[int, str, dict[str, Any]]:
    import boto3
    import httpx

    from app.services.qdrant_conn import qdrant_client_kwargs

    kw = qdrant_client_kwargs()
    base = f"{'https' if kw.get('https') else 'http'}://{kw['host']}:{kw['port']}"
    headers = {"api-key": kw["api_key"]} if kw.get("api_key") else {}
    bucket = os.environ.get("AWS_BUCKET_BACKUPS", "")
    if not bucket:
        return EXIT_ERROR, "AWS_BUCKET_BACKUPS is unset in this task", {"error": "AWS_BUCKET_BACKUPS"}
    prefix = f"{SNAPSHOT_ROOT}/{_now()}-before-embed5"
    report: dict[str, Any] = {"bucket": bucket, "prefix": prefix, "collections": {}}
    s3 = boto3.client("s3")
    with httpx.Client(base_url=base, headers=headers, timeout=1800) as client:
        report["qdrant_version"] = client.get("/").json().get("version")
        names = sorted(c["name"] for c in client.get("/collections").json()["result"]["collections"])
        for name in names:
            count = client.post(f"/collections/{name}/points/count", json={"exact": True}).json()["result"]["count"]
            r = client.post(f"/collections/{name}/snapshots", params={"wait": "true"})
            r.raise_for_status()
            snap = r.json()["result"]["name"]
            path = f"/tmp/{snap}"
            with client.stream("GET", f"/collections/{name}/snapshots/{snap}") as resp:
                resp.raise_for_status()
                with open(path, "wb") as fh:
                    for chunk in resp.iter_bytes(1 << 20):
                        fh.write(chunk)
            key = f"{prefix}/{name}/{snap}"
            s3.upload_file(path, bucket, key)
            size = os.path.getsize(path)
            os.remove(path)
            client.delete(f"/collections/{name}/snapshots/{snap}")
            report["collections"][name] = {"points": count, "s3": f"s3://{bucket}/{key}", "bytes": size}
            print(f"QDRANT_SNAPSHOT s3://{bucket}/{key} ({size} bytes, {count} points)", flush=True)
    lines = ["| collection | points | snapshot | bytes |", "|---|---|---|---|"]
    lines += [f"| {n} | {c['points']} | `{c['s3']}` | {c['bytes']} |" for n, c in report["collections"].items()]
    lines += ["", f"**snapshot_prefix:** `{prefix}` (pass it to the reset step)"]
    return EXIT_OK, "\n".join(lines), report


# ---------------------------------------------------------------------------
# reset
# ---------------------------------------------------------------------------


def _snapshot_exists(prefix: str) -> tuple[bool, str]:
    import boto3

    bucket = os.environ.get("AWS_BUCKET_BACKUPS", "")
    if not bucket:
        return False, "AWS_BUCKET_BACKUPS is unset"
    if not prefix.startswith(SNAPSHOT_ROOT + "/"):
        return False, f"prefix must start with {SNAPSHOT_ROOT}/"
    page = boto3.client("s3").list_objects_v2(Bucket=bucket, Prefix=f"{prefix}/{COLLECTION}/", MaxKeys=5)
    keys = [o["Key"] for o in page.get("Contents", [])]
    if not keys:
        return False, f"no object under s3://{bucket}/{prefix}/{COLLECTION}/"
    return True, f"s3://{bucket}/{keys[0]}"


async def step_reset(snapshot_prefix: str, confirm: str) -> tuple[int, str, dict[str, Any]]:
    report: dict[str, Any] = {"gates": {}}
    gates = report["gates"]
    backend = os.environ.get("EMBEDDING_BACKEND", "")
    gates["embedding_backend"] = backend
    if backend != "cohere":
        return EXIT_ERROR, f"refused: this task's EMBEDDING_BACKEND is {backend!r}, not 'cohere' (apply Terraform and redeploy first)", report
    ok, detail = _snapshot_exists(snapshot_prefix)
    gates["snapshot"] = detail
    if not ok:
        return EXIT_ERROR, f"refused: no snapshot - {detail}", report
    if confirm != CONFIRM_PHRASE:
        return EXIT_ERROR, f"refused: --confirm must be {CONFIRM_PHRASE!r}", report

    import importlib.util

    script = _APP_ROOT / "scripts" / "reset_embeddings_for_reencode.py"
    spec = importlib.util.spec_from_file_location("reset_embeddings_for_reencode", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    pg = await _connect()
    before = await passage_counts(pg)
    await pg.close()
    report["before"] = before
    try:
        await module.main(reset_everything=True, assume_yes=True)
    except SystemExit as exc:
        report["reset_exit"] = exc.code
        if exc.code not in (0, None):
            return EXIT_ERROR, f"reset_embeddings_for_reencode.py --all exited {exc.code}", report
    pg = await _connect()
    after = await passage_counts(pg)
    await pg.close()
    report["after"] = after
    left = after["totals"]["embedded"]
    rc = EXIT_OK if left == 0 else EXIT_ERROR
    summary = (
        f"Snapshot `{detail}` present. Passages before: {before['totals']}. After: {after['totals']}.\n\n"
        + ("**Reset complete** - the embed sweep (every 10 min) re-encodes from here; run `verify` until it passes."
           if rc == EXIT_OK else f"**{left} passage(s) still carry an embedding_id** - investigate before continuing.")
    )
    return rc, summary, report


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------


async def step_verify() -> tuple[int, str, dict[str, Any]]:
    from app.services import embedding as emb

    model_tag = emb.COHERE_EMBED_MODEL
    pg = await _connect()
    try:
        passages = await passage_counts(pg)
    finally:
        await pg.close()
    points = await point_counts(model_tag)
    points["model"] = model_tag
    rc, line = verdict(points, passages["totals"])
    report = {"model": model_tag, "points": points, "passages": passages, "verdict": line, "exit": rc}
    summary = "\n".join(
        [
            "| | count |",
            "|---|---|",
            f"| Qdrant points | {points['total']} |",
            f"| tagged `embed_model={model_tag}` | {points['tagged_with_model']} |",
            f"| lacking the tag (old space) | {points['lacking_model_tag']} |",
            f"| image points | {points['images']} |",
            f"| passages | {passages['totals']['total']} |",
            f"| passages embedded | {passages['totals']['embedded']} |",
            f"| passages still to embed | {passages['totals']['unembedded']} |",
            f"| image passages / embedded | {passages['totals']['images']} / {passages['totals']['images_embedded']} |",
            "",
            f"**{line}**",
        ]
    )
    return rc, summary, report


# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--step", required=True, choices=["probe", "questions", "snapshot", "reset", "verify"])
    parser.add_argument("--project-slugs", default="red-star", help="comma-separated, for questions")
    parser.add_argument("--label", default="", help="before|after, for questions")
    parser.add_argument("--snapshot-prefix", default="", help="for reset: the prefix the snapshot step printed")
    parser.add_argument("--confirm", default="", help=f"for reset: type {CONFIRM_PHRASE}")
    args = parser.parse_args(argv)

    try:
        if args.step == "probe":
            rc, summary, report = step_probe()
        elif args.step == "questions":
            slugs = [s.strip() for s in args.project_slugs.split(",") if s.strip()]
            rc, summary, report = asyncio.run(step_questions(slugs, args.label or "unlabelled"))
        elif args.step == "snapshot":
            rc, summary, report = step_snapshot()
        elif args.step == "reset":
            rc, summary, report = asyncio.run(step_reset(args.snapshot_prefix, args.confirm))
        else:
            rc, summary, report = asyncio.run(step_verify())
    except Exception as exc:  # noqa: BLE001
        rc, summary, report = EXIT_ERROR, f"**{args.step} crashed:** {type(exc).__name__}: {exc}", {"error": _err(exc)}
    report = {"step": args.step, "at": _now(), "exit": rc, **report}
    _emit(f"### embed5 cutover: `{args.step}`\n\n{summary}", report)
    return rc


if __name__ == "__main__":
    sys.exit(main())
