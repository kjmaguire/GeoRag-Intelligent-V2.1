import { useState } from 'react';
import { Head } from '@inertiajs/react';
import AppLayout from '@/Layouts/AppLayout';
import { PageHeader, Card, Pill, EmptyState, Segmented } from '@/Components/Foundry/primitives';

interface RetrievalInspectorProps {
    trace_id: string;
    run: {
        answer_run_id: string;
        query_text: string | null;
        query_class: string | null;
        confidence: number | null;
        latency_ms: number | null;
        rejection_reason: string | null;
        created_at: string | null;
    } | null;
    plan: {
        plan_id?: string;
        triggers?: string[];
        sub_queries?: Array<{ id: string; class: string; text: string; status: string; citations?: number; revise_count?: number }>;
        decisions?: Array<{ point: string; verdict: string; note: string }>;
        revise_count?: number;
        revise_budget?: number;
    } | null;
    retrieval_items: Array<{
        item_id: string;
        rank: number;
        stage: string;
        source_store: string;
        chunk_id: string;
        relevance: number | null;
        retriever_score: number | null;
        reranker_score: number | null;
        document_title: string;
        snippet: string;
    }>;
    citations: Array<{ citation_id: string; citation_type: string; chunk_id: string; document_title: string; relevance: number | null }>;
    trace: {
        trace_id: string;
        normalized_query: string | null;
        conversation_turn: number | null;
        system_prompt_tokens: number | null;
        remaining_context_budget: number | null;
        final_token_count: number | null;
        router_decision: string | null;
        router_confidence: number | null;
        effective_intent: string | null;
        guard_pass: boolean | null;
        guard_failure_codes: string[];
        repair_attempts: number;
        repair_strategies_used: string[];
        death_loop_triggered: boolean;
        cache_hit: boolean;
        cache_type: string | null;
        latency_total_ms: number | null;
        latency_routing_ms: number | null;
        latency_retrieval_ms: number | null;
        latency_reranking_ms: number | null;
        latency_generation_ms: number | null;
        latency_guards_ms: number | null;
        context_prep_audit: {
            intent?: string;
            quota_used?: Record<string, number>;
            reached_budget?: boolean;
            dropped_evidence_ids?: string[];
            budget_reason?: string | null;
            kind_distribution_before?: Record<string, number>;
            kind_distribution_after?: Record<string, number>;
        } | null;
        multi_turn_resolution: {
            original_query?: string;
            rewritten_query?: string;
            overall_confidence?: number;
            trace?: Array<{
                kind: string;
                original_phrase: string;
                resolved_to: string;
                source_turn_index: number;
                confidence: number;
            }>;
        } | null;
    } | null;
    empty: boolean;
}

type Stage = 'plan' | 'router' | 'retrieval' | 'rerank' | 'context' | 'gates' | 'trace';

export default function FoundryRetrievalInspector({ trace_id, run, plan, retrieval_items, citations, trace, empty }: RetrievalInspectorProps) {
    const hasPlan = Boolean(plan && (plan.sub_queries?.length || plan.decisions?.length));
    const hasTrace = Boolean(trace);
    const [stage, setStage] = useState<Stage>(hasPlan ? 'plan' : 'retrieval');

    return (
        <AppLayout>
            <Head title={`Retrieval · ${trace_id.slice(0, 8)}`} />

            <div className="flex-1 overflow-y-auto" style={{ background: 'var(--bg-0)', color: 'var(--fg-1)' }}>
                <PageHeader
                    eyebrow="RETRIEVAL INSPECTOR"
                    title={run?.query_text ? run.query_text.slice(0, 80) : 'Trace'}
                    sub={run ? (
                        <span className="font-mono">
                            {run.answer_run_id} ·{' '}
                            {run.confidence !== null && <>conf {run.confidence.toFixed(2)} · </>}
                            {run.latency_ms !== null && <>{run.latency_ms}ms</>}
                        </span>
                    ) : 'no run found'}
                />

                {empty ? (
                    <div className="px-8 py-12">
                        <EmptyState
                            title={`No answer_run found for trace ${trace_id}`}
                            detail="The trace ID may have expired, been purged, or the row hasn't been written yet. Run a fresh query in Chat and click INSPECT RETRIEVAL on the response."
                        />
                    </div>
                ) : (
                    <>
                        <section className="px-8 py-3 border-b" style={{ borderColor: 'var(--line-1)' }}>
                            <Segmented<Stage>
                                value={stage}
                                onChange={setStage}
                                options={[
                                    ...(hasPlan ? [{ value: 'plan' as Stage, label: 'Plan' }] : []),
                                    { value: 'retrieval', label: 'Retrieval' },
                                    { value: 'rerank', label: 'Rerank' },
                                    { value: 'context', label: 'Context' },
                                    { value: 'gates', label: 'Gates' },
                                    ...(hasTrace ? [{ value: 'trace' as Stage, label: 'Trace' }] : []),
                                ]}
                            />
                        </section>

                        <section className="px-8 py-6">
                            {stage === 'plan' && plan && (
                                <PlanStage plan={plan} />
                            )}
                            {stage === 'retrieval' && (
                                <Card eyebrow={`RETRIEVAL ITEMS · ${retrieval_items.length}`} padded={false}>
                                    {retrieval_items.length === 0 ? (
                                        <div className="px-4 py-6 text-xs" style={{ color: 'var(--fg-3)' }}>No retrieval items recorded.</div>
                                    ) : retrieval_items.map((it) => (
                                        <div key={it.item_id} className="grid grid-cols-[40px_1fr_120px_70px] gap-3 items-center px-4 py-2 border-b" style={{ borderColor: 'var(--line-1)' }}>
                                            <span className="font-mono text-xs" style={{ color: 'var(--fg-3)' }}>#{it.rank}</span>
                                            <div>
                                                <div className="text-xs font-medium" style={{ color: 'var(--fg-0)' }}>{it.document_title || it.chunk_id}</div>
                                                <div className="text-[11px] mt-0.5" style={{ color: 'var(--fg-2)' }}>{it.snippet}</div>
                                            </div>
                                            <span className="text-[10px] font-mono uppercase tracking-wider" style={{ color: 'var(--fg-3)' }}>{it.source_store}</span>
                                            <span className="text-xs font-mono text-right" style={{ color: it.relevance && it.relevance >= 0.7 ? 'var(--accent)' : 'var(--fg-2)' }}>
                                                {it.relevance !== null ? it.relevance.toFixed(2) : '—'}
                                            </span>
                                        </div>
                                    ))}
                                </Card>
                            )}
                            {stage === 'rerank' && (
                                (() => {
                                    // Items that came through the BGE cross-encoder
                                    // reranker land with stage='reranked' and carry a
                                    // numeric reranker_score. Anything else (direct PK
                                    // lookups, graph traversals) bypasses rerank and
                                    // shows up only in the Retrieval panel.
                                    const reranked = retrieval_items.filter(
                                        (it) => it.stage === 'reranked' && it.reranker_score !== null,
                                    );
                                    const max = reranked.reduce(
                                        (m, it) => Math.max(m, it.reranker_score ?? 0),
                                        0,
                                    );
                                    const min = reranked.reduce(
                                        (m, it) => Math.min(m, it.reranker_score ?? Infinity),
                                        Infinity,
                                    );
                                    return (
                                        <Card
                                            eyebrow={`RERANK · ${reranked.length} item${reranked.length === 1 ? '' : 's'}`}
                                            title="Cross-encoder rerank decisions"
                                            padded={false}
                                        >
                                            {reranked.length === 0 ? (
                                                <div className="px-4 py-6 text-xs" style={{ color: 'var(--fg-3)' }}>
                                                    No rerank stage rows recorded for this run. Direct
                                                    lookups (PK collar fetches, graph traversals) bypass
                                                    the cross-encoder and only appear under Retrieval.
                                                </div>
                                            ) : (
                                                <>
                                                    <div
                                                        className="px-4 py-2 text-[10px] font-mono uppercase tracking-wider border-b"
                                                        style={{ color: 'var(--fg-3)', borderColor: 'var(--line-1)' }}
                                                    >
                                                        bge-reranker-base · score range {min.toFixed(3)} – {max.toFixed(3)}
                                                    </div>
                                                    {reranked.map((it) => {
                                                        const score = it.reranker_score ?? 0;
                                                        // Normalize the score for the score bar so the
                                                        // top-ranked item is full-width and the lowest is
                                                        // proportional. Bg-reranker logits aren't bounded
                                                        // to [0,1] so use the run's own max as the scale.
                                                        const pct = max > 0 ? Math.max(4, (score / max) * 100) : 0;
                                                        return (
                                                            <div
                                                                key={it.item_id}
                                                                className="grid grid-cols-[40px_1fr_140px] gap-3 items-center px-4 py-2 border-b"
                                                                style={{ borderColor: 'var(--line-1)' }}
                                                            >
                                                                <span className="font-mono text-xs" style={{ color: 'var(--fg-3)' }}>#{it.rank}</span>
                                                                <div>
                                                                    <div className="text-xs font-medium truncate" style={{ color: 'var(--fg-0)' }}>
                                                                        {it.document_title || it.chunk_id}
                                                                    </div>
                                                                    <div className="text-[11px] mt-0.5 truncate" style={{ color: 'var(--fg-2)' }}>
                                                                        {it.snippet}
                                                                    </div>
                                                                </div>
                                                                <div className="flex items-center gap-2">
                                                                    <div
                                                                        className="flex-1 h-1 rounded"
                                                                        style={{ background: 'var(--line-1)' }}
                                                                    >
                                                                        <div
                                                                            className="h-1 rounded"
                                                                            style={{
                                                                                width: `${pct}%`,
                                                                                background: score >= max * 0.66
                                                                                    ? 'var(--accent)'
                                                                                    : score >= max * 0.33
                                                                                        ? 'var(--warn)'
                                                                                        : 'var(--fg-3)',
                                                                            }}
                                                                        />
                                                                    </div>
                                                                    <span
                                                                        className="text-xs font-mono w-12 text-right"
                                                                        style={{ color: score >= max * 0.66 ? 'var(--accent)' : 'var(--fg-2)' }}
                                                                    >
                                                                        {score.toFixed(3)}
                                                                    </span>
                                                                </div>
                                                            </div>
                                                        );
                                                    })}
                                                </>
                                            )}
                                        </Card>
                                    );
                                })()
                            )}
                            {stage === 'context' && (
                                <Card eyebrow={`FINAL CONTEXT · ${citations.length} citations`} padded={false}>
                                    {citations.length === 0 ? (
                                        <div className="px-4 py-6 text-xs" style={{ color: 'var(--fg-3)' }}>No citations recorded.</div>
                                    ) : citations.map((c) => (
                                        <div key={c.citation_id} className="grid grid-cols-[60px_1fr_70px] gap-3 px-4 py-2 border-b items-center" style={{ borderColor: 'var(--line-1)' }}>
                                            <Pill tone="info">{c.citation_type}</Pill>
                                            <div className="text-xs truncate" style={{ color: 'var(--fg-0)' }}>{c.document_title || c.chunk_id}</div>
                                            <span className="text-xs font-mono text-right" style={{ color: 'var(--fg-2)' }}>
                                                {c.relevance !== null ? c.relevance.toFixed(2) : '—'}
                                            </span>
                                        </div>
                                    ))}
                                </Card>
                            )}
                            {stage === 'gates' && (
                                <Card eyebrow="HALLUCINATION GATES" title={run?.rejection_reason ? 'Refused' : 'All gates passed'}>
                                    {run?.rejection_reason ? (
                                        <div className="text-xs" style={{ color: 'var(--warn)' }}>{run.rejection_reason}</div>
                                    ) : (
                                        <div className="text-xs" style={{ color: 'var(--accent)' }}>● Retrieval quality, citation anchor, typed output, numerical claims, entity resolution, geological constraints — all passed.</div>
                                    )}
                                </Card>
                            )}
                            {stage === 'trace' && trace && (
                                <TraceStage trace={trace} />
                            )}
                        </section>
                    </>
                )}
            </div>
        </AppLayout>
    );
}

function PlanStage({ plan }: { plan: NonNullable<RetrievalInspectorProps['plan']> }) {
    const decTone = (v: string): 'accent' | 'warn' | 'danger' | 'neutral' => {
        if (v.startsWith('revise')) return 'warn';
        if (v === 'proceed' || v === 'consistent') return 'accent';
        if (v === 'abstain') return 'warn';
        return 'neutral';
    };

    return (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
            <Card eyebrow={`PLAN · ${plan.plan_id ?? '—'}`} title="Decomposition">
                <div className="text-[10px] font-mono uppercase tracking-wider mb-2" style={{ color: 'var(--fg-3)' }}>Triggers</div>
                <div className="flex flex-wrap gap-1.5 mb-4">
                    {(plan.triggers ?? []).map((t) => (
                        <Pill key={t} tone="accent">{t}</Pill>
                    ))}
                </div>
                <div className="text-[10px] font-mono uppercase tracking-wider mb-2" style={{ color: 'var(--fg-3)' }}>
                    Sub-queries · {plan.sub_queries?.length ?? 0}
                </div>
                <div className="space-y-2">
                    {(plan.sub_queries ?? []).map((sq, i) => (
                        <div key={sq.id} className="p-2 rounded border" style={{ background: 'var(--bg-2)', borderColor: 'var(--line-1)' }}>
                            <div className="flex items-center gap-2 mb-1">
                                <span className="text-[10px] font-mono" style={{ color: 'var(--fg-3)' }}>#{i + 1}</span>
                                <span className="text-[10px] font-mono px-1.5 py-0.5 rounded" style={{ color: 'var(--fg-2)', border: '1px solid var(--line-2)' }}>{sq.class}</span>
                                <Pill tone={sq.status === 'ok' ? 'accent' : sq.status === 'revised' ? 'warn' : 'danger'} dot>
                                    {sq.status}
                                </Pill>
                                {sq.citations !== undefined && (
                                    <span className="text-[10px] font-mono ml-auto" style={{ color: 'var(--fg-3)' }}>{sq.citations} cite{sq.citations === 1 ? '' : 's'}</span>
                                )}
                            </div>
                            <div className="text-xs" style={{ color: 'var(--fg-1)' }}>{sq.text}</div>
                        </div>
                    ))}
                </div>
            </Card>

            <Card eyebrow="D4 DECISION POINTS" title={`${plan.decisions?.length ?? 0} decisions · revise ${plan.revise_count ?? 0}/${plan.revise_budget ?? 1}`}>
                <ol className="space-y-2">
                    {(plan.decisions ?? []).map((d, i) => (
                        <li key={i} className="grid grid-cols-[160px_80px_1fr] gap-3 items-baseline text-xs">
                            <span className="font-mono" style={{ color: 'var(--fg-2)' }}>{d.point}</span>
                            <Pill tone={decTone(d.verdict)} dot>{d.verdict}</Pill>
                            <span style={{ color: 'var(--fg-1)' }}>{d.note}</span>
                        </li>
                    ))}
                </ol>
            </Card>
        </div>
    );
}

/**
 * "Trace" stage panel — surfaces the silver.query_traces audit JSONB the
 * retrieval pipeline writes alongside each answer_run. Four sub-cards:
 *
 *   1. Router + budget summary (router decision/confidence, effective
 *      intent, system_prompt + remaining + final token counts)
 *   2. Guard codes + repair attempts + strategies used + death-loop flag
 *   3. context_prep_audit (plan §3 spine) — quota usage, dropped
 *      evidence, kind distribution before/after
 *   4. multi_turn_resolution (plan §3e) — original/rewritten query +
 *      per-substitution trace with confidence
 *
 * Each card no-ops gracefully when the underlying column is NULL so the
 * panel works on traces from before the audit columns were wired.
 */
function TraceStage({ trace }: { trace: NonNullable<RetrievalInspectorProps['trace']> }) {
    const cp = trace.context_prep_audit;
    const mt = trace.multi_turn_resolution;
    const guardTone: 'accent' | 'warn' | 'danger' | 'neutral' =
        trace.guard_pass === true ? 'accent'
            : trace.guard_pass === false ? 'danger'
                : 'neutral';

    return (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
            {/* Card 1 — Router + budget summary */}
            <Card eyebrow="ROUTING & BUDGET" title={trace.effective_intent ?? trace.router_decision ?? '—'}>
                <dl className="grid grid-cols-[160px_1fr] gap-y-1.5 text-xs">
                    <dt style={{ color: 'var(--fg-3)' }}>Router decision</dt>
                    <dd style={{ color: 'var(--fg-1)' }} className="font-mono">
                        {trace.router_decision ?? '—'}
                        {trace.router_confidence !== null && (
                            <span style={{ color: 'var(--fg-3)' }}> · conf {trace.router_confidence.toFixed(2)}</span>
                        )}
                    </dd>
                    <dt style={{ color: 'var(--fg-3)' }}>Effective intent</dt>
                    <dd style={{ color: 'var(--fg-1)' }} className="font-mono">{trace.effective_intent ?? '—'}</dd>
                    <dt style={{ color: 'var(--fg-3)' }}>Conversation turn</dt>
                    <dd style={{ color: 'var(--fg-1)' }} className="font-mono">{trace.conversation_turn ?? 1}</dd>
                    <dt style={{ color: 'var(--fg-3)' }}>System prompt</dt>
                    <dd style={{ color: 'var(--fg-1)' }} className="font-mono">{trace.system_prompt_tokens ?? '—'} tok</dd>
                    <dt style={{ color: 'var(--fg-3)' }}>Remaining budget</dt>
                    <dd style={{ color: 'var(--fg-1)' }} className="font-mono">{trace.remaining_context_budget ?? '—'} tok</dd>
                    <dt style={{ color: 'var(--fg-3)' }}>Final tokens</dt>
                    <dd style={{ color: 'var(--fg-1)' }} className="font-mono">{trace.final_token_count ?? '—'}</dd>
                    {trace.cache_hit && (
                        <>
                            <dt style={{ color: 'var(--fg-3)' }}>Cache</dt>
                            <dd><Pill tone="accent" dot>{trace.cache_type ?? 'hit'}</Pill></dd>
                        </>
                    )}
                </dl>
            </Card>

            {/* Card 2 — Guards + repair */}
            <Card eyebrow="GUARDS & REPAIR" title={`${trace.repair_attempts} attempt${trace.repair_attempts === 1 ? '' : 's'}`}>
                <div className="flex items-center gap-2 mb-3 text-xs">
                    <Pill tone={guardTone} dot>
                        {trace.guard_pass === true ? 'pass' : trace.guard_pass === false ? 'fail' : 'unknown'}
                    </Pill>
                    {trace.death_loop_triggered && <Pill tone="danger" dot>death-loop</Pill>}
                </div>
                {trace.guard_failure_codes.length > 0 && (
                    <>
                        <div className="text-[10px] font-mono uppercase tracking-wider mb-1" style={{ color: 'var(--fg-3)' }}>
                            Failure codes
                        </div>
                        <div className="flex flex-wrap gap-1.5 mb-3">
                            {trace.guard_failure_codes.map((code) => (
                                <Pill key={code} tone="warn">{code}</Pill>
                            ))}
                        </div>
                    </>
                )}
                {trace.repair_strategies_used.length > 0 && (
                    <>
                        <div className="text-[10px] font-mono uppercase tracking-wider mb-1" style={{ color: 'var(--fg-3)' }}>
                            Strategies tried
                        </div>
                        <ol className="space-y-1 text-xs">
                            {trace.repair_strategies_used.map((s, i) => (
                                <li key={i} className="font-mono" style={{ color: 'var(--fg-1)' }}>
                                    <span style={{ color: 'var(--fg-3)' }}>#{i + 1}</span> {s}
                                </li>
                            ))}
                        </ol>
                    </>
                )}
                {trace.guard_failure_codes.length === 0 && trace.repair_strategies_used.length === 0 && (
                    <div className="text-xs" style={{ color: 'var(--fg-3)' }}>No guard failures or repair attempts recorded.</div>
                )}
            </Card>

            {/* Card 3 — Context prep audit */}
            <Card eyebrow="CONTEXT PREP · §3" title={cp?.intent ?? '—'}>
                {cp === null ? (
                    <div className="text-xs" style={{ color: 'var(--fg-3)' }}>
                        Context-prep audit not written. CONTEXT_PREP_ENABLED was off when this query ran, or the trace predates the §3 wire.
                    </div>
                ) : (
                    <>
                        <div className="flex items-center gap-2 mb-3 text-xs">
                            <Pill tone={cp.reached_budget ? 'warn' : 'accent'} dot>
                                {cp.reached_budget ? 'budget reached' : 'under budget'}
                            </Pill>
                            {cp.budget_reason && (
                                <span className="text-xs" style={{ color: 'var(--fg-2)' }}>{cp.budget_reason}</span>
                            )}
                        </div>
                        {cp.quota_used && Object.keys(cp.quota_used).length > 0 && (
                            <>
                                <div className="text-[10px] font-mono uppercase tracking-wider mb-1" style={{ color: 'var(--fg-3)' }}>
                                    Quota used
                                </div>
                                <div className="flex flex-wrap gap-1.5 mb-3">
                                    {Object.entries(cp.quota_used).map(([kind, n]) => (
                                        <Pill key={kind} tone="info">{kind} · {n}</Pill>
                                    ))}
                                </div>
                            </>
                        )}
                        {cp.kind_distribution_before && cp.kind_distribution_after && (
                            <>
                                <div className="text-[10px] font-mono uppercase tracking-wider mb-1" style={{ color: 'var(--fg-3)' }}>
                                    Kind distribution · before → after
                                </div>
                                <ol className="space-y-1 text-xs mb-3">
                                    {Object.keys({ ...cp.kind_distribution_before, ...cp.kind_distribution_after }).sort().map((kind) => {
                                        const before = cp.kind_distribution_before?.[kind] ?? 0;
                                        const after = cp.kind_distribution_after?.[kind] ?? 0;
                                        return (
                                            <li key={kind} className="grid grid-cols-[120px_60px_24px_60px] gap-2 font-mono" style={{ color: 'var(--fg-1)' }}>
                                                <span style={{ color: 'var(--fg-2)' }}>{kind}</span>
                                                <span className="text-right">{before}</span>
                                                <span style={{ color: 'var(--fg-3)' }}>→</span>
                                                <span className="text-right" style={{ color: after < before ? 'var(--warn)' : 'var(--fg-1)' }}>{after}</span>
                                            </li>
                                        );
                                    })}
                                </ol>
                            </>
                        )}
                        {cp.dropped_evidence_ids && cp.dropped_evidence_ids.length > 0 && (
                            <>
                                <div className="text-[10px] font-mono uppercase tracking-wider mb-1" style={{ color: 'var(--fg-3)' }}>
                                    Dropped · {cp.dropped_evidence_ids.length}
                                </div>
                                <div className="text-[11px] font-mono space-y-0.5" style={{ color: 'var(--fg-2)' }}>
                                    {cp.dropped_evidence_ids.slice(0, 5).map((id) => (
                                        <div key={id} className="truncate">{id}</div>
                                    ))}
                                    {cp.dropped_evidence_ids.length > 5 && (
                                        <div style={{ color: 'var(--fg-3)' }}>… +{cp.dropped_evidence_ids.length - 5} more</div>
                                    )}
                                </div>
                            </>
                        )}
                    </>
                )}
            </Card>

            {/* Card 4 — Multi-turn resolution */}
            <Card eyebrow="MULTI-TURN · §3e" title={mt?.overall_confidence !== undefined ? `conf ${mt.overall_confidence.toFixed(2)}` : '—'}>
                {mt === null ? (
                    <div className="text-xs" style={{ color: 'var(--fg-3)' }}>
                        No multi-turn rewrite recorded. MULTI_TURN_RESOLUTION_ENABLED was off, or the query carried no pronouns/demonstratives that needed resolving.
                    </div>
                ) : (
                    <>
                        {mt.original_query && mt.rewritten_query && mt.original_query !== mt.rewritten_query && (
                            <div className="mb-3 text-xs space-y-1">
                                <div style={{ color: 'var(--fg-3)' }} className="text-[10px] font-mono uppercase tracking-wider">Original</div>
                                <div style={{ color: 'var(--fg-2)' }} className="italic">{mt.original_query}</div>
                                <div style={{ color: 'var(--fg-3)' }} className="text-[10px] font-mono uppercase tracking-wider mt-2">Rewritten</div>
                                <div style={{ color: 'var(--fg-1)' }}>{mt.rewritten_query}</div>
                            </div>
                        )}
                        {mt.trace && mt.trace.length > 0 ? (
                            <ol className="space-y-2">
                                {mt.trace.map((step, i) => (
                                    <li key={i} className="grid grid-cols-[24px_80px_1fr_60px] gap-2 items-baseline text-xs">
                                        <span className="font-mono" style={{ color: 'var(--fg-3)' }}>#{i + 1}</span>
                                        <Pill tone="info">{step.kind}</Pill>
                                        <div className="font-mono" style={{ color: 'var(--fg-1)' }}>
                                            <span style={{ color: 'var(--fg-2)' }}>{step.original_phrase}</span>
                                            <span style={{ color: 'var(--fg-3)' }}> → </span>
                                            <span>{step.resolved_to}</span>
                                            <span style={{ color: 'var(--fg-3)' }} className="ml-1">(turn {step.source_turn_index})</span>
                                        </div>
                                        <span className="text-right font-mono" style={{ color: step.confidence >= 0.7 ? 'var(--accent)' : 'var(--warn)' }}>
                                            {step.confidence.toFixed(2)}
                                        </span>
                                    </li>
                                ))}
                            </ol>
                        ) : (
                            <div className="text-xs" style={{ color: 'var(--fg-3)' }}>No substitutions applied.</div>
                        )}
                    </>
                )}
            </Card>

            {/* Latency breakdown — full width below */}
            {(trace.latency_total_ms !== null || trace.latency_routing_ms !== null) && (
                <div className="lg:col-span-2">
                    <Card eyebrow={`LATENCY · ${trace.latency_total_ms ?? '—'}ms total`} title="Per-stage breakdown">
                        <div className="grid grid-cols-2 md:grid-cols-5 gap-3 text-xs">
                            {([
                                ['Routing', trace.latency_routing_ms],
                                ['Retrieval', trace.latency_retrieval_ms],
                                ['Rerank', trace.latency_reranking_ms],
                                ['Generation', trace.latency_generation_ms],
                                ['Guards', trace.latency_guards_ms],
                            ] as const).map(([label, ms]) => (
                                <div key={label}>
                                    <div className="text-[10px] font-mono uppercase tracking-wider" style={{ color: 'var(--fg-3)' }}>{label}</div>
                                    <div className="font-mono" style={{ color: 'var(--fg-1)' }}>{ms !== null ? `${ms}ms` : '—'}</div>
                                </div>
                            ))}
                        </div>
                    </Card>
                </div>
            )}
        </div>
    );
}
