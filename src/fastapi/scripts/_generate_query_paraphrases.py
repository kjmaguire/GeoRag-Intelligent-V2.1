from __future__ import annotations

import json
import pathlib
import sys

import httpx

VLLM_URL = "http://georag-vllm:8000/v1/chat/completions"
MODEL = "Qwen/Qwen3-14B-AWQ"
OUT_DIR = pathlib.Path("/tmp/reranker-paraphrases")
OUT_DIR.mkdir(exist_ok=True)

CLEAN_IDS = {1, 2, 3, 8, 9, 11, 12, 20, 21}
STYLES = ["direct", "factual", "comparative", "analytical", "spatial", "conversational"]

records = []
for f in pathlib.Path("/tmp/reranker-train-real-only").glob("*.jsonl"):
    for line in f.read_text().strip().splitlines():
        records.append(json.loads(line))

seen = {}
for r in records:
    if r["query"] not in seen:
        seen[r["query"]] = r["positive_chunk_text"]

clean_pairs = [{"qid": i, "query": q, "positive": p}
               for i, (q, p) in enumerate(seen.items(), 1) if i in CLEAN_IDS]

print(f"Paraphrasing {len(clean_pairs)} clean queries...", flush=True)

SYSTEM = ("You are a geological information retrieval expert. "
          "Generate diverse rephrasings of geologist queries. "
          "Each rephrasing must be answerable by the SAME passage. "
          "Output exactly one query per line, no numbering, no explanations, no thinking.")

def do_paraphrase(qid, query, positive):
    prompt = (
        f"Original query: {query}\n\n"
        f"Answer passage (first 400 chars): {positive[:400]}\n\n"
        "Generate exactly 6 rephrasings, one per line, covering these styles in order:\n"
        "1. direct (short, keyword-forward)\n"
        "2. factual (explicit question for a specific fact)\n"
        "3. comparative (comparison or ranking)\n"
        "4. analytical (why / what caused / interpretation)\n"
        "5. spatial (location, depth, zone, or section anchored)\n"
        "6. conversational (casual chat-UI phrasing)\n\n"
        "Output ONLY the 6 query strings, one per line, nothing else."
    )
    resp = httpx.post(VLLM_URL, json={
        "model": MODEL,
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": prompt}],
        "temperature": 0.7, "max_tokens": 800,
        "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
    }, timeout=90)
    resp.raise_for_status()
    raw = resp.json()["choices"][0]["message"]["content"]
    # Strip <think>...</think> block if present
    if "</think>" in raw:
        raw = raw.split("</think>", 1)[1]
    lines = [line.strip().lstrip("0123456789.-) ").strip()
             for line in raw.strip().splitlines()
             if line.strip() and not line.strip().startswith("<")][:6]
    return [{"qid": qid, "original": query, "style": s, "variant": v, "positive_chunk_text": positive}
            # strict=False: `lines` is LLM output capped at [:6], so
            # fewer lines than STYLES is the expected case, not a bug.
            for s, v in zip(STYLES, lines, strict=False)]

all_variants = []
for p in clean_pairs:
    print(f"  Q{p['qid']:02d}: {p['query'][:70]}...", flush=True)
    try:
        vs = do_paraphrase(p["qid"], p["query"], p["positive"])
        all_variants.extend(vs)
        print(f"    -> {len(vs)} variants", flush=True)
    except Exception as e:
        print(f"    FAILED: {e}", file=sys.stderr, flush=True)

out = OUT_DIR / "paraphrases.jsonl"
with out.open("w") as fh:
    for v in all_variants:
        fh.write(json.dumps(v) + "\n")

print(f"\nWrote {len(all_variants)} variants to {out}", flush=True)
print("\n" + "="*70)
print("REVIEW (grouped by original query)")
print("="*70)
by_qid = {}
for v in all_variants:
    by_qid.setdefault(v["qid"], []).append(v)
for qid in sorted(by_qid):
    grp = by_qid[qid]
    print(f"\nQ{qid:02d} ORIGINAL: {grp[0]['original']}")
    for v in grp:
        print(f"  [{v['style']:15s}] {v['variant']}")
