# Chapter 10 — Frontend

> **Reconciled 2026-09-07** against `resources/js/`, `package.json`,
> `routes/web.php`, `routes/channels.php` and `app/Events/`. The previous
> version catalogued roughly eighty pages including a 41-page admin console,
> six dashboards, a Neo4j Source Graph and React Flow. **Sixteen pages
> exist.** `resources/js/Pages/Admin/`, `Pages/Dashboards/`,
> `Pages/Onboarding/` and `Pages/PublicGeoscience/` are not directories in
> this repository, and `reactflow` is not a dependency. What follows is read
> from the tree.

React 19 + Inertia.js v3 + Tailwind v4 + shadcn-style primitives (Radix) +
MapLibre GL + Plotly, streaming over Laravel Reverb (Pusher protocol),
bundled by Vite. React Flow (`@xyflow/react`) was removed on 2026-08-28 with
the graph view; no graph rendering library is installed.

## 1. Repo layout

79 `.tsx` files in total.

| Path | What lives there |
|---|---|
| [`resources/js/Pages/`](../../../resources/js/Pages/) | `Login`, `ForgotPassword`, `ResetPassword`, `Error` |
| [`resources/js/Pages/Foundry/`](../../../resources/js/Pages/Foundry/) | the twelve product pages listed in §2 |
| [`resources/js/Components/`](../../../resources/js/Components/) | shared cards, charts and map wrappers |
| [`resources/js/Components/Foundry/`](../../../resources/js/Components/Foundry/) | workspace-mode views (3D, section, structure), command palette, toasts |
| [`resources/js/Components/Analytics/`](../../../resources/js/Components/Analytics/), `HoleAnalysis/`, `PublicGeoscience/` | feature component groups |
| [`resources/js/Components/ui/`](../../../resources/js/Components/ui/) | the shadcn-style primitive set |
| [`resources/js/Hooks/`](../../../resources/js/Hooks/) | five hooks: `useEvidenceMapPin`, `useFullscreenToggle`, `useTileInvalidation`, `useWorkspaceActivity`, `useWorkspaceDataUpdated` |
| [`resources/js/lib/`](../../../resources/js/lib/) | map, tile, citation, upload and formatting helpers — including `echoChannel.ts`, `mvtLayers.ts`, `tileFailureWatchdog.ts` |
| [`resources/js/Layouts/`](../../../resources/js/Layouts/), `Types/`, `test/` | shell layout, shared TS types, Vitest setup |

There is **no** `Pages/Admin/`, `Pages/Dashboards/`, `Pages/Onboarding/`,
`Pages/PublicGeoscience/`, `Components/Map/`, `Components/Charts/`,
`Components/Citation/`, `Hooks/useWorkspaceData` or `Lib/echo.ts`. Earlier
versions of this chapter linked all of them.

## 2. Pages

Every name below is reachable: `Inertia::render` is called with exactly
these sixteen strings and no others.

| Page | File | What it renders |
|---|---|---|
| Login | `Pages/Login.tsx` | Sanctum SPA login |
| ForgotPassword / ResetPassword | `Pages/ForgotPassword.tsx`, `ResetPassword.tsx` | password reset flow |
| Error | `Pages/Error.tsx` | Inertia v3 error page (not a route) |
| Projects | `Foundry/Projects.tsx` | per-workspace project list |
| NewProject | `Foundry/NewProject.tsx` | project creation form |
| Overview | `Foundry/Overview.tsx` | project home, including the ingest card |
| Workspace | `Foundry/Workspace.tsx` | the main surface — map plus the mode bar (`WorkspaceModeBar`) that switches between SECTION, 3D, STRUCTURE, LOGS and COMPARE views, each its own component under `Components/Foundry/` |
| DrillholeDetail | `Foundry/DrillholeDetail.tsx` | per-hole strip log and inset map |
| Chat | `Foundry/Chat.tsx` | the RAG chat surface |
| Sources | `Foundry/Sources.tsx` | corpus inventory |
| Reports | `Foundry/Reports.tsx` | report list and document body view |
| IngestionRuns | `Foundry/IngestionRuns.tsx` | per-project run list over `silver.ingest_progress` |
| DataImportWizard | `Foundry/DataImportWizard.tsx` | step-by-step upload |
| AttributeTables | `Foundry/AttributeTables.tsx` | tabular view of silver data |
| PublicGeoscience | `Foundry/PublicGeoscience.tsx` | public geoscience overlay browsing |
| RasterLayers | `Foundry/RasterLayers.tsx` | raster layer management |

**Design-only.** The feedback UI, follow-up chips, evidence inspector,
conflict and freshness UX, refusal panels, Lakehouse, row-level drill
review, the targeting and hypothesis surfaces, the audit log, the support
cockpit and every admin console page are described elsewhere in this
manual and in `georag-architecture.html` but have no page here. Treat
them as target state.

## 3. Reverb broadcast channels

Authentication for private channels goes through `routes/channels.php`
(Sanctum-authed). Echo client: [`resources/js/lib/echoChannel.ts`](../../../resources/js/lib/echoChannel.ts).

| Channel | Event | Producer | Consumer (page) |
|---|---|---|---|
| `query.{queryId}` | `QueryStreamEvent` | FastAPI SSE frames (`status`/`bind`/`delta`/`citation`/`completed`/`failed`) re-broadcast by the `StreamQueryFromFastApi` job | Chat — one event class carries every frame type, not three |
| `project.{projectId}.ingestion` | `IngestionProgressBroadcast` | Hatchet ingest workflows → Laravel `/internal` callback | IngestionRuns |
| `workspace.{workspaceId}.activity` | `WorkspaceActivityBroadcast`, `WorkspaceDataUpdated` | Hatchet workflows and Laravel mutations | Overview, Workspace — `useWorkspaceActivity` / `useWorkspaceDataUpdated` invalidate caches and tiles |
| `App.Models.User.{id}` | `User\\UserInboxUpdated` | Laravel | no page consumes it |
| `admin.*` (22 channels) | `Admin\\AdminSurfaceUpdated`, `Admin\\ReportBuildProgress`, `Admin\\IngestionReviewDispositionChanged` | Hatchet workflows and Laravel | **nothing** — `routes/channels.php` still authorises the whole admin family, but no admin page exists to subscribe. The cost-burn and alert broadcasts described in [Ch 12 §7](12-observability.md) land here |

### Reverb dual-purpose env trap

[project_reverb_dual_purpose_env_2026_05_21](../notes/INDEX.md#project_reverb_dual_purpose_env_2026_05_21):
`REVERB_HOST/PORT` serve two purposes — server-side publisher and browser
client. Vite doesn't expand `${VAR}`, so a previous `.env` literal
`${REVERB_HOST_PORT}` ended up in the bundle → 60 s channel-drop timeouts.
Fix: server uses `laravel-reverb:8080`, browser uses literal `8085`.

## 4. Inertia surface

- Server-side rendering: Inertia v3 supports `@inertiajs/vite` SSR mode
  in dev (no separate Node SSR server). See CLAUDE.md inertia-laravel/core
  rules.
- Page components export from `resources/js/Pages/<Name>.tsx` are resolved
  via `Inertia::render('Name', $props)` in Laravel controllers.
- Deferred props: `Inertia::optional()` (replaces v2 `Inertia::lazy()`).
- Events: `httpException`, `networkError` (renamed from `invalid`/`exception`
  in v3).

## 5. Vite build

- Vite config: [vite.config.ts](../../../vite.config.ts).
- Output: `public/build/`.
- Important: after every `vite build`, run `php artisan octane:reload`
  ([feedback_octane_vite_reload](../notes/INDEX.md#feedback_octane_vite_reload)) —
  Swoole workers cache the Vite manifest, so a stale bundle hash 404s
  otherwise.

## 6. Sanctum SPA auth

Stateful domains
([docker-compose.yml:530](../../../docker-compose.yml)):
`localhost,localhost:8888,127.0.0.1,127.0.0.1:8888,host.docker.internal,host.docker.internal:8888`.
Without these, `EnsureFrontendRequestsAreStateful` treats inbound traffic as
token-only and skips `StartSession`, so spaLogin 500s with "Session store
not set on request".

## 7. Workspace context provider

A React context provider mounted near the Inertia root injects the current
`workspace_id` into every API client request header. Without the
`X-Workspace-Id` header, controllers default to the user's primary
workspace; with it, the controller calls
`SET LOCAL app.workspace_id = ?` on the PG connection before the query.

## 8. Chat surface specifics

Read from [`Foundry/Chat.tsx`](../../../resources/js/Pages/Foundry/Chat.tsx)
and [`Components/InlineViz.tsx`](../../../resources/js/Components/InlineViz.tsx).

**The stream.** `POST /api/v1/queries` returns `{ query_id, channel }`; the
page subscribes with `Echo.channel(channel).listen('.QueryStreamEvent')` and
switches on the frame's type — `status`, `routing`, `delta`, `citation`,
`completed`, `failed`. One event class carries all six. A watchdog gives up
after two minutes of silence and tells the reader plainly that the channel
may have dropped and the text above is unchecked — one of the few
uncertainty affordances that actually ships.

**Inline cards.** `_build_chat_card_payloads` in the graph's assemble step
emits an optional `MapPayload` and `VizPayload`; `InlineViz` dispatches on
`chart_type`. The variants that exist:

| `chart_type` | Component | Produced for |
|---|---|---|
| `technique_timeline` | `TimelineCard` | `project_summary` intent |
| `coverage_table` | `CoverageTableCard` (plus a `MapPayload` of collars coloured by whether they have downstream data) | `coverage_gap` intent |
| `downhole_strip` | `StripLogViewer` | a hole in context |
| `assay_histogram`, `cross_section` | `GeoPlot` (Plotly) | assay and section queries |
| `drill_trace_3d` | `DrillTrace3D` (Plotly 3D) | multi-hole geometry |
| `stereonet` | `StereonetCard` | structural measurements |

The five ADR-0007 card names this chapter used to list (`evidence_list`,
`metric_box`, `coverage_gap_chart`, `project_summary_card`,
`spatial_quick_map`) are not what the code emits.

**Citations.** Citation objects arrive on the `citation` frame and again in
bulk on `completed`; `CitationPGEODetail` renders public-geoscience
citations. There is no `Components/Citation/` directory and no
`CitationPill` component — the evidence drawer and pill rendering described
elsewhere in this manual are design-only.

**OIUR.** No Observation/Interpretation/Uncertainty/Recommendation card
rendering exists in `Chat.tsx`. The envelope is produced server-side behind
`GEO_ANSWER_OIUR_ENABLED` and arrives as text.

## 9. Plotly

`react-plotly.js` + `plotly.js-dist-min`, loaded lazily through
`InlineViz`. Users: `GeoPlot.tsx` (scatter, histogram, section),
`DrillTrace3D.tsx` (3D hole paths), `Components/Foundry/Borehole3DView.tsx`
and the other workspace 3D views, `StripLogViewer.tsx`, `StereonetCard.tsx`.
Export contract: [`docs/chart_export_contract_spec.md`](../../chart_export_contract_spec.md).

There is no `Components/Charts/` directory.

## 10. Maps

MapLibre GL 5 (never Mapbox GL — licensing, CLAUDE.md hard rule 8).
`Components/MapView.tsx` plus the `lib/` helpers: `mvtSources.ts`,
`mvtLayers.ts`, `tileUrl.ts`, `basemap.ts`, `layerVisibilityStorage.ts` and
`tileFailureWatchdog.ts`, which surfaces a tile-server failure instead of
leaving the map silently empty. Tiles come from Martin
([Ch 09](09-martin-and-maplibre.md)).

## 11. Browser logs / debugging

`browser-logs` MCP via Laravel Boost — also surfaced in dev.
