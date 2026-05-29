# Context Preparation Pipeline (Plan §3a + §3b + §3c + §3f)

**Status:** Library modules + composition + flag-gated wire all shipped 2026-05-27. Default off pending live golden-query benchmark.

This document describes how the four pure-function passes shipped under plan §3 compose into the **context preparation pipeline** — the step that takes the raw retrieval output (`state.tool_results`) and produces a typed, authority-ranked, diversity-balanced, budget-fit `EvidencePacket` for the LLM context block.

The companion spec is `repair_loop_spec.md` (plan §4b/§4c). Together they describe the two algorithmic spines of agentic retrieval.

The four library modules:

| Module | Module-level callable |
|---|---|
| `evidence_converter.py` | `build_evidence_packet(...)` — `tool_results → EvidencePacket` |
| `authority.py` | `rank_evidence_by_authority(packet)` — §3b sort |
| `authority.py` | `annotate_evidence_packet_with_authority(packet)` — §3b refresh |
| `source_diversity.py` | `apply_source_diversity(packet, kind_quotas=...)` — §3c |
| `context_budget.py` | `enforce_token_budget(packet, ...)` — §3f drop loop |
| `context_prep.py` | `prepare_evidence_for_intent(packet, intent, ...)` — composition |

---

## 1. Pipeline shape

```
state.tool_results  (list[(tool_name, payload)])
       │
       ▼
build_evidence_packet   ← evidence_converter
       │  EvidencePacket (kind-typed, authority_rank=default 3, total_tokens)
       ▼
annotate_authority      ← authority.annotate_evidence_packet_with_authority
       │  authority_rank refreshed from document_type per §3b table
       ▼
rank_evidence_by_authority  ← authority.rank_evidence_by_authority
       │  sort by (rank, currency, confidence)
       ▼
apply_source_diversity      ← source_diversity.apply_source_diversity
       │  kind_quotas = QUOTA_BY_INTENT[intent]
       │  preserves within-kind authority order
       ▼
enforce_token_budget        ← context_budget.enforce_token_budget
       │  drop low-authority members until remaining_budget ≥ 0
       │  protected_kinds = PROTECTED_KINDS_BY_INTENT[intent]
       │  min_per_kind defaults to 1
       ▼
PreparedContext  (frozen dataclass)
  .packet                    ← the prepared EvidencePacket
  .intent                    ← echoed for trace logging
  .quota_used                ← the active quota table
  .reached_budget            ← bool
  .dropped_evidence_ids      ← budget-pass drops only
  .budget_reason             ← when reached_budget=False
  .kind_distribution_before  ← per-kind counts on input
  .kind_distribution_after   ← per-kind counts on output
```

The composition is wrapped in `prepare_evidence_for_intent(packet, intent, *, max_context_tokens, quota_override, protected_kinds_override, min_per_kind)` so callers (the orchestrator, the golden-query harness, A/B benchmarks) make a single call.

---

## 2. Per-intent quota tables (`QUOTA_BY_INTENT`)

Each intent gets a curated kind quota. The numbers come from plan §2b's retrieval-profile spec + the Phase 1.3 answer-mode policy. **Ratios matter more than absolute counts** — the §3c diversity pass uses the quotas to bound per-kind contribution, and the §3f budget pass trims from there.

| Intent | doc | spatial | assay | table | collar | graph | Notes |
|---|---|---|---|---|---|---|---|
| `factual_lookup` | **5** | 1 | 1 | 1 | 1 | 0 | citation-heavy; no graph |
| `synthesis` | 3 | 2 | 2 | 1 | 1 | 1 | balanced across all six |
| `hypothesis_generation` | 2 | 1 | **3** | 1 | 1 | **3** | adversarial-pass intent — leave room |
| `anomaly_detection` | 1 | 1 | **5** | **3** | 1 | 0 | the answer IS the numeric table |
| `uncertainty_quantification` | 3 | 2 | 3 | 2 | 1 | 1 | conflict-detection enabled |
| `decision_support` | **4** | 2 | 2 | 1 | 1 | 1 | regulatory spine; ranked options |
| `project_summary` (ADR-0007) | 2 | 0 | 0 | 2 | 0 | 0 | chat-card narration only |
| `coverage_gap` (ADR-0007) | 1 | 1 | 0 | **3** | 1 | 0 | coverage rows ARE the answer |

Unknown / `None` intent → `synthesis` fallback (most balanced).

### Caller overrides

The `quota_override` keyword argument REPLACES the per-intent table entirely. Used by A/B benchmarks and the eventual UI knob for power users:

```python
prepare_evidence_for_intent(
    packet, "synthesis",
    quota_override={"document": 5, "assay": 0, "spatial": 0, "table": 0, "collar": 0, "graph": 0},
)
```

A `0` quota always drops the kind (same as omitting it). The `unspecified_quota` parameter on `apply_source_diversity` defaults to 0 (drop kinds not in the map) — `prepare_evidence_for_intent` keeps the default.

---

## 3. Per-intent protected sets (`PROTECTED_KINDS_BY_INTENT`)

The §3f budget pass can drop low-authority members to fit the context window. The protected set blocks the drop for kinds where the *intent* structurally requires the kind to be present.

| Intent | Protected set | Rationale |
|---|---|---|
| factual_lookup | `{document}` | cite or refuse |
| synthesis | `{document}` | document spine |
| hypothesis_generation | `{document}` | hypotheses still need anchored sources |
| anomaly_detection | `{assay, document}` | the answer IS the numbers; cite their sources |
| uncertainty_quantification | `{document}` | conflicts need claim sources |
| decision_support | `{document}` | regulatory spine |
| project_summary | `{document}` | narration text |
| coverage_gap | `{table}` | coverage rows |

When `enforce_token_budget` can't reach `remaining_budget ≥ 0` because the protected set + `min_per_kind` floor pins enough evidence to keep the budget negative, the returned `BudgetTrimResult` carries `reached_target=False` and a `reason` like `"per-kind floor pinned 1 kind(s) — cannot drop further: ['document']"`. The caller logs it and **does not block the answer** — see §6.

### Override

`protected_kinds_override=frozenset(...)` REPLACES the per-intent set. Pass `frozenset()` to disable protection entirely (the test suite uses this to verify drop behavior in isolation).

---

## 4. Drop order (§3f)

When the budget pass needs to trim, it drops in the REVERSE of the §3b sort:

  `(authority_rank DESC, is_current False first, confidence ASC)`

So the first member dropped is:

1. The lowest authority rank (5 — Internal Memo / Email / Field Note / Uncited)
2. Among rank 5: superseded before current
3. Among rank-5 + currency-tied: lowest confidence first

This is the same ordering as §3b's sort key, REVERSED — the first drop is the member the LLM would have read last anyway.

Per-kind floor (`min_per_kind=1` default): each present kind keeps at least 1 member. When the floor pins enough evidence to keep the budget negative, `reached_target=False` with a reason. Set `min_per_kind=0` to disable.

---

## 5. The wire (`assemble_node`)

The composition is invoked from `assemble_node` in `app.agent.agentic_retrieval.nodes`, guarded by the `CONTEXT_PREP_ENABLED` feature flag.

```python
# assemble_node — abbreviated
if (
    settings.CONTEXT_PREP_ENABLED
    and state.evidence_packet is not None
    and state.evidence_packet.evidence
):
    prepared = prepare_evidence_for_intent(
        state.evidence_packet,
        effective_intent,
        max_context_tokens=settings.effective_max_context_tokens,
    )
    state.evidence_packet = prepared.packet
    # context_block built from packet.evidence below

# context_block construction
if use_packet_for_context:
    for ev in state.evidence_packet.evidence:
        citation_counter += 1
        context_lines.append(
            f"[DATA:{citation_counter}] kind={ev.kind} ..."
        )
else:
    # Legacy path — render from tool_results directly
    for tool_name, result in state.tool_results:
        ...
```

When the flag is **off**, the context block is built from `state.tool_results` byte-identical to the pre-§27 path. When the flag is **on**, it's built from the prepared packet's evidence list — authority-ranked, diversity-balanced, budget-fit.

Defensive: any exception inside `prepare_evidence_for_intent` logs but never blocks the answer path — the legacy path runs as a fallback. The wire can't take down production even if the prep pipeline throws on malformed input.

### Flag rollout

| Stage | Flag value | Risk |
|---|---|---|
| 1 | `False` (default) | No behavior change — wire is dark |
| 2 | `True` for synthetic eval workspace | Compare LLM output side-by-side against legacy path |
| 3 | `True` for one power-user workspace | Real-corpus validation |
| 4 | `True` everywhere | General availability |

The golden-query regression suite (`test_golden_query_regression.py`) is the offline gate that runs at Stage 1 — quotas + protected sets + algorithm behavior are all locked behind pytest before any flag flips.

---

## 6. Observability

Once the flag is on, the trace inspector (`silver.query_traces`) carries:

| Trace field | Source |
|---|---|
| `evidence_types_in_context` | `[e.kind for e in prepared.packet.evidence]` — canonical authority-ranked order |
| `remaining_context_budget` | `prepared.packet.remaining_budget` (truth, after the budget pass) |
| `system_prompt_tokens` | The chars/4 estimate from `assemble_node` |

The `PreparedContext` audit fields (`quota_used`, `dropped_evidence_ids`, `budget_reason`, `kind_distribution_before/_after`) are NOT yet wired to the trace — they're available on `assemble_node`'s state for future trace expansion. A follow-up commit should add a `context_prep_audit` JSONB field to `silver.query_traces` and persist them.

### Grafana panels (deferred)

| Panel | Source field | Use case |
|---|---|---|
| Per-intent budget pressure | `remaining_context_budget` bucketed | Spot intents that consistently hit the budget ceiling |
| Kind distribution by intent | `evidence_types_in_context` aggregated | Verify quota tables produce the expected mix |
| Top-dropped kinds | `context_prep_audit.dropped_evidence_ids` (after wire) | Tune `min_per_kind` and protected sets |

---

## 7. Frontend surface

`Chat.tsx` renders a small **`<EvidencePacketBadge />`** strip below each assistant message when `response.evidence_packet` is non-null:

- One chip per kind with the count
- A "Budget" pill coloured by `remaining_budget` tier (negative=error, <500=warn, else=neutral)

When `CONTEXT_PREP_ENABLED=False`, the packet IS still built (from execute_node), so the badge still renders — it just reflects the **un-prepared** packet shape. When the flag flips, the badge starts showing the diversity-balanced + budget-trimmed view, which is exactly the signal users + ops want.

See `OVERNIGHT_LOG.md` §22 for the UI wire details.

---

## 8. The eight intents — when does each matter?

The per-intent quota table is the leverage point for tuning answer quality without touching algorithm code. Per intent:

### `factual_lookup`
**Use case:** "What's the deepest hole?" / "What was the top assay grade in PLS-22-08?"
**Quota:** Document-heavy (5). No graph (lookup answers shouldn't pull relationship paths).
**Tuning lever:** If lookups start citing non-authoritative documents, drop `document` quota to 3 to force higher selectivity.

### `synthesis`
**Use case:** "Integrate the corridor evidence" / "What's the geological story of this property?"
**Quota:** Balanced — every kind gets ≥ 1.
**Tuning lever:** If synthesis answers consistently lack spatial context, bump `spatial=2 → 3`.

### `hypothesis_generation`
**Use case:** "What geological models could explain the Cu-Au anomaly?"
**Quota:** Graph + assay heavy (3 each). Adversarial pass is enabled by the retrieval profile so we leave document room.
**Tuning lever:** When hypotheses repeat the same model, increase `graph=3 → 4` to surface more relationship paths.

### `anomaly_detection`
**Use case:** "Show me grade outliers in the south corridor"
**Quota:** Assay-dominant (5), table=3. The answer IS the numeric table.
**Tuning lever:** If anomaly reports omit context, bump `document=1 → 2` to bring in the doc citing each anomaly.

### `uncertainty_quantification`
**Use case:** "How certain are we about the mineralisation depth?"
**Quota:** Balanced; document + assay + spatial heavy.
**Tuning lever:** Conflict-detection is enabled by the profile — if conflicts surface too rarely, increase `document=3 → 4`.

### `decision_support`
**Use case:** "Should we drill the corridor at coordinates 105.4W 39.5N this season?"
**Quota:** Document=4 (regulatory spine).
**Tuning lever:** When `regulatory_touch=True`, the protected set should expand to include any "regulatory" document_type — currently this is implicit via the document quota.

### `project_summary` (ADR-0007)
**Use case:** Chat-card narration. The structured tool result feeds the card; the LLM only narrates.
**Quota:** Doc + table only.
**Tuning lever:** If narration starts inventing numbers, drop `document=2 → 1` to force tighter quoting.

### `coverage_gap` (ADR-0007)
**Use case:** "Which holes have no downstream assay data?"
**Quota:** Table=3 (coverage rows ARE the answer).
**Tuning lever:** `table` is the only protected kind for this intent.

---

## 9. A/B benchmark methodology (future)

The infrastructure to run quota A/B tests is already in place:

1. **Quota override** — `quota_override` keyword argument supports arbitrary tables
2. **Golden-query harness** — `run_golden_harness(queries, factory)` evaluates against criteria
3. **Per-intent JSON fixture** — `golden_queries.json` supplies the test set

A minimal A/B harness wrapper would:

```python
def benchmark_quotas(intent, baseline_quota, variant_quota, queries):
    factory_baseline = lambda g: prepare_evidence_for_intent(
        input_packet, g.intent, quota_override=baseline_quota,
    ).packet
    factory_variant = lambda g: prepare_evidence_for_intent(
        input_packet, g.intent, quota_override=variant_quota,
    ).packet
    report_a = run_golden_harness(queries, factory_baseline)
    report_b = run_golden_harness(queries, factory_variant)
    return report_a, report_b
```

The live-corpus version needs a real-data factory (run the agentic graph, prepare with the variant quota, evaluate). That's a Hatchet workflow, NOT in scope here.

---

## 10. Drift detection

The golden-query regression suite (`test_golden_query_regression.py`) catches three classes of drift:

1. **Quota typo** — a developer bumps `document=5 → document=4` and forgets to update the fixture. The `min_kind_count` / `first_kind_is` criteria on factual_lookup queries fail loudly.
2. **Algorithm drift** — someone refactors `apply_source_diversity` to a different selection algorithm. The `first_kind_is` criteria on synthesis / decision_support queries catch the new ordering.
3. **Authority sort drift** — someone tweaks `_sort_key` in `authority.py`. The `first_document_type_matches` criterion on `authority.ni43_above_memo` catches it.

The fixture lives at `src/fastapi/tests/golden_queries.json` and is JSON — extendable without touching Python. Each query carries `tags` for filtering (e.g. `["regression", "authority_ranking"]`).

---

## 11. Open questions / future expansion

- **Per-workspace quota overrides** — should power users be able to set `quota_override` via a UI knob? Currently it's code-only. Decision deferred until Stage 4 rollout data lands.
- **Dynamic intent-quota tuning** — could the per-intent tables themselves be learned from outcome data (the §5d feedback loop)? Possible, but the human-readable defaults need to stay the fallback.
- **`min_per_kind` per intent** — currently a single `min_per_kind=1` default applies to all intents. `coverage_gap` arguably wants `min_per_kind=3` on table since the rows ARE the answer.
- **Cross-passing PreparedContext into the trace** — `context_prep_audit` JSONB column on `silver.query_traces` would let the inspector show the per-query quota + drop trail. Follow-up commit.

---

## References

- `src/fastapi/app/agent/evidence.py` — typed evidence classes + EvidencePacket
- `src/fastapi/app/agent/authority.py` — §3b ranking
- `src/fastapi/app/agent/evidence_converter.py` — tool_results bridge
- `src/fastapi/app/agent/source_diversity.py` — §3c reranker
- `src/fastapi/app/agent/context_budget.py` — §3f drop loop
- `src/fastapi/app/agent/context_prep.py` — composition
- `src/fastapi/app/agent/golden_query_harness.py` — eval framework
- `src/fastapi/tests/golden_queries.json` — fixture set
- `src/fastapi/tests/test_golden_query_regression.py` — drift gate
- `docs/architecture/repair_loop_spec.md` — companion §4b/§4c spec
- `OVERNIGHT_LOG.md` §19 — typed evidence foundation
- `OVERNIGHT_LOG.md` §20 — authority + bridge
- `OVERNIGHT_LOG.md` §21 — graph wire (packet on state)
- `OVERNIGHT_LOG.md` §22 — UI surface
- `OVERNIGHT_LOG.md` §23 — §3c diversity
- `OVERNIGHT_LOG.md` §24 — §3f budget
- `OVERNIGHT_LOG.md` §26 — composition pipeline
- `OVERNIGHT_LOG.md` §27 — flag-gated wire into assemble_node
- `OVERNIGHT_LOG.md` §28 — golden-query regression suite
