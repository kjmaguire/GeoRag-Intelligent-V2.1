# Senior Review — Module 6 Phase B Chunk 2: Two-Stage Citation Pipeline

**Reviewer:** senior-reviewer (Opus), 2026-04-22
**Scope:** two-stage citation pipeline draft (Chunk 2), pre-apply
**Authority:** Global Invariants 1 (citation-first) + 2 (refusal-is-product) + 14 (evidence-model hard gate)

## Verdict: APPROVE WITH CONDITIONS

The architecture is sound. Stage 1 / Stage 2 separation matches §04h / §6 B1. Flag-gating is clean. Failure modes are non-fatal and preserve the legacy path. The `has_target` CHECK correctly keeps tool-slot bindings (no-FK) out of the table today, and the design doc is honest about that. All conditions below are small and apply-facing.

## Files reviewed

- `src/fastapi/app/agent/citation_binding.py` (new)
- `src/fastapi/app/services/span_resolver.py` (new)
- `src/fastapi/app/services/answer_run_store.py` (lines 246–405 — stubs replaced with real impls)
- `src/fastapi/app/agent/orchestrator.py` (lines 1184–1211 prompt selector; 3789–3813 Stage 1; 4717–4773 Stage 2)
- `src/fastapi/app/config.py` (lines 356–365 — CITATION_SPAN_RESOLVER_ENABLED)
- `docs/module-6-chunk-2-design.md`
- Chunk 1 DDL context (via `ops/audit/2026-04-21-citation-guards-audit.md`)

## Per-OFR verdicts

- **OFR-1 (FK SET NULL vs RESTRICT)** — **Keep SET NULL for now; track post-B8.5 RESTRICT-flip in backlog.** Module 3 B3's RESTRICT precedent was about live audit rows. Today `answer_citation_items.evidence_id` is nullable-by-design (tool-slot bindings) and 0 rows exist. RESTRICT would block evidence pruning even where no citation depends on it. Post-B8.5 flip is correct long term.

- **OFR-2 (`[ev:<8-char>]` collision risk)** — **Accept.** Birthday-collision ≈3×10⁻⁵ at n=50 per run. Bindings per-run-scoped. 8→12 fallback implemented in `bind_evidence`. Non-issue.

- **OFR-3 (`partial_resolution_rate` column on `answer_runs`)** — **Defer to Chunk 4.** Pairs naturally with `hybrid_delayed_attachment` enum addition. Telemetry log adequate until then.

- **OFR-4 (unresolved marker `[DATA:99]`)** — **Telemetry-only correct for Chunk 2; Chunk 3 must harden to refusal.** Belongs with B2 completeness guard + B4 refusal payload.

- **OFR-5 (dash-form deprecation timeline)** — **Approve: remove `_LEGACY_DASH_RE` when Chunk 3 ships.** Condition: the removal PR must bump `_SYSTEM_PROMPT_VERSION`.

## Per-invariant assessment

- **Invariant 1 (citation-first) — PASS with conditions.** Bound set → marker → span → FK is deterministic. `by_marker` O(1) lookup prevents ambiguity. Condition: design doc §3 must loudly state that **zero citation rows land on the flag flip today** (has_target + 0 evidence_items + tool-slot-no-FK).
- **Invariant 2 (refusal-is-product) — PASS.** Chunk 2 defers guard/refusal to Chunks 3–4 correctly. The "0 markers resolved" silent-success path must be hardened in Chunk 3.
- **Invariant 7 (no precedence) — PASS.** `bind_evidence` counter is monotonic; `BoundEvidenceSet.add` can't produce silent merges in the current path. Conflict detection correctly deferred to Chunk 4 / B7.
- **Invariant 14 (evidence-model hard gate) — PASS.** `has_target` CHECK enforces correctly; `resolve_spans` skips tool-slot bindings with no FK before INSERT.

## Design-seam concerns (beyond OFRs)

1. **Items + spans non-atomic write.** `insert_citation_items` and `batch_insert_citation_spans` each acquire their own `pool.acquire()` at orchestrator lines 4754 + 4768. Crash between them leaves orphan items with no spans. FK direction is CASCADE-safe; audit trail is half-written. **Fix: wrap both in a single `async with conn.transaction():` block.** Or document non-atomicity in runbook.

2. **`BoundEvidenceSet.add` silently overwrites on marker collision.** Counter-based assignment can't produce collisions today; advisory assertion recommended for future safety.

3. **Prompt-version discipline.** Two new large prompt variants added without bumping `_SYSTEM_PROMPT_VERSION`. Rationale in code comments is correct (inert until flag flips, cache reflects via `use_colon` branching). **Condition:** apply dispatch MUST bump 8→9 co-committed with the flag flip. PV-01 pre-commit hook would flag this draft edit — either hook was bypassed or needs a documented exception.

4. **Feature-flag thread safety.** `settings` is Pydantic-Settings singleton, immutable after module load. Flag flip requires FastAPI process restart (design doc specifies). Three call sites all read at call time — safe.

5. **Span offsets reference `normalized_text`, not `response.text`.** `resolve_spans` returns spans indexing into the normalized (dash→colon rewritten) string. Orchestrator passes raw `response.text` in, gets normalized offsets, and does NOT substitute. If any dash-form rewrite happened, stored spans are off. **Fix: either (a) `response.text ← normalized_text` before return, or (b) store normalized_text as the canonical answer spans reference.** Design doc §4 flags this; implementation doesn't. Must fix before flag flip.

## Tool-slot writability (explicit)

**Are `[DATA:N]` tool-slot citations writable given `has_target`? NO in Chunk 2.** Tool-slot bindings have `evidence_id=None` and `passage_id=None`. `resolve_spans` skips them (lines 215–223). **When the flag flips, only `[ev:*]` bindings produce rows. Since `evidence_items` has 0 rows, zero rows will be written.**

**Acceptable for Chunk 2.** The span resolver + binding + DB schema + telemetry all exercise correctly. Chunk 3 option (a): ship `search_documents` chunk_id → passage_id lookup so `[NI43:N]` / `[PUB:N]` bindings get `passage_id` populated at Stage 2. Smaller than (b) fast-tracking B8.5.

## Apply-dispatch readiness: NOT READY

**Required before flag flip:**

- **C1 (blocking)**: orchestrator must use `normalized_text` as canonical answer, OR document off-by-ε span offsets when `legacy_dash_rewrites > 0`. (Seam #5)
- **C2 (blocking)**: apply dispatch bumps `_SYSTEM_PROMPT_VERSION` 8→9 co-committed with flag flip + FastAPI restart. (Seam #3)
- **C3 (important)**: wrap items+spans INSERTs in a single transaction, OR document non-atomicity in runbook. (Seam #1)
- **C4 (advisory)**: OFR-1 post-B8.5 RESTRICT-flip task filed in `ops/backlog/module-6-intake.md`.
- **C5 (advisory)**: design doc §3 updated with prominent "zero rows land today" note.

## One-paragraph summary for Kyle

Approve with conditions. Two-stage pipeline is architecturally correct and faithfully implemented; flag-gating is clean, failure modes are non-fatal, invariants 1/2/7/14 all hold. Four items close before you flip `CITATION_SPAN_RESOLVER_ENABLED=true`: (1) orchestrator swaps `response.text` for `normalized_text` so span offsets are valid; (2) bump `_SYSTEM_PROMPT_VERSION` 8→9 co-committed with flag flip (cache-bust + PV-01 hook consistency); (3) wrap items+spans INSERTs in single transaction or document non-atomicity; (4) add prominent note that **zero citation rows will actually land until Chunk 3 wires passage_id for tool-slot bindings or B8.5 enables evidence_items writes** — the Chunk 2 flip exercises plumbing, not data. OFR-2/3/4/5 fine as proposed. OFR-1 defer to tracked backlog task. Not apply-ready tonight; one short follow-up commit closes C1+C2+C3 and it's green.

## Budget discipline

~15 min compute; ~8k tokens read. Skipped: full test files (coverage summary in design doc §8), full runbook, Chunk 1 migration DDL (audit file), orchestrator prompt-variant bodies (structure only), models/answer_run.py (validated via memory file).
