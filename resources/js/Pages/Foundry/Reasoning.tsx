import { useMemo, useState } from 'react';
import { Head, Link, router } from '@inertiajs/react';
import AppLayout from '@/Layouts/AppLayout';
import { PageHeader, Card, Pill, EmptyState } from '@/Components/Foundry/primitives';
import { useWorkspaceDataUpdated } from '@/Hooks/useWorkspaceDataUpdated';

interface Hypothesis {
    id: string;
    title: string;
    label: string;
    description: string;
    rationale: string;
    status: string;
    confidence: number | null | undefined;
    confidence_method: string;
    support_count: number;
    contradict_count: number;
    missing_count: number;
    tests_count: number;
    updated: string;
}

interface EvidenceLink {
    id: string;
    hypothesis_id: string;
    role: 'supporting' | 'contradicting' | 'missing' | 'recommended_test' | string;
    weight: number | null | undefined;
    source_chunk_id: string;
    passage_excerpt: string | null;
    document_id: string | null;
    payload: Record<string, unknown> | null;
}

interface ReasoningStats {
    total_hypotheses: number;
    total_evidence: number;
    supporting: number;
    contradicting: number;
    missing: number;
    recommended_tests: number;
    passages_indexed: number;
    reports_indexed: number;
    collars_in_project: number;
}

interface ReasoningProps {
    project: { project_id: string; project_name: string; slug: string };
    hypotheses: Hypothesis[];
    evidence: EvidenceLink[];
    stats: ReasoningStats;
    empty: boolean;
    scope_note: string;
}

type Stage = 'evidence' | 'reasoning' | 'candidates' | 'graph';

const STAGES: Array<{ id: Stage; label: string; sub: string }> = [
    { id: 'evidence', label: '1 · Evidence', sub: 'Review the evidence stack feeding each hypothesis' },
    { id: 'reasoning', label: '2 · Reasoning', sub: 'Hypotheses with parent question, rationale, support counts' },
    { id: 'candidates', label: '3 · Candidates', sub: 'Ranked answers · confidence · evidence breakdown' },
    { id: 'graph', label: '4 · Evidence Graph', sub: 'Raw → source → fact → conclusion node flow' },
];

const ROLE_TONES: Record<string, 'accent' | 'danger' | 'info' | 'neutral'> = {
    supporting: 'accent',
    contradicting: 'danger',
    missing: 'neutral',
    recommended_test: 'info',
};

function roleLabel(r: string): string {
    return r.replaceAll('_', ' ');
}

function confidenceTone(c: number | null | undefined): 'accent' | 'info' | 'neutral' {
    if (typeof c !== 'number') return 'neutral';
    if (c >= 0.6) return 'accent';
    if (c >= 0.3) return 'info';
    return 'neutral';
}

export default function FoundryReasoning({
    project,
    hypotheses,
    evidence,
    stats,
    empty,
    scope_note,
}: ReasoningProps) {
    // Phase 5 real-time push — continuous_learning_loop +
    // field_outcome_learning + sync_silver_to_kg all change hypothesis-
    // related rollups. The `hypotheses` affected_type is newly emitted
    // by DebounceWorkspaceMvRefresh's superset (Phase 5 extension).
    useWorkspaceDataUpdated(project.project_id, (event) => {
        if (event.affected_types.includes('hypotheses')) {
            router.reload({ only: ['hypotheses', 'evidence', 'stats', 'empty'] });
        }
    });

    // Open on Evidence (stage 1) — the workbench starts by reviewing the
    // evidence stack before drilling into hypotheses.
    const [stage, setStage] = useState<Stage>('evidence');
    const [visited, setVisited] = useState<Set<Stage>>(new Set(['evidence']));
    const [selectedHypothesisId, setSelectedHypothesisId] = useState<string | null>(
        hypotheses[0]?.id ?? null,
    );
    const stageIndex = STAGES.findIndex((s) => s.id === stage);

    function goStage(id: Stage) {
        setStage(id);
        setVisited((v) => new Set([...v, id]));
    }

    const evidenceById = useMemo(() => {
        const m = new Map<string, EvidenceLink[]>();
        evidence.forEach((e) => {
            const arr = m.get(e.hypothesis_id) ?? [];
            arr.push(e);
            m.set(e.hypothesis_id, arr);
        });
        return m;
    }, [evidence]);

    return (
        <AppLayout>
            <Head title={`Reasoning · ${project.project_name}`} />

            <div
                className="flex-1 flex flex-col overflow-hidden"
                style={{ background: 'var(--bg-0)', color: 'var(--fg-1)' }}
            >
                <PageHeader
                    eyebrow={`PROJECT · ${project.project_name.toUpperCase()} · REASONING`}
                    title="4-stage workbench"
                    sub="Evidence → Reasoning → Candidates → Evidence Graph"
                />

                {scope_note && (
                    <div
                        className="px-8 py-2 text-[10px] font-mono uppercase tracking-wider border-b"
                        style={{
                            background: 'var(--bg-1)',
                            color: 'var(--fg-3)',
                            borderColor: 'var(--line-1)',
                        }}
                    >
                        Scope · {scope_note}
                    </div>
                )}

                {/* Stage strip */}
                <div
                    className="flex items-center gap-2 px-8 py-3 border-b"
                    style={{ borderColor: 'var(--line-1)' }}
                >
                    {STAGES.map((s, i) => (
                        <div key={s.id} className="flex items-center gap-2">
                            <button
                                type="button"
                                onClick={() => goStage(s.id)}
                                className="flex items-center gap-2 px-3 py-1.5 rounded transition-colors"
                                style={{
                                    background: stage === s.id ? 'var(--accent-bg)' : 'transparent',
                                    border:
                                        '1px solid ' +
                                        (stage === s.id ? 'var(--accent-dim)' : 'var(--line-1)'),
                                }}
                            >
                                <span
                                    className="w-5 h-5 rounded-full text-[10px] font-mono flex items-center justify-center"
                                    style={{
                                        background:
                                            stage === s.id
                                                ? 'var(--accent)'
                                                : visited.has(s.id)
                                                  ? 'var(--accent-bg)'
                                                  : 'var(--bg-2)',
                                        color:
                                            stage === s.id
                                                ? 'var(--bg-0)'
                                                : visited.has(s.id)
                                                  ? 'var(--accent)'
                                                  : 'var(--fg-3)',
                                        border:
                                            '1px solid ' +
                                            (visited.has(s.id) ? 'var(--accent-dim)' : 'var(--line-2)'),
                                    }}
                                >
                                    {visited.has(s.id) && stage !== s.id ? '✓' : i + 1}
                                </span>
                                <span
                                    className="text-[11px] font-mono uppercase tracking-wider"
                                    style={{ color: stage === s.id ? 'var(--fg-0)' : 'var(--fg-2)' }}
                                >
                                    {s.label.split(' · ')[1]}
                                </span>
                            </button>
                            {i < STAGES.length - 1 && <span style={{ color: 'var(--fg-4)' }}>›</span>}
                        </div>
                    ))}
                </div>

                {empty ? (
                    <div className="flex-1 px-8 py-12">
                        <EmptyState
                            title="No hypotheses or evidence in this workspace yet."
                            detail="As the §04j agentic retrieval pipeline runs, silver.hypotheses and silver.hypothesis_evidence_links populate automatically. Try asking a question in Chat to seed them."
                        />
                    </div>
                ) : (
                    <div className="flex-1 overflow-y-auto p-8 space-y-4">
                        {stage === 'evidence' && (
                            <EvidenceStage
                                evidence={evidence}
                                hypotheses={hypotheses}
                                stats={stats}
                                selectedHypothesisId={selectedHypothesisId}
                                onSelectHypothesis={setSelectedHypothesisId}
                            />
                        )}
                        {stage === 'reasoning' && (
                            <ReasoningStage
                                hypotheses={hypotheses}
                                selectedHypothesisId={selectedHypothesisId}
                                onSelectHypothesis={setSelectedHypothesisId}
                            />
                        )}
                        {stage === 'candidates' && (
                            <CandidatesStage
                                hypotheses={hypotheses}
                                evidenceById={evidenceById}
                                project={project}
                            />
                        )}
                        {stage === 'graph' && <GraphStage project={project} stats={stats} />}
                    </div>
                )}

                {/* Footer nav */}
                <footer
                    className="flex items-center justify-between px-8 py-3 border-t shrink-0"
                    style={{ borderColor: 'var(--line-1)' }}
                >
                    <button
                        type="button"
                        disabled={stageIndex === 0}
                        onClick={() => goStage(STAGES[Math.max(0, stageIndex - 1)].id)}
                        className="text-[10px] font-mono uppercase tracking-wider px-3 py-1.5 rounded border disabled:opacity-30"
                        style={{ color: 'var(--fg-2)', borderColor: 'var(--line-2)' }}
                    >
                        ← Back
                    </button>
                    <span
                        className="text-[10px] font-mono uppercase tracking-wider"
                        style={{ color: 'var(--fg-3)' }}
                    >
                        {STAGES[stageIndex].sub}
                    </span>
                    <button
                        type="button"
                        disabled={stageIndex === STAGES.length - 1}
                        onClick={() => goStage(STAGES[Math.min(STAGES.length - 1, stageIndex + 1)].id)}
                        className="text-[10px] font-mono uppercase tracking-wider px-3 py-1.5 rounded border disabled:opacity-30"
                        style={{
                            color: 'var(--accent)',
                            background: 'var(--accent-bg)',
                            borderColor: 'var(--accent-dim)',
                        }}
                    >
                        Next →
                    </button>
                </footer>
            </div>
        </AppLayout>
    );
}

// ── STAGE 1: Evidence ─────────────────────────────────────────────────
function EvidenceStage({
    evidence,
    hypotheses,
    stats,
    selectedHypothesisId,
    onSelectHypothesis,
}: {
    evidence: EvidenceLink[];
    hypotheses: Hypothesis[];
    stats: ReasoningStats;
    selectedHypothesisId: string | null;
    onSelectHypothesis: (id: string) => void;
}) {
    const filtered = useMemo(
        () =>
            selectedHypothesisId
                ? evidence.filter((e) => e.hypothesis_id === selectedHypothesisId)
                : evidence,
        [evidence, selectedHypothesisId],
    );

    return (
        <>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                <StatTile label="Evidence links" value={stats.total_evidence} tone="info" />
                <StatTile label="Supporting" value={stats.supporting} tone="accent" />
                <StatTile label="Contradicting" value={stats.contradicting} tone="danger" />
                <StatTile
                    label="Missing / tests"
                    value={stats.missing + stats.recommended_tests}
                    tone="neutral"
                />
            </div>

            <Card
                eyebrow="STAGE 1"
                title={
                    selectedHypothesisId
                        ? `Evidence stack — ${
                              hypotheses.find((h) => h.id === selectedHypothesisId)?.label || '?'
                          }`
                        : `${filtered.length} evidence links across all hypotheses`
                }
            >
                <div className="text-xs mb-4" style={{ color: 'var(--fg-2)' }}>
                    Pick a hypothesis at left to filter. Each row is a row in
                    <code className="mx-1">silver.hypothesis_evidence_links</code>
                    with a role (supporting / contradicting / missing / recommended_test), a weight, and an
                    optional source-chunk reference.
                </div>

                <div className="grid grid-cols-[260px_1fr] gap-4">
                    {/* Hypothesis picker */}
                    <div className="space-y-1">
                        <div
                            className="text-[10px] font-mono uppercase tracking-wider mb-2"
                            style={{ color: 'var(--fg-3)' }}
                        >
                            Hypotheses ({hypotheses.length})
                        </div>
                        <button
                            type="button"
                            onClick={() => onSelectHypothesis('')}
                            className="block w-full text-left px-2 py-1.5 rounded text-[11px] transition-colors"
                            style={{
                                background: selectedHypothesisId === '' || selectedHypothesisId === null
                                    ? 'var(--accent-bg)'
                                    : 'transparent',
                                color: 'var(--fg-2)',
                                border:
                                    '1px solid ' +
                                    (selectedHypothesisId === '' || selectedHypothesisId === null
                                        ? 'var(--accent-dim)'
                                        : 'var(--line-1)'),
                            }}
                        >
                            All hypotheses
                        </button>
                        {hypotheses.map((h) => (
                            <button
                                key={h.id}
                                type="button"
                                onClick={() => onSelectHypothesis(h.id)}
                                className="block w-full text-left px-2 py-1.5 rounded text-[11px] transition-colors"
                                style={{
                                    background:
                                        selectedHypothesisId === h.id ? 'var(--accent-bg)' : 'transparent',
                                    color: 'var(--fg-1)',
                                    border:
                                        '1px solid ' +
                                        (selectedHypothesisId === h.id
                                            ? 'var(--accent-dim)'
                                            : 'var(--line-1)'),
                                }}
                            >
                                <div className="flex items-center justify-between mb-1">
                                    <span className="font-mono">{h.label || 'H'}</span>
                                    <span
                                        className="text-[10px] font-mono"
                                        style={{ color: 'var(--fg-3)' }}
                                    >
                                        {h.support_count}+ / {h.contradict_count}-
                                    </span>
                                </div>
                                <div
                                    className="truncate text-[10px]"
                                    style={{ color: 'var(--fg-3)' }}
                                >
                                    {h.title || 'untitled'}
                                </div>
                            </button>
                        ))}
                    </div>

                    {/* Evidence rows */}
                    <div>
                        {filtered.length === 0 ? (
                            <div className="text-xs" style={{ color: 'var(--fg-3)' }}>
                                No evidence links for this hypothesis yet.
                            </div>
                        ) : (
                            <div
                                className="rounded-md border divide-y"
                                style={{ borderColor: 'var(--line-1)' }}
                            >
                                {filtered.slice(0, 40).map((e) => (
                                    <div
                                        key={e.id}
                                        className="grid grid-cols-[110px_1fr_70px] gap-3 items-start px-3 py-2 text-xs"
                                        style={{ borderColor: 'var(--line-1)' }}
                                    >
                                        <Pill tone={ROLE_TONES[e.role] ?? 'neutral'} dot>
                                            {roleLabel(e.role)}
                                        </Pill>
                                        <div>
                                            <div style={{ color: 'var(--fg-1)' }}>
                                                {e.passage_excerpt ||
                                                    (e.source_chunk_id
                                                        ? `Source chunk: ${e.source_chunk_id}`
                                                        : 'No chunk attached')}
                                            </div>
                                            {e.payload?.evaluator && (
                                                <div
                                                    className="text-[10px] font-mono mt-1"
                                                    style={{ color: 'var(--fg-3)' }}
                                                >
                                                    via {String(e.payload.evaluator)}
                                                </div>
                                            )}
                                        </div>
                                        <div
                                            className="text-right font-mono text-[11px]"
                                            style={{
                                                color:
                                                    typeof e.weight === 'number' && e.weight >= 0.7
                                                        ? 'var(--accent)'
                                                        : 'var(--fg-2)',
                                            }}
                                        >
                                            {typeof e.weight === 'number' ? e.weight.toFixed(2) : '—'}
                                        </div>
                                    </div>
                                ))}
                            </div>
                        )}
                    </div>
                </div>
            </Card>
        </>
    );
}

// ── STAGE 2: Reasoning ────────────────────────────────────────────────
function ReasoningStage({
    hypotheses,
    selectedHypothesisId,
    onSelectHypothesis,
}: {
    hypotheses: Hypothesis[];
    selectedHypothesisId: string | null;
    onSelectHypothesis: (id: string) => void;
}) {
    return (
        <Card eyebrow="STAGE 2" title={`${hypotheses.length} hypotheses in play`}>
            <div className="text-xs mb-4" style={{ color: 'var(--fg-2)' }}>
                Each hypothesis carries a parent question, a stance label (A primary / B alternative / C
                null), a written description, and a rationale narrative. Click a row to drill into its
                evidence in stage 1 or its candidates in stage 3.
            </div>

            {hypotheses.length === 0 ? (
                <div className="text-xs" style={{ color: 'var(--fg-3)' }}>
                    No hypotheses recorded yet. Trigger via Chat or the §9.10 hypothesis register.
                </div>
            ) : (
                <div className="space-y-2">
                    {hypotheses.map((h, i) => {
                        const selected = h.id === selectedHypothesisId;
                        return (
                            <button
                                key={h.id}
                                type="button"
                                onClick={() => onSelectHypothesis(h.id)}
                                className="block w-full text-left rounded-md p-3 transition-colors"
                                style={{
                                    background: selected ? 'var(--accent-bg)' : 'var(--bg-1)',
                                    border:
                                        '1px solid ' +
                                        (selected ? 'var(--accent-dim)' : 'var(--line-1)'),
                                }}
                            >
                                <div className="flex items-center gap-3 mb-1">
                                    <span
                                        className="font-mono text-[11px] w-7 h-7 rounded flex items-center justify-center"
                                        style={{
                                            background: 'var(--bg-2)',
                                            color: 'var(--fg-2)',
                                            border: '1px solid var(--line-2)',
                                        }}
                                    >
                                        {h.label || `H${i + 1}`}
                                    </span>
                                    <span
                                        className="text-sm font-medium flex-1"
                                        style={{ color: 'var(--fg-0)' }}
                                    >
                                        {h.title || 'Untitled hypothesis'}
                                    </span>
                                    <Pill tone={confidenceTone(h.confidence)}>
                                        conf {typeof h.confidence === 'number' ? h.confidence.toFixed(2) : '—'}
                                    </Pill>
                                    <Pill
                                        tone={
                                            h.status === 'accepted'
                                                ? 'accent'
                                                : h.status === 'retired'
                                                  ? 'neutral'
                                                  : 'info'
                                        }
                                        dot
                                    >
                                        {h.status}
                                    </Pill>
                                </div>
                                {h.description && (
                                    <div className="text-xs mb-1" style={{ color: 'var(--fg-2)' }}>
                                        {h.description}
                                    </div>
                                )}
                                {h.rationale && (
                                    <div
                                        className="text-[11px] italic"
                                        style={{ color: 'var(--fg-3)' }}
                                    >
                                        Rationale · {h.rationale}
                                    </div>
                                )}
                                <div
                                    className="text-[10px] font-mono uppercase tracking-wider mt-2 flex gap-3"
                                    style={{ color: 'var(--fg-3)' }}
                                >
                                    <span style={{ color: 'var(--accent)' }}>
                                        +{h.support_count} supporting
                                    </span>
                                    <span style={{ color: 'var(--danger, #d65a5a)' }}>
                                        −{h.contradict_count} contradicting
                                    </span>
                                    <span>· {h.missing_count} missing</span>
                                    <span>· {h.tests_count} tests</span>
                                    {h.confidence_method && <span>· {h.confidence_method}</span>}
                                </div>
                            </button>
                        );
                    })}
                </div>
            )}
        </Card>
    );
}

// ── STAGE 3: Candidates ───────────────────────────────────────────────
function CandidatesStage({
    hypotheses,
    evidenceById,
    project,
}: {
    hypotheses: Hypothesis[];
    evidenceById: Map<string, EvidenceLink[]>;
    project: ReasoningProps['project'];
}) {
    const ranked = useMemo(
        () => [...hypotheses].sort((a, b) => (b.confidence ?? 0) - (a.confidence ?? 0)),
        [hypotheses],
    );

    return (
        <Card eyebrow="STAGE 3" title="Ranked candidate hypotheses">
            <div className="text-xs mb-4" style={{ color: 'var(--fg-2)' }}>
                Ranked by confidence. Each card surfaces the evidence stack and a quick-launch into
                Chat / Investigation / Report.
            </div>

            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
                {ranked.length === 0 ? (
                    <div className="text-xs col-span-2" style={{ color: 'var(--fg-3)' }}>
                        No candidates yet.
                    </div>
                ) : (
                    ranked.map((h, i) => {
                        const ev = evidenceById.get(h.id) ?? [];
                        const top = ev
                            .filter((e) => e.role === 'supporting')
                            .slice(0, 3);
                        return (
                            <div
                                key={h.id}
                                className="p-4 rounded-md border"
                                style={{ background: 'var(--bg-1)', borderColor: 'var(--line-1)' }}
                            >
                                <div className="flex items-center gap-2 mb-3">
                                    <span
                                        className="w-7 h-7 rounded text-[12px] font-mono flex items-center justify-center"
                                        style={{
                                            background: 'var(--accent-bg)',
                                            color: 'var(--accent)',
                                        }}
                                    >
                                        #{i + 1}
                                    </span>
                                    <Pill tone={confidenceTone(h.confidence)}>
                                        conf {typeof h.confidence === 'number' ? h.confidence.toFixed(2) : '—'}
                                    </Pill>
                                    <Pill
                                        tone={
                                            h.status === 'accepted'
                                                ? 'accent'
                                                : h.status === 'retired'
                                                  ? 'neutral'
                                                  : 'info'
                                        }
                                        dot
                                    >
                                        {h.status}
                                    </Pill>
                                    <span
                                        className="ml-auto font-mono text-[10px]"
                                        style={{ color: 'var(--fg-3)' }}
                                    >
                                        {h.label}
                                    </span>
                                </div>

                                <div
                                    className="text-sm font-medium mb-1"
                                    style={{ color: 'var(--fg-0)' }}
                                >
                                    {h.title}
                                </div>

                                {h.description && (
                                    <div className="text-xs mb-2" style={{ color: 'var(--fg-2)' }}>
                                        {h.description}
                                    </div>
                                )}

                                <div
                                    className="text-[10px] font-mono uppercase tracking-wider mb-3 flex gap-3 flex-wrap"
                                    style={{ color: 'var(--fg-3)' }}
                                >
                                    <span style={{ color: 'var(--accent)' }}>
                                        +{h.support_count}
                                    </span>
                                    <span style={{ color: 'var(--danger, #d65a5a)' }}>
                                        −{h.contradict_count}
                                    </span>
                                    <span>{h.missing_count} missing</span>
                                    <span>{h.tests_count} tests</span>
                                </div>

                                {top.length > 0 && (
                                    <div
                                        className="space-y-1 mb-3 text-[11px]"
                                        style={{ color: 'var(--fg-2)' }}
                                    >
                                        {top.map((e) => (
                                            <div
                                                key={e.id}
                                                className="flex items-start gap-2 px-2 py-1.5 rounded border"
                                                style={{ borderColor: 'var(--line-1)' }}
                                            >
                                                <span
                                                    className="font-mono mt-0.5"
                                                    style={{
                                                        color:
                                                            typeof e.weight === 'number' && e.weight >= 0.7
                                                                ? 'var(--accent)'
                                                                : 'var(--fg-3)',
                                                    }}
                                                >
                                                    {typeof e.weight === 'number'
                                                        ? e.weight.toFixed(2)
                                                        : '—'}
                                                </span>
                                                <span>
                                                    {e.passage_excerpt ||
                                                        e.source_chunk_id ||
                                                        'no excerpt'}
                                                </span>
                                            </div>
                                        ))}
                                    </div>
                                )}

                                <div className="flex gap-2 flex-wrap">
                                    <Link
                                        href={`/projects/${project.slug}/investigations`}
                                        className="text-[10px] font-mono uppercase tracking-wider px-2 py-1 rounded border"
                                        style={{
                                            color: 'var(--accent)',
                                            background: 'var(--accent-bg)',
                                            borderColor: 'var(--accent-dim)',
                                        }}
                                    >
                                        Pin investigation →
                                    </Link>
                                    <Link
                                        href={`/projects/${project.slug}/chat`}
                                        className="text-[10px] font-mono uppercase tracking-wider px-2 py-1 rounded border"
                                        style={{
                                            color: 'var(--fg-2)',
                                            borderColor: 'var(--line-2)',
                                        }}
                                    >
                                        Discuss in chat →
                                    </Link>
                                </div>
                            </div>
                        );
                    })
                )}
            </div>
        </Card>
    );
}

// ── STAGE 4: Evidence Graph ───────────────────────────────────────────
function GraphStage({
    project,
    stats,
}: {
    project: ReasoningProps['project'];
    stats: ReasoningStats;
}) {
    const nodes = [
        { label: 'Raw imports', value: stats.reports_indexed, sub: 'silver.reports' },
        { label: 'Sources', value: stats.passages_indexed, sub: 'silver.document_passages' },
        { label: 'Drillholes', value: stats.collars_in_project, sub: 'silver.collars' },
        { label: 'Evidence', value: stats.total_evidence, sub: 'hypothesis_evidence_links' },
        { label: 'Hypotheses', value: stats.total_hypotheses, sub: 'silver.hypotheses' },
    ];

    return (
        <Card eyebrow="STAGE 4" title="Evidence graph — raw → source → fact → conclusion">
            <div className="text-xs mb-4" style={{ color: 'var(--fg-2)' }}>
                Left-to-right flow of how raw imports become hypotheses. The full interactive Neo4j
                node-flow is on the dedicated Graph page.
            </div>

            <div className="flex items-stretch gap-2 overflow-x-auto pb-2">
                {nodes.map((n, i) => (
                    <div key={n.label} className="flex items-center gap-2 shrink-0">
                        <div
                            className="p-3 rounded-md border min-w-[140px]"
                            style={{
                                background: 'var(--bg-1)',
                                borderColor: 'var(--line-1)',
                            }}
                        >
                            <div
                                className="text-[10px] font-mono uppercase tracking-wider mb-1"
                                style={{ color: 'var(--fg-3)' }}
                            >
                                {n.label}
                            </div>
                            <div
                                className="text-2xl font-mono"
                                style={{ color: 'var(--fg-0)' }}
                            >
                                {n.value.toLocaleString()}
                            </div>
                            <div
                                className="text-[10px] font-mono mt-1"
                                style={{ color: 'var(--fg-3)' }}
                            >
                                {n.sub}
                            </div>
                        </div>
                        {i < nodes.length - 1 && (
                            <span
                                className="text-2xl"
                                style={{ color: 'var(--fg-4)' }}
                            >
                                →
                            </span>
                        )}
                    </div>
                ))}
            </div>

            <div className="mt-4 flex gap-2">
                <Link
                    href={`/projects/${project.slug}/graph`}
                    className="text-[10px] font-mono uppercase tracking-wider px-3 py-1.5 rounded border"
                    style={{
                        color: 'var(--accent)',
                        background: 'var(--accent-bg)',
                        borderColor: 'var(--accent-dim)',
                    }}
                >
                    Open interactive graph →
                </Link>
                <Link
                    href={`/projects/${project.slug}/sources`}
                    className="text-[10px] font-mono uppercase tracking-wider px-3 py-1.5 rounded border"
                    style={{ color: 'var(--fg-2)', borderColor: 'var(--line-2)' }}
                >
                    Browse sources →
                </Link>
            </div>
        </Card>
    );
}

// ── Helpers ───────────────────────────────────────────────────────────
function StatTile({
    label,
    value,
    tone,
}: {
    label: string;
    value: number;
    tone: 'accent' | 'info' | 'neutral' | 'danger';
}) {
    const color =
        tone === 'accent'
            ? 'var(--accent)'
            : tone === 'danger'
              ? 'var(--danger, #d65a5a)'
              : tone === 'info'
                ? 'var(--info, #6aa7ff)'
                : 'var(--fg-2)';
    return (
        <div
            className="p-3 rounded-md border"
            style={{ background: 'var(--bg-1)', borderColor: 'var(--line-1)' }}
        >
            <div
                className="text-[10px] font-mono uppercase tracking-wider mb-1"
                style={{ color: 'var(--fg-3)' }}
            >
                {label}
            </div>
            <div className="text-2xl font-mono" style={{ color }}>
                {value.toLocaleString()}
            </div>
        </div>
    );
}
