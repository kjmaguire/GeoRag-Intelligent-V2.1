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

export interface ProjectsIndexProps {
    /** Phase 3 — Reverb subscription target for useWorkspaceActivity. */
    workspace_id: string;
    projects: FoundryProject[];
    empty: boolean;
}
