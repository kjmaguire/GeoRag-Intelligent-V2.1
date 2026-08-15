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
  2. Its GeoRAGResponse has >= 1 citation.
  3. That citation has a non-empty `source_chunk_id`.

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

import httpx
import jwt


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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--workspace-id", required=True)
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

    if not citations:
        print("query: completed event has ZERO citations -- money path broken", file=sys.stderr)
        return 1

    first = citations[0]
    chunk_id = first.get("source_chunk_id")
    if not chunk_id:
        print(f"query: first citation missing source_chunk_id: {first}", file=sys.stderr)
        return 1

    print(f"query: OK -- citation[0].source_chunk_id={chunk_id!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
