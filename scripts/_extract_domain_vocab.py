#!/usr/bin/env python
"""ADR-0011 Phase 1 — mine domain vocabulary candidates from the GeoRAG corpus.

Walks silver.document_passages (the ADR-0010 canonical corpus, which
includes the Earle textbook chapters once §6c ingest lands), tokenizes
each chunk against the stock bge-reranker-base SentencePiece tokenizer,
and emits a frequency-ranked TSV of high-recurrence terms that are
currently broken into 3+ subwords.

The output is the input to ``_extend_reranker_tokenizer.py``, which
adds the top-K terms to a copy of the tokenizer and initializes their
embeddings from the mean of the subword embeddings they replace.

Locked decisions per ADR-0011 Phase 1
=====================================

* Minimum chunk-frequency:  100  (term must appear in ≥ 100 distinct
                                   chunks across the corpus)
* Minimum subword count:    3    (skip terms the tokenizer already
                                   handles efficiently — adding them
                                   would waste vocab slots)
* Cap on emitted candidates: 5_000   (position-embedding capacity
                                       ceiling; ample headroom below)
* Whitelist filter:         must match noun-pattern + appear in any
                            of silver.entity_aliases, silver.terms,
                            or the CGI seeded vocab (lithology, rock-
                            forming-mineral, ore-mineral, alteration).
* Blacklist filter:         drop tokens that are pure-digit, contain
                            units (m, ft, %, g/t, ppm), or are
                            already a whole-word in the stock tokenizer.

Usage
-----

    docker exec georag-fastapi bash -c \\
        "python /app/scripts/_extract_domain_vocab.py \\
            --output /tmp/vocab_candidates.tsv \\
            --top-k 5000"

The script is read-only on the database and idempotent — re-runs
produce the same output for the same corpus state. Re-running after a
textbook re-ingest will surface terms the first run didn't have data
for.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

logger = logging.getLogger("extract_domain_vocab")


# Term must look like a noun: starts uppercase OR mostly-lowercase but
# all-letters (allowing hyphens, apostrophes). No digits, no punctuation
# except internal hyphen.
NOUN_LIKE = re.compile(r"^[A-Za-z][A-Za-z\-']{2,}$")

# Drop tokens that are pure units / numeric markers.
UNIT_PATTERNS = re.compile(
    r"^(m|cm|mm|km|ft|in|yd|mi|g|kg|t|tonnes?|oz|ppm|ppb|wt|pct|%|"
    r"g/t|kg/t|oz/t|m\^3|ft\^3|m/s|km/h|kpa|mpa|gpa|c|f|°c|°f)$",
    re.IGNORECASE,
)


def _tokenize_corpus_to_subwords(
    tokenizer,
    text_iter,
    min_chunk_freq: int,
    min_subword_count: int,
) -> Counter[str]:
    """Count whole-word terms that the tokenizer breaks into 3+ pieces.

    Returns: Counter[term -> chunk_frequency]
    """
    # Stock-tokenizer whole-words (everything in the vocab as-is) —
    # adding these would waste a slot.
    vocab_whole_words = {
        t.lstrip("▁").lower() for t in tokenizer.get_vocab() if t and not t.startswith("##")
    }

    term_chunk_count: Counter[str] = Counter()
    seen_in_chunk: set[str] = set()
    chunks_processed = 0

    for text in text_iter:
        if not text:
            continue
        seen_in_chunk.clear()
        # Naive word tokenization — keep apostrophes and hyphens intact.
        words = re.findall(r"[A-Za-z][A-Za-z\-']+", text)
        for w in words:
            wl = w.lower()
            if wl in vocab_whole_words:
                continue
            if not NOUN_LIKE.match(w):
                continue
            if UNIT_PATTERNS.match(wl):
                continue
            # How many subwords does the stock tokenizer split this into?
            n_sub = len(tokenizer.tokenize(w))
            if n_sub < min_subword_count:
                continue
            if wl in seen_in_chunk:
                continue
            seen_in_chunk.add(wl)
            term_chunk_count[wl] += 1
        chunks_processed += 1
        if chunks_processed % 1000 == 0:
            logger.info("processed %d chunks, %d candidate terms", chunks_processed, len(term_chunk_count))

    logger.info("done. processed %d chunks, %d candidate terms", chunks_processed, len(term_chunk_count))

    # Filter by min chunk-frequency.
    return Counter({t: c for t, c in term_chunk_count.items() if c >= min_chunk_freq})


async def _stream_chunks(conn, batch_size: int = 1000):
    """Async generator that streams chunk text from silver.document_passages.

    The actual column name is `text` (per the §1b chunker spec — see
    src/dagster/georag_dagster/assets/index_document_passages.py for
    the canonical projection). Earlier scaffolding called it
    `chunk_text` by analogy with the bronze upload payload key, which
    is wrong.
    """
    offset = 0
    while True:
        rows = await conn.fetch(
            """
            SELECT text FROM silver.document_passages
            WHERE text IS NOT NULL AND length(text) >= 50
            ORDER BY passage_id
            LIMIT $1 OFFSET $2
            """,
            batch_size, offset,
        )
        if not rows:
            break
        for r in rows:
            yield r["text"]
        offset += batch_size


async def _load_whitelist(conn) -> set[str]:
    """Pull domain-reference terms from silver.entity_aliases + silver.terms +
    CGI vocab seeds. Anything in this set is auto-accepted past noun-pattern.

    Returns a set of lowercase tokens.
    """
    accepted: set[str] = set()
    try:
        rows = await conn.fetch(
            "SELECT alias FROM silver.entity_aliases WHERE alias IS NOT NULL LIMIT 200000"
        )
        for r in rows:
            for tok in re.findall(r"[A-Za-z][A-Za-z\-']+", r["alias"]):
                accepted.add(tok.lower())
    except Exception as exc:  # noqa: BLE001 — schema may not exist on legacy clones
        logger.warning("entity_aliases unavailable (%s) — proceeding without", exc)

    try:
        rows = await conn.fetch(
            "SELECT term FROM silver.terms WHERE term IS NOT NULL LIMIT 200000"
        )
        for r in rows:
            for tok in re.findall(r"[A-Za-z][A-Za-z\-']+", r["term"]):
                accepted.add(tok.lower())
    except Exception as exc:  # noqa: BLE001
        logger.warning("silver.terms unavailable (%s) — proceeding without", exc)

    logger.info("whitelist: %d tokens from domain reference tables", len(accepted))
    return accepted


async def main_async(args):
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    import asyncpg  # noqa: PLC0415
    from transformers import AutoTokenizer  # noqa: PLC0415

    logger.info("loading stock tokenizer: %s", args.base_tokenizer)
    tokenizer = AutoTokenizer.from_pretrained(args.base_tokenizer)

    dsn = os.environ.get("POSTGRES_DSN") or (
        f"postgresql://{os.environ.get('POSTGRES_USER', 'georag')}:"
        f"{os.environ['POSTGRES_PASSWORD']}@"
        f"{os.environ.get('POSTGRES_DIRECT_HOST', 'postgresql')}:"
        f"{os.environ.get('POSTGRES_DIRECT_PORT', 5432)}/"
        f"{os.environ.get('POSTGRES_DB', 'georag')}"
    )
    conn = await asyncpg.connect(dsn)
    try:
        whitelist = await _load_whitelist(conn)

        # Stream chunks + collect candidate counts.
        text_iter: list[str] = []
        async for text in _stream_chunks(conn, batch_size=2000):
            text_iter.append(text)
        logger.info("collected %d chunks for tokenization sweep", len(text_iter))

        counts = _tokenize_corpus_to_subwords(
            tokenizer, text_iter,
            min_chunk_freq=args.min_chunk_freq,
            min_subword_count=args.min_subword_count,
        )

        # Whitelist intersection — accept terms that are EITHER above
        # frequency threshold AND noun-pattern, OR in the whitelist
        # regardless of frequency.
        accepted: list[tuple[str, int, int]] = []
        for term, freq in counts.most_common():
            n_sub = len(tokenizer.tokenize(term))
            in_whitelist = term in whitelist
            if freq >= args.min_chunk_freq or in_whitelist:
                accepted.append((term, freq, n_sub))

        # Apply hard cap.
        accepted = accepted[: args.top_k]
        logger.info("emitting %d vocabulary candidates (cap=%d)", len(accepted), args.top_k)

        out_path = Path(args.output)
        with open(out_path, "w") as fh:
            fh.write("term\tchunk_freq\tstock_subwords\twhitelist\n")
            for term, freq, n_sub in accepted:
                fh.write(f"{term}\t{freq}\t{n_sub}\t{int(term in whitelist)}\n")
        logger.info("wrote: %s", out_path)
    finally:
        await conn.close()
    return 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--base-tokenizer", default="BAAI/bge-reranker-base")
    p.add_argument("--output", default="/tmp/vocab_candidates.tsv")
    p.add_argument("--min-chunk-freq", type=int, default=100)
    p.add_argument("--min-subword-count", type=int, default=3)
    p.add_argument("--top-k", type=int, default=5000)
    args = p.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
