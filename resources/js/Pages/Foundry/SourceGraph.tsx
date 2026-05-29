import { useMemo } from 'react';
import { Head, Link, router } from '@inertiajs/react';
import {
    ReactFlow,
    Background,
    Controls,
    MiniMap,
    Handle,
    Position,
    type Node,
    type Edge,
    type NodeProps,
    type NodeTypes,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';

import AppLayout from '@/Layouts/AppLayout';
import { PageHeader, EmptyState, Pill } from '@/Components/Foundry/primitives';
import { useWorkspaceDataUpdated } from '@/Hooks/useWorkspaceDataUpdated';

type Kind =
    | 'section'
    | 'parser'
    | 'report'
    | 'report-summary'
    | 'collars'
    | 'hypothesis'
    | 'evidence';

interface GraphNodeData extends Record<string, unknown> {
    label: string;
    meta: string;
    kind: Kind;
    support_count?: number;
    contradict_count?: number;
    report_id?: string;
    hypothesis_id?: string;
    slug: string;
}

interface GraphNodeRaw {
    id: string;
    col: number;
    kind: Kind;
    label: string;
    meta: string;
    support_count?: number;
    contradict_count?: number;
    report_id?: string;
    hypothesis_id?: string;
}

interface GraphEdgeRaw {
    id: string;
    source: string;
    target: string;
}

interface Stats {
    sections: number;
    parsers: number;
    reports: number;
    reports_featured: number;
    collars: number;
    hypotheses: number;
    evidence_links: number;
}

interface Props {
    project: { project_id: string; project_name: string; slug: string };
    nodes: GraphNodeRaw[];
    edges: GraphEdgeRaw[];
    stats: Stats;
    empty: boolean;
}

const COL_X: Record<number, number> = {
    0: 0,
    1: 280,
    2: 560,
    3: 920,
    4: 1240,
};
const NODE_W = 240;
const NODE_H = 80;
const ROW_GAP = 22;

const KIND_COLOR: Record<Kind, { bg: string; border: string; accent: string }> = {
    section: { bg: 'oklch(0.2 0.02 240)', border: 'oklch(0.4 0.04 240)', accent: '#7da7ff' },
    parser: { bg: 'oklch(0.2 0.02 280)', border: 'oklch(0.4 0.04 280)', accent: '#b07dff' },
    report: { bg: 'oklch(0.2 0.04 80)', border: 'oklch(0.45 0.08 80)', accent: '#d6a14a' },
    'report-summary': { bg: 'oklch(0.18 0.02 80)', border: 'oklch(0.35 0.04 80)', accent: '#d6a14a' },
    collars: { bg: 'oklch(0.2 0.04 200)', border: 'oklch(0.45 0.08 200)', accent: '#5ec7c2' },
    hypothesis: { bg: 'oklch(0.22 0.06 160)', border: 'oklch(0.48 0.1 160)', accent: '#56d6a0' },
    evidence: { bg: 'oklch(0.2 0.04 30)', border: 'oklch(0.45 0.1 30)', accent: '#e07a5a' },
};

const COL_LABEL: Record<number, string> = {
    0: 'Bronze · sections',
    1: 'Parsers',
    2: 'Documents',
    3: 'Hypotheses',
    4: 'Evidence',
};

function FoundryNode({ data }: NodeProps<Node<GraphNodeData>>) {
    const c = KIND_COLOR[data.kind];
    const isReport = data.kind === 'report' && data.report_id;
    const isHyp = data.kind === 'hypothesis' && data.hypothesis_id;
    const inner = (
        <div
            className="rounded-md border h-full px-3 py-2 flex flex-col gap-1 justify-center"
            style={{ background: c.bg, borderColor: c.border, color: '#e8e8ea' }}
        >
            <div
                className="text-[10px] font-mono uppercase tracking-wider"
                style={{ color: c.accent }}
            >
                {data.kind === 'report-summary' ? 'reports' : data.kind}
            </div>
            <div className="text-[13px] font-medium leading-tight line-clamp-2">{data.label}</div>
            <div className="text-[10px] font-mono opacity-70 truncate">{data.meta}</div>
            {data.kind === 'hypothesis' && (
                <div className="text-[10px] font-mono mt-0.5 flex gap-2">
                    <span style={{ color: '#56d6a0' }}>+{data.support_count ?? 0}</span>
                    <span style={{ color: '#e07a5a' }}>−{data.contradict_count ?? 0}</span>
                </div>
            )}
        </div>
    );

    return (
        <>
            <Handle type="target" position={Position.Left} style={{ background: c.accent, width: 6, height: 6 }} />
            {isReport ? (
                <Link
                    href={`/projects/${data.slug}/reports/${data.report_id}`}
                    style={{ width: NODE_W, height: NODE_H, display: 'block' }}
                >
                    {inner}
                </Link>
            ) : isHyp ? (
                <Link
                    href={`/projects/${data.slug}/reasoning`}
                    style={{ width: NODE_W, height: NODE_H, display: 'block' }}
                >
                    {inner}
                </Link>
            ) : (
                <div style={{ width: NODE_W, height: NODE_H }}>{inner}</div>
            )}
            <Handle type="source" position={Position.Right} style={{ background: c.accent, width: 6, height: 6 }} />
        </>
    );
}

const nodeTypes: NodeTypes = { foundry: FoundryNode };

export default function FoundrySourceGraph({ project, nodes, edges, stats, empty }: Props) {
    // Phase 5 real-time push — ingest_pdf + sync_silver_to_kg change the
    // graph node/edge counts. Filter on `reports` (covers ingest writes).
    useWorkspaceDataUpdated(project.project_id, (event) => {
        if (event.affected_types.includes('reports')) {
            router.reload({ only: ['nodes', 'edges', 'stats', 'empty'] });
        }
    });

    const { flowNodes, flowEdges } = useMemo(() => {
        // Bucket nodes by column to compute vertical layout
        const byCol: Record<number, GraphNodeRaw[]> = { 0: [], 1: [], 2: [], 3: [], 4: [] };
        for (const n of nodes) {
            if (byCol[n.col] === undefined) byCol[n.col] = [];
            byCol[n.col].push(n);
        }

        const fn: Node<GraphNodeData>[] = [];
        for (const col of Object.keys(byCol).map(Number).sort((a, b) => a - b)) {
            const list = byCol[col];
            const totalHeight = list.length * (NODE_H + ROW_GAP) - ROW_GAP;
            const startY = -totalHeight / 2;
            list.forEach((n, i) => {
                fn.push({
                    id: n.id,
                    type: 'foundry',
                    position: { x: COL_X[col] ?? col * 280, y: startY + i * (NODE_H + ROW_GAP) },
                    data: {
                        label: n.label,
                        meta: n.meta,
                        kind: n.kind,
                        support_count: n.support_count,
                        contradict_count: n.contradict_count,
                        report_id: n.report_id,
                        hypothesis_id: n.hypothesis_id,
                        slug: project.slug,
                    },
                    draggable: true,
                });
            });
        }

        const fe: Edge[] = edges.map((e) => ({
            id: e.id,
            source: e.source,
            target: e.target,
            type: 'smoothstep',
            animated: false,
            style: { stroke: 'oklch(0.45 0.02 240)', strokeWidth: 1 },
        }));

        return { flowNodes: fn, flowEdges: fe };
    }, [nodes, edges, project.slug]);

    return (
        <AppLayout>
            <Head title={`Evidence graph · ${project.project_name}`} />

            <div
                className="flex-1 flex flex-col overflow-hidden"
                style={{ background: 'var(--bg-0)', color: 'var(--fg-1)' }}
            >
                <PageHeader
                    eyebrow={`PROJECT · ${project.project_name.toUpperCase()} · EVIDENCE GRAPH`}
                    title="Bronze → Parsers → Documents → Hypotheses → Evidence"
                    sub={`${stats.sections} sections · ${stats.parsers} parsers · ${stats.reports.toLocaleString()} reports · ${stats.collars.toLocaleString()} collars · ${stats.hypotheses} hypotheses · ${stats.evidence_links} evidence links`}
                    actions={
                        <div className="flex gap-2">
                            <Link
                                href={`/projects/${project.slug}/reasoning`}
                                className="text-xs font-mono uppercase tracking-wider px-3 py-1.5 rounded border"
                                style={{ color: 'var(--fg-2)', borderColor: 'var(--line-2)' }}
                            >
                                ← Back to reasoning
                            </Link>
                            <Link
                                href={`/projects/${project.slug}/sources`}
                                className="text-xs font-mono uppercase tracking-wider px-3 py-1.5 rounded border"
                                style={{
                                    color: 'var(--accent)',
                                    background: 'var(--accent-bg)',
                                    borderColor: 'var(--accent-dim)',
                                }}
                            >
                                Data inventory →
                            </Link>
                        </div>
                    }
                />

                {/* Column legend */}
                <div
                    className="px-8 py-2 flex items-center gap-4 border-b text-[10px] font-mono uppercase tracking-wider"
                    style={{ background: 'var(--bg-1)', borderColor: 'var(--line-1)', color: 'var(--fg-3)' }}
                >
                    {[0, 1, 2, 3, 4].map((col) => (
                        <div key={col} className="flex items-center gap-2">
                            <span
                                className="w-2 h-2 rounded-full"
                                style={{ background: KIND_COLOR[colKind(col)].accent }}
                            />
                            <span>{COL_LABEL[col]}</span>
                        </div>
                    ))}
                    <Pill tone="info">drag to rearrange</Pill>
                    <Pill tone="neutral">scroll to zoom</Pill>
                </div>

                {empty ? (
                    <div className="px-8 py-12">
                        <EmptyState
                            title="No evidence graph yet."
                            detail="Once Bronze ingest produces silver rows for this project, the directed flow of provenance will render here. Connect a source or trigger §C/D ingest."
                        />
                    </div>
                ) : (
                    <div className="flex-1 relative" style={{ background: 'var(--bg-0)' }}>
                        <ReactFlow
                            nodes={flowNodes}
                            edges={flowEdges}
                            nodeTypes={nodeTypes}
                            fitView
                            fitViewOptions={{ padding: 0.15 }}
                            minZoom={0.2}
                            maxZoom={2}
                            proOptions={{ hideAttribution: true }}
                            colorMode="dark"
                        >
                            <Background gap={32} size={1} color="oklch(0.25 0.02 240)" />
                            <Controls position="bottom-right" />
                            <MiniMap
                                position="bottom-left"
                                pannable
                                zoomable
                                nodeColor={(n) => {
                                    const data = n.data as GraphNodeData | undefined;
                                    return data ? KIND_COLOR[data.kind].accent : '#888';
                                }}
                                style={{ background: 'var(--bg-1)', border: '1px solid var(--line-1)' }}
                            />
                        </ReactFlow>
                    </div>
                )}
            </div>
        </AppLayout>
    );
}

function colKind(col: number): Kind {
    switch (col) {
        case 0:
            return 'section';
        case 1:
            return 'parser';
        case 2:
            return 'report';
        case 3:
            return 'hypothesis';
        case 4:
            return 'evidence';
        default:
            return 'section';
    }
}
