# Sentry Tagging Conventions for Shadow Telemetry

**Status:** Spec — companion to `repair_loop_spec.md` and `context_prep_spec.md`. The Sentry SDK isn't currently installed (see MEMORY: *Sentry removed 2026-05-21*). This doc locks the tagging schema so that when Sentry is re-enabled, the tag conventions across `repair_shadow_node`, `context_prep`, and `multi_turn` align without per-engineer interpretation.

This document defines the Sentry tag schema for the three algorithmic
spines shipped under plans §3 and §4. Tags are short string keys that
attach to every transaction / breadcrumb / event Sentry receives —
they're the primary filter axes in the Sentry UI ("show me all events
where `repair.terminal_strategy=SURFACE_CONFLICT`").

## Why this matters

Without a consistent schema, every engineer who adds a `set_tag` call
picks their own naming. Six months later the Sentry filter UI lists
40 near-duplicate tags (`repair.strategy`, `repair_strategy`,
`repairStrategy`, `agent.repair.strategy`...) and the dashboard
breaks. This spec is the contract.

## Tag namespace conventions

All shadow-telemetry tags use a **dotted namespace prefix**:

| Prefix | Spine |
|---|---|
| `repair.*` | §4b/§4c repair-loop shadow + future full loop |
| `context_prep.*` | §3a + §3b + §3c + §3f composition |
| `multi_turn.*` | §3e resolver |
| `evidence.*` | §3a typed evidence packet — kind counts, budget |
| `guards.*` | §4b classifier output (firings, not strategies) |

Values are always strings — Sentry doesn't support numeric tag values
for filtering. Numeric measurements go into Sentry's `measurements`
field instead (covered in §3 below).

## The full tag inventory

### `repair.*` — §4b/§4c repair loop

| Tag | Type | Cardinality | Example | Notes |
|---|---|---|---|---|
| `repair.shadow_mode` | bool | 2 | `"true"` / `"false"` | Reflects `REPAIR_LOOP_SHADOW_ENABLED`. Stage 4 will flip this when the full loop ships. |
| `repair.codes_count` | int (as string) | 0–16 | `"3"` | Number of distinct `GuardErrorCode` values fired. |
| `repair.terminal` | bool | 2 | `"true"` / `"false"` | From `RepairPlan.terminal`. |
| `repair.terminal_strategy` | enum | 6 | `"SURFACE_CONFLICT"` | One of the 5 terminal strategies + `""` when non-terminal. |
| `repair.attempts` | int | 0–N | `"0"` (shadow) / `"1"`+ (full) | `len(state.repair_attempts)`. |
| `repair.death_loop` | bool | 2 | `"true"` / `"false"` | From `detect_death_loop`. |

### `context_prep.*` — §3 composition

| Tag | Type | Cardinality | Example | Notes |
|---|---|---|---|---|
| `context_prep.enabled` | bool | 2 | `"true"` | Reflects `CONTEXT_PREP_ENABLED`. |
| `context_prep.intent` | enum | 8 | `"synthesis"` | One of the 8 agentic intents (or `"unspecified"`). |
| `context_prep.budget_reached` | bool | 2 | `"true"` | From `PreparedContext.reached_budget`. |
| `context_prep.drops_count` | int | 0–N | `"4"` | `len(PreparedContext.dropped_evidence_ids)`. |
| `context_prep.budget_pressure` | enum | 4 | `"tight"` | `comfortable` / `tight` / `over` / `unknown` — same bucket scheme as the Grafana dashboard. |

### `multi_turn.*` — §3e

| Tag | Type | Cardinality | Example | Notes |
|---|---|---|---|---|
| `multi_turn.enabled` | bool | 2 | `"true"` | Reflects `MULTI_TURN_RESOLUTION_ENABLED`. |
| `multi_turn.made_changes` | bool | 2 | `"true"` | True iff the rewriter substituted at least one phrase. |
| `multi_turn.steps_count` | int | 0–N | `"2"` | Number of substitutions applied. |
| `multi_turn.confidence_bucket` | enum | 4 | `"high"` | `high` (≥0.85) / `medium` (0.6–0.85) / `low` (<0.6) / `unknown`. |
| `multi_turn.history_depth` | int | 0–N | `"5"` | Number of turns in history passed in. |

### `evidence.*` — §3a packet shape

| Tag | Type | Cardinality | Example | Notes |
|---|---|---|---|---|
| `evidence.kinds_count` | int | 0–6 | `"4"` | Distinct evidence kinds in the prepared packet. |
| `evidence.has_spatial` | bool | 2 | `"true"` | Triggers the §6b MapLibre card. Tagged for quick "show me spatial queries" filtering. |
| `evidence.has_graph` | bool | 2 | `"true"` | Indicates hypothesis-style or synthesis multi-hop. |
| `evidence.first_kind` | enum | 6 | `"document"` | The kind that landed at the top of the authority sort. |

### `guards.*` — §4b classifier

| Tag | Type | Cardinality | Example | Notes |
|---|---|---|---|---|
| `guards.fired_any` | bool | 2 | `"true"` | True iff `guard_failure_codes` is non-empty. |
| `guards.fired_terminal` | bool | 2 | `"true"` | True iff one of the fired codes maps to a terminal strategy. Useful filter for "show me queries that needed a surface like SURFACE_CONFLICT or ASK_FOR_DISAMBIGUATION". |
| `guards.codes_csv` | string | — | `"NUMERIC_GROUNDING_FAILED,CITATION_INCOMPLETE"` | Up to 200 chars CSV of the fired codes (Sentry caps tag value length at 200). Use only when filtering by a single specific code in a debug session — for aggregation, prefer the individual `guards.fired_*` bools per code. |

### `card.*` — §6b chat-card rendering

Added 2026-05-29 (P6 of the §6b polish backlog). Tags which inline visualisation the response shipped — `Chat.tsx`'s `InlineViz` renders one of 8 React cards keyed on the backend `viz_payload.chart_type`. Dashboards measure "% of queries that produce an inline visualisation" + per-card render rate.

| Tag | Type | Cardinality | Example | Notes |
|---|---|---|---|---|
| `card.rendered` | bool | 2 | `"true"` | True iff `state.response.viz_payload` is non-None. |
| `card.type` | enum | 10 | `"stereonet"` | One of: `drill_trace_3d`, `downhole_strip`, `stereonet`, `technique_timeline`, `coverage_table`, `assay_histogram`, `cross_section`, `graph_viz`, `none`, `unknown`. `unknown` is the canary — fires when a new `chart_type` is added to `_build_chat_card_payloads` without being mirrored into the `_KNOWN_CARD_TYPES` frozenset in `sentry_tags.py`. |

## Measurements (numeric, not tags)

Sentry's measurements API is for numeric values to plot in panels.
Five measurements are mandatory on every agentic-retrieval transaction:

| Measurement | Unit | Source |
|---|---|---|
| `agent.latency.total` | ms | `state.latency_ms.total` |
| `agent.latency.retrieval` | ms | `state.latency_ms.retrieval_fan_out` |
| `agent.latency.generation` | ms | `state.latency_ms.generation` |
| `agent.evidence.total_tokens` | none | `packet.total_tokens` |
| `agent.evidence.remaining_budget` | none | `packet.remaining_budget` (can be negative — a real signal) |

## Where to call `set_tag`

The tag-setting code lives in **one place per spine**, not scattered:

| Spine | Call site |
|---|---|
| `repair.*` | `repair_shadow_node` (Plan §4b Stage 1) — and the future `repair_loop_node` (Stage 4) shares the same tags |
| `context_prep.*` | `assemble_node` immediately after `prepare_evidence_for_intent` returns (when the flag is on) |
| `multi_turn.*` | `resolve_node` immediately after `resolve_multi_turn` returns (when the flag is on) |
| `evidence.*` + `guards.*` | `persist_node` immediately after `classify_guards` runs (this is the latest point before the trace writes; all upstream tag-setters have run by then) |

Each spine's tag-setter wraps its own try/except — a Sentry SDK
import failure or a bad tag value MUST NOT block the answer path.
This mirrors the shadow-node's defensive logging pattern.

## Workspace-scope tag

Every event should also carry the workspace UUID:

```python
sentry_sdk.set_tag("workspace.id", str(state.deps.workspace_id))
```

This isn't shadow-telemetry-specific (it lands on every transaction
the agent emits), but it's worth stating here because the
shadow-telemetry filters consistently AND-by-workspace.

## When the Sentry SDK isn't installed

Per the MEMORY note (*Sentry removed 2026-05-21*), `sentry-laravel`
is not currently installed. The tag-setter implementations should:

1. Import `sentry_sdk` lazily inside the call site
2. Catch `ImportError` and no-op when the SDK isn't installed
3. Catch any other Sentry exception and log + continue

Sketch:

```python
def _stamp_repair_tags(state):
    try:
        import sentry_sdk
    except ImportError:
        return
    try:
        sentry_sdk.set_tag("repair.shadow_mode", str(settings.REPAIR_LOOP_SHADOW_ENABLED).lower())
        sentry_sdk.set_tag("repair.codes_count", str(len(state.repair_codes_observed)))
        sentry_sdk.set_tag("repair.terminal", str(bool(state.repair_terminal_reason)).lower())
        # ...
    except Exception:
        logger.debug("sentry tag set failed (non-fatal)", exc_info=True)
```

## Tag value normalisation rules

To prevent low-cardinality drift over time:

1. **Booleans are always lowercase strings**: `"true"` / `"false"`. Never `"True"` / `"1"`.
2. **Enum tags use the SOURCE enum's `.value`**, never a friendly-name variant. `RepairStrategy.SURFACE_CONFLICT.value` not `"Surface conflict"`.
3. **Counts are strings**: Sentry can't aggregate over numeric tag values, but `"0"` / `"1"` / `"2"` filter cleanly.
4. **Buckets, not exact numbers**: `multi_turn.confidence_bucket=high` not `multi_turn.confidence=0.87`. Sentry's tag UI lists distinct values; cardinality explodes otherwise.

## Schema versioning

When this schema changes (add a tag, remove a tag, change an enum
value), bump the file's section heading and document the migration in
the PR description. The Sentry UI doesn't migrate filter saves
automatically — operators with pinned filters on `repair.strategy`
(say) need to rebuild them on the new `repair.terminal_strategy`.

## References

- `docs/architecture/repair_loop_spec.md` — §4b/§4c spine
- `docs/architecture/context_prep_spec.md` — §3 spine
- `docs/architecture/multi_turn_resolution_spec.md` — §3e spine
- `OVERNIGHT_LOG.md` §29 — repair shadow wire
- `OVERNIGHT_LOG.md` §30 — adversarial classify_guards fuzz suite
- MEMORY: *Sentry removed 2026-05-21*
