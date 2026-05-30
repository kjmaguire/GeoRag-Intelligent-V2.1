"""ADR-0011 Path A+B — Generate paraphrase variants of clean real queries.

Calls vLLM (Qwen3-14B-AWQ) to produce 6 stylistically diverse paraphrases
per query, all answerable by the same positive passage.

Styles generated per query:
  1. direct      — short, keyword-forward ("Gold grades BattleNorth 2021")
  2. factual     — question-form, specific ("What are the reported gold grades...")
  3. comparative — asks for comparison or ranking
  4. analytical  — asks for interpretation/reason
  5. spatial     — location or depth anchored
  6. conversational — casual, as a geologist might phrase it in a chat UI

Output: /tmp/reranker-paraphrases/<query_id>.jsonl
  Each line: {"query_id": int, "original": str, "variant": str, "style": str,
              "positive_chunk_text": str}
"""
from __future__ import annotations

import json
import pathlib
import re
import sys
import httpx

VLLM_URL = "http://georag-vllm:8000/v1/chat/completions"
MODEL = "Qwen/Qwen3-14B-AWQ"
OUT_DIR = pathlib.Path("/tmp/reranker-paraphrases")
OUT_DIR.mkdir(exist_ok=True)

# ── Load clean queries ───────────────────────────────────────────────
CLEAN_IDS = {1, 2, 3, 8, 9, 11, 12, 20, 21}   # 1-indexed from audit

records = []
for f in pathlib.Path("/tmp/reranker-train-real-only").glob("*.jsonl"):
    for line in f.read_text().strip().splitlines():
        records.append(json.loads(line))

seen = {}
for r in records:
    q = r["query"]
    if q not in seen:
        seen[q] = r["positive_chunk_text"]

clean_pairs = []
for i, (q, pos) in enumerate(seen.items(), 1):
    if i in CLEAN_IDS:
        clean_pairs.append({"query_id": i, "query": q, "positive": pos})

print(f"Generating paraphrases for {len(clean_pairs)} clean queries...")

STYLES = ["direct", "factual", "comparative", "analytical", "spatial", "conversational"]

SYSTEM = (
    "You are a geological information retrieval expert. "
    "You generate diverse rephrasings of geologist queries. "
    "Each rephrasing must be answerable by the SAME passage as the original. "
    "Output exactly one query per line, no numbering, no explanations."
)

def paraphrase_query(query_id: int, query: str, positive: str) -> list[dict]:
    prompt = f"""Original query: {query}

Answer passage (first 400 chars): {positive[:400]}

Generate exactly 6 rephrasings of the original query, one per line.
Cover these styles in order:
1. direct (short, keyword-forward, like a search engine query)
2. factual (explicit question asking for a specific fact)
3. comparative (asks for comparison, ranking, or "how does X compare to Y")
4. analytical (asks why, what caused, or for interpretation)
5. spatial (anchored to location, depth, zone, or section)
6. conversational (casual phrasing a geologist would use in a chat interface)

Rules:
- Every rephrasing must be answerable by the SAME passage
- Keep geological entities (project names, hole IDs, elements) accurate
- Do NOT invent facts not in the original query
- Output only the 6 query strings, one per line, no numbering"""

    resp = httpx.post(
        VLLM_URL,
        json={
            "model": MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.7,
            "max_tokens": 512,
            "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
        },
        timeout=60,
    )
    resp.raise_for_status()
    text = resp.json()["choices"][0]["message"]["content"].strip()
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    # Take first 6 non-empty lines
    lines = lines[:6]
    results = []
    for style, variant in zip(STYLES, lines):
        results.append({
            "query_id": query_id,
            "original": query,
            "style": style,
            "variant": variant,
            "positive_chunk_text": positive,
        })
    return results


all_variants: list[dict] = []
for pair in clean_pairs:
    print(f"  Q{pair['query_id']:02d}: {pair['query'][:70]}...")
    try:
        variants = paraphrase_query(pair["query_id"], pair["query"], pair["positive"])
        all_variants.extend(variants)
        print(f"    → {len(variants)} variants")
    except Exception as e:
        print(f"    ✗ FAILED: {e}", file=sys.stderr)

# Write output
out_file = OUT_DIR / "paraphrases.jsonl"
with out_file.open("w") as fh:
    for v in all_variants:
        fh.write(json.dumps(v) + "\n")

print(f"\nWrote {len(all_variants)} variants to {out_file}")

# Pretty-print for review
print("\n" + "="*70)
print("PARAPHRASE REVIEW (grouped by original query)")
print("="*70)
by_qid: dict[int, list[dict]] = {}
for v in all_variants:
    by_qid.setdefault(v["query_id"], []).append(v)

for qid in sorted(by_qid):
    grp = by_qid[qid]
    print(f"\nQ{qid:02d} ORIGINAL: {grp[0]['original']}")
    for v in grp:
        print(f"  [{v['style']:15s}] {v['variant']}")
