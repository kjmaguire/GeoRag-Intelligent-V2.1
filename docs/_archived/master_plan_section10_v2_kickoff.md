# Master-plan §10-v2 (Authoring UI + Eval Dashboard) — Kickoff

**Doc-phase:** 179 (kickoff)
**Status:** PROPOSAL — drafted 2026-05-16
**Predecessor:** `docs/master_plan_section10_kickoff.md` (§10-v1 shipped)
**Locked defaults:** all 4 questions answered with recommended options

---

## TL;DR

§10-v1 shipped the harness (110 golden questions, promotion gate,
cross-workspace audit, acceptance script). §10-v2 ships the **two
operator surfaces** that turn that harness into a daily-driver:

1. **Authoring UI** — admin-only page for creating + reviewing
   golden questions (hybrid form + Monaco JSON editor)
2. **Eval Dashboard** — admin-only page for visualising eval-run
   trends + comparing any two runs head-to-head (uses the §10.6
   promotion gate as the "compare" action)

Estimated effort: **8-12 hours** (one autonomous session).

---

## Locked decisions

| Decision                       | Value                                                                              |
|--------------------------------|------------------------------------------------------------------------------------|
| Editor shape                   | **Hybrid** — structured form for scalars; Monaco JSON for jsonb columns           |
| Dashboard primary view         | Pass-rate trend (line) + per-run diff (bars) + regression drill-down               |
| Chart library                  | **Plotly** (already in stack per CLAUDE.md)                                        |
| Compare-runs action            | Reuses `POST /api/v1/admin/eval/assess-promotion` from §10.6                       |
| Reviewer model                 | `reviewed_by_user_id` ≠ `authored_by_user_id` (already in schema)                  |
| Question status flow           | `draft` → admin review → `active`; `active` → admin retire → `retired`             |
| Route prefix                   | `/admin/eval/questions` (authoring) + `/admin/eval/dashboard` (dashboard)          |
| Auth gate                      | Laravel admin middleware (mirrors `/admin/support/*`)                              |

---

## Sub-step detail

### §10-v2.1 — Authoring UI page

**What ships:**
- New Inertia page `resources/js/Pages/Admin/EvalQuestions.tsx`
- Index view: paginated table of questions, filter by set + status,
  search by question_text substring
- Detail/edit view: split panel
  - **Left (form):** question_text, question_set (dropdown from
    CHECK constraint values), expected_intent_class, difficulty,
    expected_refusal toggle, expected_refusal_reason, status
    transition buttons (draft → active, active → retired)
  - **Right (JSON):** Monaco editors for context_setup,
    expected_citations, expected_entities, expected_numeric_values,
    expected_language_compliance — each with schema-aware syntax
    highlighting + format-on-save
- New page action: "Run dry-run" → invokes `evaluate_workspace`
  with `aio_mock_run` against this single question, surfaces the
  result inline (pass/fail + failure_layer + actual_payload)

**Backend:**
- New Laravel controller `app/Http/Controllers/Admin/EvalQuestionsController.php`
  - `index()` — paginated list, Inertia render
  - `show($id)` — single question detail
  - `store(Request)` — create draft
  - `update(Request, $id)` — update draft or active question
  - `transition(Request, $id)` — flip status (draft → active → retired);
    emits `eval.golden_question.{activated|retired}` audit row
- New FastAPI internal endpoint `POST /internal/eval/dry-run-question`
  - Body: `{question_id, evaluator_kind}` (defaults to `aio_mock`)
  - Returns: `{passed, failure_layer, failure_detail, actual_payload}`
  - Service-key gated

**Acceptance:**
- Admin user can create a draft question via UI
- Admin user can transition draft → active; audit row lands
- Dry-run against the active question returns a structured result
- Non-admin user is 403 on every endpoint

### §10-v2.2 — Eval Dashboard page

**What ships:**
- New Inertia page `resources/js/Pages/Admin/EvalDashboard.tsx`
- Top section: **Trend** — Plotly line chart, one line per
  question_set, x=run completed_at, y=pass_rate %. Range filter:
  last 7 / 30 / 90 days. Hover shows run_id + raw pass/total.
- Middle section: **Compare two runs**
  - Two dropdowns (baseline / candidate), default to latest 2 runs
  - Side-by-side Plotly bar chart: per-set pass_rate baseline vs
    candidate, color-coded delta (green if up, red if down >5pp)
  - "Assess promotion" button → calls
    `POST /api/v1/admin/eval/assess-promotion` and renders
    `allow / blocking_sets / regressions` inline
- Bottom section: **Regression drill-down** (visible after a
  compare) — table of every regressed question (was-pass →
  now-fail), with question_text + set + failure_layer + 👁 link
  to its row in the authoring UI

**Backend:**
- New FastAPI endpoint
  `GET /api/v1/admin/eval/runs?from=...&to=...&question_set=...`
  - Returns paginated `{run_id, completed_at, pass_count, fail_count, per_set}`
  - Service-key gated
- New FastAPI endpoint
  `GET /api/v1/admin/eval/runs/{run_id}/per-set-summary`
  - Returns `{question_set: {pass_count, total_count, pass_rate_pct}}`
  - Service-key gated
- Laravel controller `app/Http/Controllers/Admin/EvalDashboardController.php`
  - `index()` — Inertia render with the latest-2-runs preloaded

**Acceptance:**
- Trend chart renders with ≥1 run plotted per set
- Compare picker defaults to the two most-recent runs
- Assess-promotion button surfaces a real allow/block result
- Regression drill-down deep-links into the authoring UI

### §10-v2.3 — Acceptance harness extension

**What ships:**
- Extend `scripts/section10_acceptance.sh` with new checks:
  - `GET /api/v1/admin/eval/runs` 200 + 401 paths
  - `GET /api/v1/admin/eval/runs/{id}/per-set-summary` 200 + 404 paths
  - `POST /internal/eval/dry-run-question` 200 + 401 paths
  - File existence of new Inertia pages + controllers

---

## Out of scope (deferred to §10-v3)

- Live LLM dry-run (currently only `aio_mock` evaluator path)
- Bulk question import from CSV/JSON
- Diff visualisation across MORE than 2 runs (e.g. heatmap across
  last 10 runs) — covered by trend chart for trends but not
  per-question
- Edit-with-side-by-side preview against last-known-good run

---

## Sign-off

If approved as-written:

- [ ] §10-v2 = authoring UI + dashboard above
- [ ] Ship in one autonomous session
- [ ] `scripts/section10_acceptance.sh` extended is the done-test
