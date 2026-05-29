# ADR 0009: §3 and §4 algorithmic-spines rollout — stage-gated, flag-gated, evidence-gated

- **Date**: 2026-05-27
- **Status**: Accepted
- **Deciders**: Kyle Maguire (SME)
- **Related**:
  `docs/architecture/repair_loop_spec.md`,
  `docs/architecture/context_prep_spec.md`,
  `docs/architecture/multi_turn_resolution_spec.md`,
  `docs/architecture/shadow_telemetry_sentry_tags.md`,
  `OVERNIGHT_LOG.md` §19–§31

## Context

Over 2026-05-26 → 2026-05-27 the agentic-retrieval surface gained two
algorithmic spines as pure-function library modules + tests:

- **Spine A** — context preparation (§3a typed evidence → §3b authority
  rank → §3c source-diversity rerank → §3f budget enforcer →
  composition in `context_prep.py`).
- **Spine B** — repair loop (§4b dispatcher mapping `GuardErrorCode`
  → `RepairStrategy`, §4c death-loop detector, shadow-mode wire).

Plus three companion pieces shipped under the same week:

- `multi_turn_resolver.py` (§3e — pronoun / demonstrative / comparative
  resolution against conversation history)
- `entity_resolver.py` (§2c — `silver.entity_aliases` lookup + gap
  logging)
- `geospatial_planner.py` + `tools_geospatial.py` (§2g — PostGIS query
  planner + tool wire)

All ten library modules have unit-test coverage (471+ tests at time of
writing); the wires are landed behind feature flags, default off.

The question this ADR settles: **how do these spines roll out from
"library shipped + flag off" to "fully on in production"?**

## Decision

A staged rollout with explicit gates between stages. Three parallel
tracks (Context, Repair, Multi-turn), each gated independently.

### Stage 0 — Foundation (CURRENT — DONE 2026-05-27)

Library modules + tests + flag-gated wires + companion docs (specs in
`docs/architecture/`, ADR-0009 in `docs/adr/`).

**Gates:**
- 471+ unit tests green
- Golden-query regression suite green (`test_golden_query_regression.py`,
  26 fixtures × 80+ criteria)
- All three companion specs published (`repair_loop_spec.md`,
  `context_prep_spec.md`, `multi_turn_resolution_spec.md`)
- Shadow-telemetry tag conventions locked
  (`shadow_telemetry_sentry_tags.md`)

### Stage 1 — Shadow mode (NEXT — pending operator flag flip)

Flip `REPAIR_LOOP_SHADOW_ENABLED=True` in dev → staging only. Telemetry
collects into `silver.query_traces` + nightly aggregation into
`gold.repair_shadow_daily` (Hatchet workflow shipped). Grafana
dashboard already provisioned at
`docker/grafana/dashboards/georag-repair-shadow.json`.

**Gates to enter:**
- Operator decision (Kyle): when to flip the flag in dev
- Grafana datasource `PostgreSQL-GeoRAG` configured (password env var
  set; `georag_read` role provisioned)

**Gates to exit (i.e. proceed to Stage 2):**
- ≥ 1 week of staging shadow telemetry collected
- No new spurious guard codes firing (false-positive rate ≤ 5% per
  dashboard's `Top guard codes` panel)
- No latency tail expansion (`p95_latency_ms` ≤ 110% of pre-Stage-1
  baseline)

### Stage 2 — Terminal-only repair surfaces

Enable `_apply_terminal_strategy` for the 5 terminal `RepairStrategy`
values (`ASK_FOR_DISAMBIGUATION`, `SURFACE_CONFLICT`,
`REQUEST_UNIT_CLARIFICATION`, `REQUEST_DEPTH_CLARIFICATION`,
`REFUSE_OUT_OF_SCOPE`). The 8 loop-friendly strategies stay
shadow-only.

This lights up the user-visible surfaces (React `AmbiguityPicker`,
`ConflictSideBySide`, `RefusalBanner`, plus two future picker
components for the clarification strategies) from real signals
without any LLM-cost amplification (terminal strategies don't
re-issue retrieval).

**Gates to enter:**
- Stage 1 exit gates met
- The two missing React components (`UnitPickerCard`,
  `DepthPickerCard`) implemented
- A new feature flag `REPAIR_LOOP_TERMINAL_ENABLED` (off by default)

**Gates to exit:**
- ≥ 1 week of staging with terminal surfaces on
- User-feedback signal: < 10% of clarification surfaces lead to a
  "give up and rephrase" abandonment (measured via chat-session
  duration heuristic — TBD)
- No regression in answer pass rate per the golden-query suite

### Stage 3 — Low-cost loop strategies

Enable `REPHRASE_NUMERIC_CLAIM` + `REQUEST_CITATION_RETRY` only.
Both are LLM-only re-issues (no extra retrieval), so the cost
amplification is one extra LLM call per failed query.

**Gates to enter:**
- Stage 2 exit gates met
- `cost_burn_watcher` Hatchet workflow shows pre-Stage-3 per-workspace
  LLM token spend baselined (a number to compare against)
- A new feature flag `REPAIR_LOOP_LOWCOST_ENABLED` (off by default)
- `REPAIR_LOOP_MAX_ATTEMPTS=2` hardcoded (no more than 2 retries
  per query)

**Gates to exit:**
- ≥ 1 week of staging
- Cost burn ≤ 120% of pre-Stage-3 baseline per workspace
- p95 latency ≤ pre-Stage-3 baseline + 4s
- Answer quality (golden-query) unchanged

### Stage 4 — Full retrieval-side loop

Enable `LOOSEN_FILTERS`, `BROADEN_KNN`, `ENABLE_FUZZY_ENTITY`,
`ADD_SPATIAL_BUFFER`, `TRANSFORM_CRS`, `INCREASE_GRAPH_DEPTH`. These
re-issue the retrieval pipeline with modified parameters — most
expensive amplification path.

**Gates to enter:**
- Stage 3 exit gates met
- §2c entity resolver wired into the orchestrator (so
  `ENABLE_FUZZY_ENTITY` has something to call)
- §2g geospatial tool wired into the dispatcher (so
  `ADD_SPATIAL_BUFFER` has something to call)
- A new feature flag `REPAIR_LOOP_FULL_ENABLED` (off by default)

**Gates to exit:**
- ≥ 2 weeks of staging
- Cost burn ≤ 140% of pre-Stage-4 baseline
- p95 latency ≤ pre-Stage-4 baseline + 6s
- Answer quality (golden-query) UP by ≥ 5% (this is the rollout's
  reason for existing — Stage 4 is the only stage that's expected
  to improve quality measurably; Stages 1–3 are observability + UI)

### Parallel track — context_prep rollout

Independent of the repair-loop staging:

| Sub-stage | Flag | Risk |
|---|---|---|
| C1 | `CONTEXT_PREP_ENABLED=True` in dev | Changes LLM input shape |
| C2 | Run golden-query benchmark against the live LLM with the flag on | Direct answer-quality comparison |
| C3 | Flip on for one power-user workspace | Real-corpus validation |
| C4 | GA | All workspaces |

Each sub-stage gated on the golden-query regression staying ≥ 95%
pass rate (the harness can run with `--quota-override` to A/B
test specific knobs).

### Parallel track — multi-turn rollout

Independent of both above:

| Sub-stage | Action |
|---|---|
| M1 | Ship the Laravel-side `chat_messages` history loader |
| M2 | Bridge job forwards history to `/v1/query` |
| M3 | Flip `MULTI_TURN_RESOLUTION_ENABLED=True` in dev |
| M4 | Ship Chat.tsx resolution preview chip ("Interpreted as:") |
| M5 | GA |

## Alternatives Considered

### Alternative 1 — Ship everything live behind one flag

Flip `AGENTIC_RETRIEVAL_V2_ENABLED=True` and let all spines fire at
once. Rejected because:

- Repair-loop cost amplification (LLM re-issues) is the highest-risk
  vector; bundling it with the context-prep input-shape change makes
  the rollback decision a single coarse switch
- The shadow telemetry doesn't yet exist for repair → no way to size
  the cost impact before flipping
- The golden-query baseline for context_prep doesn't yet have
  live-LLM data → answer-quality regressions would only surface in
  production

### Alternative 2 — Ship all spines flag-on by default

Same rejection reasoning. The defaults stay off; ops flips per
deployment.

### Alternative 3 — Hard-gate Stage N on Stage N+1 ETA

Considered making each stage's exit-gate include a max-stayed-in-stage
duration (e.g. "spend ≤ 30 days in Stage 1"). Rejected because:

- We don't know the right durations yet
- Forcing a stage advance without the evidence-quality gate
  re-introduces the "ship everything at once" risk
- The Grafana panels make it obvious when a stage has stayed long
  enough to draw conclusions; this can stay informal

## Consequences

### Positive

- **No production surprises.** Every rollout stage either changes only
  observability (Stage 1) or adds a single capability with its own
  cost gate (Stages 2–4).
- **The shadow data sizes itself.** Stage 1 gives us real-corpus
  telemetry on which codes fire and which strategies the dispatcher
  picks BEFORE we commit to amplifying LLM costs.
- **Parallel tracks de-risk each spine independently.** Context-prep
  drift is independent of repair-loop drift; one bad rollout doesn't
  poison the other.
- **The golden-query harness is the offline gate.** Drift catches
  itself in CI before any flag flip.

### Negative

- **Slower path to "full benefits."** Stage 4 — the only stage with
  measurable answer-quality improvement — is gated on at least
  ~4 weeks of telemetry across Stages 1–3.
- **Operational complexity.** Four new feature flags
  (`REPAIR_LOOP_SHADOW_ENABLED`, `_TERMINAL_ENABLED`,
  `_LOWCOST_ENABLED`, `_FULL_ENABLED`, plus `CONTEXT_PREP_ENABLED`
  and `MULTI_TURN_RESOLUTION_ENABLED`) for ops to track. Mitigated
  by the staged spec being THIS doc + the per-spine specs.
- **Coordination cost on Stage 4.** Stage 4 depends on the §2c
  entity-resolver wire + the §2g geospatial dispatcher wire being
  complete. Both have foundation; both need follow-up commits.
  Mitigation: ADR-0009 makes the dependency explicit so the work
  can be scheduled.

### Neutral / forward-looking

- A Stage 5 ("LLM-refiner for the §1c document classifier") is
  envisioned but not yet specified. It would graduate this ADR.
- The per-intent quota tables (`QUOTA_BY_INTENT`) are tunable via
  the harness's `--quota-override`; no ADR needed to adjust the
  ratios as production data accumulates.

## Status of dependencies (as of 2026-05-27)

| Dependency | Status |
|---|---|
| Library modules (10) | ✅ Shipped + tested |
| Companion specs (3) | ✅ Shipped |
| Sentry tag conventions | ✅ Shipped (waits on SDK re-enable) |
| Repair-shadow Hatchet workflow | ✅ Shipped |
| Grafana dashboard | ✅ Provisioned |
| `PostgreSQL-GeoRAG` datasource | ⚠️ Needs operator env-var configuration |
| `REPAIR_LOOP_SHADOW_ENABLED=True` in dev | ⚠️ Operator decision |
| Stage 2 React components (UnitPicker, DepthPicker) | ❌ Not built |
| Stage 4 §2c wire into orchestrator | ❌ Not built |
| Stage 4 §2g dispatcher wire | ❌ Not built |
| Live-LLM golden-query benchmark | ❌ Pending Stage C2 |
| Laravel history loader (multi-turn M1) | ❌ Pending |

## Decision-record metadata

This ADR is the canonical record of the rollout strategy. The per-spine
specs (`repair_loop_spec.md`, `context_prep_spec.md`,
`multi_turn_resolution_spec.md`) carry the technical detail; this ADR
carries the **ordering + gating** decision. Any change to the stage
ordering, gates, or parallel-track strategy requires either:

1. A new ADR superseding parts of 0009, or
2. An explicit amendment block at the bottom of this file with the
   date + reason + Kyle's sign-off.

— Kyle Maguire (SME), Claude Sonnet 4.7 (drafter)
