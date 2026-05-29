#!/usr/bin/env python3
"""Qwen3 sampling-parameter validation sweep — V1.5-25.

Companion to `qwen_moe_validator.py`. Uses the same five-prompt rubric
and scoring path, but holds the model fixed at the live primary
(`qwen3:30b-a3b`) and varies the sampling parameters across two
configurations:

  A. **Pre-review (control)** — `temperature=0.7` only; everything else
     defaults to Ollama's built-in (`top_p=0.9`, `top_k=40`,
     no presence_penalty). This is what we shipped before 2026-04-27.

  B. **Post-review (treatment)** — Qwen team's published recommendations:
     `temperature=0.7, top_p=0.8, top_k=20, min_p=0`. With thinking off
     (the default for grounded synthesis) we additionally apply
     `presence_penalty=1.5` to mitigate Qwen3's repetition-loop tendency
     on long structured outputs.

The sweep runs each prompt N times per configuration (default 3) so the
per-config score is averaged across stochastic noise rather than reflecting
a single lucky/unlucky sample.

Decision rule (mirrors the V1.5-25 backlog item):
  * Treatment beats control by ≥5% rubric score → keep the new params.
  * Treatment is within ±5% of control → keep the new params (defensive
    parity; the published guidance has citation weight).
  * Treatment regresses by >5% → back out via the QWEN3_* env knobs.

Runs inside the georag-fastapi container — same prerequisites as the MoE
validator. All runs share `seed=42` so divergence is purely from the
sampling distribution.

Usage:
    docker exec georag-fastapi python /app/ops/validation/qwen3_sampling_sweep.py

Output: JSON report under `ops/validation/reports/sampling_sweep_<ts>.json`
plus a markdown summary at the same stem.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from openai import OpenAI

# Import the existing rubric + prompts from the MoE validator so the
# scoring path stays the single source of truth.
from qwen_moe_validator import (  # type: ignore[import-not-found]
    GEO_PROMPTS,
    GEO_RUBRIC,
)

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://ollama:11434/v1")
MODEL = os.getenv("QWEN3_VALIDATION_MODEL", "qwen3:30b-a3b")
RUNS_PER_PROMPT = int(os.getenv("QWEN3_VALIDATION_RUNS", "3"))

OUTPUT_DIR = Path(os.getenv("VALIDATION_OUTPUT_DIR", "ops/validation/reports"))
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


# Two configurations under test. Keep `temperature` identical so the only
# moving parts are the post-review additions.
CONFIGS: dict[str, dict[str, Any]] = {
    "A_pre_review_control": {
        "temperature": 0.7,
        # No top_p / top_k / min_p / presence_penalty — Ollama defaults apply.
    },
    "B_post_review_treatment": {
        "temperature": 0.7,
        "top_p": 0.8,
        "top_k": 20,
        "min_p": 0.0,
        "presence_penalty": 1.5,  # thinking-off path
    },
}


def _score_answer(prompt_label: str, answer: str) -> float:
    """Hit-rate against the rubric keywords for this prompt. 0..1."""
    keywords = GEO_RUBRIC[prompt_label]
    lower = answer.lower()
    hits = sum(1 for kw in keywords if kw.lower() in lower)
    return hits / len(keywords) if keywords else 0.0


def _call_one(client: OpenAI, sampling: dict[str, Any], prompt: str) -> dict[str, Any]:
    """One chat completion under the given sampling config. Thinking is
    OFF — that's the path our grounded-synthesis call sites use, and the
    one the new sampling defaults target."""
    extra_body: dict[str, Any] = {"think": False}
    # Translate options that aren't OpenAI-compat fields. The OpenAI shim
    # lifts top-level temperature/top_p/presence_penalty into options;
    # top_k and min_p don't have an OpenAI shape so we send via extra_body.
    options: dict[str, Any] = {}
    if "top_k" in sampling:
        options["top_k"] = sampling["top_k"]
    if "min_p" in sampling:
        options["min_p"] = sampling["min_p"]
    if options:
        extra_body["options"] = options

    payload_top: dict[str, Any] = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 2048,
        "seed": 42,
        "extra_body": extra_body,
    }
    for key in ("temperature", "top_p", "presence_penalty"):
        if key in sampling:
            payload_top[key] = sampling[key]

    t0 = time.time()
    response = client.chat.completions.create(**payload_top)
    latency_s = time.time() - t0

    answer = (response.choices[0].message.content or "").strip()
    completion_tokens = (
        response.usage.completion_tokens if response.usage else len(answer.split())
    )
    return {
        "answer": answer,
        "latency_s": latency_s,
        "completion_tokens": completion_tokens,
        "tok_per_sec": (completion_tokens / latency_s) if latency_s > 0 else 0.0,
    }


def main() -> int:
    client = OpenAI(base_url=OLLAMA_BASE_URL, api_key="ollama")
    ts = int(time.time())

    aggregate: dict[str, dict[str, Any]] = {}

    for config_name, sampling in CONFIGS.items():
        per_prompt: dict[str, list[dict[str, Any]]] = {}
        for label, prompt in GEO_PROMPTS:
            per_prompt[label] = []
            for run in range(RUNS_PER_PROMPT):
                print(f"[{config_name}] {label} run {run + 1}/{RUNS_PER_PROMPT}")
                result = _call_one(client, sampling, prompt)
                result["score"] = _score_answer(label, result["answer"])
                per_prompt[label].append(result)

        # Average across runs and prompts.
        all_scores: list[float] = []
        all_tps: list[float] = []
        for label, runs in per_prompt.items():
            all_scores.extend(r["score"] for r in runs)
            all_tps.extend(r["tok_per_sec"] for r in runs)

        aggregate[config_name] = {
            "sampling": sampling,
            "avg_score": sum(all_scores) / len(all_scores),
            "avg_tok_per_sec": sum(all_tps) / len(all_tps),
            "per_prompt": per_prompt,
        }

    # Decision.
    control = aggregate["A_pre_review_control"]["avg_score"]
    treatment = aggregate["B_post_review_treatment"]["avg_score"]
    delta = treatment - control
    delta_pct = (delta / control * 100) if control > 0 else 0.0

    if delta_pct >= 5:
        decision = "KEEP_NEW (improvement)"
    elif delta_pct >= -5:
        decision = "KEEP_NEW (parity — defensive)"
    else:
        decision = f"BACK_OUT (regression {delta_pct:.1f}%)"

    report = {
        "timestamp": ts,
        "model": MODEL,
        "runs_per_prompt": RUNS_PER_PROMPT,
        "configs": aggregate,
        "delta_score": delta,
        "delta_pct": delta_pct,
        "decision": decision,
    }
    out_json = OUTPUT_DIR / f"sampling_sweep_{ts}.json"
    out_json.write_text(json.dumps(report, indent=2))

    out_md = OUTPUT_DIR / f"sampling_sweep_{ts}.md"
    out_md.write_text(
        f"# Qwen3 sampling sweep — {MODEL}\n\n"
        f"- runs/prompt: {RUNS_PER_PROMPT}\n"
        f"- control (pre-review): avg_score={control:.3f}, "
        f"avg_tok/s={aggregate['A_pre_review_control']['avg_tok_per_sec']:.1f}\n"
        f"- treatment (Qwen-team defaults): avg_score={treatment:.3f}, "
        f"avg_tok/s={aggregate['B_post_review_treatment']['avg_tok_per_sec']:.1f}\n"
        f"- delta: {delta:+.3f} ({delta_pct:+.1f}%)\n"
        f"- **decision: {decision}**\n\n"
        f"Full data: `{out_json.name}`\n"
    )

    print()
    print(f"Decision: {decision}")
    print(f"Reports:  {out_json}\n          {out_md}")
    return 0 if "BACK_OUT" not in decision else 2


if __name__ == "__main__":
    raise SystemExit(main())
