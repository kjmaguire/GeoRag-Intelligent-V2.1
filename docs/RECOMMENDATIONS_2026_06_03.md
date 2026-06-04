# Long-term architectural recommendations (2026-06-03)

After two `audit-and-fix.md` remediation passes in 30 days (2026-06-02,
2026-06-03) finding the same shape of bugs, Kyle asked for the
architectural fixes that would make whole bug classes impossible
rather than patching instances. This document captures the
recommendations and what shipped against each.

The five recommendations were:

1. **Type-safe workspace contract** — `workspace_id` required at boundaries
2. **RLS scoping at connection layer** — kill the dual GUC pattern
3. **Audit invariants as CI gates** — convert audits to PR-time checks
4. **Settings lifecycle registry** — `PROPOSED→SHADOW→LIVE→DEAD` machinery
5. **Testcontainers Postgres** — eliminate host-vs-container test gap

Each is "roughly a week of focused work." Kyle authorised the full set
on 2026-06-03; this session shipped FOUNDATIONS for all five + reference
migrations to prove the patterns. The remaining migration work is
itemised under "What's left" at the bottom.

---

## REC#3 — Audit invariants as CI gates (SHIPPED COMPLETE)

**Bug class closed:** "Audit caught a bug after merge, then we wrote a
regression test, then the same bug class came back in a new file 3
weeks later because no one knew the test existed."

**Delivered:**
- `.github/workflows/audit-invariants.yml` — runs ALL the existing
  file-content regression tests on every PR. PHP + Python jobs run in
  parallel, both under 60s wall-clock.
- `docs/AUDIT_INVARIANTS.md` — human-readable index. Every guard has a
  one-line "what it catches" + link to its source. New guards land
  with a row update.
- `.pre-commit-config.yaml` — new `audit-invariants-php` +
  `audit-invariants-python` hooks fire on file changes that touch the
  monitored paths, so the catch happens at commit time too.
- `scripts/check_audit_invariants_{php,python}.sh` — wrapper scripts
  the pre-commit hooks call. List of invariant files lives in the
  script + the CI workflow + the docs index (three sources, deliberate
  duplication so any drift surfaces immediately).
- A "pre-commit config integrity" job that verifies every script the
  config references actually exists on disk — catches the "deleted a
  script but forgot the hook entry" drift class.

**Verification:** 38 PHP + 38 Python tests run via the exact CI
invocation. Adding a new audit guard is now a 4-step process
(write test, add to wrapper script, add to CI workflow, add to docs
index) instead of a "hope someone runs it next audit" process.

---

## REC#1 — Type-required workspace contract (FOUNDATION SHIPPED)

**Bug class targeted:** "Code silently fell back to the default tenant
because someone forgot to thread workspace_id through."

**Delivered:**
- `app/agent/workspace_dependency.py` — typed FastAPI Depends factories:
  - `require_workspace_context` → raises `HTTPException(401)` if no
    workspace_id on auth context. Detail names the route so ops can
    attribute 401s to specific endpoints.
  - `optional_workspace_context` → returns `WorkspaceContext | None`,
    making absence a TYPE-SYSTEM signal (callers must branch on it
    explicitly).
  - `RequiredWorkspace` / `OptionalWorkspace` Depends aliases for
    readable router signatures.
- `app/hatchet_workflows/_workspace_input.py` — `bootstrap_workspace_id(
  reason=...)` factory for the small set of legitimate default-tenant
  callers (Dagster jobs, CLI repair). Reason must be on a
  `frozenset` allowlist — adding a new reason requires a source change
  + code review.
- **Removed `default=LEGACY_DEFAULT_TENANT_UUID` from two workflow
  input models** (`embed_pending_passages`, `enrich_passage_context`).
  Pydantic now raises `ValidationError` if a dispatcher forgets the
  workspace_id. The two existing callers already pass it explicitly,
  so no breakage.
- **Migrated `routers/visualizations.py` as the reference site** — uses
  `OptionalWorkspace` Depends (chart gallery genuinely serves
  anonymous traffic, so this is the right pick) instead of the
  hand-rolled `hasattr(request.state, ...)` dance.
- `tests/test_workspace_dependency.py` — 13 tests pinning the contract.

**What's left to migrate:** 6 other routers still use the
`hasattr(request.state, "workspace_id")` pattern. Each one is a
2-line edit to swap in `RequiredWorkspace` (or `OptionalWorkspace`)
following the visualizations.py reference. Files identified in the B4
sweep: `shadow_trigger.py`, `queries.py`, `interpretation.py`,
`target_recommendation_cockpit.py`, `citation_feedback.py`,
`ocr_render.py`.

---

## REC#2 — RLS at connection layer (FOUNDATION SHIPPED)

**Bug class targeted:** "20+ files each hand-roll `set_config('
app.workspace_id', ...)` with subtle variations (wrong GUC name,
wrong scope flag, missing transaction, f-string injection). Each
audit catches another bad variant."

**Delivered:**
- `app/db/scoped_pool.py` + `app/db/__init__.py` — canonical
  `scoped_connection` async context manager. Validates the UUID
  shape before interpolation (Theme G injection class), opens a
  transaction (SET LOCAL needs one + PgBouncer transaction-pool mode
  requires it), parameter-binds the GUC via `set_config(..., true)`,
  yields the connection. Refuses missing/empty/non-UUID workspace_id
  with `BareConnectionError`.
- **Migrated `services/tool_gateway/impls.py` as the reference site** —
  3 lines of bespoke GUC plumbing replaced with one `async with`.
- `tests/test_scoped_connection.py` — 6 tests pinning the contract,
  including a **migration-baseline test** that lists every file
  currently using bespoke `set_config('app.workspace_id', ...)`. The
  list is allowed to SHRINK over time but cannot GROW — a new
  bespoke site is a hard CI failure.

**Migration baseline:** 56 files currently in
`LEGACY_AWAITING_MIGRATION`. Each one is a 5-line edit. The test
will fail the day a 57th appears, which is the architectural property
we want.

**What's left:** Phase-2 sweep migrating the 56 files. Plus a
Postgres migration that drops the `georag.workspace_id` GUC name from
RLS policies (currently fail-open) once no production code sets it.
Three audit memories track variants of this same bug class — once
both Phase-2 actions land, the bug class becomes impossible.

---

## REC#4 — Settings lifecycle registry (SCAFFOLD SHIPPED)

**Bug class targeted:** "Setting was added, never wired, lived in
config.py for months until an audit re-derived its status from
scratch."

**Delivered:**
- `app/settings_lifecycle.py` — `SettingLifecycle` enum
  (`PROPOSED/SHADOW/LIVE/DEPRECATED/DEAD`) + `SettingEntry` dataclass
  + `REGISTRY` seed with the 10 settings touched by the 2026-06-03
  audits.
- `entries_by_stage()` helper + `get_entry()` lookup so the future
  CI gate (and ad-hoc ops queries) have a stable API.

**Why scaffold-only:** Categorising the remaining 163 settings in
`config.py` is a multi-day effort — each one needs a reading of its
consumers + a judgement call. Doing it half-baked in this session
would ship a registry that's less trustworthy than the unannotated
status quo. The pattern is established; the bulk migration is its own
task.

**What's left:**
1. Categorise the remaining 163 settings (incremental — entries can
   land in chunks as files are touched for other work).
2. CI check: "every setting on `Settings` class must have a registry
   entry" — fails the build on a new unregistered setting.
3. Time-based graduation check: `PROPOSED` settings must move to
   `SHADOW` within 30 days; `SHADOW` to `LIVE` within 90 days of
   metric data being clean. Catches settings that get parked
   indefinitely.

---

## REC#5 — Testcontainers Postgres (SCAFFOLD SHIPPED)

**Bug class targeted:** "Half the PHPUnit suite + 15+ pytest files
fail when run outside Docker because they need a live PG. So all
our regression tests are file-content checks; behavior bugs only
catch in prod."

**Delivered:**
- `tests/conftest_pg.py` — fixture skeleton with full implementation
  intent. Uses the same image (`postgis/postgis:18-3.6`) as the
  existing CI workflow for bit-exact parity. Not yet registered with
  pytest (filename intentionally non-magic) — activation is documented
  in the module docstring.

**Why scaffold-only:** The full delivery needs:
1. Add `testcontainers[postgres]>=4.0` to `src/fastapi/pyproject.toml`
2. Decide scope policy (session vs module vs transactional-per-test —
   recommendation is session + `transactional_pg_conn` sub-fixture
   wrapping each test in a `SAVEPOINT`)
3. Migration runner integration (apply Laravel migrations against the
   testcontainer at session start — likely subprocess wrapper around
   `php artisan migrate --force`)
4. PHP-side equivalent (`testcontainers-php` or docker-compose
   fixture). This is a parallel work item to the Python side.

Each piece is straightforward but together they're "a week of focused
work" the audit-and-fix loop kept punting. The scaffold captures the
architectural decisions so the next session doesn't re-derive them.

---

## What shipped this session vs. what's left

| Item | Status | Tests added |
|------|--------|-------------|
| REC#3 | Complete | 0 (uses existing tests) |
| REC#1 foundation | Shipped + 1 reference migration | 13 |
| REC#1 full migration | 6 routers left (each 2-line edit) | — |
| REC#2 foundation | Shipped + 1 reference migration | 6 |
| REC#2 full migration | 56 files in baseline (each 5-line edit) | — |
| REC#2 Phase-3 | RLS policy migration to drop legacy GUC | — |
| REC#4 scaffold | 10 of 173 settings categorised | 0 |
| REC#4 full categorisation | 163 settings | — |
| REC#4 CI gate | Pending bulk categorisation | — |
| REC#5 scaffold | Fixture skeleton + activation guide | 0 |
| REC#5 activation | Pyproject add + migration integration | — |

**Net architectural delta this session:**
- ✅ Audit guards are CI-enforced (REC#3)
- ✅ Workspace_id is now type-required at any new boundary site (REC#1)
- ✅ `set_config('app.workspace_id', ...)` is monotonically decreasing
  (REC#2 baseline test)
- 🔬 Scaffolds in place for REC#4 + REC#5 with explicit activation paths

Three of the five bug classes can no longer GROW. Phase-2 migrations
on REC#1 and REC#2 will shrink the existing surface to zero.
