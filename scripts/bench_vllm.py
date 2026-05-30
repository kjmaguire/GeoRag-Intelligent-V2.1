"""Benchmark the live vLLM service end-to-end.

Measures the metrics that actually affect user-facing chat latency:
  - Time-to-first-token (TTFT)
  - End-to-end latency
  - Generation tokens/sec
  - Throughput under concurrent load (1, 5, 10 simultaneous users)

Runs against the running vllm container (http://localhost:8001/v1) — does NOT
touch Laravel, FastAPI, retrieval, or reranking. This isolates the inference
component so the numbers are directly comparable to a hypothetical Ollama+same-
model baseline.

Why this matters: when comparing vLLM+§04p vs Ollama+RAGFlow, parsing speed is
an offline ingest concern. The user-facing query cost is dominated by LLM
generation, which is what this script measures.

Usage:
    python scripts/bench_vllm.py                 # full run
    python scripts/bench_vllm.py --reps 5        # quick run
    python scripts/bench_vllm.py --concurrency 1 # single-user only
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

# Realistic geologist-style prompts. Mix of short factual + longer analytical.
PROMPTS: list[str] = [
    "Summarize the typical alteration zoning in a porphyry copper deposit.",
    "What does a downhole gamma log spike indicate in a uranium exploration context?",
    "Explain the difference between geometric and grade-tonnage estimation.",
    "List three indicators of a fertile granitoid for porphyry copper mineralization.",
    "What is the purpose of QA/QC blanks in assay sample submission?",
    "Describe the role of magnetic susceptibility in iron oxide-copper-gold (IOCG) targeting.",
    "What does an anomalous Au:Ag ratio of 1:5 in a vein system suggest?",
    "Explain the concept of a structural corridor in orogenic gold exploration.",
    "How is the cut-off grade determined for an open pit gold project?",
    "Describe the typical workflow of a Phase 1 due diligence on a junior mining property.",
    "What geophysical methods are most effective for sub-surface kimberlite pipe detection?",
    "Compare ICP-MS and fire assay as analytical methods for low-grade gold samples.",
    "What is the difference between an Inferred and Indicated mineral resource per NI 43-101?",
    "Explain core recovery percentage and why it matters for resource estimation.",
    "What does a high V:Cr ratio in mafic intrusives suggest about magmatic fertility?",
    "Describe the role of fluid inclusions in characterizing hydrothermal systems.",
    "What is the significance of a clay-altered shear zone hosting visible gold?",
    "Explain how a 3D structural model is built from oriented core and surface mapping.",
    "What are the geochemical pathfinders for sediment-hosted lead-zinc deposits?",
    "Describe a typical drill program design for a greenfield porphyry target.",
]

import os

VLLM_URL = os.environ.get("BENCH_VLLM_URL", "http://localhost:8001/v1/chat/completions")
MODEL = os.environ.get("BENCH_VLLM_MODEL", "Qwen/Qwen3-14B-AWQ")
MAX_TOKENS = 256  # capped so we measure consistent work per request


@dataclass
class RequestResult:
    prompt_idx: int
    ttft_s: float | None
    total_s: float
    prompt_tokens: int
    completion_tokens: int
    ok: bool
    error: str | None = None

    @property
    def tokens_per_sec(self) -> float:
        if not self.ok or self.total_s <= 0 or self.completion_tokens == 0:
            return 0.0
        gen_time = self.total_s - (self.ttft_s or 0)
        if gen_time <= 0:
            return 0.0
        return self.completion_tokens / gen_time


@dataclass
class ConcurrencyResult:
    concurrency: int
    requests: list[RequestResult] = field(default_factory=list)
    wall_clock_s: float = 0.0

    def summary(self) -> dict[str, Any]:
        ok = [r for r in self.requests if r.ok]
        if not ok:
            return {"concurrency": self.concurrency, "ok_count": 0, "error_count": len(self.requests)}
        ttft = [r.ttft_s for r in ok if r.ttft_s is not None]
        e2e = [r.total_s for r in ok]
        tps = [r.tokens_per_sec for r in ok]
        total_completion_tokens = sum(r.completion_tokens for r in ok)
        return {
            "concurrency": self.concurrency,
            "ok_count": len(ok),
            "error_count": len(self.requests) - len(ok),
            "wall_clock_s": round(self.wall_clock_s, 2),
            "ttft_s_p50": round(statistics.median(ttft), 3) if ttft else None,
            "ttft_s_p95": round(_p95(ttft), 3) if ttft else None,
            "e2e_s_p50": round(statistics.median(e2e), 3),
            "e2e_s_p95": round(_p95(e2e), 3),
            "per_req_gen_tok_per_s_p50": round(statistics.median(tps), 2),
            "aggregate_gen_tok_per_s": round(total_completion_tokens / self.wall_clock_s, 2),
        }


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    sorted_v = sorted(values)
    idx = max(0, int(len(sorted_v) * 0.95) - 1)
    return sorted_v[idx]


async def _stream_request(client: httpx.AsyncClient, prompt_idx: int, prompt: str) -> RequestResult:
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": MAX_TOKENS,
        "temperature": 0.7,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    start = time.perf_counter()
    ttft: float | None = None
    prompt_tokens = 0
    completion_tokens = 0
    try:
        async with client.stream("POST", VLLM_URL, json=payload, timeout=120.0) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                return RequestResult(
                    prompt_idx=prompt_idx,
                    ttft_s=None,
                    total_s=time.perf_counter() - start,
                    prompt_tokens=0,
                    completion_tokens=0,
                    ok=False,
                    error=f"HTTP {resp.status_code}: {body[:200].decode('utf-8', 'replace')}",
                )
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data: "):
                    continue
                data = line[len("data: ") :].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                # First chunk with content marks TTFT
                if ttft is None:
                    choices = obj.get("choices") or []
                    if choices and choices[0].get("delta", {}).get("content"):
                        ttft = time.perf_counter() - start
                if obj.get("usage"):
                    prompt_tokens = obj["usage"].get("prompt_tokens", 0)
                    completion_tokens = obj["usage"].get("completion_tokens", 0)
        return RequestResult(
            prompt_idx=prompt_idx,
            ttft_s=ttft,
            total_s=time.perf_counter() - start,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            ok=True,
        )
    except Exception as exc:  # noqa: BLE001
        return RequestResult(
            prompt_idx=prompt_idx,
            ttft_s=None,
            total_s=time.perf_counter() - start,
            prompt_tokens=0,
            completion_tokens=0,
            ok=False,
            error=f"{type(exc).__name__}: {exc}",
        )


async def warm_up(client: httpx.AsyncClient) -> None:
    """Single throwaway request to ensure FlashInfer kernels are hot."""
    print("[warm-up] firing one request...", flush=True)
    r = await _stream_request(client, prompt_idx=-1, prompt="Hello.")
    if not r.ok:
        print(f"[warm-up] WARNING: {r.error}", flush=True)
    else:
        print(f"[warm-up] ok ({r.total_s:.2f}s, ttft={r.ttft_s:.2f}s)", flush=True)


async def run_concurrency(client: httpx.AsyncClient, concurrency: int, reps: int) -> ConcurrencyResult:
    """Fire `reps` total requests, `concurrency` at a time."""
    print(f"\n[concurrency={concurrency}, reps={reps}] starting...", flush=True)
    sem = asyncio.Semaphore(concurrency)
    results: list[RequestResult] = []

    async def bound(i: int) -> None:
        prompt = PROMPTS[i % len(PROMPTS)]
        async with sem:
            r = await _stream_request(client, i, prompt)
            status = "ok" if r.ok else f"ERR({r.error})"
            print(
                f"  [{i + 1:>3}/{reps}] c={concurrency} ttft={r.ttft_s or 0:.2f}s "
                f"e2e={r.total_s:.2f}s gen={r.completion_tokens}tok "
                f"({r.tokens_per_sec:.1f} tok/s) {status}",
                flush=True,
            )
            results.append(r)

    wall_start = time.perf_counter()
    await asyncio.gather(*(bound(i) for i in range(reps)))
    wall_clock = time.perf_counter() - wall_start
    return ConcurrencyResult(concurrency=concurrency, requests=results, wall_clock_s=wall_clock)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reps", type=int, default=20, help="requests per concurrency level")
    parser.add_argument(
        "--concurrency",
        type=int,
        nargs="+",
        default=[1, 5, 10],
        help="concurrency levels to sweep",
    )
    parser.add_argument("--out", default="scripts/bench_vllm_results.json")
    args = parser.parse_args()

    print("=" * 70)
    print(f"vLLM benchmark — model={MODEL}, max_tokens={MAX_TOKENS}")
    print(f"target={VLLM_URL}")
    print("=" * 70)

    summaries: list[dict[str, Any]] = []
    async with httpx.AsyncClient() as client:
        await warm_up(client)
        for c in args.concurrency:
            result = await run_concurrency(client, concurrency=c, reps=args.reps)
            summary = result.summary()
            summaries.append(summary)
            print(f"\n[concurrency={c}] summary: {json.dumps(summary, indent=2)}", flush=True)

    print("\n" + "=" * 70)
    print("FINAL SUMMARY")
    print("=" * 70)
    for s in summaries:
        print(json.dumps(s, indent=2))

    with open(args.out, "w") as f:
        json.dump({"model": MODEL, "max_tokens": MAX_TOKENS, "results": summaries}, f, indent=2)
    print(f"\nResults written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
