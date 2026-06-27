# Overnight autonomous run — 2026-06-26

Continuation of the version-audit sweep on `pr/14-version-audit-updates`.
Kyle's instruction: run autonomously overnight, work the recommended items,
skip anything needing his auth or judgment.

## Scope discipline

**Done autonomously (validatable + reversible).** **Skipped (needs Kyle):**
PR open / `gh auth login`; Qwen3-VL *promote* (unvetted community quant — his
sign-off); PaddleOCR-VL *promote* (golden-corpus shadow is his gate);
Promtail→Alloy *cutover* (the decision, not the tooling); **pr/13's 44+25
in-flight files** (his uncommitted WIP — not mine to land or discard).

## What landed

| Item | Validation | Status |
|---|---|---|
| Promtail→Alloy shadow-diff tool (`scripts/ops/alloy_promtail_shadow_diff.sh`) | standalone bash, no stack mutation | ✅ `0e2766b` |
| Kestra 1.2.18 → 1.3.25 (SHA-pinned) | image boots, JVM healthy, reports `1.3.25`, exit 0 | ✅ `8585626` |
| Node 22 → 24 (CI `NODE_VERSION` + Laravel Dockerfile NodeSource) | frontend builds clean on `node:24` | ✅ `56dd2e1` |
| concurrently 9 → 10 | tsc-neutral + build green + tests unchanged | ✅ `e170a37` |
| react-plotly.js 2 → 4 | tsc-neutral + build green + tests unchanged | ✅ `e170a37` |
| @inertiajs/react 2 → 3 (+ app.tsx v3 resolver fix) | tsc-neutral + build green + tests unchanged | ✅ `e170a37` |
| Reusable in-container FE validator (`scripts/ops/fe_container_validate.sh`) | n/a (tooling) | ✅ `a5e719d` |
| Dependabot triage doc (`docs/handover/DEPENDABOT_TRIAGE.md`) | manifest-derived | ✅ committed |

## Validation results (the npm majors)

Measured baseline-vs-bumped on the **same** node:24 container for clean
attribution:

| Metric | Baseline | After 3 bumps + app.tsx fix |
|---|---|---|
| `tsc --noEmit` errors | 35 | **35** (tsc-neutral) |
| `vite build` | green | **green** |
| vitest | 642 passed / 28 failed | **642 passed / 28 failed** (identical) |

The bumps introduced exactly **one** net-new tsc error (the Inertia-v3 page
resolver in `app.tsx`), now fixed — so the net delta is zero.

## ⚠ Pre-existing finding — frontend `tsc` CI gate is RED (NOT mine to fix)

The validation surfaced that **`npx tsc --noEmit` reports 35 type errors on the
branch independent of any of tonight's changes.** The CI `frontend` job runs
exactly this command (`.github/workflows/ci.yml` line ~216) as a hard gate, and
it has been there since the V2.1 baseline — so that job's TypeScript step does
not currently pass. `vite build` stays green because Vite strips types via
esbuild (no type-check), which is why the app still ships.

Dominant categories (from the tsc output):
- **maplibre-gl type friction** — `PublicGeoscienceMap.tsx`: `Source`/`setData`
  casts, `map` possibly-null, `Point.coordinates` vs `[number, number]` (~9).
- **React-19 JSX namespace** — `DrillReview.tsx`: `Cannot find namespace 'JSX'`
  (×3) — likely a bare `JSX.Element` that needs `React.JSX.Element`.
- **misc prop/type mismatches** — `Reasoning.tsx` (unknown→ReactNode),
  `ReportView.tsx` (`body` vs `detail` prop), `Index.tsx` (`string|null`).

This is real, self-contained tech debt but **out of scope for a version
sweep** and risky to fix blind overnight (type-only edits can mask real bugs,
and the resolver-style changes need runtime eyes). Left untouched; flagged as a
background task for a dedicated cleanup PR. Recommend fixing it so the frontend
CI gate goes green — happy to take it next session.

## Validation method

No local Node on this box, so frontend changes were validated inside a
`node:24` container: a **targeted copy** of just the build inputs (configs +
`resources/`, ~3.6 MB — the full repo tar-copy was 1.7 GB+ of data/model dirs
and far too slow) → `npm ci` → `tsc --noEmit` → `npm run build` (Vite) →
`vitest`, first on the baseline then after `npm install`-ing the three majors.
A persistent `georag_npm_cache` volume keeps subsequent runs fast. This both
proves the bumps build clean **and** doubles as the Node-24 compatibility gate
(same container = same runtime the CI + Laravel image will use).

## Why the npm majors were low-risk (pre-flight findings)

- **@inertiajs/react 2→3:** the Laravel side is already on `inertia-laravel ^3`.
  Grepped for every v3-breaking pattern — `router.cancel(` (→`cancelAll`),
  `Inertia.lazy`/`LazyProp` (→`optional`), `invalid`/`exception` event
  listeners (→`httpException`/`networkError`) — **none present**. App already
  uses standalone `axios` (the dep v3 drops from its bundle). So the adapter
  bump just closes the last 2.x/3.x version-skew, no code rewrite.
- **react-plotly.js 2→4:** `GeoPlot.tsx` (+ DrillTrace3D, Borehole3DView,
  HoleAnalysisPanel) **deliberately bypass** react-plotly.js entirely — they
  call `Plotly.react`/`newPlot` from `plotly.js-dist-min` directly, after a
  documented rolldown CJS-interop crash with `react-plotly.js/factory`. The
  remaining consumers (EvalCompare, ChartsGallery, EvidenceQuality, LlmCost)
  use `lazy(() => import('react-plotly.js'))` + `<Plot data layout style
  useResizeHandler />` with props cast `as any` — that component API is
  unchanged across v2→v4, so minimal TS surface.
- **concurrently 9→10:** dev-only (`composer dev` script runner). Zero runtime
  surface.

## Still pending (carried forward — your call)

- **Open the PR** — needs `gh auth login` (interactive). Branch is pushed.
- **Qwen3-VL-8B promote** — machinery built (`pdf_vl_shadow.py`, dual-write,
  gate); blocked on a vetted servable V3 endpoint + your quant sign-off
  (ADR-0015: official 8B-AWQ doesn't exist). Shadow-trigger wiring intentionally
  left untouched — not meaningfully validatable overnight without the endpoint.
- **PaddleOCR-VL Phase 2 promote** — parser + shadow harness landed; needs the
  golden-20-PDF shadow run → per-doc-class routing decision (ADR-0016).
- **Promtail→Alloy cutover** — tooling now delivered (above); run it after a
  shadow window, then promote per the runbook.
- **Dependabot config nits** — switch pip→uv ecosystem; add docker patch-group
  (see `DEPENDABOT_TRIAGE.md` §2).

---
*Autonomous run, 2026-06-26. Every bump validated before commit; nothing
promoted that needed Kyle's judgment.*
