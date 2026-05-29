# CC-01 + CC-03 + CC-04 — Side-Chat Follow-ups Closeout 2026-05-24

Continuation of `cc01_cc03_cc04_handoff_2026_05_23.md`. Captures the side-chat
deliveries + this morning's punch-list of small fixes that fell out of the
overnight task chips.

---

## Side-chat deliveries (already landed before this session opened)

These were spawned as chips on 2026-05-23 and shipped autonomously overnight.
Listed for visibility — no further work needed unless follow-ups below say so.

| Chip | Status | Files |
|------|--------|-------|
| CC-01 Item 1 — Drill upload + SRQ + unit-ambiguity + overlap | ✅ Shipped | `DrillUploadController.php`, `DrillReviewController.php`, `silver_review_queue` migration, `csv_sample.py`, `csv_lithology.py` |
| CC-03 Item 3 — Geochronology | ✅ Shipped | `2026_05_24_010000_create_silver_geochronology_samples.php`, `2026_05_24_010100_extend_data_sub_type_geochronology.php` |
| CC-03 Item 4 — QField | ✅ Shipped | `2026_05_24_020000_extend_data_sub_type_qfield_field_observation.php`, `silver_spatial.py` + `spatial_parser.py` updates |
| MapView uncertainty rings | ✅ Shipped | `resources/js/Components/MapView.tsx` |
| Completeness + coverage UI | (partial — coverage controller landed; full UI deferred) | `CoverageDensityController.php` |

---

## This-session punch list (all done)

| # | Item | Status |
|---|------|--------|
| 1 | Apply `silver.review_queue` migration (2026_05_24_120000) | ✅ |
| 2 | Pint sweep — all dirty PHP | ✅ pass |
| 3 | Reconcile `decided_by_user_id` UUID/BIGINT mismatch in `models/review_queue.py` (4 fields fixed: `assigned_to_user_id`, `decided_by_user_id`, `actor_user_id`, `ReviewLineage.decided_by_user_id`) | ✅ |
| 4 | DrillReview nav link added to `FoundryShell.tsx` PROJECT_NAV (label "Review", icon shield, sibling of Data) | ✅ |
| 5 | Port `uncertainty-rings` layer into `WorkspaceMap.tsx` + extend `MapCollar` type + `WorkspaceController` `selectRaw` + prop projection | ✅ |
| 6 | Apply `2026_05_24_backfill_georef_method.sql` — 302 collars → `detected/0.7`, 265 → `assumed/0.3` | ✅ |
| 7 | `npm run build` (1m 25s) | ✅ |
| 8 | `php artisan octane:reload` | ✅ |
| 9 | FastAPI restart for Pydantic type change | ✅ healthy |
| 10 | Pytest: `test_assessment_summarizer.py` + `test_domain_classifier.py` → 24/24 | ✅ |
| 11 | Dagster pytest: `test_unit_ambiguity.py` + `test_interval_overlap.py` → 21/21 | ✅ |
| 12 | Vitest: `MapView.uncertaintyRings.test.tsx` → 12/12 | ✅ |
| 13 | Laravel feature tests (`CollarController|DrillUpload|DrillReview`) → 11/12 (1 pre-existing parity gap) | ⚠️ flagged |
| 14 | Endpoint smoke (`/assessment_summary`, `/completeness_audit/.../latest`, `/coverage/density`, `/maps/ingest`) | ✅ 4/4 green |

---

## Items I deliberately did NOT do (with rationale)

### CC-03 Item 6 (IOgas) — still parked

Per the side-chat note: "without the fixture the work is genuinely parked".
No work happens until Kyle supplies a real IOgas export. Csv fallback
(`csv_sample.py`) handles non-IOgas formats today with manual column mapping
so users aren't blocked. Memory entry already captures the unblock criteria.

### CC-03 Item 8 (Hibernation) — pricing decision

Per side-chat: "this is a pricing/packaging decision before it's a code
feature." No schema change, no controller. The chip from 2026-05-23
remains the right artefact.

### Extend MVT functions with uncertainty triple

Kyle flagged this himself as a spawn-task candidate. The legacy GeoJSON
path is now complete (collars → WorkspaceMap → ring). The MVT path
(`pg_collars_by_project` + `spatial_features` MVT function) still doesn't
emit `spatial_uncertainty_m / crs_confidence / georef_method` in the tile
payload, so the default project view at high zoom (where MVT takes over)
won't show rings. Spawned as a chip below.

### Backfill `spatial_uncertainty_m`

Distinct from the `georef_method` backfill that just ran. The conservative
detected/0.7 assignment is in place, but the actual uncertainty *radius*
needs SME input on the typical NI 43-101 georeferencing error budget
(usually 50-250 m for legacy reports, but it varies by jurisdiction +
era). Spawned as a chip below — needs Kyle.

### `silver.alterations` test-DB parity gap

`CollarControllerTest::test_show` fails on `silver_test` because
`silver.alterations` was never provisioned for the test DB. Per the
memory note `test_db_parity_gap`, the convention is a
`*_provision_alterations_for_test_db.php` sibling migration. Pre-existing,
unrelated to CC-* work; flagged below as a chip.

---

## Open questions for Kyle (parked from side chats — answer when convenient)

1. **CC-03 Item 6 (IOgas)** — sample export fixture needed before parser
   work can resume.
2. **CC-03 Item 8 (Hibernation)** — 4-question product call (per the chip).
3. **CC-03 Item 3 (Geochronology) dedup behaviour** — current uniqueness is
   `(workspace_id, sample_id, isotopic_system)`. Should the asset reject
   rows whose `(lab, publication_ref)` pair already exists for a *different*
   `age_ma`? (Catches duplicate-citation errors.) Reasonable default:
   no — keep both rows, flag as `outlier_flags`-style review item.
4. **CC-03 Item 3 (Geochronology) sub-type slots** — currently 211-217
   under domain 2 (Geology). If you'd rather they sit under a future
   "Geochronology" sibling domain (id=5), the IDs would move. Cheaper to
   leave under Geology unless you expect a third-party UI to filter by
   "show me everything that's geochronology" at the domain level.
5. **CC-04 auto-classifier rollout** — service shipped + tested but not yet
   wired into the Dagster bronze asset. Confirm "auto on every new ingest"
   or "manual QA batch first".
6. **Item 6 review_status filter scope** — implemented only on
   `DxfExporter` as the reference. Other exporters (Csv*, Shapefile,
   GeoPackage, Csa, Las) silently ignore the filter (silver-only = safe
   default). Extend to all or leave reference-only?

---

## Operational state at handoff

- All 26 migrations in `2026_05_2*` series applied to live `georag` DB.
- FastAPI: healthy, all 4 CC-routers loaded (`assessment_summary`,
  `coverage`, `completeness_audit`, `maps`). Pydantic `review_queue` types
  reconciled with DB BIGINT.
- Octane reloaded; Vite manifest current (1m 25s build, includes
  WorkspaceMap + MapView changes).
- Backfill applied — 567 collar rows now carry `georef_method`.
- Test summary: 24 pytest + 21 dagster + 12 vitest + 11/12 Laravel
  feature = **68/69 pass** (1 pre-existing parity gap).
