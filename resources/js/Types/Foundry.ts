/**
 * Foundry — shared TypeScript types for the Wave 0+ redesign.
 *
 * Every Foundry page is bound to one of these prop shapes via Inertia
 * controllers. All shapes are minimal — they reflect the existing georag
 * schema (silver.projects, silver.collars, audit.query_audit_log, etc.)
 * and don't invent fields.
 *
 * See plan: ~/.claude/plans/enumerated-tickling-bachman.md
 */

export type ProjectStatus = 'active' | 'indexing' | 'degraded' | 'archived';

export interface FoundryProject {
    project_id: string;
    project_name: string;
    slug: string;
    region: string | null;
    commodity: string | null;
    status: ProjectStatus;
    crs_epsg: number | null;
    data_version: number;
    workspace_id: string;
    created_at: string;
    updated_at: string;
}

export interface FoundryKpi {
    label: string;
    value: string | number;
    sub?: string;
    tone?: 'neutral' | 'accent' | 'warn' | 'danger';
}

export interface FoundryActivityItem {
    id: string;
    timestamp: string;
    actor: string;
    project: string | null;
    kind: string;
    text: string;
}

export interface FoundryCollar {
    collar_id: string;
    project_id: string;
    hole_id: string;
    hole_id_canonical: string | null;
    total_depth: number | null;
    latitude: number | null;
    longitude: number | null;
    plss_section: string | null;
    state_plane_easting: number | null;
    state_plane_northing: number | null;
    utm_easting: number | null;
    utm_northing: number | null;
    utm_zone: number | null;
    status: string | null;
    completed_at: string | null;
}

export interface FoundryHoleSummary extends FoundryCollar {
    grade_avg: number | null;
    grade_top: number | null;
    grade_unit: string | null;
    rock_summary: string | null;
}

export interface FoundryCitation {
    n: number;
    src: string;
    page?: string;
    chunk_id?: string;
}

export interface FoundryAnaphora {
    pattern: 'spatial' | 'temporal' | 'pronoun' | 'relative' | string;
    original: string;
    resolved: string;
    reason: string;
}

/* ---------- Surface-specific prop shapes ---------- */

export interface PortfolioProps {
    org_name: string;
    /** Phase 3 — Reverb subscription target for useWorkspaceActivity. */
    workspace_id: string;
    projects: FoundryProject[];
    kpis: FoundryKpi[];
    activity: FoundryActivityItem[];
    empty: boolean;
}

export interface ProjectsIndexProps {
    /** Phase 3 — Reverb subscription target for useWorkspaceActivity. */
    workspace_id: string;
    projects: FoundryProject[];
    empty: boolean;
}

export interface RationaleEvidenceItem {
    factor: string;
    detail: string;
    weight: number;
}

export interface RationaleAnalogue {
    name: string;
    similarity: number;
    geometry: string;
    grade: string | null;
    source: string;
}

export interface RationaleConfidencePoint {
    run: number;
    date: string;
    value: number;
    event: string;
}

export interface RationaleAltTarget {
    target_id: string;
    rank: number;
    score: number;
    summary: string;
}

export interface RationaleProps {
    target_id: string;
    project: Pick<FoundryProject, 'project_id' | 'project_name' | 'slug'>;
    rank: number | null;
    coord: string | null;
    confidence: number | null;
    summary: string | null;
    positives: RationaleEvidenceItem[];
    negatives: RationaleEvidenceItem[];
    analogues: RationaleAnalogue[];
    confidence_trajectory: RationaleConfidencePoint[];
    alternates: RationaleAltTarget[];
    citations: FoundryCitation[];
    deposit_model_slug: string | null;
    empty: boolean;
}

export interface CompareLithoSegment {
    from_depth: number;
    to_depth: number;
    kind: string;
    color?: string;
}

export interface CompareHoleDetail extends FoundryHoleSummary {
    azimuth: number | null;
    dip: number | null;
    lithology: CompareLithoSegment[];
    intercepts: Array<{
        from_depth: number;
        to_depth: number;
        grade: number;
        grade_unit: string;
    }>;
}

export interface HoleCompareProps {
    project: Pick<FoundryProject, 'project_id' | 'project_name' | 'slug'>;
    pickable: Array<Pick<FoundryCollar, 'hole_id' | 'hole_id_canonical'>>;
    left: CompareHoleDetail | null;
    right: CompareHoleDetail | null;
    empty: boolean;
}

// Restored 2026-08-17 (reader-core trim reversal) — §5.12 anchored-scroll
// per-hole page. No `qa` field: the FastAPI /v1/viz/qa endpoint that fed it
// no longer exists, see DrillholeDetailController's docblock.
export interface DrillholeDetailProps {
    project: Pick<FoundryProject, 'project_id' | 'project_name' | 'slug'>;
    collar: {
        collar_id: string;
        hole_id: string;
        project_id: string;
        workspace_id?: string;
        elevation_m?: number | null;
        total_depth_m?: number | null;
        azimuth_deg?: number | null;
        dip_deg?: number | null;
        spatial_uncertainty_m?: number | null;
        crs_confidence?: number | null;
        georef_method?: 'declared' | 'detected' | 'assumed' | 'manual' | 'survey' | null;
    };
    intervals: Array<{
        depth_from: number;
        depth_to: number;
        interval_kind: string;
        lithology_code?: string | null;
        lithology_label?: string | null;
        color_hint?: string | null;
        assay_payload?: Record<string, unknown>;
    }>;
    assays: Array<{
        sample_id?: string;
        from_depth?: number;
        to_depth?: number;
        element?: string;
        value?: number;
        value_ppm?: number;
    }>;
    structures: Array<{
        depth: number;
        structure_type: string;
        stereonet_x: number | null;
        stereonet_y: number | null;
        strike_deg?: number | null;
        dip_deg?: number | null;
    }>;
    cross_sections: Array<{ panel_id: string; section_name: string; hole_count: number }>;
    lithology_quality: { exact: number; fuzzy: number; unmapped: number; total: number } | null;
    data_quality_flags?: import('@/Components/DataQualityFlagsBadge').DataQualityFlagsBadgeData | null;
}

/*
 * IngestQuality* types removed 2026-08-18. The /imports/quality page was
 * merged into the reports surface; its per-document row and project rollup
 * now live as ReportListRow / QualityRollup, exported from
 * Pages/Foundry/Reports.tsx alongside the component that consumes them.
 */

export interface TargetsRecommendation {
    target_id: string;
    rank: number;
    status: string;
    coord: string | null;
    score: number;
    confidence: number;
    evidence_count: number;
    summary: string;
    positives: RationaleEvidenceItem[];
    negatives: RationaleEvidenceItem[];
    analogues: RationaleAnalogue[];
    next_data: Array<{
        kind: string;
        detail: string;
        cost_estimate: string | null;
        reduces_uncertainty: number;
    }>;
    constraints: Record<string, string>;
    geochem: Record<string, number>;
}

export interface DepositModelTemplate {
    slug: string;
    display_name: string;
    commodity_primary: string;
    populated: boolean;
    is_active: boolean;
    templates_count: number;
    ontology_terms: number;
}

export interface TargetsProps {
    project: Pick<FoundryProject, 'project_id' | 'project_name' | 'slug'>;
    deposit_models: DepositModelTemplate[];
    active_model_slug: string | null;
    recommendations: TargetsRecommendation[];
    empty: boolean;
}

export interface DecisionCaptureContext {
    kind: 'drill_target' | 'report_approved' | 'threshold_change' | 'source_promoted' | 'hypothesis_accept' | 'query_pin' | 'manual';
    subject: string | null;
    project_id: string | null;
}

export interface WhatChangedEvent {
    id: string;
    timestamp_seconds_ago: number;
    group: 'today' | 'yesterday' | 'this week' | 'older';
    kind: 'evidence_new' | 'ingestion' | 'hypothesis_flip' | 'retrieval_drift' | 'threshold_breach' | 'source_promoted' | 'ontology' | 'decision_logged';
    priority: 'high' | 'med' | 'low';
    title: string;
    detail: string;
    refs: string[];
    impacted: string[];
}

export interface WhatChangedFeedProps {
    project: Pick<FoundryProject, 'project_id' | 'project_name' | 'slug'>;
    events: WhatChangedEvent[];
    empty: boolean;
}

export interface SavedMapView {
    id: string;
    scope: 'user' | 'project' | 'workspace';
    name: string;
    owner: string;
    updated: string;
    basemap: string;
    layers_count: number;
    viewport: string;
}

export interface SavedMapViewsProps {
    project_id: string;
    views: SavedMapView[];
    empty: boolean;
}
