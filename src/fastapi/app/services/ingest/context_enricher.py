"""Contextual retrieval enrichment (Anthropic technique).

For each passage in silver.document_passages where contextualized_content
IS NULL, ask the LLM for a 2-3 sentence context header that situates the
chunk within its source document, and write header + original back to
contextualized_content. passage_embedder builds its dense and sparse
vectors from COALESCE(contextualized_content, text), so an enriched
passage becomes findable by what its section is ABOUT and not only by the
words it happens to contain.

Doc: https://www.anthropic.com/news/contextual-retrieval

2026-08-21 — this had never enriched a passage, for three stacked reasons.

1. The selection required `embedding_id IS NULL`, i.e. "not yet embedded".
   embed_pending_passages runs on `*/10 * * * *` and sets embedding_id
   within ten minutes of a passage landing; this workflow runs once a day.
   So a passage qualified only if it was created inside the ~10 minute
   window immediately before the daily run. retention_sweep's docstring
   still records the original reasoning -- "enrich_passage_context (04:30),
   before the embed sweep (05:45)" -- which was true until the */10 tick
   was added to the embed sweep and silently invalidated the ordering this
   workflow depended on.

2. 04:30 UTC is inside the 00:00-10:00 UTC window where the Flexible
   Server is deliberately Stopped, so even a qualifying passage had no
   database to be read from.

3. `_call_vllm_for_context` posted to settings.VLLM_URL, whose default is
   `http://vllm:8000/v1`. The local vllm service was removed 2026-07-30
   when Azure AI Foundry replaced it, so that hostname does not resolve.
   Every call would have raised, been caught per-passage, and counted into
   `result.errors` -- a list nothing reads and nothing alerts on.

The fix is all three: select on enrichment state alone, re-embed what gets
enriched, move the cron out of the shutdown window, and go through the
same backend resolution every other LLM caller uses.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

import asyncpg
import httpx

from app.db import bind_workspace_scope
from app.db.dsn import build_dsn
from app.services.ingest.pdf_report import WINDOW_CHARS

log = logging.getLogger("georag.ingest.context_enricher")

#: Headers are asked for as "2-3 sentences"; this is the hard stop.
_CONTEXT_HEADER_CHARS = 300

#: Backstop only, against a malformed row -- NOT a design parameter.
#:
#: This was a flat 4096 while the passage window is WINDOW_CHARS (5000),
#: so after the header every full-size passage lost its last ~1200
#: characters. That does not shorten a quoted answer: passage_embedder
#: writes row["text"] into the Qdrant payload and uses the enriched string
#: only to build vectors. It makes the truncated tail UNSEARCHABLE -- the
#: text is still returned once the chunk is retrieved, but nothing said in
#: those last characters can cause it to be retrieved, and no field on the
#: chunk shows that it happened. Derived from the window now rather than
#: guessed alongside it; the multiplier is headroom for producers that emit
#: larger chunks than pdf_report (public_geo_synthesis, kg_narrative), and
#: _combine_enriched logs when it actually bites.
_MAX_ENRICHED_LENGTH = _CONTEXT_HEADER_CHARS + 2 + (WINDOW_CHARS * 4)

#: How much of the passage the header-writing model gets to read. Same
#: derive-don-t-duplicate rule: at the old 3000 the model described the
#: first 60% of a 5000-character passage and named a topic the rest of it
#: had moved on from.
_MAX_PASSAGES_CONTEXT = WINDOW_CHARS


# One DSN builder for the whole service — see app/db/dsn.py for why
# sixty copies of this existed and what the drift cost.
_dsn = build_dsn


@dataclass
class ContextEnrichmentResult:
    workspace_id: str
    project_id: str | None
    passages_seen: int = 0
    passages_enriched: int = 0
    passages_skipped: int = 0
    errors: list[str] = field(default_factory=list)


def _make_enrichment_prompt(
    document_title: str,
    ordinal: int,
    total_passages: int,
    text: str,
) -> str:
    text_snippet = text[:_MAX_PASSAGES_CONTEXT]
    return (
        "You are a geology document analyst. Given a document title and a passage "
        "from that document, write a brief context description (2-3 sentences) that "
        "situates the passage within the document. Focus on: what section this likely "
        "comes from, what specific topic it covers, and why it matters to a mining geologist.\n\n"
        f"Document: {document_title}\n"
        f"Position: passage {ordinal + 1} of {total_passages}\n\n"
        f"Passage:\n{text_snippet}\n\n"
        "Context header (2-3 sentences, no bullet points, plain prose):"
    )


async def _call_llm_for_context(
    prompt: str,
    http_client: httpx.AsyncClient,
    base_url: str,
    model: str,
    headers: dict[str, str] | None = None,
) -> str:
    """One header generation against whichever OpenAI-compatible backend is live.

    This used to hardcode settings.VLLM_URL. The local vllm service was
    removed on 2026-07-30 and its default hostname stopped resolving, so
    every call raised into the per-passage handler below. The caller now
    resolves the endpoint through settings.effective_llm_url the same way
    llm_calls does, which means this follows the backend instead of
    pinning itself to one that was decommissioned.
    """
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": 150,
        "stream": False,
    }
    resp = await http_client.post(
        f"{base_url.rstrip('/')}/chat/completions",
        json=payload,
        headers=headers or None,
        timeout=30.0,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"].strip()


def _resolve_llm_target() -> tuple[str, str, dict[str, str]]:
    """(base_url, model, headers) for the active backend.

    Mirrors _call_openai_compatible_llm's backend branch rather than
    importing it: that function is the agent's streaming synthesis path
    and pulling it into an ingest module would drag the whole agent
    dependency tree behind it. What must stay in step is the auth shape,
    which is one header.
    """
    from app.config import settings

    base_url = settings.effective_llm_url
    model = settings.effective_llm_model
    headers: dict[str, str] = {}
    if settings.LLM_BACKEND == "azure":
        headers["api-key"] = settings.AZURE_FOUNDRY_API_KEY
    return base_url, model, headers


def _combine_enriched(context_header: str, original_text: str) -> str:
    """Header + passage: the string the embedder encodes, not what retrieval returns."""
    header = context_header[:_CONTEXT_HEADER_CHARS]
    combined = f"{header}\n\n{original_text}"
    if len(combined) > _MAX_ENRICHED_LENGTH:
        # Losing the tail costs searchability silently, so say it out loud.
        log.warning(
            "context_enricher.enriched_truncated chars=%d cap=%d lost=%d",
            len(combined), _MAX_ENRICHED_LENGTH,
            len(combined) - _MAX_ENRICHED_LENGTH,
        )
        combined = combined[:_MAX_ENRICHED_LENGTH]
    return combined


async def enrich_passage_context(
    *,
    workspace_id: str,
    project_id: str | None = None,
    batch_size: int = 8,
    max_passages: int | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> ContextEnrichmentResult:
    """Enrich pending passages with LLM-generated context headers."""
    result = ContextEnrichmentResult(workspace_id=workspace_id, project_id=project_id)

    own_client = False
    if http_client is None:
        http_client = httpx.AsyncClient()
        own_client = True

    base_url, model, auth_headers = _resolve_llm_target()

    pg_conn = await asyncpg.connect(_dsn(), statement_cache_size=0)
    try:
        # is_local=False — this is a dedicated, non-pooled connection with no
        # enclosing conn.transaction() (see the `asyncpg.connect()` above and
        # the `pg_conn.close()` in `finally` below). bind_workspace_scope's
        # default (is_local=True, SET LOCAL semantics) is a silent no-op
        # outside a transaction — the exact class of bug fixed in
        # passage_embedder.py this session, reproduced here: without this,
        # the GUC never actually gets set, and the canonical RLS policy on
        # silver.document_passages admits ALL rows when the GUC is unset —
        # so this cron (enrich_passage_context_wf, daily) would enrich and
        # mutate every workspace's pending passages, not just the one it was
        # invoked for. The SELECT above has no workspace_id filter in SQL at
        # all; RLS was the only thing meant to scope it.
        await bind_workspace_scope(
            pg_conn, workspace_id=workspace_id, site="ingest.context_enricher", is_local=False,
        )

        query = (
            "SELECT dp.passage_id::text, dp.text, dp.ordinal, "
            "       COALESCE(r.title, dp.chunk_kind, 'Document') AS document_title, "
            "       COUNT(*) OVER (PARTITION BY dp.document_id) AS total_passages "
            "  FROM silver.document_passages dp "
            "  LEFT JOIN silver.reports r ON r.report_id = dp.document_id "
            " WHERE dp.contextualized_content IS NULL "
            # `AND dp.embedding_id IS NULL` used to sit here, and it is why
            # this workflow had never enriched a passage -- see the module
            # docstring. Enrichment state is the only thing that decides
            # whether a passage needs enriching; the writeback below clears
            # embedding_id so the embed sweep picks the row back up.
            #
            # A document still being ingested is still being written to.
            # Enriching it races the ingest's own passage writes, and
            # clearing embedding_id underneath an open run would make
            # stale_run_detector._project_is_fully_embedded read that run as
            # incomplete and dispatch a recovery re-ingest of a document
            # that is perfectly fine.
            "   AND NOT EXISTS ( "
            "         SELECT 1 FROM silver.ingest_progress ip "
            "          WHERE ip.report_id = dp.document_id "
            "            AND ip.status IN ('queued', 'started') "
            "       ) "
            # Same predicate the embed sweep uses. A passage the OCR quality
            # router has rejected or queued for re-OCR is never embedded, so
            # writing a header for it buys nothing and costs one generation.
            "   AND (dp.ocr_status IS NULL "
            "        OR dp.ocr_status NOT IN ('rejected', 'pending_reocr')) "
            # An image passage's vector comes from the page render via
            # _encode_image_sync, not from its text at all -- passage_embedder
            # splits the batch on modality and only the text branch reads
            # COALESCE(contextualized_content, text). Enriching one is spend
            # that nothing can ever read.
            "   AND (dp.modality IS NULL OR dp.modality <> 'image') "
        )
        params: list = []
        if project_id:
            query += " AND r.project_id = $1::uuid "
            params.append(project_id)
        query += " ORDER BY dp.created_at ASC"
        if max_passages:
            query += f" LIMIT {int(max_passages)}"

        rows = await pg_conn.fetch(query, *params)
        result.passages_seen = len(rows)

        if not rows:
            log.info(
                "context_enricher.no_pending workspace=%s project=%s",
                workspace_id, project_id,
            )
            return result

        log.info(
            "context_enricher.start workspace=%s project=%s pending=%d",
            workspace_id, project_id, len(rows),
        )

        # This was `for batch in batches: for row in batch:` -- a nested walk
        # over the same rows one at a time, which made batch_size decorative:
        # every passage was its own awaited round trip. The generations are
        # independent of each other, so a batch is now real bounded
        # concurrency, which is what the parameter always claimed to be.
        #
        # The WRITES stay serial deliberately. asyncpg raises
        # InterfaceError("another operation is in progress") when two
        # coroutines use one connection at once, and this function holds a
        # single non-pooled connection on purpose so the session-scoped
        # workspace GUC bound above stays bound to the connection doing the
        # writing.
        for i in range(0, len(rows), batch_size):
            batch = rows[i : i + batch_size]
            headers = await asyncio.gather(
                *(
                    _call_llm_for_context(
                        _make_enrichment_prompt(
                            document_title=row["document_title"],
                            ordinal=row["ordinal"],
                            total_passages=row["total_passages"],
                            text=row["text"],
                        ),
                        http_client,
                        base_url,
                        model,
                        auth_headers,
                    )
                    for row in batch
                ),
                return_exceptions=True,
            )

            for row, context_header in zip(batch, headers, strict=True):
                try:
                    if isinstance(context_header, BaseException):
                        # Re-raised so one handler covers generation and
                        # write failures alike. CancelledError is a
                        # BaseException and is NOT caught below, so a
                        # cancelled task still tears the run down.
                        raise context_header
                    enriched = _combine_enriched(context_header, row["text"])
                    await pg_conn.execute(
                        "UPDATE silver.document_passages "
                        "   SET contextualized_content = $1, "
                        # Clearing embedding_id is what actually delivers the
                        # enrichment. The row's current vector was built from
                        # the bare text; the embed sweep re-encodes from
                        # COALESCE(contextualized_content, text) and upserts
                        # to the SAME Qdrant point id -- _passage_to_point_id
                        # derives it deterministically from passage_id -- so
                        # this replaces the vector in place rather than
                        # orphaning a point. Retrieval sees no gap: the old
                        # point stays live until the new one overwrites it.
                        "       embedding_id = NULL, updated_at = NOW() "
                        " WHERE passage_id = $2::uuid",
                        enriched,
                        row["passage_id"],
                    )
                    result.passages_enriched += 1
                except Exception as exc:
                    result.passages_skipped += 1
                    result.errors.append(
                        f"passage={row['passage_id'][:8]}:{type(exc).__name__}:{exc}"
                    )
                    log.warning(
                        "context_enricher.passage_failed pid=%s err=%s",
                        row["passage_id"][:8], exc,
                    )

    finally:
        await pg_conn.close()
        if own_client:
            await http_client.aclose()

    # `errors` used to be the only record of a failure and nothing reads it.
    # Log at WARNING with a sample when anything failed, so a dead backend
    # shows up in the log-alert rules instead of dying inside a return value.
    if result.errors:
        log.warning(
            "context_enricher.complete_with_errors workspace=%s project=%s "
            "enriched=%d skipped=%d errors=%d sample=%s",
            result.workspace_id, result.project_id,
            result.passages_enriched, result.passages_skipped,
            len(result.errors), result.errors[:3],
        )
    else:
        log.info(
            "context_enricher.complete enriched=%d skipped=%d errors=%d",
            result.passages_enriched, result.passages_skipped, len(result.errors),
        )
    return result


__all__ = ["enrich_passage_context", "ContextEnrichmentResult"]
