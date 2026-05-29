/**
 * GeoRAG Dashboard — TypeScript type contracts.
 *
 * These types are the authoritative frontend contract for all dashboard API
 * endpoints. They match the spec in georag-dashboard-spec.md §14 exactly.
 */

// ── Status & enum types ───────────────────────────────────────────────────

export type ProjectStatus = 'active' | 'indexing' | 'degraded' | 'archived';
export type ActivityKind = 'ingest' | 'query' | 'failure' | 'crs' | 'graph';
export type CitationState = 'resolved' | 'partial' | 'failed';
export type HealthStatus = 'green' | 'warn' | 'fail';
export type DocumentStage = 'bronze' | 'silver' | 'gold' | 'index' | 'failed';
export type CrsStatus = 'resolved' | 'unresolved' | 'failed';

// ── Response envelope ─────────────────────────────────────────────────────

export type DashboardResponse<T> = {
    data: T;
    generated_at: string;
    cache_ttl_seconds: number;
};

// ── Platform readiness (§3.1) ─────────────────────────────────────────────

export type PlatformReadiness = {
    ingestion: { status: HealthStatus; pipeline_version: string; sensor_summary: string };
    agents: { status: HealthStatus; framework_version: string; resolver_version: string };
    schema: { status: HealthStatus; deployed_count: number; total_count: number; pending_count: number };
    inference: { status: HealthStatus; backend: 'vllm' | 'ollama'; healthy_replicas: number; total_replicas: number };
};

// ── Portfolio view types ──────────────────────────────────────────────────

export type PortfolioKpis = {
    projects: { active: number; archived: number };
    documents_indexed: number;
    queries_24h: number;
    citation_resolution_rate: number; // 0..1
    feedback_signal: number; // -1..1
    trends: {
        documents_indexed_7d_delta: number;
        queries_24h_vs_avg_pct: number;
        citation_resolution_wow_delta: number;
    };
};

export type ProjectRosterRow = {
    id: string;
    slug: string;
    name: string;
    status: ProjectStatus;
    region: string;
    doc_count: number;
    queries_7d: number;
    last_activity_at: string | null;
    last_activity_kind: ActivityKind | null;
};

export type QueryActivityPoint = { date: string; count: number };

export type QueryActivity = {
    points: QueryActivityPoint[];
    today_count: number;
    window_avg: number;
};

export type IngestionHealthRow = {
    project_id: string;
    project_name: string;
    bronze: number;
    silver: number;
    gold: number;
    index: number;
    failed: number;
    total: number;
};

export type FeedbackBreakdown = {
    total: number;
    helpful: number;
    citation_issue: number;
    wrong_irrelevant: number;
    positive_rate: number; // 0..1
    top_issue: string | null;
};

export type ActivityFeedItem = {
    id: string;
    occurred_at: string;
    project_id: string;
    project_name: string;
    kind: ActivityKind;
    summary: string;
    detail_ref?: string;
};

// ── Project view types ────────────────────────────────────────────────────

export type ProjectHeader = {
    id: string;
    slug: string;
    name: string;
    commodity: string;
    region: string;
    coordinate_system: string;
    aoi_area_km2: number;
    operator: string;
    last_ingestion_at: string | null;
    status: ProjectStatus;
};

export type ProjectKpis = {
    documents: number;
    kg_entities: number;
    queries_7d: number;
    citation_resolution_rate: number;
    avg_query_latency_ms: number;
    p95_query_latency_ms: number;
    trends: {
        documents_today_delta: number;
        kg_entities_7d_delta: number;
        queries_7d_delta_pct: number;
        citation_resolution_7d_delta_pp: number;
    };
};

export type AOIPayload = {
    display_crs: string;
    source_crs: string;
    aoi: GeoJSON.Feature<GeoJSON.Polygon>;
    features: {
        drill_collars: GeoJSON.FeatureCollection<GeoJSON.Point>;
        mineralized_zones: GeoJSON.FeatureCollection<GeoJSON.Point>;
    };
    bounds: [number, number, number, number];
};

export type KgCounts = {
    node_types: Array<{ type: string; count: number; delta_7d: number }>;
    edge_count: number;
    edge_delta_7d: number;
};

export type RecentQueryItem = {
    id: string;
    query_text: string;
    user_email: string;
    created_at: string;
    citation_state: CitationState;
    citation_count: number;
    unresolved_spans?: number;
    latency_ms: number | null;
};

export type DocumentInventoryRow = {
    id: string;
    filename: string;
    doc_type: string;
    source: string;
    stage: DocumentStage;
    chunk_count: number | null;
    crs_detected: string | null;
    crs_status: CrsStatus;
    crs_failure_reason?: string;
    ingested_at: string;
};
