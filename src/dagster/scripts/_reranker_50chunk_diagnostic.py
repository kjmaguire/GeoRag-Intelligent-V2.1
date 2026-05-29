"""Phase 3 §5e XL pre-flight diagnostic — 50-chunk query generation.

Purpose: verify Blocker 3 (placeholder query generation, observed in the
2026-05-19 dataset on Qwen3-30B) is resolved on the current
Qwen3-14B-AWQ + the same LITERAL_PROMPT_SYSTEM / PARAPHRASE_PROMPT_SYSTEM
prompts the asset uses. If queries are diverse and meaningful, the
full reranker_label_dataset re-materialisation can proceed. If they're
still placeholder-y, a prompt-engineering pass is needed first.

Operator tool, not part of the asset graph. Drives the actual vLLM client
with the actual prompts, so the verdict is end-to-end-faithful.

Run inside the dagster container:
    docker exec georag-dagster-webserver python scripts/_reranker_50chunk_diagnostic.py

Output: prints a verdict + 10-sample query inspection table to stdout.
Does NOT write to silver.* or s3:// — pure read.
"""

import os
import random

import psycopg2
import psycopg2.extras

from georag_dagster.assets.reranker_labels import (
    LITERAL_PROMPT_SYSTEM,
    PARAPHRASE_PROMPT_SYSTEM,
    USER_PROMPT_QUERY_TEMPLATE,
)
from georag_dagster.clients.vllm_openai import (
    VllmJsonRequest,
    VllmOpenAIClient,
    build_default_client,
)


# Diagnostic config — locked decisions from Kyle on 2026-05-29:
#   • min 200 chars (filter)
#   • 50 chunks
N_CHUNKS = 50
MIN_CHUNK_CHARS = 200
INSPECT_SAMPLE_SIZE = 10
SEED = 42  # diagnostic determinism; not the production run-id-seeded one


SAMPLE_SQL = """
SELECT
    report_id::text AS report_id,
    page,
    region,
    LENGTH(text_content) AS chunk_chars,
    text_content AS chunk_text
FROM silver.ingest_extractions
WHERE LENGTH(COALESCE(text_content, '')) >= %s
ORDER BY RANDOM()
LIMIT %s
"""


def fetch_sample_chunks(conn, n: int, min_chars: int) -> list[dict]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(SAMPLE_SQL, (min_chars, n))
        return [dict(r) for r in cur.fetchall()]


def build_client() -> VllmOpenAIClient:
    base_url = os.environ.get("VLLM_BASE_URL", "http://vllm:8000/v1")
    api_key = os.environ.get("VLLM_API_KEY", "EMPTY")
    model = os.environ.get("VLLM_MODEL", "Qwen/Qwen3-14B-AWQ")
    timeout_s = float(os.environ.get("VLLM_TIMEOUT_S", "60"))
    openai_client = build_default_client(base_url, api_key, timeout_s)
    return VllmOpenAIClient(client=openai_client, model=model, max_workers=8)


def main() -> int:
    pg_dsn = (
        "postgresql://georag_app:georag-app-dev-2026@postgresql:5432/georag"
    )

    print(f"=== §5e XL pre-flight diagnostic ===")
    print(f"N_CHUNKS={N_CHUNKS}  MIN_CHUNK_CHARS={MIN_CHUNK_CHARS}")
    print()

    with psycopg2.connect(pg_dsn) as conn:
        chunks = fetch_sample_chunks(conn, N_CHUNKS, MIN_CHUNK_CHARS)
    print(f"Sampled {len(chunks)} chunks from silver.ingest_extractions "
          f"(length >= {MIN_CHUNK_CHARS} chars)")
    if not chunks:
        print("ABORT: no chunks matched the filter. Lower MIN_CHUNK_CHARS or "
              "verify silver.ingest_extractions has content.")
        return 2
    print(f"Char-length range: [{min(c['chunk_chars'] for c in chunks)}, "
          f"{max(c['chunk_chars'] for c in chunks)}]; "
          f"avg={sum(c['chunk_chars'] for c in chunks) // len(chunks)}")
    print()

    client = build_client()

    # Build literal + paraphrase requests for every sampled chunk.
    requests: list[VllmJsonRequest] = []
    for i, c in enumerate(chunks):
        user = USER_PROMPT_QUERY_TEMPLATE.format(chunk_text=c["chunk_text"])
        requests.append(VllmJsonRequest(
            custom_id=f"lit_{i}",
            system=LITERAL_PROMPT_SYSTEM,
            user=user,
            max_tokens=200,
            temperature=0.2,
        ))
        requests.append(VllmJsonRequest(
            custom_id=f"para_{i}",
            system=PARAPHRASE_PROMPT_SYSTEM,
            user=user,
            max_tokens=200,
            temperature=0.2,
        ))

    print(f"Issuing {len(requests)} vLLM requests "
          f"({N_CHUNKS} literal + {N_CHUNKS} paraphrase)...")
    print("(this typically takes 5-10 min on Qwen3-14B at concurrency=8)")
    print()

    def _progress(done: int, total: int) -> None:
        if done % 10 == 0 or done == total:
            print(f"  progress: {done}/{total}")

    results = client.run_many(requests, progress=_progress)
    print()
    print(f"Got {len(results)} results — analysing...")
    print()

    # Tally outcomes.
    parsed_ok = sum(1 for r in results if r.parsed is not None)
    parse_failed = len(results) - parsed_ok

    queries: list[str] = []
    for r in results:
        if r.parsed and isinstance(r.parsed.get("query"), str):
            queries.append(r.parsed["query"].strip())

    unique_queries = set(queries)
    print(f"Parsed JSON OK:          {parsed_ok} / {len(results)}")
    print(f"Parse failed:            {parse_failed} / {len(results)}")
    print(f"Queries with 'query' key: {len(queries)}")
    print(f"Distinct query strings:  {len(unique_queries)}")
    print()

    # The placeholder check — Blocker 3's symptom was 905 rows × 1 unique
    # query ("What is the numerical value of the chunk?"). If diversity
    # ratio is < 0.5 we have a problem.
    diversity_ratio = (
        len(unique_queries) / len(queries) if queries else 0.0
    )
    print(f"Query diversity ratio: {diversity_ratio:.3f} "
          f"(target > 0.80; <0.50 = Blocker 3 still present)")
    print()

    # Inspect a random 10 samples — pair literal + paraphrase per chunk.
    rng = random.Random(SEED)
    sample_indices = rng.sample(range(len(chunks)), min(INSPECT_SAMPLE_SIZE, len(chunks)))
    by_custom_id = {r.custom_id: r for r in results}

    print(f"=== {len(sample_indices)} random samples ===")
    for i in sample_indices:
        chunk = chunks[i]
        lit = by_custom_id.get(f"lit_{i}")
        para = by_custom_id.get(f"para_{i}")
        snippet = chunk["chunk_text"][:140].replace("\n", " ")
        print(f"\n--- sample {i}: page {chunk['page']} | "
              f"{chunk['chunk_chars']} chars ---")
        print(f"chunk: {snippet}{'...' if len(chunk['chunk_text']) > 140 else ''}")
        if lit and lit.parsed:
            print(f"literal:   {lit.parsed.get('query', '<no key>')}")
        else:
            print(f"literal:   ERROR ({lit.error if lit else 'no result'})")
        if para and para.parsed:
            print(f"paraphrase: {para.parsed.get('query', '<no key>')}")
        else:
            print(f"paraphrase: ERROR ({para.error if para else 'no result'})")

    print()
    print("=== VERDICT ===")
    if parsed_ok < len(results) * 0.80:
        print("FAIL — too many JSON parse failures. Investigate the vLLM "
              "response format / model state before any real run.")
        return 1
    if diversity_ratio < 0.50:
        print("FAIL — Blocker 3 still present. >50% of queries collapsed to "
              "a small set. Prompt engineering pass needed before "
              "re-materialisation. Inspect the samples above for the "
              "collapse pattern (placeholder string vs duplicate template "
              "vs all-empty).")
        return 1
    if diversity_ratio < 0.80:
        print("WARN — diversity ratio between 0.50 and 0.80. Not a hard "
              "fail, but check the samples for systematic patterns (e.g. "
              "every literal query starts with the same 5 words). Consider "
              "prompt tightening before the full run.")
        return 0
    print("PASS — Blocker 3 resolved on Qwen3-14B. Queries are diverse and "
          "JSON parse rate is healthy. Proceed to full re-materialisation.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
