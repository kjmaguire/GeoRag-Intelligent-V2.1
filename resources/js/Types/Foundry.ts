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

export interface IngestQualityFileRow {
    file_id: string;
    name: string;
    format: 'LAS' | 'CSV' | 'PDF' | 'TIFF' | 'CAMECO_LOG' | 'AGS' | 'KMZ' | 'XLSX' | 'OTHER';
    size_bytes: number | null;
    rows: number | null;
    accepted: number | null;
    flagged: number | null;
    rejected: number | null;
    status: 'ok' | 'warn' | 'error' | 'awaiting_ocr' | 'regex_incomplete';
    crs_detected: string | null;
    crs_confidence: number | null;
    duration_seconds: number | null;
}

export interface IngestQualityAnomaly {
    row: number | null;
    column: string | null;
    value: string | null;
    rule: string;
    action: 'flag' | 'reject' | 'review';
}

export interface IngestQualityProps {
    import_id: string;
    project: Pick<FoundryProject, 'project_id' | 'project_name' | 'slug'>;
    files: IngestQualityFileRow[];
    anomalies: IngestQualityAnomaly[];
    totals: { accepted: number; flagged: number; rejected: number; awaiting_ocr: number };
    pass_gate: boolean;
    empty: boolean;
}

export interface Tier3LayerOption {
    layer_id: string;
    label: string;
    jurisdictions: string[];
    license: string;
    row_count_estimate: string;
}

export interface Tier3UnlockProps {
    workspace_id: string;
    layers: Tier3LayerOption[];
    request_status: 'none' | 'pending' | 'approved' | 'denied';
    can_approve: boolean;
    empty: boolean;
}

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

export interface SupportWorkspace {
    id: string;
    name: string;
    region: string;
    users: number;
    plan: string;
    eval_overall: number | null;
    status: 'ok' | 'watch' | 'incident';
}

export interface SupportTrace {
    run_id: string;
    workspace_id: string;
    user: string;
    when: string;
    question: string;
    status: 'ok' | 'refused' | 'warn';
    latency_ms: number;
    citations: number;
    confidence: number;
}

export interface SupportThreshold {
    id: string;
    label: string;
    value: number;
    min_value: number;
    max_value: number;
    unit: string;
}

export interface SupportCockpitProps {
    workspaces: SupportWorkspace[];
    traces: SupportTrace[];
    thresholds: SupportThreshold[];
    can_admin: boolean;
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
