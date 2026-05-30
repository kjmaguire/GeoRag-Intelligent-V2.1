"""Full-corpus contextual enrichment sweep.

Enriches ALL silver.document_passages with LLM-generated context headers —
including passages that are already embedded. After this script completes,
run reset_embeddings_for_reencode.py to clear embedding_id so the embed
sweep re-encodes everything with the enriched text.

Unlike the nightly enrich_passage_context Hatchet workflow (which only
touches passages with embedding_id IS NULL), this script re-enriches the
entire corpus so the contextual retrieval improvement applies to all 158k
passages, not just the 73k that haven't been embedded yet.

Usage (inside georag-fastapi container):
    python3 /app/scripts/enrich_all_passages_full.py

Options via env:
    ENRICH_CONCURRENCY=24   — parallel vLLM requests (default 24)
    ENRICH_BATCH=500        — DB fetch batch size (default 500)
    ENRICH_SKIP_EXISTING=1  — skip passages that already have contextualized_content
                              (default 1; set to 0 to re-enrich everything)
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from dataclasses import dataclass, field

import asyncpg
import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("georag.enrich_all")

PG_DSN = (
    f"postgresql://{os.environ.get('POSTGRES_USER', 'georag')}:"
    f"{os.environ.get('POSTGRES_PASSWORD', '')}@"
    f"{os.environ.get('POSTGRES_DIRECT_HOST', 'postgresql')}:"
    f"{os.environ.get('POSTGRES_DIRECT_PORT', '5432')}/"
    f"{os.environ.get('POSTGRES_DB', 'georag')}"
)
VLLM_URL = os.environ.get("VLLM_URL", "http://vllm:8000/v1")
VLLM_MODEL = os.environ.get("VLLM_MODEL", "Qwen/Qwen3-14B-AWQ")

CONCURRENCY = int(os.environ.get("ENRICH_CONCURRENCY", "24"))
BATCH_SIZE = int(os.environ.get("ENRICH_BATCH", "500"))
SKIP_EXISTING = os.environ.get("ENRICH_SKIP_EXISTING", "1") == "1"

_MAX_ENRICHED_LENGTH = 4096
_MAX_TEXT_TO_LLM = 2500


@dataclass
class EnrichStats:
    total: int = 0
    enriched: int = 0
    skipped: int = 0
    errors: int = 0
    start_time: float = field(default_factory=time.time)

    @property
    def elapsed(self) -> float:
        return time.time() - self.start_time

    @property
    def rate(self) -> float:
        return self.enriched / max(self.elapsed, 1)

    @property
    def eta_seconds(self) -> float:
        remaining = self.total - self.enriched - self.skipped - self.errors
        return remaining / max(self.rate, 0.001)


def _make_prompt(document_title: str, ordinal: int, total: int, text: str) -> str:
    snippet = text[:_MAX_TEXT_TO_LLM]
    return (
        "You are a geology document analyst. Given a document title and a passage "
        "from that document, write a brief context description (2-3 sentences) that "
        "situates the passage within the document. Focus on: what section this likely "
        "comes from, what specific topic it covers, and why it matters to a mining "
        "geologist.\n\n"
        f"Document: {document_title}\n"
        f"Position: passage {ordinal + 1} of {total}\n\n"
        f"Passage:\n{snippet}\n\n"
        "Context header (2-3 sentences, plain prose, no bullets):"
    )


def _combine(header: str, original: str) -> str:
    return f"{header[:300]}\n\n{original}"[:_MAX_ENRICHED_LENGTH]


async def _enrich_one(
    row: dict,
    http: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    pg_conn: asyncpg.Connection,
    stats: EnrichStats,
) -> None:
    async with semaphore:
        try:
            prompt = _make_prompt(
                row["document_title"],
                row["ordinal"],
                row["total_passages"],
                row["text"],
            )
            resp = await http.post(
                f"{VLLM_URL}/chat/completions",
                json={
                    "model": VLLM_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.3,
                    "max_tokens": 150,
                    "stream": False,
                },
                timeout=45.0,
            )
            resp.raise_for_status()
            header = resp.json()["choices"][0]["message"]["content"].strip()
            enriched = _combine(header, row["text"])
            await pg_conn.execute(
                "UPDATE silver.document_passages "
                "   SET contextualized_content = $1 "
                " WHERE passage_id = $2::uuid",
                enriched,
                row["passage_id"],
            )
            stats.enriched += 1
        except Exception as exc:
            stats.errors += 1
            log.warning("enrich_failed pid=%s err=%s", row["passage_id"][:8], exc)


async def main() -> None:
    log.info(
        "Starting full-corpus enrichment  concurrency=%d  skip_existing=%s",
        CONCURRENCY, SKIP_EXISTING,
    )

    pg = await asyncpg.connect(PG_DSN, statement_cache_size=0)

    # Count total work
    if SKIP_EXISTING:
        count_row = await pg.fetchrow(
            "SELECT COUNT(*) AS n FROM silver.document_passages WHERE contextualized_content IS NULL"
        )
    else:
        count_row = await pg.fetchrow("SELECT COUNT(*) AS n FROM silver.document_passages")

    stats = EnrichStats(total=count_row["n"])
    log.info("Passages to enrich: %d", stats.total)

    if stats.total == 0:
        log.info("Nothing to do — all passages already have contextualized_content.")
        await pg.close()
        return

    # Fetch in order of creation so we make steady progress that can be resumed
    offset = 0
    semaphore = asyncio.Semaphore(CONCURRENCY)

    async with httpx.AsyncClient() as http:
        while True:
            skip_clause = "WHERE contextualized_content IS NULL" if SKIP_EXISTING else ""
            rows = await pg.fetch(
                f"""
                SELECT dp.passage_id::text,
                       dp.text,
                       dp.ordinal,
                       COALESCE(r.title, dp.chunk_kind, 'Document') AS document_title,
                       COUNT(*) OVER (PARTITION BY dp.document_id) AS total_passages
                FROM silver.document_passages dp
                LEFT JOIN silver.reports r ON r.report_id = dp.document_id
                {skip_clause}
                ORDER BY dp.created_at ASC, dp.passage_id ASC
                LIMIT {BATCH_SIZE} OFFSET {offset}
                """
            )

            if not rows:
                break

            tasks = [
                _enrich_one(dict(r), http, semaphore, pg, stats)
                for r in rows
            ]
            await asyncio.gather(*tasks)

            offset += len(rows)
            pct = 100 * (stats.enriched + stats.skipped + stats.errors) / max(stats.total, 1)
            log.info(
                "Progress: %d/%d (%.1f%%)  enriched=%d  errors=%d  "
                "rate=%.1f/s  ETA=%.0fm",
                stats.enriched + stats.skipped + stats.errors,
                stats.total,
                pct,
                stats.enriched,
                stats.errors,
                stats.rate,
                stats.eta_seconds / 60,
            )

            # Yield to event loop briefly between batches
            await asyncio.sleep(0.1)

    await pg.close()
    log.info(
        "Done. enriched=%d  skipped=%d  errors=%d  elapsed=%.0fs",
        stats.enriched, stats.skipped, stats.errors, stats.elapsed,
    )


if __name__ == "__main__":
    asyncio.run(main())
