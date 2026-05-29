import { useCallback, useEffect, useMemo } from 'react';
import {
    ReactFlow,
    useNodesState,
    useEdgesState,
    Background,
    Controls,
    MiniMap,
    type Node,
    type Edge,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';

export interface GraphNodeData {
    label: string | React.ReactNode;
    entityType?: string;
    color?: string;
    [key: string]: unknown;
}

export type GraphNode = Node<GraphNodeData>;
export type GraphEdge = Edge;

interface KnowledgeGraphProps {
    graphNodes?: GraphNode[];
    graphEdges?: GraphEdge[];
}

const TYPE_COLORS: Record<string, string> = {
    Project: '#f59e0b',
    DrillHole: '#22c55e',
    Formation: '#3b82f6',
    Report: '#a855f7',
    QualifiedPerson: '#ec4899',
    Deposit: '#ef4444',
    MineralOccurrence: '#14b8a6',
    Query: '#f59e0b',
};

function applyRadialLayout(nodes: Node<GraphNodeData>[]): Node<GraphNodeData>[] {
    if (nodes.length === 0) return nodes;

    const centerNode = nodes.find((n) => n.id === 'center');
    const others = nodes.filter((n) => n.id !== 'center');

    const cx = 300;
    const cy = 200;
    const radius = 180;

    if (centerNode) {
        centerNode.position = { x: cx, y: cy };
    }

    others.forEach((node, i) => {
        const angle = (2 * Math.PI * i) / Math.max(others.length, 1) - Math.PI / 2;
        node.position = {
            x: cx + radius * Math.cos(angle),
            y: cy + radius * Math.sin(angle),
        };
    });

    return nodes;
}

function getNodeStyle(data: GraphNodeData | undefined): React.CSSProperties {
    const color = data?.color || TYPE_COLORS[data?.entityType || ''] || '#6b7280';
    return {
        background: '#111827',
        color: '#f3f4f6',
        border: `2px solid ${color}`,
        borderRadius: '8px',
        padding: '8px 12px',
        fontSize: '11px',
        fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
        minWidth: '80px',
        textAlign: 'center' as const,
        boxShadow: `0 0 8px ${color}33`,
    };
}

const defaultEdgeOptions = {
    style: { stroke: '#374151', strokeWidth: 1.5 },
    labelStyle: { fill: '#9ca3af', fontSize: 9, fontFamily: 'ui-monospace, monospace' },
    labelBgStyle: { fill: '#111827', fillOpacity: 0.9 },
    labelBgPadding: [4, 2] as [number, number],
    labelBgBorderRadius: 3,
};

export default function KnowledgeGraph({ graphNodes = [], graphEdges = [] }: KnowledgeGraphProps) {
    const styledNodes = useMemo(() => {
        const laid = applyRadialLayout(
            graphNodes.map((n) => ({
                ...n,
                style: getNodeStyle(n.data),
                data: {
                    ...n.data,
                    label: (
                        <div>
                            <div style={{ fontWeight: 700, fontSize: '12px', marginBottom: '2px' }}>
                                {n.data?.label || '?'}
                            </div>
                            <div style={{ fontSize: '9px', color: n.data?.color || '#9ca3af', opacity: 0.8 }}>
                                {n.data?.entityType || ''}
                            </div>
                        </div>
                    ),
                },
            }))
        );
        return laid;
    }, [graphNodes]);

    const styledEdges = useMemo(
        () =>
            graphEdges.map((e) => ({
                ...e,
                ...defaultEdgeOptions,
            })),
        [graphEdges],
    );

    const [nodes, setNodes, onNodesChange] = useNodesState(styledNodes);
    const [edges, setEdges, onEdgesChange] = useEdgesState(styledEdges);

    useEffect(() => {
        setNodes(styledNodes);
        setEdges(styledEdges);
    }, [styledNodes, styledEdges, setNodes, setEdges]);

    return (
        <div className="w-full h-full bg-gray-950">
            <ReactFlow
                nodes={nodes}
                edges={edges}
                onNodesChange={onNodesChange}
                onEdgesChange={onEdgesChange}
                fitView
                fitViewOptions={{ padding: 0.3 }}
                proOptions={{ hideAttribution: true }}
                minZoom={0.3}
                maxZoom={2}
                className="bg-gray-950"
            >
                <Background color="#1f2937" gap={20} size={1} />
                <Controls
                    showInteractive={false}
                    className="!bg-gray-900 !border-gray-700 !shadow-lg [&_button]:!bg-gray-800 [&_button]:!border-gray-700 [&_button]:!text-gray-400 [&_button:hover]:!bg-gray-700"
                />
                <MiniMap
                    nodeColor={(n) => (n.style as Record<string, string>)?.borderColor || '#6b7280'}
                    maskColor="rgba(0,0,0,0.7)"
                    className="!bg-gray-900 !border-gray-700"
                />
            </ReactFlow>
        </div>
    );
}
