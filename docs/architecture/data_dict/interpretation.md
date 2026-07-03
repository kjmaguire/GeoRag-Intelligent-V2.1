# Schema `interpretation` — Data Dictionary (skeleton)

See [phase0/107-section19-3-interpretation-schema.sql](../../../database/raw/phase0/107-section19-3-interpretation-schema.sql).

Holds geologist annotations layered on top of the silver layer.

## Tables

| Table | Purpose | Status |
|---|---|---|
| `interpretation.interpretation_notes` | Free-text notes anchored to a silver row or geometry | Live |
| `interpretation.interpretation_section_lines` | Geologist-drawn cross-section lines (user-authored; distinct from the pipeline-computed geometry in `gold.cross_section_panels.section_line_geom` — `silver.section_lines` was never created) | Live |
| `interpretation.interpretation_target_zones` | Hand-drawn target polygons (precursor to `targeting.target_*`) | Live |
| `interpretation.interpretation_comments` | Threaded comments on notes / zones | Live |

## Reader

Frontend page [InterpretationWorkspace.tsx](../../../resources/js/Pages/InterpretationWorkspace.tsx).

## RLS

Workspace-fenced via the standard policy ([Ch 11 §4](../manual/11-tenancy-and-rls.md)).
