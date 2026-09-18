"""Post-deploy smoke — a one-off ECS task in the VPC, called from cd.yml.

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
1. FastAPI answers on its Cloud Map address (not just "a process exists").
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
cd.yml registers a `georag-smoke` task definition cloned from
`georag-fastapi` — same image, same VPC, same task role, same environment
— overriding only the container command:

    python3 /app/scripts/ops/post_deploy_smoke.py

then runs it once with `aws ecs run-task` and reads the container's exit
code. That override is the fact this file has to be written around: THE
APP DOES NOT RUN IN THIS CONTAINER. uvicorn is replaced by this script,
so every check must address its target over the network, by its
in-environment name, exactly as a real caller would. Nothing here may
dial loopback. check_fastapi_self did until 2026-09-18 and could only
ever fail; see its comment.

Inheriting fastapi's environment is what makes that workable —
FASTAPI_INTERNAL_URL, LARAVEL_INTERNAL_URL, QDRANT_HOST and the database
settings all arrive with the task definition rather than being
reconstructed here, so the check exercises the values production uses.

It was `az containerapp exec` until the ECS port (2026-09-08, ADR-0022).
Two limits of that mechanism shaped this file and no longer bind: the
command travelled in a URL query parameter capped near 2048 characters,
so it had to be invoked by path rather than inlined, and repeated exec
calls were rate-limited into `429 ... retry-after: 600`. RunTask has
neither limit. It also has no PTY, which is the point — `exec` called
tty.setcbreak() and died on a CI runner's non-terminal stdin, and on
2026-08-23 that crash was misread as a failed smoke and rolled back a
healthy deployment.

The image build context is `src` (cd.yml's build-fastapi job), and
`COPY fastapi/ .` lands this at /app/scripts/ops/ — which is also why the
file lives here rather than under ops/, where it would be outside the
build context and silently absent from the image.

Exit contract
-------------
The process exit code is the verdict: 0 when every check passed, 1
otherwise. `az containerapp exec` returned 0 for a successful CONNECTION
regardless of what the command did, which is why this file used to print
`SMOKE_OK` for the caller to grep; RunTask reports the container's real
exit code, so cd.yml reads that instead. The `SMOKE_OK` / `SMOKE_FAILED`
lines are kept as the human-readable summary in CloudWatch, which cd.yml
prints on failure.
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


#: Where the FastAPI service actually answers, reached the way every
#: other component reaches it.
#:
#: This USED to be `http://127.0.0.1:8000/health`, and that was correct
#: under Azure: cd.yml ran this file with `az containerapp exec` INSIDE a
#: live fastapi-cc replica, so loopback was the app's own listening
#: socket. The ECS port (2026-09-08) runs it as a standalone RunTask that
#: overrides the container command to `python3 .../post_deploy_smoke.py`
#: — so uvicorn never starts in this container and nothing binds :8000.
#: Loopback here could only ever fail, and on 2026-09-18 it did, taking
#: a deployment whose seven services had all reached steady state with
#: it. The check was measuring the smoke container, not the product.
#:
#: Reading FASTAPI_INTERNAL_URL is not merely the repair, it is a
#: stronger check than the original. Loopback proved "a socket in this
#: container answers". This proves the service is registered in Cloud
#: Map, the name resolves, the security group permits the hop, and the
#: app answers — which is what every caller in the platform depends on
#: and what a rollout can actually break.
#:
#: Unset must FAIL, for the same reason check_laravel_bridge treats an
#: unset LARAVEL_INTERNAL_URL as a failure: a gate that invents a
#: fallback address stops testing the deployment and starts testing its
#: own default. config.tf sets this on the fastapi task definition, and
#: the smoke task is registered from that definition, so it is present
#: unless someone removes it — in which case the product is broken too.

#: The old loopback diagnosis, kept because it must not be re-derived:
#: on 2026-08-23 the in-replica check raised `[Errno 99] Cannot assign
#: requested address` seconds after a rollout while Laravel, Postgres and
#: Qdrant all passed from the same container. That was measured, and the
#: tidy "localhost picks ::1 and ::1 is dead" story does not survive the
#: measurement — at rest both 127.0.0.1 and ::1 answered 200. It is
#: history now rather than a live concern, since nothing dials loopback,
#: but it is why the retry below exists and the retry is still needed:
#: this runs seconds after ECS reports steady state, when a task may be
#: registered in Cloud Map without yet serving.
SELF_ATTEMPTS = 6
SELF_BACKOFF = 5


def check_fastapi_self() -> None:
    base = os.environ.get("FASTAPI_INTERNAL_URL", "")
    if not base:
        _report("fastapi-self", False, "FASTAPI_INTERNAL_URL is UNSET")
        return
    if "localhost" in base or "127.0.0.1" in base:
        _report(
            "fastapi-self", False,
            f"FASTAPI_INTERNAL_URL={base} points at loopback. This task runs "
            "the smoke script INSTEAD of uvicorn, so nothing listens here — "
            "it must address the fastapi service, not this container.",
        )
        return

    url = base.rstrip("/") + "/health"
    last = ""
    for attempt in range(1, SELF_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(url, timeout=TIMEOUT) as r:
                body = r.read().decode()[:200]
            _report(
                "fastapi-self",
                r.status == 200,
                f"{url} -> HTTP {r.status} {body}"
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
        f"{SELF_BACKOFF * (SELF_ATTEMPTS - 1)}s against {url})",
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
    if not key:
        _report("answer-path", False, "FASTAPI_SERVICE_KEY is UNSET")
        return

    # X-Service-Key ALONE IS NOT ENOUGH, and this check used to send only
    # that. app/services/auth.py closed the graceful-rollout window where it
    # was (Module 9 Chunk 9.4 / A1-02): /internal/queries is not in
    # _AUTH_OPTIONAL_PATH_PREFIXES, so a request with no `Authorization`
    # header is refused with 401 "Authorization header required" before any
    # of the answer path runs.
    #
    # That made this check unable to pass. It went unnoticed because it only
    # runs when PROD_SMOKE_PROJECT_ID is set and nobody had set it — so the
    # first person to enable it would have read a 401 as "the answer path is
    # broken" when the truth was "the gate never learned to authenticate".
    # Same shape as the loopback defect in check_fastapi_self above: written
    # against an older contract, never exercised since, could only fail.
    #
    # Mirrors app/Services/FastApiJwtMinter.php exactly — HS256 over
    # FASTAPI_SERVICE_KEY, iss georag-laravel, aud georag-fastapi, 60 s TTL,
    # and the `kid` header FastAPI maps back to a signing key.
    try:
        import jwt  # noqa: PLC0415 — present in the fastapi image
    except ImportError:
        _report("answer-path", False, "PyJWT missing — cannot mint the service JWT")
        return

    now = int(time.time())
    claims = {
        "iss": "georag-laravel",
        "aud": "georag-fastapi",
        "sub": "0",
        "project_id": project_id,
        "roles": [],
        "iat": now,
        "exp": now + 60,
    }
    workspace_id = os.environ.get("PROD_SMOKE_WORKSPACE_ID", "")
    if workspace_id:
        claims["workspace_id"] = workspace_id
    bearer = jwt.encode(
        claims, key, algorithm="HS256",
        headers={"kid": os.environ.get("FASTAPI_SERVICE_KEY_KID", "primary")},
    )

    body = json.dumps({
        "query": "What does this project contain?",
        "project_id": project_id,
        "workspace_id": os.environ.get("PROD_SMOKE_WORKSPACE_ID", ""),
    }).encode()
    # Same correction as check_fastapi_self: loopback is not this
    # container's app under the RunTask harness. Guarded rather than
    # assumed — this check is opt-in and would otherwise fail for a
    # reason that has nothing to do with the answer path.
    base = os.environ.get("FASTAPI_INTERNAL_URL", "")
    if not base or "localhost" in base or "127.0.0.1" in base:
        _report(
            "answer-path", False,
            f"FASTAPI_INTERNAL_URL={base or 'UNSET'} is not an in-VPC address",
        )
        return
    req = urllib.request.Request(
        base.rstrip("/") + "/internal/queries",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Service-Key": key,
            "Authorization": f"Bearer {bearer}",
        },
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
