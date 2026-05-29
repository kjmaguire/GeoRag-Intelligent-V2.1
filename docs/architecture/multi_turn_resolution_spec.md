# Multi-Turn Resolution Specification (Plan §3e)

**Status:** Library foundation shipped 2026-05-27 (`app/agent/multi_turn_resolver.py`). Wire pending — requires conversation-history plumbing across Laravel + FastAPI.

This document is the third in the spec trilogy alongside
`repair_loop_spec.md` (plan §4b/§4c) and `context_prep_spec.md`
(plan §3a/§3b/§3c/§3f). Together they describe the three algorithmic
spines of agentic retrieval: **what to read, what to do when it's wrong,
and how to interpret follow-up queries in context.**

The §3e resolver translates references in a follow-up query against
conversation history. Geologists chain queries:

> Turn 1: "What's the deepest hole in Crackingstone?"
> Turn 2: "What were ITS top assays?"  ← `its` → hole from T1
> Turn 3: "And THE SAME HOLE'S lithology log?" ← demonstrative
> Turn 4: "What about hole 36-1085?" ← explicit new entity
> Turn 5: "How does it compare to THE PREVIOUS ONE?" ← comparative

Without resolution, T2's classifier would receive `"What were ITS top assays?"`
which contains zero project-specific entities — the retrieval profile
fires generic search, the answer cites the wrong corpus, the user
restarts the conversation. With resolution, T2 reaches the classifier
as `"What were PLS-22-08's top assays?"` — a high-recall factual_lookup.

---

## 1. Three resolution classes

The resolver handles three distinct reference patterns, applied in
this order (longest / most-specific phrases tested first):

### Class 1 — Pronoun coreference

Possessive (`its`, `their`) and nominative (`it`, `they`, `that`,
`those`, `this`) pronouns. Each pronoun has a default entity-type
bias:

| Pronoun | Bias | Possessive form? |
|---|---|---|
| `its` | hole | yes — renders as `X's` |
| `their` | hole | yes — renders as `X's` |
| `it` | hole | no |
| `they` | hole | no |
| `that` | hole | no |
| `those` | hole | no |
| `this` | property | no |

When the type-specific recency lookup misses, the resolver falls back
to the **most recent entity of any type**. Pronouns are inherently
ambiguous; we choose recency over silence.

### Class 2 — Demonstrative reference

Phrase-level references that include the entity type:

| Phrase | Resolves to |
|---|---|
| `the same hole` / `that hole` / `this hole` | latest hole |
| `those holes` | latest hole (plural ignored — recency wins) |
| `those assays` | latest hole (assays belong to a hole) |
| `the same property` / `that property` / `this property` | latest property |
| `the same formation` / `that formation` | latest formation |
| `the same report` | latest report |

Substitution is whole-phrase: `"the lithology for THE SAME HOLE"` becomes
`"the lithology for PLS-22-08"` (not `"the lithology for the same PLS-22-08"`).

### Class 3 — Comparative reference

| Phrase | Walk-back distance |
|---|---|
| `the previous one` / `the previous hole` / `the previous property` | 1 (most recent) |
| `the earlier one` / `the earlier hole` / `the earlier property` | 1 |
| `the other one` / `the other hole` / `the other property` | 1 |
| `the first one` / `the first hole` / `the first property` | -1 (oldest) |

Comparative references are type-agnostic — `the first one` could resolve
to a hole or a property depending on what was first mentioned.

---

## 2. Input contract

```python
@dataclass(frozen=True)
class EntityMention:
    surface_form: str                    # exact text as it appeared
    entity_type: Literal["hole", "property", "formation",
                          "commodity", "report"]
    turn_index: int                      # which turn introduced it
    normalised_id: str | None = None     # optional canonical UUID

@dataclass(frozen=True)
class ConversationTurn:
    turn_index: int                      # 0 = oldest, N = most recent
    role: Literal["user", "assistant"]
    text: str
    entity_mentions: tuple[EntityMention, ...]  # pre-extracted

def resolve_multi_turn(
    query: str,
    history: list[ConversationTurn],
) -> ResolvedQuery: ...
```

When `history` is empty or `entity_mentions` is empty on every turn,
the resolver uses `extract_entity_mentions(text, turn_index)` as a
fallback. This heuristic regex captures:

- Hole IDs (`PLS-22-08`, `DDH-1234`, `36-1085`, `BG21-001`)
- Property names (`X Property/Project/Deposit` with title-case `X`)

The fallback is intentionally conservative — false positives silently
mutate the user's query, which is worse than a missed substitution.

---

## 3. Output contract

```python
@dataclass(frozen=True)
class ResolutionStep:
    kind: Literal["pronoun", "demonstrative", "comparative"]
    original_phrase: str       # the pronoun / phrase as written
    resolved_to: str            # the entity surface form
    source_turn_index: int      # which turn introduced it
    confidence: float           # per-step confidence

@dataclass(frozen=True)
class ResolvedQuery:
    query: str                  # original, untouched
    rewritten_query: str        # with substitutions inline
    resolution_trace: tuple[ResolutionStep, ...]
    overall_confidence: float   # in [0.0, 1.0]

    @property
    def made_changes(self) -> bool: ...
```

**Per-step confidence:**

- Pronouns (possessive): `0.85`
- Pronouns (nominative): `0.75`
- Demonstratives: `0.90`
- Comparatives: `0.70`

**Overall confidence:**

- `1.0` when no references found in the query (pristine pass-through)
- `1.0 - (unresolved / total)` otherwise
- Each reference that lacked a referent in history bumps the
  unresolved count without changing the query text

The orchestrator uses `overall_confidence` as a demotion signal — a low
confidence on a resolved query should propagate to the answer's
confidence so geologists see the uncertainty.

---

## 4. Resolution order

Within a single query, the three classes are applied in fixed order:

1. **Demonstratives** (longest patterns first within the class)
2. **Comparatives**
3. **Pronouns** (longest pronoun strings first: `their` before `it`)

This order matters: a query like
`"show ITS top assays and the lithology for THE SAME HOLE"` would
otherwise expand `ITS` first, leaving the demonstrative path's regex
to match against a query that already contains a surface form. Doing
demonstratives first ensures both substitutions resolve to the same
underlying entity without accidental nesting.

---

## 5. Pure-function invariants

The resolver is pure: no I/O, no DB, no LLM, no session-state mutation.

- `resolve_multi_turn(query, history)` never modifies `history` —
  enforced by the test
  `test_pure_function_does_not_mutate_history`
- Empty / no-history input returns `query` unchanged with
  `made_changes=False` and `overall_confidence=1.0`
- When a turn's `entity_mentions` is empty, the resolver **internally**
  augments a local copy via `extract_entity_mentions` — the input
  turn objects stay untouched

---

## 6. Wire (not yet shipped)

The resolver needs **conversation history** as input. The current
`run_agentic_retrieval(query, deps)` signature doesn't carry history.
Wiring requires three changes:

### 6.1 Laravel side

Already persists chat turns in `chat_messages`. The query bridge
(`StreamQueryFromFastApi` job) needs to load the prior N turns from
the conversation and forward them to FastAPI on the `/v1/query`
request:

```php
// app/Jobs/StreamQueryFromFastApi.php (sketch — future commit)
$history = ChatMessage::where('conversation_id', $this->conversationId)
    ->orderBy('created_at')
    ->take(self::HISTORY_MAX_TURNS)
    ->get()
    ->map(fn ($m) => [
        'turn_index' => $m->turn_index,
        'role' => $m->role,
        'text' => $m->content,
        'entity_mentions' => $m->metadata['entity_mentions'] ?? [],
    ])
    ->toArray();
```

The `entity_mentions` would come from a new Laravel-side extractor
(or the existing `extract_hole_ids` Python helper exposed via an
internal endpoint).

### 6.2 FastAPI side

`AgenticRetrievalState` gains an optional `history` field:

```python
class AgenticRetrievalState(BaseModel):
    ...
    history: list[ConversationTurn] = Field(default_factory=list)
```

`run_agentic_retrieval` accepts a new `history` keyword argument and
threads it into the initial state.

### 6.3 New pre-classifier node

A new `resolve_node` runs BEFORE `classify_node`:

```python
async def resolve_node(state: AgenticRetrievalState) -> dict[str, Any]:
    from app.config import settings
    if not settings.MULTI_TURN_RESOLUTION_ENABLED:
        return {}
    if not state.history:
        return {}
    resolved = resolve_multi_turn(state.query, state.history)
    if not resolved.made_changes:
        return {}
    # Stamp BOTH onto state so the trace records the original AND
    # the rewritten form for audit + debugging.
    return {
        "query": resolved.rewritten_query,
        "resolution_trace": [
            {
                "kind": s.kind,
                "original_phrase": s.original_phrase,
                "resolved_to": s.resolved_to,
                "source_turn_index": s.source_turn_index,
                "confidence": s.confidence,
            }
            for s in resolved.resolution_trace
        ],
        "resolution_confidence": resolved.overall_confidence,
    }
```

Flag-gated via `MULTI_TURN_RESOLUTION_ENABLED=False` default. Same
shadow-mode pattern as `CONTEXT_PREP_ENABLED` and `REPAIR_LOOP_SHADOW_ENABLED`.

### 6.4 LangGraph wiring

```python
_PIPELINE = (
    ("resolve", resolve_node),     # NEW — runs first
    ("classify", classify_node),
    ("route", route_node),
    ...
)
```

The classify node receives the REWRITTEN query, not the original.
The original is still on `state.query_original` (a new field) so the
trace preserves both.

---

## 7. Trace shape

Once wired, the trace surface:

```python
class RetrievalTrace(BaseModel):
    ...
    multi_turn_resolution: dict[str, Any] | None = None
    # Shape: {
    #   "original_query": str,
    #   "rewritten_query": str,
    #   "trace": [{kind, original_phrase, resolved_to,
    #              source_turn_index, confidence}, ...],
    #   "overall_confidence": float,
    # }
```

When the rewriter made changes, the trace inspector can show
side-by-side:

```
Original:    "what's ITS top assay?"
Rewritten:   "what's PLS-22-08's top assay?"
Steps:
  pronoun "ITS" → "PLS-22-08" (turn 0, conf 0.85)
```

This is observability the operator can use to spot misresolutions
(e.g. `it` resolved to a wrong entity because the user expected a
different referent).

---

## 8. Limitations (documented in module header)

The foundation pass is deliberately conservative:

- **English-only patterns.** Multilingual UI is a Phase F11 concern.
- **Surface-form match only.** `"hole"` in T1 and `"drillhole"` in
  T3 are treated as the same word in pronoun matching — but the
  resolver doesn't normalise them.
- **No semantic similarity.** A query like "the deeper one" can't
  resolve via comparison of total_depth values across history;
  comparatives only walk position-back.
- **Entity-type compatibility is heuristic.** `it` defaults to
  `hole` bias; `this` defaults to `property` bias. Outside those
  defaults the resolver falls back to most-recent-of-any-type.

Each limitation is addressable in a follow-up but not blocking the
foundation.

---

## 9. Rollout plan

1. **Stage 1** (CURRENT) — library shipped, wire pending. The
   resolver is callable from any caller; tests pin behaviour.
2. **Stage 2** — Laravel-side history loader + FastAPI history
   acceptance. Flag-off shadow: capture the rewritten query in the
   trace but do NOT route through the rewritten form yet. Same
   pattern as the repair-loop shadow.
3. **Stage 3** — Flip `MULTI_TURN_RESOLUTION_ENABLED=True` for one
   power-user workspace. Side-by-side answer comparison.
4. **Stage 4** — GA. Monitor `multi_turn_resolution.overall_confidence`
   distribution; low-confidence resolutions surface a confirmation
   chip in the chat UI ("Do you mean PLS-22-08?") before the answer
   runs.

Each stage is independently gated.

---

## 10. UI surface (deferred)

Two complementary surfaces:

### 10.1 Resolution preview chip

When the rewriter changed the query, the chat UI shows a small
preview chip below the user's bubble:

> 💡 _Interpreted as:_ "what's **PLS-22-08's** top assay?" [edit]

Clicking [edit] reverts the query to the original and reruns with
`MULTI_TURN_RESOLUTION_ENABLED=False` for that one call.

### 10.2 Low-confidence confirmation prompt

When `overall_confidence < 0.6` AND a terminal resolution happened
(no fallback to most-recent-of-any-type), the chat UI surfaces an
inline confirmation before submitting:

> The reference "**it**" is ambiguous — did you mean
> [PLS-22-08] or [DDH-1234]?

Both are out-of-scope for the foundation pass; they need wire +
Laravel-side persistence of resolution trace.

---

## 11. Open questions

- **How many prior turns to load?** Default `HISTORY_MAX_TURNS = 20`
  is a starting guess. Tune after Stage 3 data lands. Cost: linear
  in turn count for the regex sweep.
- **Should turns from assistant roles be included?** Yes —
  entities mentioned in the assistant's prior response are
  legitimate referents. The resolver already accepts both roles in
  history.
- **Multi-turn + multi-turn user sessions** — if a user opens TWO
  chat threads in parallel, both have separate history. The wire
  must scope history to `conversation_id`, not `user_id`. Easy in
  the Laravel loader; worth calling out.
- **Race against the multi-resolver and the §3e CGI vocab arm
  (§1d-iii)** — when both resolve at the same step, the CGI vocab
  enrichment should apply AFTER the multi-turn rewrite. Order
  documented in the wire.

---

## References

- `src/fastapi/app/agent/multi_turn_resolver.py` — resolver foundation
- `src/fastapi/tests/test_multi_turn_resolver.py` — 27 tests pinning behaviour
- `docs/architecture/repair_loop_spec.md` — companion §4b/§4c spec
- `docs/architecture/context_prep_spec.md` — companion §3a/§3b/§3c/§3f spec
- `OVERNIGHT_LOG.md` §27 (laps 4) — foundation shipped
- `OVERNIGHT_LOG.md` §30 — context_prep_spec + adversarial fuzz (paired specs)
