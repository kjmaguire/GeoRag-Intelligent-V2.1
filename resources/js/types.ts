/**
 * Shared TypeScript types for GeoRAG Intelligence frontend.
 */

// Ambient declaration for Ziggy's `route()` helper. Ziggy is not installed
// in the package.json, but some admin pages reference `route()` for URL
// generation. Declared as `(name, params?) => string` to satisfy tsc; at
// runtime these callsites should either be replaced with explicit URLs or
// Ziggy should be installed (tightenco/ziggy + @routes Blade directive).
declare global {
    function route(name: string, params?: Record<string, unknown> | string | number, absolute?: boolean): string;
}

// ── Inertia shared props ───────────────────────────────────────────────────

/**
 * Authenticated user as exposed by HandleInertiaRequests::share().
 * Always read identity from `usePage().props.auth.user` — not from
 * localStorage. localStorage is untrusted, can drift from the server
 * session, and is an XSS-exfiltration target.
 */
export interface AuthUser {
    id: number | string;
    name: string;
    email: string;
    /** Surfaced by HandleInertiaRequests::share() (doc-phase 142).
     *  AppLayout uses this to render Admin nav links to the four
     *  Track-3 admin surfaces (eval-dashboard, decision-history,
     *  support-cockpit, hypothesis-workspace). */
    is_admin?: boolean;
}

export interface SharedAppInfo {
    env: string;   // 'local' | 'staging' | 'production' | ...
    debug: boolean;
}

/**
 * Active workspace shared via Inertia's HandleInertiaRequests::share().
 * Present on all authenticated pages. data_version is bumped by the
 * ingestion pipeline on every successful project data update and is used
 * as the client-side cache-bust suffix on silver MVT tile URLs (Module 8 §8.5).
 */
export interface SharedWorkspace {
    id: string;
    name: string;
    data_version: number;
}

/**
 * Shape of Inertia's shared page props. Pages augment via generics:
 *   usePage<PageProps<MyPageSpecificProps>>()
 */
export type PageProps<T extends Record<string, unknown> = Record<string, unknown>> = T & {
    auth: { user: AuthUser | null };
    flash: { success: string | null; error: string | null };
    app: SharedAppInfo;
    /** Active workspace; present on all authenticated pages. */
    workspace?: SharedWorkspace;
};

// ── API / Domain types ─────────────────────────────────────────────────────

export interface Project {
    project_id: string;
    project_name: string;
    slug?: string | null;
    operator: string | null;
    commodity: string[];
    crs_epsg: number | null;
    created_at: string;
    updated_at: string;
}

export interface CollarRecord {
    collar_id: string;
    hole_id: string;
    project_id: string;
    easting: number;
    northing: number;
    elevation: number;
    total_depth: number;
    hole_type: string;
    azimuth: number;
    dip: number;
    drill_date: string | null;
    status: string;
    longitude?: number | null;
    latitude?: number | null;
}

export interface LithologyInterval {
    log_id: string;
    collar_id: string;
    hole_id: string;
    from_depth: number;
    to_depth: number;
    lithology_code: string | null;
    lithology_description: string | null;
    grain_size: string | null;
    color: string | null;
    hardness: string | null;
    rqd: number | null;
    recovery: number | null;
    weathering: string | null;
}

export interface Citation {
    citation_id: string;
    citation_type: 'DATA' | 'NI43' | 'PUB' | 'PGEO';
    source_chunk_id: string;
    document_title: string;
    relevance_score: number;
    section_number?: string | null;
    section_title?: string | null;
    section?: string | null;
    page?: number | null;

    // ── Public Geoscience extensions (plan §08) ────────────────────────
    // Present only on PGEO citations; populated by the FastAPI response
    // assembler from the Qdrant payload + PostGIS registry hydration.
    // The Chat UI reads these directly to avoid a second /resolve round-
    // trip on the hover/click path for cheap fields.
    corpus?: 'internal_archive' | 'public_geo' | null;
    jurisdiction_code?: string | null;
    jurisdiction_name?: string | null;
    license_summary?: string | null;
    license_url?: string | null;
    source_url?: string | null;
    staleness_seconds?: number | null;
}

export interface GeoRAGResponse {
    text: string;
    citations: Citation[];
    confidence: number;
    sources_used: string[];
    map_payload?: MapPayload | null;
    viz_payload?: VizPayload | null;
}

// ── Map types ──────────────────────────────────────────────────────────────

export interface MapPayload {
    type: 'FeatureCollection';
    features: GeoJSON.Feature[];
    bbox?: [number, number, number, number];
}

/**
 * Known chart_type values dispatched by `InlineViz.tsx`. §6b P5
 * (2026-05-29) — centralised here as the canonical TS-side enum to
 * pair with the `_KNOWN_CARD_TYPES` frozenset in
 * `src/fastapi/app/agent/sentry_tags.py`. A drift between the two
 * surfaces as `card.type = "unknown"` in the Sentry dashboard.
 *
 * The trailing `(string & {})` lets the wire's free-form
 * `chart_type` string land without a type assertion while still
 * surfacing the known names in editor autocomplete + narrow checks.
 */
export type VizChartType =
    | 'downhole_strip'
    | 'assay_histogram'
    | 'cross_section'
    | 'graph_viz'
    | 'drill_trace_3d'
    // ADR-0007 PR-1 — project_summary + coverage_gap intents
    | 'technique_timeline'
    | 'coverage_table'
    // ADR-0007 PR-2 — stereonet card (mplstereonet server render)
    | 'stereonet'
    | (string & {});

/**
 * Set of chart_type values the React InlineViz dispatcher recognises.
 * Mirror of `_KNOWN_CARD_TYPES` in `src/fastapi/app/agent/sentry_tags.py`.
 * Used by the §6b P3 frontend dispatcher tests to assert every value
 * routes to a card.
 */
export const KNOWN_VIZ_CHART_TYPES = [
    'downhole_strip',
    'assay_histogram',
    'cross_section',
    'graph_viz',
    'drill_trace_3d',
    'technique_timeline',
    'coverage_table',
    'stereonet',
] as const;

/** Type alias for the literal-union element type of `KNOWN_VIZ_CHART_TYPES`. */
export type KnownVizChartType = (typeof KNOWN_VIZ_CHART_TYPES)[number];

/**
 * Shape of `viz_payload.plotly_layout.meta` — the per-card payload
 * the FastAPI dispatcher (`_build_chat_card_payloads`) emits. Every
 * field is optional because each chart_type populates only its own
 * subset; the InlineViz dispatcher switches on `chart_type` and reads
 * only the relevant fields. See `docs/architecture/spatial_chat_card_audit_2026_05_29.md`
 * §6b P1 for the per-card meta contract.
 *
 * Array element types use `unknown[]` so the child components own the
 * final narrowing — keeping InlineViz's responsibility limited to
 * "this card_type should render". The child components define their
 * own row types in their `*Props` interfaces.
 */
export interface VizPayloadMeta {
    // Shared identifiers
    project_id?: string;
    hole_id?: string;
    collar_id?: string;

    // graph_viz — KnowledgeGraph card
    nodes?: unknown[];
    edges?: unknown[];

    // drill_trace_3d — DrillTrace3D card
    collars?: unknown[];
    intervals?: unknown[];
    structures?: unknown[];
    hole_id_filter?: string | null;

    // technique_timeline — TimelineCard
    swimlanes?: unknown[];
    breakdown_table?: unknown[];
    extraction_pending_fields?: string[];

    // coverage_table — CoverageTableCard
    rows?: unknown[];
    ingest_gap?: { indexed: number; processed: number; gap_pct: number } | null;
    findings?: unknown[];

    // stereonet — StereonetCard
    image_base64?: string;
    projection?: string;
    structure_count?: number;
    points?: unknown[];
}

export interface VizPayloadLayout {
    meta?: VizPayloadMeta;
    // Plotly layout fields beyond `meta` are passed through to GeoPlot
    // (e.g. `xaxis`, `yaxis`, `title`); they're free-form per Plotly
    // contract.
    [key: string]: unknown;
}

export interface VizPayload {
    chart_type: VizChartType;
    title?: string;
    plotly_data?: Record<string, unknown>[];
    plotly_layout?: VizPayloadLayout;
}

// ── Chat types ─────────────────────────────────────────────────────────────

/**
 * 5-state lifecycle for assistant messages (Module 7 Phase B §B2).
 *
 * State machine (derived from SSE event ordering):
 *   draft       — first `delta` event received; tokens flowing
 *   generated   — stream ended; awaiting `completed` event (transient ~300ms)
 *   validated   — `completed` received with no refusal_payload
 *   committed   — ~500ms after validated (pure visual; no structural change)
 *   rejected    — `completed` with refusal_payload OR `failed` event
 *
 * Absent value defaults to 'committed' for backward compatibility with
 * messages loaded from localStorage before Module 7 Chunk 3.
 */
export type LifecycleState = 'draft' | 'generated' | 'validated' | 'committed' | 'rejected';

/**
 * Structured refusal payload shape (Module 6 Chunk 4a + Module 7 §B7).
 * Present on `completed` events where the answer was refused, and synthesised
 * from `failed` events for system-level failures.
 */
export type RefusalReasonCode =
  | 'insufficient_evidence'
  | 'guard_numeric_fail'
  | 'guard_entity_fail'
  | 'guard_completeness_fail'
  | 'llm_unavailable'
  | 'budget_exhausted';

export interface NearestCandidate {
  marker: string;
  source_store: string;
  relevance_score: number;
  preview: string;
  evidence_id?: string | null;
}

export interface RefusalPayload {
  type: 'refusal';
  reason_code: RefusalReasonCode;
  searched: {
    stores_queried: string[];
    candidates_considered: number;
    query_class: string;
  };
  missing: {
    what_was_needed: string;
    nearest_candidates: NearestCandidate[];
  };
  message: string;
  failed_guards?: string[];
}

// ── Conflict + Freshness types (Module 7 §B8) ─────────────────────────────

/**
 * A single conflicting-evidence entry (Global Invariant 7 — never auto-pick winner).
 * Parallel arrays: values[i] is supported by evidence_ids[i].
 */
export interface ConflictEntry {
    entity_key: string;
    property_name: string;
    evidence_ids: string[];
    values: string[];
}

/**
 * Freshness metadata snapshotted at query time.
 * Module 7 computes staleness by comparing workspace_data_version_at_query
 * against the current workspace data_version, which is exposed via Inertia's
 * shared props as `usePage<PageProps>().props.workspace.data_version`
 * (wired in Module 8 §8.5 — no separate endpoint needed).
 * Fallback: clock-based age from answered_at.
 */
export interface FreshnessData {
    workspace_data_version_at_query: number;
    project_data_version_at_query?: number | null;
    answered_at: string;  // ISO 8601
}

export interface ChatMessage {
    id: string;
    role: 'user' | 'assistant';
    content: string;
    timestamp: string;
    citations?: Citation[];
    confidence?: number;
    sources_used?: string[];
    mapPayload?: MapPayload | null;
    vizPayload?: VizPayload | null;
    /** Module 7 §B2 — per-message lifecycle state. Defaults to 'committed' when absent. */
    lifecycle_state?: LifecycleState;
    /** Module 7 §B7 — structured refusal payload; present when lifecycle_state === 'rejected'. */
    refusal_payload?: RefusalPayload | null;
    /** Module 7 §B8 — conflicting evidence entries; null/absent = no conflicts detected. */
    conflicting_evidence?: ConflictEntry[] | null;
    /** Module 7 §B8 — freshness metadata snapshotted at query time; null/absent = not available. */
    freshness?: FreshnessData | null;
    /** Module 7 §B6 — answer_run_id from completed SSE event; used for feedback POST. */
    answer_run_id?: string | null;
}

export interface ChatThread {
    id: string;
    title: string;
    createdAt: string | number;
    updatedAt: string | number;
}

// ── Source viewer types ────────────────────────────────────────────────────

export interface SourceData {
    source_type: string;
    title: string | null;
    text: string | null;
    section_title?: string | null;
    section_number?: string | null;
    metadata?: Record<string, unknown>;
    // PGEO envelope passthrough. Non-null on source_type === 'public_geo'
    // (see app/Http/Controllers/Api/V1/CitationController.php publicGeoscienceEnvelope).
    corpus?: 'internal_archive' | 'public_geo' | null;
    canonical_type?: 'mine' | 'mineral_occurrence' | 'drillhole_collar' | 'resource_potential_zone' | null;
    jurisdiction?: {
        code: string | null;
        name: string | null;
        authority: string | null;
    } | null;
    source?: {
        source_id: string | null;
        name: string | null;
        service_url: string | null;
    } | null;
    license?: {
        summary: string | null;
        url: string | null;
    } | null;
    refresh?: {
        last_refreshed_at: string | null;
        staleness_seconds: number | null;
    } | null;
    references_summary?: {
        count: number;
        documents: Array<{
            document_id: string;
            title: string | null;
            filename: string | null;
            filing_date: string | null;
            confidence: number;
            signals: string[];
            established_at: string | null;
            established_by: string | null;
        }>;
    } | null;
    entity?: Record<string, unknown> | null;
    // Inverse (on document citations): which PGEO entities the document touches
    references_to_entities?: {
        total: number;
        by_canonical_type: Record<string, number>;
        entities: Array<{
            canonical_type: string;
            entity_id: string;
            confidence: number;
            signals: string[];
        }>;
    } | null;
}

export interface PgeoSourceChunkIdParts {
    canonical_type: 'mine' | 'mineral_occurrence' | 'drillhole_collar' | 'resource_potential_zone';
    source_id: string;
    feature_id: string | null;
    pg_id: string | null;
}

export interface EntityReferencesResponse {
    canonical_type: string;
    pg_id: string;
    total: number;
    min_confidence: number;
    documents: Array<{
        document_id: string;
        title: string | null;
        filename: string | null;
        filing_date: string | null;
        company: string | null;
        commodity: string | null;
        confidence: number;
        signals: string[];
        extracted_context: string | null;
        established_at: string | null;
        established_by: string | null;
    }>;
}

// ── Export types ────────────────────────────────────────────────────────────

export interface ExportRecord {
    export_id: string;
    project_id: string;
    format: string;
    status: 'pending' | 'processing' | 'completed' | 'failed';
    file_path: string | null;
    file_size: number | null;
    created_at: string;
}
