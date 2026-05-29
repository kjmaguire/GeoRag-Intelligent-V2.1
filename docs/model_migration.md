# LLM Model Migration: qwen2.5:14b → Qwen 3.x MoE

**Module 5 · 2026-04-21 · target: MoE as default.**

---

> ## ⚠️ CURRENT STATE (2026-05-23) — read this first
>
> **The MoE-as-default target described below has been reverted.** The
> running and canonical model is `Qwen/Qwen3-14B-AWQ` (dense, AWQ INT4 on
> Marlin kernels) served by vLLM.
>
> **Why the revert:** the dev workstation A4500 (20 GB VRAM) is co-tenanted
> with `hatchet-worker-ai`, which runs `bge-small-en-v1.5` + `bge-reranker-base`
> + SPLADE++ on the same GPU. Holding Qwen3-30B-A3B (~17 GB AWQ) alongside
> those models forced the embed/rerank stack off-GPU and collapsed
> embedding throughput from ~144 chunks/sec to 3-4 chunks/sec. The 14B AWQ
> (~8 GB weights) leaves ~4 GB headroom for the co-tenants at
> `VLLM_GPU_MEM_UTIL=0.80`.
>
> **What's authoritative now:**
> - `.env` → `LLM_BACKEND=vllm`, `VLLM_MODEL=Qwen/Qwen3-14B-AWQ`,
>   `VLLM_SERVED_MODEL_NAME=Qwen/Qwen3-14B-AWQ`, `LLM_PRIMARY_MODEL=Qwen/Qwen3-14B-AWQ`
> - `docker/compose.vllm.yml` defaults point at the 14B
> - Architecture doc §08 (Inference canonical) + §11 (Dev workstation) state
>   the 14B as the canonical inference path
> - Helm chart `charts/georag/values.yaml` + k8s manifests target the 14B
>
> **What still mentions 30B-A3B:** historical sections of this doc, the
> dated `ops/audit/2026-04-21-*.md` audit reports, the dated
> `ops/validation/reports/qwen_moe_validation_*.json` validator runs,
> ADR-0003 + ADR-0004, and `ops/backlog/*` — all of those capture earlier
> states and are NOT edited to preserve the historical record.
>
> The narrative below is preserved as the record of the Module 5 / vLLM
> serving migration, but its model-name claims are no longer current.

---

## Goal

Move GeoRAG's dev/primary LLM from `qwen2.5:14b` (dense, ~9 GB) to a Qwen 3.x
MoE variant. MoE architecture is the target — routing-over-experts should give
better geological-reasoning quality at the 3B-active-params effective cost.
Not a one-off model swap — this is the new default for all geological
synthesis going forward.

## Hardware

- **NVIDIA RTX A4500 20 GB VRAM** (Ampere GA102, 7168 CUDA cores, 640 GB/s) — actual dev workstation as of 2026-05-08.
- **AMD Ryzen Threadripper Pro 5955WX** (16 physical / 32 logical cores, Zen 3, 280 W TDP) — `QWEN3_NUM_THREAD=12` on the offload path, leaves 4 physical cores for FastAPI / Postgres / Dagster.
- 64 GB host DDR4 RAM for CPU-offloaded layers when MoE total weights exceed VRAM (Q5_K_M scenario only — Q4_K_M fits cleanly in 20 GB).
- 1.8 TB NVMe — single-purpose machine, no other workloads competing for IO.
- Ollama 0.21.0 (digest-pinned in `docker-compose.yml`).
- Prod GPU tbd (Module 10 concern); planned profile is L40S 48 GB.

**Pre-2026-05-08 hardware (for context):** RTX 4080 16 GB + 8-core CPU. The 8K → 16K → 24K context oscillation in this doc reflects the prior card's 16 GB ceiling. The A4500 refresh let us push back to 24K cleanly and unlocks the v1.5-26 Q5_K_M validator path with a small (~0.5 GB) Threadripper-friendly CPU offload.

## Tag availability (probed 2026-04-21)

Plan's `q2_k` / `q3_k_m` sub-tag names do NOT exist in Ollama's library.
Custom quants come from HuggingFace GGUF + Ollama Modelfile, not from the
official library. Viable tags confirmed:

| Tag | Size | Architecture | Fit on 16 GB | Fit on 20 GB (A4500) |
|---|---|---|---|---|
| `qwen2.5:14b` | 9.0 GB | dense | clean baseline | clean (lots of headroom) |
| `qwen3:14b` | 9.3 GB | dense | clean — size-matched baseline | clean |
| `qwen3:30b-a3b` (Q4_K_M) | 18.0 GB | MoE (3B active) | ~2 GB offload, viable target | clean fit @ 24K ctx |
| `qwen3:30b-a3b-q5km` | 20.4 GB | MoE (3B active) | not viable | ~0.5–1 GB offload, viable on Threadripper |
| `qwen3.6:35b-a3b` | 23.0 GB | MoE (3B active) | ~10 GB offload, stretch | ~3-5 GB offload, slow |

Smaller Qwen 3.x variants: `qwen3:8b` (5.2 GB), `qwen3:4b` (tiny) — not
target but kept as knowledge.

## Validation protocol

**Script**: `ops/validation/qwen_moe_validator.py`
**Baseline**: `qwen2.5:14b` (current live; NOT deepseek-r1:14b which was never
deployed — see Module 5 Phase A audit finding OLA-01).
**Prompts**: 5 geological scenarios (tectonic, stratigraphic, fault mechanics,
geochem pathfinder, drilling-program design).
**Seed**: 42 (mandatory, reproducibility).
**Scoring**: keyword rubric per prompt — proxies domain accuracy.
**Gates**:
  - Score ≥ 90% of baseline average
  - Tokens/sec ≥ 5.0
  - Zero runtime errors
**Preference among passing candidates**: MoE (`*a3b*`) over dense, then
higher score, then higher throughput.

## Thinking-mode discipline

Qwen 3.x may emit reasoning trace tokens when thinking is on. On Ollama
0.21.0+ with `qwen3:30b-a3b` the authoritative knob is the top-level
`"think": <bool>` field on the chat-completions request; `chat_template_kwargs.
enable_thinking` is also accepted but its effect is unclear on this build,
so the orchestrator now sends only `think` to keep one source of truth.
Reasoning tokens land in the response's `reasoning` field — they never
appear in `choices[0].message.content` — but they **do** draw from
`num_predict`. The orchestrator bumps `num_predict` by `LLM_MAX_THINKING_TOKENS`
when thinking is on so the visible answer doesn't get truncated.

**Application-code rule**: the env knob is `ENABLE_THINKING_FREE_TEXT_DEFAULT`
(default **false**). All **grounded synthesis** call sites pass
`enable_thinking=False` explicitly (TOOL-CALL-01 fix), and every
**structured / JSON** path MUST also pass `enable_thinking=False`. The env
default only changes behavior for callers that don't pass an explicit
value (e.g. classifier escalation / exploratory free-text). Setting it to
`true` lets those callers think before answering.

For structured endpoints, also include `format: "json"` to constrain output:

```python
request_payload["think"] = False
request_payload["format"] = "json"
```

Forgetting the explicit `enable_thinking=False` on a structured endpoint =
reasoning tokens consume the answer budget under load. Every LLM call site in
`src/fastapi/app/` was audited during the qwen3 flip; the three grounded
synthesis sites pass `False` explicitly.

Baseline `qwen2.5:14b` does not have a thinking phase, so the knob is a
no-op when rolled back.

## Rollback

1. Revert `.env`: `LLM_PRIMARY_MODEL=qwen2.5:14b` (current baseline).
2. `docker compose restart fastapi` (clears model-pinning cache on the agent).
3. Keep the new tags pulled in Ollama — they don't cost VRAM when not in use
   (`OLLAMA_KEEP_ALIVE=5m` unloads after idle). If reclaim needed, `ollama rm <tag>`.
4. Do NOT `ollama rm qwen2.5:14b` until the replacement has been in production
   for at least one week with stable metrics.

## Env changes applied 2026-04-21

- `.env` + `.env.example`: added `OLLAMA_GPU_LAYERS=auto`, `OLLAMA_NOHISTORY=1`,
  `ENABLE_THINKING_FREE_TEXT_DEFAULT=false` (renamed from `ENABLE_THINKING=true`
  during the post-flip review — scope made explicit, default flipped to safe).
- `docker-compose.yml` ollama service: added `OLLAMA_GPU_LAYERS` + `OLLAMA_NOHISTORY`
  env vars plumbed from `.env` defaults.
- `LLM_PRIMARY_MODEL` intentionally NOT changed yet — waiting for validator
  winner.
- `OLLAMA_NUM_CTX` intentionally still `24576` — will drop to 8192 in the same
  commit that flips `LLM_PRIMARY_MODEL` to the MoE winner, paired with a
  proportional reduction in FastAPI's `MAX_CONTEXT_TOKENS`.

## Known landmines

1. **Tag naming**: Ollama's library uses default quants only. Custom quants
   must come from HuggingFace GGUF + `ollama create` with a local Modelfile.
   If a future migration wants Q2_K / Q3_K_M, plan for the HF → Modelfile
   path; don't assume tag parity with HF GGUF naming.

2. **MoE + low quant**: Qwen router gating degrades sharply at Q2_K (the
   router is noise-sensitive). Q3_K_M is the documented floor for MoE.
   Do not deploy Q2_K MoE in production.

3. **CPU offload latency**: on 16 GB VRAM with the 35B-A3B model (23 GB),
   expect ~10 GB offloaded → 2-5 tok/sec. The 30B-A3B (18 GB) is the
   sweet spot on dev hardware. Refresh 2026-05-08 — on the A4500 20 GB
   the Q4_K_M MoE fits cleanly with no offload at 24K context, and
   Q5_K_M (~20.4 GB) offloads ~0.5–1 GB to the 16-core Threadripper
   Pro CPU — fast enough on Zen 3 with `QWEN3_NUM_THREAD=12` (~12-15
   tok/s). Q5_K_M router quality > Q4_K_M is the v1.5-26 validation
   gate.

4. **MAX_CONTEXT_TOKENS / OLLAMA_NUM_CTX coupling**: if these diverge,
   Ollama silently truncates at its limit BEFORE the model sees the prompt.
   They MUST stay in lockstep. Current: both at 24576. Post-MoE-flip: both at
   8192 (MoE + q8_0 KV cache at 24K context would exceed 16 GB VRAM budget).

5. **Qdrant collections are named-vector**: payload writes need the dict form,
   not plain float-list. Already fixed in Module 4 Chunk 2; unrelated to this
   migration but worth noting if the synthesis path changes.

## Validation → production flip checklist

When the validator picks a winner:

1. [ ] Validator report committed to `ops/validation/reports/`
2. [ ] Winner matches expected profile (MoE, passing both gates, zero errors)
3. [ ] `src/fastapi/app/` grep audit complete: every LLM call classified as
      free-text or structured; structured paths have `enable_thinking=False` +
      `format="json"` overrides; PR review by backend-fastapi
4. [ ] `.env` flip: `LLM_PRIMARY_MODEL=<winner>`, `OLLAMA_NUM_CTX=8192` (if
      MoE) or stay at 24576 (if dense 14B winner)
5. [ ] FastAPI `MAX_CONTEXT_TOKENS` updated to match `OLLAMA_NUM_CTX`
6. [ ] `_SYSTEM_PROMPT_VERSION` bumped (new model → new behavior → cache bust)
7. [ ] `RETRIEVAL_STRATEGY_VERSION` bumped (if prompt content changed)
8. [ ] Module 5 PR B2 task: pre-commit hook enforcing prompt version bumps
      takes effect at or before this flip
9. [ ] `docker compose restart fastapi` + warm-up query to load the model
10. [ ] 24h observation: `answer_runs` rows show `backend_used=ollama`, no
       elevated error rate, latency distribution within expected envelope

## Post-flip cleanup

When the winner has been stable for 1 week:

1. `ollama rm qwen2.5:14b`
2. `ollama rm <loser-tag-1>` (the 2 non-winning Qwen 3.x candidates)
3. Update Module 10 doc-sweep backlog: arch §12 LLM reference bumps from
   `qwen2.5:14b (dev) + DeepSeek V3 (prod)` to `<winner> (dev) + DeepSeek V3
   (prod)`. Note MoE default in §08 too.

---

## Final state — applied 2026-04-21

Migration executed per plan. Full trail in `ops/audit/2026-04-21-llm-inference-audit.md`.

| Setting | Pre-flip | Post-flip | Post-TOOL-CALL-01 fix (current) |
|---|---|---|---|
| `LLM_PRIMARY_MODEL` | `qwen2.5:14b` | `qwen3:30b-a3b` | `qwen3:30b-a3b` |
| `OLLAMA_NUM_CTX` | 24576 | 8192 | **16384** |
| `MAX_CONTEXT_TOKENS` | 24000 | 7500 | **15000** |
| `_SYSTEM_PROMPT_VERSION` | 5 | 7 | **8** |
| `RETRIEVAL_STRATEGY_VERSION` | v2.1-citation-per-claim | v3-qwen3-moe | **v3.1-think-off-2026-04-21** |
| Thinking on synthesis call | n/a | env-default true | explicit **false** (3 call sites) |
| Empty-content guard | n/a | absent | **present** in `_call_openai_compatible_llm` |

**Key finding post-flip**: TOOL-CALL-01 investigation (`ops/audit/2026-04-21-tool-call-01-investigation.md`) discovered Qwen 3 thinking mode IS active on Ollama 0.21.0 (output goes to `reasoning` field, not `content`) and consumed 1000-2000 tokens per call — under the 8K context budget this caused intermittent empty-content responses on large prompts. Fix landed the same day: context doubled to 16K, thinking disabled on grounded-synthesis call sites, empty-content fallback guard added.

**Validator results** (2026-04-21 pre-flip): `qwen3:30b-a3b` scored 0.819 vs baseline 0.552 (+48%), sustained 14.6 tok/s, fits in 15.4 GiB VRAM with ~2 GB CPU offload. See `ops/validation/reports/qwen_moe_validation_*.json`.

**Known performance ceiling on dev GPU** (RTX 4080 16 GB): 30B-A3B cannot generate multi-paragraph narrative answers (e.g., full-project summaries) within the current `TIMEOUT_GATHER_S=120s`. Short factual queries return in ~90s warm. Prod GPU sizing should eliminate this. Dev users may need longer timeouts for long-narrative classes.

---

# Serving migration: Ollama → vLLM (2026-05-08)

**Status: in-progress.** Pre-prep for the production-readiness initiative. The
model itself stays the same — Qwen3-30B-A3B — but the serving layer flips
from Ollama (GGUF on llama.cpp) to vLLM (AWQ INT4 on Marlin kernels), and
the dev/prod split codified earlier in this doc collapses: vLLM runs in
both environments, hardware tier and context window scale up in prod.

DeepSeek is no longer in scope. Earlier sections of this doc reference
"DeepSeek V3 (prod)" — that was the architecture-doc placeholder for the
prod LLM and is being replaced by vLLM-served Qwen3-30B-A3B-AWQ in dev and
a larger / less-quantized variant in prod (sizing decision deferred to the
production-readiness doc).

## Why move

1. **Throughput.** vLLM's continuous batching + paged attention sustain
   higher tokens/sec than Ollama's llama.cpp backend on the same GPU,
   especially under concurrent load (Horizon worker pool, multi-user
   demo sessions, ingestion-time enrichment).
2. **Prefix caching.** vLLM's `--enable-prefix-caching` reuses the KV for
   the static system prompt across requests. We already structured
   `_call_openai_compatible_llm` to keep the system block byte-stable
   (R15) for exactly this reason; Ollama doesn't engage that cache.
3. **Prod parity.** Architecture §08 long planned vLLM for prod; running
   it in dev too means the inference-path code path is the same one prod
   exercises. No more "works in Ollama, breaks in vLLM" surprises.
4. **OpenAI-compat first-class.** vLLM is OpenAI-compat-native, not a
   shim — chunked streaming, guided decoding (JSON Schema), tool calls,
   prefix cache hit metrics all work without translation layers.

## Quant decision: AWQ INT4 (W4A16) with Marlin kernels

Constrained by the dev workstation's RTX A4500 (Ampere, compute 8.6,
20 GB VRAM):

- **FP8 is not viable.** Ampere has no native FP8; vLLM falls back to
  emulation (slow). FP8 needs Ada (8.9) or Hopper (9.0+).
- **BF16 30B is too large.** ~60 GB weights; doesn't fit.
- **INT8 (W8A16) is too large.** ~30 GB weights; still doesn't fit.
- **AWQ INT4 fits.** ~17 GB weights, ~1.5–2 GB KV cache budget at
  max-model-len=8192 with gpu-memory-utilization=0.92 (the realistic
  ceiling on the dev workstation A4500 — Windows desktop compositing
  permanently holds ~1–1.5 GiB of VRAM that the GPU compositor won't
  release to a non-display application; headless A4500 hardware can push
  to 0.95+).

Concrete model: `ELVISIO/Qwen3-30B-A3B-Instruct-2507-AWQ`. GPTQ-Int4 is the
fallback if AWQ throughput regresses on a future vLLM release. A dense
Qwen3-14B-AWQ A/B may be worth running once the cutover lands — frees
KV-cache headroom for higher concurrency.

## Phased rollout

### Phase 1 — pre-prep (this commit)

Non-breaking. Defaults still resolve to the Ollama path; the vLLM
configuration is staged so flipping `LLM_BACKEND=vllm` works once
infrastructure catches up.

- ✅ `Settings.VLLM_*` retargeted: `VLLM_MODEL=ELVISIO/Qwen3-30B-A3B-Instruct-2507-AWQ`,
  added `VLLM_QUANTIZATION=awq_marlin`, `VLLM_MAX_MODEL_LEN=8192`,
  `VLLM_GPU_MEMORY_UTILIZATION=0.95`, `VLLM_MAX_TOKENS=4096`.
- ✅ `.env.example` vLLM block rewritten for the A4500/AWQ profile.
- ✅ `LLM_BACKEND_FALLBACK` accepts `local_llm` (the new name) alongside
  the legacy `deepseek` alias for back-compat. Routing label emitted by
  the orchestrator switched to `__routing__:local_llm:…`.
- ✅ `config/ai.php`: `deepseek` provider entry removed (vendor gateway
  remains in `vendor/laravel/ai`, just not registered).
- ✅ `ops/validation/vllm_a4500_smoke.sh`: gating script that pulls the
  AWQ checkpoint, starts vLLM in a transient container, runs cold +
  warm-prefix completions, writes a JSON report. Must pass before
  Phase 2 commits land.

### Phase 2 — cutover (after the production-readiness doc lands)

Breaking changes to the serving layer. Sequenced so any single step is
revertable.

1. **Compose service.** Add a `vllm` service (image `vllm/vllm-openai:<pinned-digest>`)
   to the canonical compose source with the same env-var inputs the
   smoke test uses. `nvidia` runtime, named volume for the HF cache,
   `--enable-prefix-caching`, healthcheck on `/health`.
2. **Default flip.** `LLM_BACKEND=vllm` in `.env.example` and the
   `Settings` default. `LLM_PRIMARY_URL` / `LLM_PRIMARY_MODEL` removed
   in favour of the dedicated `VLLM_*` settings.
3. **Orchestrator cleanup.** `_call_openai_compatible_llm` currently
   sends Ollama-specific options (`num_ctx`, `num_thread`, `min_p`,
   `presence_penalty` in the `options` dict, top-level `think`). vLLM
   either ignores unknown keys (safe) or rejects them depending on
   build — the cutover removes Ollama-only options entirely. Sampling
   params move to top-level OpenAI-compat fields. Thinking-mode handling
   is rewritten around vLLM's reasoning-token output.
4. **Tier routing.** vLLM serves one model per process. The
   FAST/STANDARD/DEEP shape collapses to "always primary" on the local
   path; tier-routing remains active on the Anthropic backend.
   `OLLAMA_TIER_*` settings removed.
5. **Tests.** 22 FastAPI test files reference Ollama in fixtures /
   payload assertions; rewrite against the vLLM payload shape.
   `test_qwen3_payload_shape.py` becomes the canonical contract.
6. **Validator.** `ops/validation/qwen_moe_validator.py` retargeted to
   the vLLM endpoint (rename TBD; the geological prompts and rubric
   stay the same, only the client URL changes).
7. **Telemetry.** `ops/observability/moe_telemetry_exporter.py` and
   `docker/prometheus/rules/moe-alerts.yml` rebuilt around vLLM's
   native `/metrics` endpoint (Prometheus-format, includes prefix-cache
   hit rate, KV-cache utilization, queue depth — none of which Ollama
   exposed cleanly).
8. **Modelfiles + Ollama service.** `docker/ollama/Modelfile.*`
   deleted; ollama service removed from compose. Final cleanup.
9. **Docs.** Architecture doc §08 (LLM section) updated by the
   production-readiness doc (deliberate co-evolution per CLAUDE.md
   rule). Cold-start runbook, RUNBOOK.md, capacity-planning.md
   refreshed.

### Phase 3 — prod scale-up (production-readiness doc scope)

Hardware tier and quant change for prod. Likely candidates:

- **Single-instance** larger GPU: L40S 48 GB or A100 80 GB → BF16
  Qwen3-30B-A3B (no quant), max-model-len 32K+, higher concurrency.
- **Multi-instance** behind a load balancer for horizontal scaling of
  concurrent user sessions.
- **Prefix-cache sharing** investigation: vLLM v0.6+ supports
  prefix-cache across instances via a shared cache; relevant if multi-
  instance.

Topology decision deferred to the production-readiness doc.

## Rollback

Phase 1 is non-breaking — no rollback needed.

Phase 2 rollback: `LLM_BACKEND=ollama` re-enables the Ollama path *as
long as* the orchestrator still has the Ollama option dict (preserved
through step 3 of the rollout). After step 3 the rollback is git-revert
of the Phase 2 commits, not a config flip.

## Acceptance gates

Before Phase 2 starts:

- [ ] `vllm_a4500_smoke.sh` PASSes on the dev workstation (warm tokens/sec
      ≥ 15, response coherent, prefix cache engages).
- [ ] Production-readiness doc landed and reviewed.
- [ ] Compose source location confirmed (currently no `docker-compose.yml`
      at the root — open question).

Before Phase 2 lands:

- [ ] Golden query suite passes against vLLM at parity or better with
      Ollama on the same model.
- [ ] Hallucination failure suite shows no regression.
- [ ] `_call_openai_compatible_llm` end-to-end test against a vLLM
      container in CI (or a documented manual smoke).
- [ ] Architecture doc §08 update co-merged with the production-readiness
      doc.
