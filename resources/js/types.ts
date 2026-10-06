/**
 * Shared TypeScript types for GeoRAG Intelligence frontend.
 */

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
     *  Track-3 admin surfaces (decision-history, support-cockpit,
     *  hypothesis-workspace). */
    is_admin?: boolean;
}

export interface SharedAppInfo {
    env: string; // 'local' | 'staging' | 'production' | ...
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
    // Optional evidence-type discriminator used by the chat CitationMarker to
    // pick an icon; populated by the FastAPI assembler on some citations.
    evidence_type?: string;
    // Optional evidence id (ev:<uuid>) for evidence-packet citations.
    evidence_id?: string;

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

// ── Map types ──────────────────────────────────────────────────────────────

export interface MapPayload {
    type: 'FeatureCollection';
    features: GeoJSON.Feature[];
    bbox?: [number, number, number, number];
}

/**
 * Known chart_type values dispatched by `InlineViz.tsx`. §6b P5
 * (2026-05-29) — centralised here as the canonical TS-side enum. It
 * used to be paired with a `_KNOWN_CARD_TYPES` frozenset in
 * `src/fastapi/app/agent/sentry_tags.py`, which existed to tag the
 * card type on Sentry events; Sentry was removed from the stack on
 * 2026-08-28 and that module with it, so this list is now the single
 * definition rather than one half of a mirror.
 *
 * The trailing `(string & {})` lets the wire's free-form
 * `chart_type` string land without a type assertion while still
 * surfacing the known names in editor autocomplete + narrow checks.
 */
export type VizChartType =
    | 'downhole_strip'
    | 'assay_histogram'
    | 'cross_section'
    | 'drill_trace_3d'
    // ADR-0007 PR-1 — project_summary + coverage_gap intents
    | 'technique_timeline'
    | 'coverage_table'
    // ADR-0007 PR-2 — stereonet card (mplstereonet server render)
    | 'stereonet'
    | (string & {});

/**
 * Set of chart_type values the React InlineViz dispatcher recognises.
 * Used by the §6b P3 frontend dispatcher tests to assert every value
 * routes to a card. Formerly mirrored a Python frozenset; see the note
 * on `VizChartType` above.
 */
export const KNOWN_VIZ_CHART_TYPES = [
    'downhole_strip',
    'assay_histogram',
    'cross_section',
    'drill_trace_3d',
    'technique_timeline',
    'coverage_table',
    'stereonet',
] as const;

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
