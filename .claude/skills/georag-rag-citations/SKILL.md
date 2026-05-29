---
name: georag-rag-citations
description: GeoRAG citation-first enforcement and hallucination prevention. Use when Laravel code touches RAG response payloads, citation persistence, query lifecycle Reverb events, audit-trail writes, or refusal-path UX. Triggers on tasks involving query controllers, FastAPI bridge calls, answer_runs persistence, citation spans, source_chunk_id, query.validated/refusal events, or any code that ingests/forwards LLM output.
metadata:
  origin: GeoRAG project (Section 04i + cross-cutting hard rule #4)
  authoritative-sources:
    - georag-architecture.html §04h (multi-store retrieval + pre-gen evidence binding)
    - georag-architecture.html §04i (post-gen hallucination guards)
    - georag-architecture.html §05 (deterministic query flow)
    - georag-architecture.html §07c (streaming transport / Reverb event lifecycle)
    - georag-architecture.html §09b (query lifecycle states)
    - georag-architecture.html §10r (post-gen span resolution + partial-failure rules)
    - CLAUDE.md hard rules #4, #5
  scope: Laravel orchestration layer only. FastAPI guard implementation lives in the FastAPI service (see backend-fastapi agent).
---

# GeoRAG RAG Citations & Hallucination Prevention (Laravel side)

The refusal path is the product. A confident wrong answer is worse than an honest "I couldn't find this" — junior mining decisions ride on these outputs.

Laravel does not run §04i guards itself. FastAPI runs them. **Laravel's job is to:**
1. Refuse to persist or forward LLM output that doesn't carry validated citations
2. Respect the lifecycle state machine (`draft → generated → validated → committed`)
3. Treat refusals as a first-class response, not an error
4. Write the citation graph + retrieval trace to the audit schema, not the geological domain schema

## When to Apply

Reference this skill when working on:
- Query controllers in `app/Http/Controllers/` that POST to FastAPI
- Horizon jobs that orchestrate the query lifecycle
- Reverb event listeners forwarding `query.*` events
- DTOs / Form Requests / API Resources for query responses
- `answer_runs` persistence and the audit-schema writes
- Refusal UX rendering (insufficient-evidence path)
- Anything that touches `source_chunk_id`, `citation_ids`, `support_type`, `answer_run_id`

## Hard Rule (CLAUDE.md #4 — non-negotiable)

> Citations are mandatory on every RAG response. Every claim the LLM makes must include a `source_chunk_id` or be rejected by Pydantic AI's typed output validation. There is no "best-effort" citation mode.

Laravel enforces this at the boundary by validating FastAPI's response shape before persisting or forwarding it.

## The 6-layer model — which layers Laravel touches

CLAUDE.md hard rule #5 names six layers. Most live in FastAPI; Laravel touches three.

| # | Layer | Owner | Architecture ref |

|---|---|---|---|
| 1 | Retrieval quality gate | FastAPI | §04h |
| 2 | Typed output validation (Pydantic AI) | FastAPI | §04 Intelligence row |
| 3 | Numerical claim verification | FastAPI | §04i Numeric grounding |
| 4 | Entity resolution | FastAPI | §04i Entity grounding |
| 5 | Chunk provenance | **Laravel + FastAPI** | §04i Citation completeness, §10r resolved spans |
| 6 | Geological constraint / refusal | **Laravel + FastAPI** | §04i Refusal path, §10u refusal UX |

Laravel's enforcement surface is layers **5 and 6**: validate the citation shape on receipt, render refusals correctly, persist the audit trail.

## §04i guards — what FastAPI promises Laravel

Laravel code can rely on the following invariants holding when it receives a `query.validated` event from FastAPI. If any are missing, **fail loud**:

| Guard | Invariant Laravel can assume after `query.validated` |
|---|---|
| Numeric grounding | Every number (grade, depth, tonnage, coords) is supported within tolerance by attached citations |
| Entity grounding | Every named entity (project, hole, formation, QP, operator) resolves to a Neo4j node ID present in the bound evidence set |
| Citation completeness | Every paragraph in the answer carries ≥1 resolved citation span (or is explicitly marked uncited per §10r partial-failure rules) |
| Refusal path | If grounding failed after retries, FastAPI emits `query.refusal`, **not** `query.validated`. Never both. |

If you receive `query.complete` without a prior `query.validated` for the same `answer_run_id`, that is a system bug — alert and refuse to commit.

## Reverb event lifecycle (§07c) — order is non-negotiable

```
query.token (×N)        ← draft state, body tokens only, no citations
query.body_done         ← draft → generated
query.citation_attached ← post-gen span resolution (§10r), one per resolved span
query.validated         ← generated → validated, §04i guards passed
query.complete          ← validated → committed, the only "done" event
```

Or the refusal branch:

```
query.token (×N) → query.body_done → query.refusal  ← rejected state, triggers §10u UX
```

**Forbidden patterns:**
- Forwarding `query.complete` without a prior `query.validated`
- Persisting to `answer_runs` from any state other than `committed`
- Treating `query.refusal` as an error in the controller (it is a successful product output)

## Laravel patterns

### 1. Validate FastAPI response shape with a typed DTO

Never trust the FastAPI payload as-is. Validate at the boundary.

```php
<?php

declare(strict_types=1);

namespace App\Http\Resources\Query;

use Illuminate\Http\Request;
use Illuminate\Http\Resources\Json\JsonResource;

/**
 * @property-read string $answer_run_id
 * @property-read string $query_id
 * @property-read string $body
 * @property-read array<int, array{
 *     span_id: string,
 *     char_start: int,
 *     char_end: int,
 *     citation_ids: list<string>,
 *     source_chunk_ids: list<string>,
 *     support_type: 'direct'|'synthesized'|'comparative',
 *     resolver_confidence: float
 * }> $citation_spans
 * @property-read 'validated'|'committed' $state
 */
final class ValidatedAnswerResource extends JsonResource
{
    public function toArray(Request $request): array
    {
        // Hard fail if citation_spans is empty for a non-refusal answer.
        // Per §04i: every paragraph must carry ≥1 span (or be explicitly uncited per §10r).
        if ($this->state !== 'rejected' && empty($this->citation_spans)) {
            throw new \DomainException(
                "Answer {$this->answer_run_id} has no citation_spans but is not a refusal. §04i guard violation."
            );
        }

        return [
            'answer_run_id' => $this->answer_run_id,
            'query_id'      => $this->query_id,
            'body'          => $this->body,
            'citations'     => $this->citation_spans,
            'state'         => $this->state,
        ];
    }
}
```

### 2. Listener that gates `query.complete` on prior `query.validated`

Use Octane-safe state — **scoped state, not static, not singleton-with-request**.

```php
<?php

declare(strict_types=1);

namespace App\Listeners\Query;

use App\Events\QueryComplete;
use App\Events\QueryValidated;
use Illuminate\Support\Facades\Cache;

final class GuardLifecycleTransition
{
    /**
     * Mark answer_run_id as validated. Called from QueryValidated handler.
     */
    public function recordValidated(QueryValidated $event): void
    {
        Cache::put("answer_run:{$event->answerRunId}:validated", true, now()->addMinutes(10));
    }

    /**
     * Refuse to forward QueryComplete unless a QueryValidated was seen first.
     */
    public function gateComplete(QueryComplete $event): void
    {
        $key = "answer_run:{$event->answerRunId}:validated";

        if (! Cache::has($key)) {
            logger()->error('query.complete arrived without prior query.validated', [
                'answer_run_id' => $event->answerRunId,
                'query_id'      => $event->queryId,
            ]);

            // Do NOT forward to Reverb. Do NOT persist to answer_runs.
            // This is a §04i invariant violation — alert and drop.
            event(new \App\Events\Query\LifecycleViolation($event->answerRunId, 'complete_without_validated'));
            return;
        }

        // Safe to forward and commit.
        // ... persist to answer_runs in audit schema (§05 step 6) ...
    }
}
```

### 3. Refusal is a 200, not a 4xx/5xx

Refusals carry product value (what was searched, what was missing, what's actionable next). Never map them to HTTP error codes.

```php
public function show(Request $request, string $queryId): ValidatedAnswerResource|RefusalResource
{
    $run = AnswerRun::where('query_id', $queryId)
        ->where('workspace_id', $request->user()->currentWorkspace->id)
        ->firstOrFail();

    return match ($run->state) {
        'committed' => new ValidatedAnswerResource($run),
        'rejected'  => new RefusalResource($run),  // 200 OK, refusal payload per §10u
        default     => abort(409, "Query {$queryId} is in transient state {$run->state}; client should reconnect to Reverb"),
    };
}
```

### 4. Persist to the audit schema, not the geological domain schema (§05 step 6)

Operational query/answer/audit data must not be muddled with geology. Use a separate connection or schema.

```php
// config/database.php
'connections' => [
    'pgsql' => [/* geological domain — §04e schema */],
    'pgsql_audit' => [
        'driver'  => 'pgsql',
        'host'    => env('DB_AUDIT_HOST', env('DB_HOST')),
        'database'=> env('DB_AUDIT_DATABASE', 'georag_audit'),
        'schema'  => 'audit',
        // ...
    ],
],

// app/Models/Audit/AnswerRun.php
final class AnswerRun extends Model
{
    protected $connection = 'pgsql_audit';
    protected $table = 'answer_runs';
    // ...
}
```

## Octane safety reminder (cross-references CLAUDE.md hard rule #3)

The `Cache` example above uses Laravel's cache facade — **safe under Octane** because it resolves per-request. **Do not** use static properties to track lifecycle state across requests. **Do not** inject `Request` into a singleton.

```php
// FORBIDDEN under Octane:
class LifecycleTracker {
    private static array $validatedRuns = [];  // ❌ accumulates forever
}

// FORBIDDEN under Octane:
$this->app->singleton(LifecycleService::class, fn ($app) =>
    new LifecycleService($app['request'])  // ❌ first-request leak
);

// CORRECT:
$this->app->scoped(LifecycleService::class, fn () =>
    new LifecycleService(fn () => request())  // ✓ resolver closure
);
```

## Anti-patterns — never do these

| ❌ Don't | ✅ Do |
|---|---|
| Cache final answer text keyed only on query | Cache only retrieval results (§05c). Answer cache keys are unstable across model_version, prompt_version, citation_mode. |
| Treat `query.refusal` as an HTTP error | Map to a 200 with `RefusalResource` payload per §10u |
| Forward `query.complete` to Reverb on receipt | Gate on `query.validated` for the same `answer_run_id` |
| Inline-render free-form rationale text | Per §10t: support rationale is templated/extractive only — no free-form generation |
| Write `answer_runs` to the geological domain schema | Use the audit schema (§05 step 6) |
| Delete a citation span "to clean up" the response | Each span has audit/replay value. Mutate state, not history. |

## Validation checkpoints

Run these whenever you change query-orchestration code:

| Stage | Command | Expected |
|---|---|---|
| DTO contract | `php artisan test --filter=ValidatedAnswerResourceTest` | Empty-citations on non-refusal throws |
| Lifecycle gate | `php artisan test --filter=GuardLifecycleTransitionTest` | `complete` without prior `validated` → drop + alert |
| Refusal path | `php artisan test --filter=RefusalEndpointTest` | Refusal returns 200 with `RefusalResource` shape |
| Schema isolation | `php artisan tinker --execute='AnswerRun::query()->getConnection()->getDatabaseName()'` | Returns audit DB, not domain DB |
| Octane safety | `vendor/bin/phpstan analyse --level=max app/Listeners/Query` | No static-state warnings |

## When you're stuck

- **Citation shape unclear?** Read §04i + §10r in `georag-architecture.html`. Don't guess fields.
- **Lifecycle state ambiguous?** Read §07c + §09b for the full state machine.
- **Geological-domain question?** Don't infer. Ask Kyle (the SME).
- **Cross-cutting?** Invoke `senior-reviewer` agent.