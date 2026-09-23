"""Rehearsal step 5 — prove one real query streams, cites a REAL chunk, and ends.

Runs INSIDE the VPC as a one-off ECS task on the fastapi task definition, so
it inherits FASTAPI_INTERNAL_URL, FASTAPI_SERVICE_KEY and the database
settings. Nothing here reaches the public internet.

WHY NOT JUST USE THE DEPLOY SMOKE. post_deploy_smoke.py's check_answer_path
is the right mechanism for CD and it now authenticates correctly, but its
assertion is `"citation" in text.lower()`. That is satisfied by the WORD
citation appearing anywhere in the response — including in a refusal that
says "no citations were found". Rehearsal step 5 asks for something
stronger: that the stream carried a citation frame, that the
`source_chunk_id` in it names a row that actually exists, and that the
stream terminated on `completed` rather than simply stopping.

THE ASSERTION THAT MATTERS is the third one. Hard rule 4 says every claim
must carry a source_chunk_id or be rejected by the typed-output validator.
A gate that checks only for the PRESENCE of a citation cannot tell a real
provenance chain from a well-formed fabrication — which is precisely the
failure the six-layer design exists to prevent. So the id is resolved
against silver.document_passages before this reports success.

WHY THIS FILE LIVES HERE and not under ops/rehearsal/ with the rest of the
rehearsal tooling. cd.yml builds the fastapi image with `context: ./src` and
docker/fastapi.Dockerfile does `COPY fastapi/ .`, so ONLY src/fastapi/ lands
at /app. A script under ops/ is outside the build context and would be
silently absent from the image — the run would fail with "can't open file"
rather than anything that names the real cause. post_deploy_smoke.py's
docstring records the same constraint for the same reason; this is its
sibling and obeys it.

Exit code is the verdict: 0 only when every assertion held.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

TIMEOUT = 180

failures: list[str] = []


def _report(name: str, ok: bool, detail: str) -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    if not ok:
        failures.append(name)


def _mint(project_id: str, workspace_id: str) -> str:
    """Mirror app/Services/FastApiJwtMinter.php.

    Confirmed against three independent sources rather than inferred: the
    Laravel minter, app/services/auth.py's verifier, and the
    _mint_test_jwt helper in src/fastapi/tests/conftest.py. HS256 over
    FASTAPI_SERVICE_KEY, iss georag-laravel, aud georag-fastapi, 60 s TTL.
    """
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
        claims, os.environ["FASTAPI_SERVICE_KEY"], algorithm="HS256",
        headers={"kid": os.environ.get("FASTAPI_SERVICE_KEY_KID", "primary")},
    )


def parse_sse(raw: str) -> list[tuple[str, dict]]:
    """Split an SSE body into (event, data) pairs.

    Deliberately tolerant of the trailing fragment: a stream cut mid-frame
    leaves a block with no parsable data, and that must surface as "no
    terminal frame" rather than as a JSON error that masks it.
    """
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
            out.append((event, {"_unparsed": data}))
    return out


async def _citations_resolve(citations: list[dict]) -> tuple[bool, str]:
    """Do these citations name chunks that really exist?

    Delegates to app.services.eval.validators.validate_chunk_provenance —
    the §04i Layer 5 check the platform itself uses — rather than rolling a
    lookup here. That is not tidiness; the first draft of this file queried
    `silver.document_passages` by `passage_id` and would have been wrong in
    the worst possible direction, reporting genuine citations as fabricated:

      * ids resolve in QDRANT, not Postgres;
      * the collection depends on RETRIEVAL_USE_DOCUMENT_PASSAGES, and
        pinning the wrong one produced systematic Layer 5 failures after the
        ADR-0010 cutover (recorded in the validator's own docstring);
      * a source_chunk_id may be a bare point UUID *or* the compound trace
        form `georag_reports:<id>:section=<n>:chunk=<uuid>`, which has to be
        parsed before the lookup;
      * `corpus='public_geo'` citations are deliberately skipped — those ids
        are not Qdrant points at all.

    Reusing it also means this rehearsal asserts exactly what production
    asserts, so a pass here cannot diverge from the live guard.
    """
    from qdrant_client import AsyncQdrantClient

    from app.services.eval.validators import validate_chunk_provenance
    from app.services.eval.workspace_evaluator import QuestionRecord
    from app.services.qdrant_conn import qdrant_client_kwargs

    # expected_refusal MUST be False: the validator treats a refusal-expected
    # question as a vacuous pass, which would turn this whole assertion into
    # a no-op and report success over an unresolvable citation.
    question = QuestionRecord(
        question_id=uuid.uuid4(), question_set="rehearsal",
        question_text="", context_setup={}, expected_intent_class=None,
        expected_citations=[], expected_entities=[], expected_numeric_values=[],
        expected_refusal=False, expected_refusal_reason=None,
        expected_language_compliance=[], difficulty="n/a",
    )

    client = AsyncQdrantClient(**qdrant_client_kwargs())
    try:
        outcome = await validate_chunk_provenance(
            citations=citations, qdrant_client=client,
            qdrant_collection=None, question=question,
        )
    finally:
        await client.close()

    return bool(outcome.passed), (
        outcome.failure_message or f"all citations resolve in Qdrant: {outcome.detail}"
    )


def main() -> int:
    base = os.environ.get("FASTAPI_INTERNAL_URL", "")
    project_id = os.environ.get("PROD_SMOKE_PROJECT_ID", "")
    workspace_id = os.environ.get("PROD_SMOKE_WORKSPACE_ID", "")
    question = os.environ.get("REHEARSAL_QUESTION", "What does this project contain?")

    for name, val in (("FASTAPI_INTERNAL_URL", base),
                      ("PROD_SMOKE_PROJECT_ID", project_id),
                      ("PROD_SMOKE_WORKSPACE_ID", workspace_id),
                      ("FASTAPI_SERVICE_KEY", os.environ.get("FASTAPI_SERVICE_KEY", ""))):
        if not val:
            print(f"[FAIL] preconditions: {name} is UNSET")
            return 1
    if "localhost" in base or "127.0.0.1" in base:
        print(f"[FAIL] preconditions: FASTAPI_INTERNAL_URL={base} is loopback; this "
              "task runs the script INSTEAD of uvicorn, so nothing listens here")
        return 1

    print(f"asking {base}: {question!r}\n")

    req = urllib.request.Request(
        base.rstrip("/") + "/internal/queries",
        data=json.dumps({"query": question, "project_id": project_id}).encode(),
        headers={
            "Content-Type": "application/json",
            "X-Service-Key": os.environ["FASTAPI_SERVICE_KEY"],
            "Authorization": f"Bearer {_mint(project_id, workspace_id)}",
            "Accept": "text/event-stream",
        },
    )
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            status_code = r.status
            body = r.read().decode()
    except urllib.error.HTTPError as exc:
        print(f"[FAIL] request: HTTP {exc.code} {exc.read()[:400].decode(errors='replace')}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] request: {type(exc).__name__}: {exc}")
        return 1

    elapsed = round(time.time() - started, 1)
    frames = parse_sse(body)
    kinds = [e for e, _ in frames]
    print(f"HTTP {status_code}, {len(body)} bytes, {len(frames)} frames in {elapsed}s")
    print(f"frame sequence: {' '.join(kinds) if kinds else '(none)'}\n")

    _report("http-200", status_code == 200, f"HTTP {status_code}")

    # 1. It STREAMED. One delta is not streaming — a non-streaming
    #    implementation that emits the whole answer in a single frame would
    #    satisfy a >=1 check while breaking the thing being verified.
    deltas = [d for e, d in frames if e == "delta"]
    _report("streamed", len(deltas) >= 2,
            f"{len(deltas)} delta frames"
            + ("" if len(deltas) >= 2 else " — not a stream"))

    # 2. It TERMINATED CLEANLY on `completed`, and `completed` is LAST.
    #    A stream that emits completed and then keeps talking, or that simply
    #    stops, are both failures the chat UI shows as a hung message.
    if "failed" in kinds:
        failed_payload = next(d for e, d in frames if e == "failed")
        _report("terminated", False, f"stream emitted `failed`: {failed_payload}")
    else:
        _report("terminated", bool(kinds) and kinds[-1] == "completed",
                f"last frame is {kinds[-1] if kinds else '(none)'}")

    # 3. It CITED, and the citation names a chunk that EXISTS.
    citations = [d for e, d in frames if e == "citation"]
    if not citations:
        _report("cited", False,
                "no citation frame — the answer carried no provenance. If the "
                "project's corpus is not indexed this is the CORRECT refusal "
                "behaviour, but it does not satisfy rehearsal step 5.")
    else:
        chunk_ids = [c.get("source_chunk_id") for c in citations]
        _report("cited", all(chunk_ids),
                f"{len(citations)} citation frame(s), source_chunk_id(s)={chunk_ids}")
        try:
            ok, detail = asyncio.run(_citations_resolve(citations))
            _report("citation-resolves", ok, detail)
        except Exception as exc:  # noqa: BLE001
            _report("citation-resolves", False, f"{type(exc).__name__}: {exc}")

    print("-" * 60)
    if failures:
        print(f"STEP 5 FAILED: {', '.join(failures)}")
        return 1
    print("STEP 5 OK — streamed, cited a real source_chunk_id, terminated cleanly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
