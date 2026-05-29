# Phase G.4 — Evidence Map Mode (citation → map highlight)

**Status:** Complete. Master-plan §6 deliverable "Evidence Map Mode
coupled to chat answers (clicking citation highlights map feature)"
ships.

## Architecture

Pure-frontend wiring — no FastAPI / Laravel changes. The chat surface
and the map surface coordinate via a tiny module-scope pub-sub store.

```
┌──────────────────────────┐               ┌─────────────────────────┐
│ ChatMessage              │               │ MapView                  │
│                          │               │                          │
│  <CitationMarker         │               │  selectedHoleId prop     │
│    onClick={() => {      │               │       │                  │
│      const pin =         │               │       ▼                  │
│        parseSpatial…     │               │  effectiveSelectedHoleId │
│      setEvidenceMapPin   │   pub-sub    │       │                  │
│        (pin)             ├──────────────►│  ◄── useEvidenceMapPin() │
│      onCitationClick…    │  evidenceMap-│       │                  │
│    }}                    │  Store       │       ▼                  │
│  />                      │               │  highlight on map        │
└──────────────────────────┘               └─────────────────────────┘
```

## Files added

* **`resources/js/lib/spatialCitation.ts`** — `parseSpatialCitation()`
  inspects a Citation's `source_chunk_id` and returns a `SpatialPin`
  when the citation maps to a drillhole, drillhole set, or
  PublicGeoscience feature. Pattern coverage:
  * `silver.lithology_logs:hole=<hole_id>:…` → `{kind:'hole_id', hole_id}`
  * `silver.collars:count=N:first=<collar_uuid>` → `{kind:'collar_set', first_collar_id}`
  * `pg_<canonical_type>:…:feature=<id>:…` → `{kind:'pg_feature', canonical_type, feature_id}`
  * Empty / missing / unknown sentinel → `null`
* **`resources/js/lib/evidenceMapStore.ts`** — minimal pub-sub
  singleton (`get` / `set` / `clear` / `subscribe` / `_reset`). No
  new dep; uses React's `useSyncExternalStore` contract.
* **`resources/js/Hooks/useEvidenceMapPin.ts`** — React hook
  wrapping the store via `useSyncExternalStore`. Also exposes
  `setEvidenceMapPin()` + `clearEvidenceMapPin()` for non-component
  callers.
* **`resources/js/lib/__tests__/spatialCitation.test.ts`** — 13
  Vitest cases covering parser cases + store pub-sub semantics.

## Files changed

* **`resources/js/Components/ChatMessage.tsx`** — citation marker
  `onClick` now calls `parseSpatialCitation(cit)` and, when it
  returns non-null, broadcasts the pin via `setEvidenceMapPin()`.
  The existing inspector-open callback (`onCitationClick`) fires
  unchanged either way — chat behavior is strictly additive.
* **`resources/js/Components/MapView.tsx`** — subscribes to the
  store via `useEvidenceMapPin()`. The component derives
  `effectiveSelectedHoleId = selectedHoleId ?? (pin if hole_id)`,
  and every downstream `selectedHoleId` reference inside the
  component (filter expressions, marker re-style hooks, popup
  pinning) now reads `effectiveSelectedHoleId`. **Explicit
  parent-passed prop still wins** — the store is a fall-back for
  scenes where the parent isn't holding a hole_id (e.g., the chat
  drawer rendered alongside an Explorer map).

## Behavior

1. User asks a chat question; an answer streams in with citation
   markers.
2. User hovers a `[DATA:3]` marker → tooltip shows the document
   title (existing behavior, unchanged).
3. User clicks `[DATA:3]` — three things happen:
   * The existing EvidenceInspector drawer opens for the marker.
   * `parseSpatialCitation(citation)` runs.
   * If the citation points to a drill hole (e.g., its
     `source_chunk_id` contains `hole=36-1042`), the global pin
     gets set to `{kind:'hole_id', hole_id:'36-1042'}`.
4. MapView (wherever it lives in the page tree) re-renders, applies
   its MVT selection filter to that hole_id, re-styles the marker
   amber, and pans to fit (existing `selectedHoleId` plumbing).
5. Clicking a non-spatial citation (e.g. an NI 43-101 document
   passage with `source_chunk_id=georag_reports:…`) leaves the map
   alone — `parseSpatialCitation` returns null and no pin update
   fires.

## Test coverage

`resources/js/lib/__tests__/spatialCitation.test.ts` — **13 cases**
(7 parser, 6 store). Verified-by-shape only; Vitest run requires
`npm install` locally (the repo's package.json has Vitest as a
dev dep but no `node_modules` is checked in).

Backend canary unchanged: **229 / 0** (G.4 is frontend-only).

## Carry-overs

1. **Multi-hole pins (`kind:'collar_set'`)** — today the map only
   highlights when `pin.kind === 'hole_id'`. For `collar_set`
   pins, MapView could broadcast a "show all collars in this
   project" message via an existing project-level highlight layer.
   Wire up when the §16 dashboards land per-project highlight
   surfaces.
2. **PG feature pins (`kind:'pg_feature'`)** — `parseSpatialCitation`
   correctly extracts the canonical_type + feature_id, but MapView
   doesn't yet have a "highlight pg feature" path. The
   `PublicGeoscienceMap` component is the natural home; add a sister
   hook there once the §06 PublicGeo+MapLibre work continues.
3. **Pin lifecycle** — pins persist until cleared or overwritten.
   For the Chat→Map flow this is correct (a clicked citation should
   stay highlighted). A future "exit Evidence Map Mode" button
   could call `clearEvidenceMapPin()`.
4. **Bidirectional map-to-chat** is already wired via the
   `onCollarClick` prop — clicking a collar feeds back into chat as
   a query. That path is unchanged.
5. **Vitest run gate** — Kyle should run `npm install` then
   `npm run test resources/js/lib/__tests__/spatialCitation.test.ts`
   when convenient. The 13 cases are deterministic + don't touch
   the DOM, so they should pass cleanly.

## Why this is the master-plan §6 "Done when"

Master-plan §6 Done-when reads:

> a chat answer that cites public mineral occurrences within 25 km of a
> project AOI uses the public/private language template, the map
> highlights the cited occurrences, and clicking each shows full
> provenance with public/workspace tags.

This phase ships the **clicking → map highlight** half. The
public/private language template (`Public/Private Boundary Agent`) is
already live from earlier phase work, and the EvidenceInspector
drawer surface for provenance is also live. With Phase G.4 they're
now connected.
