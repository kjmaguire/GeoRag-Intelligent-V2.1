"""Post-deploy smoke — runs INSIDE fastapi-cc, called from cd.yml.

Why this exists
---------------
CD's entire success criterion after rolling images used to be "the five
container apps report Healthy" plus one HTTP 200 from Laravel's `/up`.
Neither statement says anything about whether the product works:

  * `healthState` is the platform's view of revision provisioning. Five of
    the eight container apps have no liveness or readiness probe at all, so
    for those it means "a process is running", not "the app answers".

  * FastAPI's `/health` returns a static `{"status":"ok"}` with no
    dependency checks, and in any case its ingress is INTERNAL — a GitHub
    runner cannot reach it. Any check has to run from inside the mesh.

The gap is not hypothetical. On 2026-08-18 `LARAVEL_INTERNAL_URL` was unset
in production, so every FastAPI -> Laravel callback resolved to the
Herd-local default `http://laravel.test` and died on DNS. Ingestion
progress, workspace-data-updated, the admin surfaces and the user inbox
were all silently dead. Every container was Healthy and `/up` returned 200
throughout, so the deploy gate was green the entire time.

What it checks
--------------
1. FastAPI actually answers on its own port (not just "a process exists").
2. The FastAPI -> Laravel bridge resolves AND returns 200 — the exact
   2026-08-18 failure.
3. Postgres is reachable and queryable with the app's own settings.
4. Qdrant is reachable AND both collections the query path needs are
   present — `init_qdrant.py` is a manual step that nothing in the runtime
   path calls, so a fresh or re-provisioned Qdrant can legitimately be up
   and empty.

What it deliberately does NOT check
-----------------------------------
A full answer-path query with a real citation. That needs a project whose
corpus is indexed, and hardcoding a project UUID into the deploy gate is
how perf-baseline ended up measuring nothing. Set PROD_SMOKE_PROJECT_ID on
fastapi-cc to enable check 5; until then the script says out loud that the
answer path was not exercised rather than implying it passed.

How it is invoked
-----------------
cd.yml runs it with a SINGLE `az containerapp exec` call, by path:

    az containerapp exec -g georag -n fastapi-cc         --command "python3 /app/scripts/ops/post_deploy_smoke.py"

It has to be one call, and it has to be by path. Two measured limits from
2026-08-21 forced that shape:

  * The exec command travels in a URL query parameter and the gateway caps
    it near 2048 characters — 1800 succeeds, 2400 returns an IIS 404
    handshake failure. This file is 8.4 KB, 4.4 KB even zlib+base64'd, so
    it cannot be passed inline.
  * Repeated exec calls are rate-limited: staging the payload in six
    chunks earned `Handshake status 429 Too Many Requests` with
    `retry-after: 600`. A deploy gate that can be throttled into failing
    is worse than no gate.

The image build context is `src` (cd.yml's build-fastapi job), and
`COPY fastapi/ .` lands this at /app/scripts/ops/ — which is also why the
file lives here rather than under ops/, where it would be outside the
build context and silently absent from the image.

Exit contract
-------------
`az containerapp exec` returns 0 for a successful CONNECTION regardless of
what the command did, so the caller cannot use the exit code. Print
`SMOKE_OK` on the last line only when every check passed; the caller greps
for it. Anything else — including a traceback — is a failure.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import urllib.error
import urllib.request

TIMEOUT = 15
failures: list[str] = []


def _report(name: str, ok: bool, detail: str) -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    if not ok:
        failures.append(name)


#: Where this process's own app is listening.
#:
#: What is MEASURED, inside a live replica: `localhost` resolves to both
#: 127.0.0.1 and ::1, with the IPv4 address first; at rest, GET /health
#: on either returns 200. So a plain name lookup is not, by itself, the
#: bug -- and the tidy "localhost picks ::1 and ::1 is dead" story does
#: not survive that measurement. Do not repeat it as the cause.
#:
#: What is KNOWN about the failure: on 2026-08-23 this check raised
#: `[Errno 99] Cannot assign requested address` seconds after the
#: rollout, while the other three checks in this same script -- Laravel,
#: Postgres, Qdrant -- all passed from the same container at the same
#: moment. So container networking was up; the LOOPBACK specifically was
#: not usable yet. EADDRNOTAVAIL is "that address is not assignable",
#: not the ECONNREFUSED a closed port gives, which fits an interface
#: still being configured rather than an app not yet listening.
#:
#: The retry below is therefore the load-bearing fix. The literal is
#: defence in depth: it removes a resolution step and a second address
#: family from a path that has already produced this error once, and
#: costs nothing.
#:
#: 8000 is fastapi-cc's ingress `targetPort`. Not read from a PORT
#: variable: there isn't one in this container (verified inside a live
#: replica), so an env lookup would be a derivation from nothing that
#: silently redirects the check the day somebody sets PORT for an
#: unrelated reason. If targetPort ever moves, this check fails loudly,
#: which is the correct outcome -- the app would not be answering where
#: the ingress sends traffic either.
SELF_URL = "http://127.0.0.1:8000/health"

#: This runs seconds after the rollout reports the revision healthy, at
#: which point the process may be listening but not yet serving. One
#: attempt makes the gate a coin toss on startup timing, and its failure
#: mode is a full rollback of five apps -- so retry, briefly, and only
#: for the connection.
SELF_ATTEMPTS = 6
SELF_BACKOFF = 5


def check_fastapi_self() -> None:
    last = ""
    for attempt in range(1, SELF_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(SELF_URL, timeout=TIMEOUT) as r:
                body = r.read().decode()[:200]
            _report(
                "fastapi-self",
                r.status == 200,
                f"HTTP {r.status} {body}"
                + (f" (attempt {attempt})" if attempt > 1 else ""),
            )
            return
        except Exception as exc:  # noqa: BLE001 — any failure is a failed check
            last = f"{type(exc).__name__}: {exc}"
            if attempt < SELF_ATTEMPTS:
                print(f"[....] fastapi-self: {last} — retrying in {SELF_BACKOFF}s")
                time.sleep(SELF_BACKOFF)

    _report(
        "fastapi-self", False,
        f"{last} (after {SELF_ATTEMPTS} attempts over "
        f"{SELF_BACKOFF * (SELF_ATTEMPTS - 1)}s against {SELF_URL})",
    )


def check_laravel_bridge() -> None:
    """The 2026-08-18 regression, checked directly.

    Reads LARAVEL_INTERNAL_URL out of the environment rather than assuming
    a value: an unset variable falling back to a Herd-local default is the
    precise bug, so an unset variable must fail here, not be papered over.
    """
    base = os.environ.get("LARAVEL_INTERNAL_URL", "")
    if not base:
        _report("laravel-bridge", False, "LARAVEL_INTERNAL_URL is UNSET")
        return
    if "laravel.test" in base or "localhost" in base:
        _report(
            "laravel-bridge", False,
            f"LARAVEL_INTERNAL_URL={base} is a local-dev default, not an "
            "in-environment address",
        )
        return
    try:
        with urllib.request.urlopen(
            base.rstrip("/") + "/up", timeout=TIMEOUT,
        ) as r:
            _report("laravel-bridge", r.status == 200, f"{base} -> HTTP {r.status}")
    except Exception as exc:  # noqa: BLE001
        _report("laravel-bridge", False, f"{base} -> {type(exc).__name__}: {exc}")


def check_qdrant() -> None:
    host = os.environ.get("QDRANT_HOST", "")
    port = os.environ.get("QDRANT_PORT", "6333")
    scheme = "https" if os.environ.get("QDRANT_HTTPS", "").lower() == "true" else "http"
    if not host:
        _report("qdrant", False, "QDRANT_HOST is UNSET")
        return
    req = urllib.request.Request(
        f"{scheme}://{host}:{port}/collections",
        headers={"api-key": os.environ.get("QDRANT_API_KEY", "")},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            payload = json.loads(r.read().decode())
    except Exception as exc:  # noqa: BLE001
        _report("qdrant", False, f"{type(exc).__name__}: {exc}")
        return

    names = {c["name"] for c in payload["result"]["collections"]}
    # Both are load-bearing for the query path, and init_qdrant.py is a
    # MANUAL bootstrap that no runtime code path calls — a re-provisioned
    # Qdrant answers 200 while serving zero collections.
    missing = {"georag_chunks", "georag_reports"} - names
    _report(
        "qdrant", not missing,
        f"HTTP 200, collections={sorted(names)}"
        + (f", MISSING={sorted(missing)}" if missing else ""),
    )


async def _pg() -> str:
    import asyncpg  # noqa: PLC0415 — only present inside the image

    from app.db.dsn import build_dsn  # noqa: PLC0415

    # This was the sixty-first hand-rolled DSN -- assembled from settings
    # fields with no percent-encoding, so a password containing "@" made it
    # dial a different host and report the deployment unhealthy for a
    # reason that had nothing to do with the deployment. build_dsn is the
    # one place that knows how to do this.
    dsn = build_dsn(scheme="postgresql", include_sslmode=True)
    conn = await asyncpg.connect(dsn, timeout=TIMEOUT)
    try:
        # to_regclass, not a row count: silver.document_passages is under
        # RLS, and a connection with no workspace GUC set legitimately sees
        # zero rows. Counting would make a healthy database look empty.
        present = await conn.fetchval(
            "SELECT to_regclass('silver.document_passages') IS NOT NULL",
        )
        version = await conn.fetchval("SHOW server_version")
    finally:
        await conn.close()
    if not present:
        raise RuntimeError("silver.document_passages does not exist")
    return f"connected, PostgreSQL {version}, silver.document_passages present"


def check_postgres() -> None:
    try:
        _report("postgres", True, asyncio.run(_pg()))
    except Exception as exc:  # noqa: BLE001
        _report("postgres", False, f"{type(exc).__name__}: {exc}")


def check_answer_path() -> None:
    """Optional: only runs when a project is nominated for it."""
    project_id = os.environ.get("PROD_SMOKE_PROJECT_ID", "")
    if not project_id:
        print(
            "[SKIP] answer-path: PROD_SMOKE_PROJECT_ID is unset — this deploy "
            "did NOT verify that a query returns a cited answer. Set it on "
            "fastapi-cc to enable.",
        )
        return
    key = os.environ.get("FASTAPI_SERVICE_KEY", "")
    body = json.dumps({
        "query": "What does this project contain?",
        "project_id": project_id,
        "workspace_id": os.environ.get("PROD_SMOKE_WORKSPACE_ID", ""),
    }).encode()
    req = urllib.request.Request(
        "http://localhost:8000/internal/queries",
        data=body,
        headers={"Content-Type": "application/json", "X-Service-Key": key},
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            text = r.read().decode()
        _report(
            "answer-path",
            r.status == 200 and "citation" in text.lower(),
            f"HTTP {r.status}, {len(text)} bytes, "
            f"citations={'yes' if 'citation' in text.lower() else 'NO'}",
        )
    except Exception as exc:  # noqa: BLE001
        _report("answer-path", False, f"{type(exc).__name__}: {exc}")


def main() -> int:
    print("post-deploy smoke (inside fastapi-cc)")
    check_fastapi_self()
    check_laravel_bridge()
    check_postgres()
    check_qdrant()
    check_answer_path()
    print("-" * 60)
    if failures:
        print(f"SMOKE_FAILED: {', '.join(failures)}")
        return 1
    print("SMOKE_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
