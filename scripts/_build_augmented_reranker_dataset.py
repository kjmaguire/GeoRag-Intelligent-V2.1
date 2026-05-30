"""ADR-0011 Path A+B — Build augmented reranker training dataset.

Pipeline:
  1. Load the 9 clean original queries + their clean paraphrases (54 variants)
  2. Patch Q12 (vLLM thinking-mode leaked) with hand-written variants
  3. Fix bad-positive queries (Q07, Q10, Q17, Q18) by re-searching Qdrant
  4. Generate 20 new domain queries across 6 under-represented domains,
     find positives via Qdrant dense search
  5. Generate 4 paraphrases per new/fixed query via vLLM
  6. Mine hard negatives for ALL (query, positive) pairs from Qdrant
  7. Combine, deduplicate, split 85/8/7, write to /tmp/reranker-train-augmented/

Target: ~180-220 training pairs across 8+ geological domains and 6 phrasing styles.
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import random
import sys

import httpx

VLLM_URL = "http://georag-vllm:8000/v1/chat/completions"
MODEL = "Qwen/Qwen3-14B-AWQ"
OUT_DIR = pathlib.Path("/tmp/reranker-train-augmented")
OUT_DIR.mkdir(exist_ok=True)

PARAPHRASE_STYLES = ["direct", "factual", "comparative", "analytical", "spatial", "conversational"]

random.seed(42)

# ─────────────────────────────────────────────────────────────────────────────
# Step 1 — Load clean paraphrases from prior run
# ─────────────────────────────────────────────────────────────────────────────

def load_clean_paraphrases() -> list[dict]:
    """Load the 54 paraphrase variants; drop the 6 bad Q12 ones."""
    path = pathlib.Path("/tmp/reranker-paraphrases/paraphrases.jsonl")
    rows = []
    for line in path.read_text().strip().splitlines():
        r = json.loads(line)
        rows.append(r)

    # Filter out Q12 (qid=12) — they contain leaked thinking text
    clean = [r for r in rows if r["qid"] != 12]
    print(f"  Loaded {len(clean)} clean paraphrase variants (dropped Q12 bad output)")
    return clean


def q12_manual_paraphrases() -> list[dict]:
    """Hand-authored paraphrases for Q12 since vLLM kept leaking thinking."""
    original = "What resource estimation method was used in the most recent NI 43-101?"
    # Positive: ordinary kriging passage from Triple R deposit
    positive = None
    path = pathlib.Path("/tmp/reranker-train-real-only")
    for f in path.glob("*.jsonl"):
        for line in f.read_text().strip().splitlines():
            r = json.loads(line)
            if "What resource estimation method" in r["query"]:
                positive = r["positive_chunk_text"]
                break
    if positive is None:
        print("  WARNING: Could not find Q12 positive passage", file=sys.stderr)
        return []

    variants = [
        ("direct",        "NI 43-101 resource estimation method Triple R deposit"),
        ("factual",       "What geostatistical technique was used to estimate the Triple R Mineral Resource?"),
        ("comparative",   "How does ordinary kriging compare to other resource estimation methods for the Triple R deposit?"),
        ("analytical",    "Why was ordinary kriging chosen for the Triple R Mineral Resource estimation in the NI 43-101?"),
        ("spatial",       "What block model dimensions were used in the Triple R NI 43-101 resource estimate?"),
        ("conversational","How did they calculate the Triple R resource estimate in the latest 43-101?"),
    ]
    return [{"qid": 12, "original": original, "style": s, "variant": v, "positive_chunk_text": positive}
            for s, v in variants]


# ─────────────────────────────────────────────────────────────────────────────
# Step 2 — Load original clean queries (for hard-neg mining)
# ─────────────────────────────────────────────────────────────────────────────

def load_original_clean_queries() -> list[dict]:
    CLEAN_IDS = {1, 2, 3, 8, 9, 11, 12, 20, 21}
    records = []
    for f in pathlib.Path("/tmp/reranker-train-real-only").glob("*.jsonl"):
        for line in f.read_text().strip().splitlines():
            records.append(json.loads(line))
    seen: dict[str, str] = {}
    for r in records:
        if r["query"] not in seen:
            seen[r["query"]] = r["positive_chunk_text"]
    result = []
    for i, (q, pos) in enumerate(seen.items(), 1):
        if i in CLEAN_IDS:
            result.append({"query": q, "positive_chunk_text": pos, "source": "original_clean"})
    print(f"  Loaded {len(result)} original clean queries")
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Step 3 — Domain queries to add (Path A)
# ─────────────────────────────────────────────────────────────────────────────

DOMAIN_QUERIES = [
    # Geophysics (4)
    "What geophysical surveys were conducted on this property?",
    "What do the ground magnetic survey results indicate in the prospect area?",
    "What airborne electromagnetic anomalies have been identified on the property?",
    "How do the geophysical anomalies spatially correlate with the drill hole intersections?",

    # QA/QC and sampling (3)
    "What quality assurance and quality control procedures were used for the sampling program?",
    "What certified reference materials and blanks were inserted into the sample stream?",
    "What analytical method and detection limits were used for the assay program?",

    # Property tenure and location (3)
    "Where is this property located and how is it accessed?",
    "What is the total area of the mineral claims and who holds them?",
    "What infrastructure or services are available near the project area?",

    # Historical exploration (4)
    "What historical exploration work was completed on this property before the current program?",
    "Who were the previous operators and what work did they complete?",
    "What historical geochemical sampling surveys have been conducted on this property?",
    "When was mineralization on this property first identified and by whom?",

    # Recommendations and future work (3)
    "What does the qualified person recommend as next exploration steps?",
    "What is the estimated budget for the recommended work program?",
    "Which drill targets are considered highest priority for the next phase?",

    # Base metals / copper-gold (3)
    "What are the significant copper grade intersections from the drill program?",
    "What alteration types are associated with the copper-gold mineralization?",
    "What deposit model is applied to this copper-gold project?",
]


# ─────────────────────────────────────────────────────────────────────────────
# Async helpers — Qdrant + vLLM
# ─────────────────────────────────────────────────────────────────────────────

async def qdrant_search(client, model, query: str, top_k: int = 12) -> list[dict]:
    """Encode query and search georag_chunks, returning payload list."""
    import os
    vec = model.encode([query], normalize_embeddings=True)[0].tolist()
    results = await client.query_points(
        collection_name="georag_chunks",
        query=vec,
        using="",
        limit=top_k,
        with_payload=True,
    )
    return [{"text": h.payload.get("text", ""), "score": h.score,
             "section_title": h.payload.get("section_title", ""),
             "chunk_kind": h.payload.get("chunk_kind", "report")}
            for h in results.points]


def paraphrase_query_sync(query: str, positive: str, n: int = 4) -> list[str]:
    """Call vLLM synchronously; return n paraphrase strings."""
    styles = PARAPHRASE_STYLES[:n]
    prompt = (
        f"Original query: {query}\n\n"
        f"Answer passage (first 350 chars): {positive[:350]}\n\n"
        f"Generate exactly {n} rephrasings of the original query, one per line.\n"
        "Cover these styles in order:\n"
        + "\n".join(f"{i+1}. {s}" for i, s in enumerate(styles))
        + "\n\nOutput ONLY the query strings, one per line, nothing else."
    )
    resp = httpx.post(VLLM_URL, json={
        "model": MODEL,
        "messages": [
            {"role": "system", "content": "You are a geological information retrieval expert. Generate diverse rephrasings of geologist queries. Each rephrasing must be answerable by the SAME passage. Output one query per line, no numbering, no explanations."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.65, "max_tokens": 600,
        "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
    }, timeout=90)
    resp.raise_for_status()
    raw = resp.json()["choices"][0]["message"]["content"]
    if "</think>" in raw:
        raw = raw.split("</think>", 1)[1]
    lines = [ln.strip().lstrip("0123456789.-) ").strip()
             for ln in raw.strip().splitlines()
             if ln.strip() and not ln.strip().startswith("<")][:n]
    return lines


# ─────────────────────────────────────────────────────────────────────────────
# Main async pipeline
# ─────────────────────────────────────────────────────────────────────────────

async def main():
    import os
    from qdrant_client import AsyncQdrantClient
    from sentence_transformers import SentenceTransformer

    print("\n=== Phase 1: Load existing clean data ===", flush=True)
    clean_paraphrases = load_clean_paraphrases()
    q12_variants = q12_manual_paraphrases()
    original_clean = load_original_clean_queries()
    print(f"  Q12 manual patch: {len(q12_variants)} variants")

    print("\n=== Phase 2: Init Qdrant + embedding model ===", flush=True)
    model = SentenceTransformer("BAAI/bge-small-en-v1.5", device="cuda")
    client = AsyncQdrantClient(
        host=os.environ.get("QDRANT_HOST", "qdrant"),
        port=int(os.environ.get("QDRANT_PORT", "6333")),
    )
    print("  Ready.", flush=True)

    print("\n=== Phase 3: Fix bad-positive queries ===", flush=True)
    # Load originals with bad positives and re-search for better ones
    all_original: dict[str, str] = {}
    for f in pathlib.Path("/tmp/reranker-train-real-only").glob("*.jsonl"):
        for line in f.read_text().strip().splitlines():
            r = json.loads(line)
            if r["query"] not in all_original:
                all_original[r["query"]] = r["positive_chunk_text"]

    queries_list = list(all_original.items())
    BAD_IDS = {7, 10, 17, 18}  # 1-indexed

    fixed_queries: list[dict] = []
    for i, (q, _) in enumerate(queries_list, 1):
        if i not in BAD_IDS:
            continue
        hits = await qdrant_search(client, model, q, top_k=5)
        # Take first hit that is NOT public_geo_synthesis (prefer report passages)
        best = next((h for h in hits if h["chunk_kind"] != "public_geo_synthesis"), None)
        if best is None:
            best = hits[0] if hits else None
        if best and best["score"] > 0.72:
            print(f"  Q{i:02d} FIXED (score={best['score']:.4f}): {q[:60]}")
            fixed_queries.append({"query": q, "positive_chunk_text": best["text"],
                                   "source": f"fixed_q{i}"})
        else:
            print(f"  Q{i:02d} DROPPED (best score={best['score']:.4f} < 0.72): {q[:60]}")

    print(f"  Fixed {len(fixed_queries)} bad-positive queries")

    print("\n=== Phase 4: Find positives for 20 new domain queries ===", flush=True)
    new_domain_pairs: list[dict] = []
    for q in DOMAIN_QUERIES:
        hits = await qdrant_search(client, model, q, top_k=5)
        best = next((h for h in hits if h["chunk_kind"] != "public_geo_synthesis"), None)
        if best is None:
            best = hits[0] if hits else None
        if best and best["score"] > 0.68:
            new_domain_pairs.append({
                "query": q,
                "positive_chunk_text": best["text"],
                "source": "domain_new",
            })
            print(f"  ✓ score={best['score']:.4f} | {q[:60]}")
        else:
            score_str = f"{best['score']:.4f}" if best else "n/a"
            print(f"  ✗ score={score_str} | {q[:60]}")

    print(f"  Found positives for {len(new_domain_pairs)}/{len(DOMAIN_QUERIES)} domain queries")

    print("\n=== Phase 5: Generate paraphrases for fixed + new queries ===", flush=True)
    extra_paraphrases: list[dict] = []

    to_paraphrase = [(d, "fixed") for d in fixed_queries] + [(d, "domain") for d in new_domain_pairs]
    for d, kind in to_paraphrase:
        q = d["query"]
        pos = d["positive_chunk_text"]
        print(f"  Paraphrasing [{kind}]: {q[:65]}...", flush=True)
        try:
            variants = paraphrase_query_sync(q, pos, n=4)
            for style, v in zip(PARAPHRASE_STYLES[:4], variants):
                extra_paraphrases.append({
                    "original": q,
                    "variant": v,
                    "style": style,
                    "positive_chunk_text": pos,
                    "source": kind,
                })
            print(f"    -> {len(variants)} variants", flush=True)
        except Exception as e:
            print(f"    FAILED: {e}", file=sys.stderr, flush=True)

    print(f"  Generated {len(extra_paraphrases)} extra paraphrase variants")

    print("\n=== Phase 6: Build combined (query, positive) base ===", flush=True)
    # All (query, positive) pairs we need hard negatives for
    base_pairs: list[dict] = []

    # Original clean queries
    for d in original_clean:
        base_pairs.append({"query": d["query"], "positive": d["positive_chunk_text"]})

    # Paraphrase variants (original clean) — use variant as query
    for v in clean_paraphrases + q12_variants:
        base_pairs.append({"query": v["variant"], "positive": v["positive_chunk_text"]})

    # Fixed bad-positive queries (original)
    for d in fixed_queries:
        base_pairs.append({"query": d["query"], "positive": d["positive_chunk_text"]})

    # New domain queries (original)
    for d in new_domain_pairs:
        base_pairs.append({"query": d["query"], "positive": d["positive_chunk_text"]})

    # Paraphrases of fixed + new domain queries
    for v in extra_paraphrases:
        base_pairs.append({"query": v["variant"], "positive": v["positive_chunk_text"]})

    # Deduplicate by (query, positive[:100])
    seen_keys: set[str] = set()
    deduped: list[dict] = []
    for p in base_pairs:
        key = (p["query"].lower().strip(), p["positive"][:100])
        if key not in seen_keys:
            seen_keys.add(key)
            deduped.append(p)

    print(f"  Combined {len(base_pairs)} pairs → {len(deduped)} after dedup")

    print("\n=== Phase 7: Mine hard negatives for all pairs ===", flush=True)
    # Group by positive to avoid positive appearing as hard neg
    training_records: list[dict] = []
    skipped = 0

    # Load golden question hashes to avoid bench-leak
    golden_hashes: set[str] = set()
    try:
        import hashlib
        golden_path = pathlib.Path("/app/tests/data/golden_queries.json")
        if golden_path.exists():
            data = json.loads(golden_path.read_text())
            for item in data:
                q_text = item.get("query", item.get("question", ""))
                h = hashlib.sha256(q_text.lower().strip().encode()).hexdigest()[:16]
                golden_hashes.add(h)
        print(f"  Loaded {len(golden_hashes)} golden question hashes for bench-leak protection")
    except Exception as e:
        print(f"  Golden hash load failed (non-fatal): {e}")

    import hashlib as _hs
    for i, pair in enumerate(deduped):
        q = pair["query"]
        pos = pair["positive"]

        # Skip if query hash is in golden set (bench contamination risk)
        qhash = _hs.sha256(q.lower().strip().encode()).hexdigest()[:16]
        if qhash in golden_hashes:
            skipped += 1
            continue

        # Search for candidates
        hits = await qdrant_search(client, model, q, top_k=22)

        # Filter out the positive (by text prefix match)
        pos_prefix = pos[:80].lower().strip()
        hard_negs = [h["text"] for h in hits
                     if not h["text"][:80].lower().strip().startswith(pos_prefix[:40])][:10]

        if len(hard_negs) < 3:
            skipped += 1
            continue

        training_records.append({
            "query": q,
            "positive_chunk_text": pos,
            "hard_negative_chunk_texts": hard_negs,
        })

        if (i + 1) % 50 == 0:
            print(f"  Mined {i+1}/{len(deduped)} pairs...", flush=True)

    print(f"  Mined hard negs for {len(training_records)} pairs (skipped {skipped})")

    print("\n=== Phase 8: Split and write final dataset ===", flush=True)
    random.shuffle(training_records)
    n = len(training_records)
    n_train = int(n * 0.85)
    n_val = int(n * 0.08)

    splits = {
        "train": training_records[:n_train],
        "val": training_records[n_train:n_train + n_val],
        "test": training_records[n_train + n_val:],
    }

    for split, rows in splits.items():
        out = OUT_DIR / f"{split}.jsonl"
        with out.open("w") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")
        print(f"  {split}: {len(rows)} rows → {out}")

    # Write manifest
    manifest = {
        "total_pairs": n,
        "train": len(splits["train"]),
        "val": len(splits["val"]),
        "test": len(splits["test"]),
        "source_breakdown": {
            "original_clean": len(original_clean),
            "clean_paraphrases": len(clean_paraphrases) + len(q12_variants),
            "fixed_bad_positives": len(fixed_queries),
            "new_domain_queries": len(new_domain_pairs),
            "domain_paraphrases": len(extra_paraphrases),
        },
    }
    (OUT_DIR / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"\nManifest: {json.dumps(manifest, indent=2)}")

    await client.close()
    print("\n✓ Dataset build complete.")


if __name__ == "__main__":
    asyncio.run(main())
