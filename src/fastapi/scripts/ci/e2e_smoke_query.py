#!/usr/bin/env python3
"""CI money-path smoke test -- query leg.

Companion to scripts/ci/e2e_smoke_ingest.py. Assumes:
  - `uvicorn app.main:app` is already running and healthy (the CI job polls
    /health before invoking this script).
  - The ingest leg already wrote + embedded a report for the given project.

Mints a JWT the same way Laravel's GeoRagService would (HS256, signed with
FASTAPI_SERVICE_KEY, iss=georag-laravel aud=georag-fastapi -- see
tests/test_jwt_auth.py::_mint for the pattern this mirrors), calls
POST /internal/queries, reads the SSE stream, and asserts:

  1. A `completed` event arrives (not `failed`/`timeout`).
  2. It is NOT a refusal: `refusal_payload` is unset. A Layer 1 retrieval-
     quality refusal is a perfectly well-formed `completed` frame, so
     "a completed event arrived" proves nothing about retrieval.
  3. At least one citation carries a REAL `source_chunk_id`.
     `GeoRAGResponse` requires >= 1 citation (`min_length=1`), so the
     assembler fills the slot with a sentinel when nothing was retrieved
     (`no-tool-call`, `georag_reports:empty`, ...). Those are
     `app.agent.response_assembler.EMPTY_SOURCE_SENTINELS`, imported rather
     than copied so this gate cannot drift from the producer.
  4. That citation is a chunk of the report the ingest leg wrote
     (`georag_reports:<report_id>:section=..:chunk=..`), so retrieval is
     shown to have found the ingested document and not just any row.

Why 2-4 exist: this script used to check only that `citations` was non-empty
and the first `source_chunk_id` was truthy. A change that broke query-time
retrieval (workspace filter, embedding dimension, score floor) turned every
query into a Layer 1 refusal carrying a `no-tool-call` sentinel, and the job
printed "OK". The decision logic lives in `judge_completed`, which
tests/test_e2e_smoke_query_oracle.py drives with frames built by the real
assembler.

Query phrasing note
--------------------
"What does the report say about hole PLS-22-08?" is chosen deliberately:
  - "what does X say" matches app/agent/agentic_retrieval/intent_classifier
    .py's factual_lookup keyword rule with high confidence, so the
    low-confidence LLM-fallback classification path (a call shape the stub
    backend does not special-case) never fires.
  - "PLS-22-08" is one of tests/e2e_smoke/stub_backend.py's marker phrases,
    and appears verbatim in the fixture PDF, so the stub's hashing-trick
    embeddings put the query and the matching passage's chunk well above
    RETRIEVAL_QUALITY_THRESHOLD (0.5) in cosine similarity.

Changing the fixture PDF or the query text requires keeping this alignment.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import jwt

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # -> src/fastapi

# The producer's own sentinel set. Importing it (instead of mirroring the
# literals) is the point: when the assembler grows a new placeholder id this
# gate picks it up the same day. The ingest leg imports `app.*` the same way.
from app.agent.response_assembler import EMPTY_SOURCE_SENTINELS  # noqa: E402


def _mint_jwt(*, secret: str, project_id: str, workspace_id: str) -> str:
    now = int(time.time())
    payload = {
        "iss": "georag-laravel",
        "aud": "georag-fastapi",
        "sub": "e2e-smoke-user",
        "project_id": project_id,
        "workspace_id": workspace_id,
        "roles": ["member"],
        "iat": now,
        "exp": now + 120,
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def judge_completed(completed: dict[str, Any], *, report_id: str) -> list[str]:
    """Why this `completed` frame is NOT a grounded, cited answer.

    Returns one human-readable reason per failed check; an empty list means
    the frame passes. Pure (no I/O) so tests/test_e2e_smoke_query_oracle.py can
    feed it frames built by the real assembler and prove each failure mode is
    actually caught.
    """
    problems: list[str] = []

    refusal = completed.get("refusal_payload")
    if refusal:
        reason = refusal.get("reason_code") if isinstance(refusal, dict) else refusal
        problems.append(
            f"completed frame is a REFUSAL (refusal_payload.reason_code={reason!r}) -- "
            "retrieval returned nothing usable, so the money path is broken"
        )

    citations = completed.get("citations") or []
    if not citations:
        problems.append("completed event has ZERO citations -- money path broken")
        return problems

    chunk_ids = [str(c.get("source_chunk_id") or "") for c in citations]
    real = [cid for cid in chunk_ids if cid and cid not in EMPTY_SOURCE_SENTINELS]
    if not real:
        problems.append(
            "every citation is an empty-source sentinel, i.e. nothing was actually "
            f"retrieved to cite: {chunk_ids}"
        )
        return problems

    # source_chunk_id is `georag_reports:<report_id>:section=..:chunk=..`
    # (response_assembler._source_chunk_id_for_doc_chunk). Anything else -- a
    # structured-tool id, a public-geoscience id -- is real evidence but not
    # evidence that THIS ingest leg's document was found.
    prefix = f"georag_reports:{report_id.lower()}:"
    if not any(cid.lower().startswith(prefix) for cid in real):
        problems.append(
            f"no citation is a chunk of the ingested report {report_id!r} "
            f"(expected a source_chunk_id starting {prefix!r}); got {real}"
        )

    return problems


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--workspace-id", required=True)
    # Required, not optional: a gate that quietly falls back to a weaker check
    # when its input is missing is how the sentinel hole stayed open.
    parser.add_argument(
        "--report-id", required=True,
        help="silver.reports.report_id the ingest leg wrote (its `report_id` output)",
    )
    parser.add_argument(
        "--query", default="What does the report say about hole PLS-22-08?",
    )
    parser.add_argument("--timeout", type=float, default=25.0)
    args = parser.parse_args()

    service_key = os.environ["FASTAPI_SERVICE_KEY"]
    token = _mint_jwt(
        secret=service_key, project_id=args.project_id, workspace_id=args.workspace_id,
    )

    print(f"query: POST {args.base_url}/internal/queries project={args.project_id}")
    print(f"query: {args.query!r}")

    completed: dict | None = None
    failed: dict | None = None
    event_name: str | None = None

    with httpx.Client(timeout=args.timeout) as client:
        with client.stream(
            "POST",
            f"{args.base_url}/internal/queries",
            json={"query": args.query, "project_id": args.project_id},
            headers={
                "X-Service-Key": service_key,
                "Authorization": f"Bearer {token}",
            },
        ) as resp:
            if resp.status_code != 200:
                body = resp.read()
                print(f"query: HTTP {resp.status_code}: {body[:500]!r}", file=sys.stderr)
                return 1

            for line in resp.iter_lines():
                if not line:
                    continue
                if line.startswith("event: "):
                    event_name = line[len("event: "):].strip()
                    continue
                if line.startswith("data: "):
                    data_raw = line[len("data: "):]
                    if event_name == "completed":
                        completed = json.loads(data_raw)
                        break
                    if event_name == "failed":
                        failed = json.loads(data_raw)
                        break

    if failed is not None:
        print(f"query: FAILED event received: {failed}", file=sys.stderr)
        return 1

    if completed is None:
        print("query: stream ended without a completed or failed event", file=sys.stderr)
        return 1

    citations = completed.get("citations") or []
    print(f"query: completed. text_first_160={completed.get('text', '')[:160]!r}")
    print(f"query: citations={len(citations)}")

    problems = judge_completed(completed, report_id=args.report_id)
    if problems:
        for problem in problems:
            print(f"query: {problem}", file=sys.stderr)
        return 1

    print(
        "query: OK -- cited answer grounded in the ingested report "
        f"{args.report_id}; citation[0].source_chunk_id={citations[0].get('source_chunk_id')!r}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
