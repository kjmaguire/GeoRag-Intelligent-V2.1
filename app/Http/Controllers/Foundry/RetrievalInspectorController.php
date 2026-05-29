<?php

declare(strict_types=1);

namespace App\Http\Controllers\Foundry;

use App\Http\Controllers\Controller;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;
use Inertia\Inertia;
use Inertia\Response;

/**
 * Foundry/RetrievalInspectorController — RAG debugger surface.
 *
 * Reads silver.answer_runs + silver.answer_retrieval_items +
 * silver.answer_citation_items for the trace. The v2 handoff added a Plan
 * stage at the start with decomposition + D4 decision points; if the
 * run carries plan_json, surface it as the first stage.
 *
 * Joins
 * -----
 * Both child tables identify chunks via the foreign key
 * `passage_id → silver.document_passages.passage_id`. Title + snippet are
 * not stored on the child rows themselves — we hop one more time to
 * `silver.reports.title` (passages.document_id → reports.report_id) and
 * grab the first 200 characters of `document_passages.text` for the
 * snippet. For non-passage candidates (PostGIS collar lookups, Neo4j
 * graph entities) the `candidate_ref` JSONB carries the title + snippet
 * inline so the LEFT JOIN can stay null without breaking the display.
 *
 * Rank
 * ----
 * `silver.answer_retrieval_items` has no `rank` column — items are
 * positionally meaningful via `rrf_rank`, `reranker_score`, and
 * `retriever_score`. We derive a stable display rank with
 * `ROW_NUMBER() OVER (...)` ordered by the best available score so the
 * inspector lists items in the same order the LLM saw them.
 */
class RetrievalInspectorController extends Controller
{
    public function show(Request $request, string $traceId): Response
    {
        $isUuid = preg_match('/^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/', $traceId) === 1;

        $run = null;
        $retrievalItems = collect();
        $citations = collect();
        $trace = null;

        if ($isUuid) {
            try {
                $run = DB::table('silver.answer_runs')->where('answer_run_id', $traceId)->first();
                if ($run) {
                    // silver.query_traces is 1:1 with answer_runs via
                    // answer_run_id; it carries the plan §0e retrieval-pipeline
                    // audit fields the answer_runs row doesn't (router
                    // confidence, guard codes, repair attempts, context-prep
                    // audit, multi-turn resolution). Wrapped in its own
                    // try/catch so a missing column on an older test DB
                    // doesn't blank the whole inspector — the original
                    // answer_runs panel still renders.
                    try {
                        $trace = DB::table('silver.query_traces')
                            ->where('answer_run_id', $traceId)
                            ->orderByDesc('created_at')
                            ->first();
                    } catch (\Throwable $e) {
                        $trace = null;
                    }

                    $retrievalItems = collect(DB::select(
                        <<<'SQL'
                        SELECT ari.retrieval_item_id,
                               ari.stage,
                               ari.source_store,
                               ari.passage_id,
                               ari.candidate_ref,
                               ari.retriever_score,
                               ari.reranker_score,
                               ari.rrf_score,
                               ari.included_in_context,
                               ari.used_in_citation,
                               ROW_NUMBER() OVER (
                                   ORDER BY COALESCE(ari.reranker_score, ari.retriever_score, ari.rrf_score, 0) DESC,
                                            ari.created_at ASC
                               )::int AS rank_computed,
                               r.title AS document_title_join,
                               LEFT(dp.text, 200) AS passage_snippet,
                               dp.page_first
                          FROM silver.answer_retrieval_items ari
                          LEFT JOIN silver.document_passages dp ON ari.passage_id = dp.passage_id
                          LEFT JOIN silver.reports r ON dp.document_id = r.report_id
                         WHERE ari.answer_run_id = ?::uuid
                         ORDER BY rank_computed ASC
                         LIMIT 100
                        SQL,
                        [$traceId],
                    ));

                    $citations = collect(DB::select(
                        <<<'SQL'
                        SELECT aci.answer_citation_item_id,
                               aci.marker_text,
                               aci.source_store,
                               aci.confidence,
                               aci.passage_id,
                               aci.evidence_id,
                               r.title AS document_title_join,
                               LEFT(dp.text, 200) AS passage_snippet
                          FROM silver.answer_citation_items aci
                          LEFT JOIN silver.document_passages dp ON aci.passage_id = dp.passage_id
                          LEFT JOIN silver.reports r ON dp.document_id = r.report_id
                         WHERE aci.answer_run_id = ?::uuid
                         ORDER BY aci.marker_text ASC
                        SQL,
                        [$traceId],
                    ));
                }
            } catch (\Throwable $e) {
                // Schema drift or DB hiccup — render empty state.
            }
        }

        $planJson = null;
        if ($run && isset($run->plan_json)) {
            $planJson = is_string($run->plan_json) ? json_decode($run->plan_json, true) : $run->plan_json;
        }

        return Inertia::render('Foundry/RetrievalInspector', [
            'trace_id' => $traceId,
            'run' => $run ? [
                'answer_run_id' => $run->answer_run_id,
                'query_text' => $run->query_text ?? null,
                'query_class' => $run->query_class ?? null,
                'confidence' => isset($run->confidence) ? (float) $run->confidence : null,
                'latency_ms' => $run->latency_ms ?? null,
                'rejection_reason' => $run->rejection_reason ?? null,
                'created_at' => $run->created_at ?? null,
            ] : null,
            'plan' => $planJson,
            'trace' => $trace ? $this->mapTrace($trace) : null,
            'retrieval_items' => $retrievalItems->map(fn ($r) => $this->mapRetrievalItem($r))->values(),
            'citations' => $citations->map(fn ($c) => $this->mapCitation($c))->values(),
            'empty' => $run === null,
        ]);
    }

    /**
     * Map a silver.query_traces row onto the Inertia prop shape the
     * inspector's "Trace" stage expects.
     *
     * Surfaces the three audit blocks the retrieval pipeline writes:
     *   - guard pass/fail + failure codes + repair attempts (plan §4b)
     *   - context_prep_audit JSONB (plan §3 spine — quota usage,
     *     dropped evidence, kind distribution before/after)
     *   - multi_turn_resolution JSONB (plan §3e — pronoun /
     *     demonstrative / comparative rewrites with confidence)
     *   - repair_strategies_used pulled out of trace_payload (plan §4)
     *
     * JSONB columns are decoded via the same helper as candidate_ref;
     * the driver may hand them back as string OR pre-decoded array
     * depending on the connection cast config.
     *
     * @return array{
     *     trace_id: string,
     *     normalized_query: string|null,
     *     conversation_turn: int|null,
     *     system_prompt_tokens: int|null,
     *     remaining_context_budget: int|null,
     *     final_token_count: int|null,
     *     router_decision: string|null,
     *     router_confidence: float|null,
     *     effective_intent: string|null,
     *     guard_pass: bool|null,
     *     guard_failure_codes: array<int, string>,
     *     repair_attempts: int,
     *     repair_strategies_used: array<int, string>,
     *     death_loop_triggered: bool,
     *     cache_hit: bool,
     *     cache_type: string|null,
     *     latency_total_ms: int|null,
     *     latency_routing_ms: int|null,
     *     latency_retrieval_ms: int|null,
     *     latency_reranking_ms: int|null,
     *     latency_generation_ms: int|null,
     *     latency_guards_ms: int|null,
     *     context_prep_audit: array<string, mixed>|null,
     *     multi_turn_resolution: array<string, mixed>|null
     * }
     */
    private function mapTrace(\stdClass $t): array
    {
        $payload = $this->decodeJsonb($t->trace_payload ?? null);
        $contextPrep = $this->decodeJsonb($t->context_prep_audit ?? null);
        $multiTurn = $this->decodeJsonb($t->multi_turn_resolution ?? null);

        // guard_failure_codes comes back as a Postgres text[] literal
        // ("{NUMERIC_GROUNDING_FAILED,OVER_FILTERED_QUERY}") or a real
        // array depending on the driver. Normalise to a JS-friendly
        // string[] for the React side.
        $guardCodes = $this->decodePgTextArray($t->guard_failure_codes ?? null);

        // repair_strategies_used lives inside trace_payload (plan §0e
        // schema); pull it out so the React side doesn't have to drill
        // into the JSONB.
        $strategies = [];
        if (isset($payload['repair_strategies_used']) && is_array($payload['repair_strategies_used'])) {
            $strategies = array_values(array_map('strval', $payload['repair_strategies_used']));
        }

        return [
            'trace_id' => (string) ($t->trace_id ?? ''),
            'normalized_query' => $t->normalized_query ?? null,
            'conversation_turn' => isset($t->conversation_turn) ? (int) $t->conversation_turn : null,
            'system_prompt_tokens' => isset($t->system_prompt_tokens) ? (int) $t->system_prompt_tokens : null,
            'remaining_context_budget' => isset($t->remaining_context_budget) ? (int) $t->remaining_context_budget : null,
            'final_token_count' => isset($t->final_token_count) ? (int) $t->final_token_count : null,
            'router_decision' => $t->router_decision ?? null,
            'router_confidence' => isset($t->router_confidence) ? (float) $t->router_confidence : null,
            'effective_intent' => $t->effective_intent ?? null,
            'guard_pass' => isset($t->guard_pass) ? (bool) $t->guard_pass : null,
            'guard_failure_codes' => $guardCodes,
            'repair_attempts' => isset($t->repair_attempts) ? (int) $t->repair_attempts : 0,
            'repair_strategies_used' => $strategies,
            'death_loop_triggered' => (bool) ($t->death_loop_triggered ?? false),
            'cache_hit' => (bool) ($t->cache_hit ?? false),
            'cache_type' => $t->cache_type ?? null,
            'latency_total_ms' => isset($t->latency_total_ms) ? (int) $t->latency_total_ms : null,
            'latency_routing_ms' => isset($t->latency_routing_ms) ? (int) $t->latency_routing_ms : null,
            'latency_retrieval_ms' => isset($t->latency_retrieval_ms) ? (int) $t->latency_retrieval_ms : null,
            'latency_reranking_ms' => isset($t->latency_reranking_ms) ? (int) $t->latency_reranking_ms : null,
            'latency_generation_ms' => isset($t->latency_generation_ms) ? (int) $t->latency_generation_ms : null,
            'latency_guards_ms' => isset($t->latency_guards_ms) ? (int) $t->latency_guards_ms : null,
            'context_prep_audit' => $contextPrep !== [] ? $contextPrep : null,
            'multi_turn_resolution' => $multiTurn !== [] ? $multiTurn : null,
        ];
    }

    /**
     * Decode a Postgres text[] column. asyncpg-backed PHP drivers can
     * return the value as the raw literal ("{a,b,c}"), a string-encoded
     * JSON array, or a real PHP array depending on connection casts.
     *
     * @return array<int, string>
     */
    private function decodePgTextArray(mixed $value): array
    {
        if ($value === null || $value === '') {
            return [];
        }
        if (is_array($value)) {
            return array_values(array_map('strval', $value));
        }
        if (is_string($value)) {
            // JSON-encoded array (less common path).
            $decoded = json_decode($value, true);
            if (is_array($decoded)) {
                return array_values(array_map('strval', $decoded));
            }
            // pg literal "{a,b,c}" — strip braces + split on comma.
            $trim = trim($value, '{}');
            if ($trim === '') {
                return [];
            }

            return array_values(array_filter(array_map(
                static fn (string $s) => trim($s, '"'),
                explode(',', $trim),
            ), static fn (string $s) => $s !== ''));
        }

        return [];
    }

    /**
     * Map an answer_retrieval_items row + joined passage/report data onto
     * the Inertia prop shape the inspector page expects.
     *
     * `stage`, `retriever_score`, and `reranker_score` are surfaced raw
     * so the React page's Rerank panel can filter on stage='reranked'
     * and show the cross-encoder score directly rather than the merged
     * `relevance` field.
     *
     * @return array{
     *     item_id: string,
     *     rank: int,
     *     stage: string,
     *     source_store: string,
     *     chunk_id: string,
     *     relevance: float|null,
     *     retriever_score: float|null,
     *     reranker_score: float|null,
     *     document_title: string,
     *     snippet: string
     * }
     */
    private function mapRetrievalItem(\stdClass $r): array
    {
        $candidateRef = $this->decodeJsonb($r->candidate_ref ?? null);

        // Chunk identifier — prefer the FK passage_id (real UUID), fall
        // back to whatever the tool stuffed into candidate_ref.chunk_id
        // (postgis lookups, neo4j entities, etc.).
        $chunkId = $r->passage_id
            ?? ($candidateRef['chunk_id'] ?? '')
            ?: ($candidateRef['canonical_id'] ?? '');

        // Document title — passages join wins; candidate_ref is the
        // fallback for non-passage candidates (collar / graph entities).
        $documentTitle = $r->document_title_join
            ?? ($candidateRef['document_title'] ?? '');

        // Snippet — same precedence: real passage text first, then
        // tool-supplied snippet in candidate_ref.
        $snippet = (string) ($r->passage_snippet
            ?? ($candidateRef['snippet'] ?? ''));

        // Relevance score — pick the most-refined score available.
        $relevance = $r->reranker_score
            ?? $r->retriever_score
            ?? $r->rrf_score
            ?? null;

        return [
            'item_id' => (string) ($r->retrieval_item_id ?? ''),
            'rank' => (int) ($r->rank_computed ?? 0),
            'stage' => (string) ($r->stage ?? 'retrieved'),
            'source_store' => (string) ($r->source_store ?? ''),
            'chunk_id' => (string) $chunkId,
            'relevance' => $relevance !== null ? (float) $relevance : null,
            'retriever_score' => isset($r->retriever_score) ? (float) $r->retriever_score : null,
            'reranker_score' => isset($r->reranker_score) ? (float) $r->reranker_score : null,
            'document_title' => (string) $documentTitle,
            'snippet' => substr($snippet, 0, 200),
        ];
    }

    /**
     * Map an answer_citation_items row + joined passage/report data onto
     * the Inertia prop shape the inspector page expects.
     *
     * Citation marker_text is stored as `[DATA:1]` / `[NI43:2]` etc.;
     * the citation_type for the UI is derived from the prefix so the
     * inspector keeps showing a recognisable pill ("DATA", "NI43").
     *
     * @return array{
     *     citation_id: string,
     *     citation_type: string,
     *     chunk_id: string,
     *     document_title: string,
     *     relevance: float|null
     * }
     */
    private function mapCitation(\stdClass $c): array
    {
        $marker = (string) ($c->marker_text ?? '');
        $citationType = '';
        if (preg_match('/^\[([A-Za-z0-9]+):/', $marker, $m) === 1) {
            $citationType = strtoupper($m[1]);
        }

        return [
            'citation_id' => $marker,
            'citation_type' => $citationType,
            'chunk_id' => (string) ($c->passage_id ?? $c->evidence_id ?? ''),
            'document_title' => (string) ($c->document_title_join ?? ''),
            'relevance' => isset($c->confidence) ? (float) $c->confidence : null,
        ];
    }

    /**
     * Decode a JSONB column whose driver may hand it back as a string
     * (raw asyncpg-style binding) or an associative array (already
     * decoded by an Eloquent cast). Returns [] on null / bad JSON so
     * the caller can use `??` chaining without defensive isset.
     *
     * @return array<string, mixed>
     */
    private function decodeJsonb(mixed $value): array
    {
        if ($value === null) {
            return [];
        }
        if (is_array($value)) {
            return $value;
        }
        if (is_string($value)) {
            $decoded = json_decode($value, true);
            if (is_array($decoded)) {
                return $decoded;
            }
        }

        return [];
    }
}
