# Master-plan §8 (Target Recommendation Engine) — Scope Proposal

**Doc-phase 84** — fourth scope proposal in the sequence after §5/§6/§7.

---

## What §8 ships

"AI ranks targets. Evidence explains why. Uncertainty is visible.
Geologist signs off. GeoRAG learns from outcomes." (§18.1 verbatim.)

**Never say "drill here."** The system always frames recommendations
as "highest-ranked untested target zone based on current evidence" —
enforced in the Recommendation Explainer Agent.

Master-plan Phase 8 deliverables (verbatim):
1. All ten deposit model templates (§20.2) loaded with attributes
2. Athabasca uranium model fully populated (commodity-specific
   factors, host rocks, alteration, structure, geochemistry,
   geophysics, analogues)
3. Target Recommendation Graph (§18.2) end-to-end — 11 nodes
4. Phase 8 weighted scoring with explicit per-factor weights
5. Per-target SHAP-equivalent breakdown via score factor table
6. Target sign-off ceremony (R5) with QP credential verification (§19.6)
7. Target Pack map layer (target heatmap, ranked target zones)
8. Target Recommendation Report template

**Done test:** an Athabasca uranium project AOI generates ranked
target candidates, each with per-factor explanation + uncertainty +
sign-off flow; the resulting Target Recommendation Report passes the
export compliance checklist.

---

## Why §8 reuses §7 substrate

The Target Recommendation Report (§7 deliverable #6, target_recommendation
template, R5) is already templated and structured in `templates.py`
(doc-phase 82). §7's:
- Export Compliance Agent (§7.8) gates the target report
- Hash chain proof JSON builder (§7.7) provides regulatory traceability
- Report Builder Graph (§7.1) handles the report rendering

§8's primary scope is therefore the **Target Recommendation Graph**
(§18.2) — a SEPARATE LangGraph from the Report Builder Graph — plus
its 11 agents (§18.4) and the **`targeting.*` schema** (§18.6).

The two graphs interlock: §8's Target Recommendation Graph produces
the `targeting.target_recommendations` rows; §7's Report Builder
Graph then renders the §15.2 `target_recommendation` template that
consumes those rows.

---

## Sub-step breakdown estimate

| # | What | Backend | Frontend | Ticks |
|---|---|---|---|---|
| 8.1 | `targeting.*` schema migrations (10 tables) | medium | none | 2 |
| 8.2 | Deposit model loader — 10 templates, JSON or Python module | small | none | 1 |
| 8.3 | Athabasca uranium template fully populated (commodity factors, analogues) | medium (SME data) | none | 2-3 |
| 8.4 | Target Recommendation Graph state model + 11 node stubs | medium | none | 2 |
| 8.5 | 11 target agents skeletons (Deposit Model, Evidence Layer, Candidate Gen, Scoring, Uncertainty, Constraint, Explainer, Sign-Off, Backtesting, Field Outcome, Scenario Planning) | medium | none | 2 |
| 8.6 | `score_targets` Hatchet workflow + fan-out per zone | small | none | 1 |
| 8.7 | Weighted scoring formula module — per-factor weights from `target_models` | medium | none | 2 |
| 8.8 | SHAP-equivalent score factor table writer | small | none | 1 |
| 8.9 | Sign-off ceremony — R5 + QP credential verification + audit ledger | medium | small | 2 |
| 8.10 | Target Pack map layer config (target_heatmap, ranked_target_zones) | small | medium | 1-2 |
| 8.11 | Activepieces target workflows (public refresh, missing data, field planning, etc.) | medium | none | 2-3 |
| 8.12 | Recommendation Explainer Agent — language template enforcement | small (LLM prompt) | none | 1 |
| 8.13 | Acceptance: Athabasca uranium AOI → ranked candidates → signed-off targets | mixed | mixed | 2 |

**Total: 22-30 ticks.** Comparable to §7 in size. Substantial SME
input needed for §8.3 (Athabasca uranium factor weights, alteration
assemblages, geophysical signatures).

Frontend skew: ~10-15% frontend (sign-off UI, Target Pack map layer
config). §8 is overwhelmingly backend.

---

## V1.49 / current baseline overlap

What exists today:
- **PostgreSQL + PostGIS** — handles `targeting.target_candidate_zones`
  polygons natively.
- **Hatchet workflows** — `score_targets` follows the same pattern as
  `generate_report` (doc-phase 83).
- **Audit ledger** — R5 sign-off ceremony writes hash-chained entries
  through `app.audit.emit_audit` (Phase 0 step 4).
- **`@georag_agent` decorator** — supports R5 risk tier per
  `wrapper.py:195-198`. 11 target agents follow the same skeleton
  pattern as §5/§7 agents.
- **§7 Export Compliance Agent** — gates target reports before delivery.

What needs to be built fresh:
- **`targeting.*` schema** — 10 tables, none exist in v1.49 baseline.
- **Target Recommendation Graph** — separate LangGraph from §7;
  langgraph extra still pending image rebuild.
- **10 deposit models** + Athabasca uranium SME-populated content.
- **Weighted scoring formula module** — pure Python computation.
- **SHAP-equivalent score factor table** — every score must include
  per-factor breakdown for the "no black-box targeting" rule.
- **QP credential verification** — §29.6.1 says this is "staffed-ops
  work for v1." Means the agent gates; humans verify.

---

## Risks

1. **§8.3 Athabasca uranium SME data.** The factor weights, alteration
   assemblages, geophysical signatures, and known-analogue list need
   to come from Kyle (SME). Auto-population from public sources is
   risky for an R5 deliverable. Tabling for Kyle.
2. **"Never say drill here" enforcement** — Recommendation Explainer
   Agent has a language-template rule analogous to §2.9 public/private
   posture. Both are regulatory anchors; both need test coverage.
3. **SHAP for weighted scoring is not actually SHAP** — the per-factor
   contribution is a deterministic decomposition of the weighted sum.
   Master plan uses "SHAP-equivalent" deliberately. Real SHAP arrives
   in Phase 12 with XGBoost. Don't mislabel.
4. **Constraints engine scope creep.** `apply_constraints` node could
   become a swamp (claim boundaries + regulatory + access + customer
   exclusions). Suggest v1 = explicit `excluded_areas` polygon table
   only; richer constraints in §8.x v2.
5. **Activepieces dependency** for 6 target workflows (§18.5) — same
   gate as §7.11.

---

## Dependencies

- **`langgraph`** — same image-rebuild blocker as §7.
- **`shap`** — NOT needed for Phase 8 (deterministic decomposition).
  Will be needed in Phase 12 when XGBoost lands.
- **`scikit-learn` / `xgboost`** — Phase 12 only. Skip for §8.
- **PostGIS** — already running. Spatial constraints + buffered
  intersections in `generate_candidate_zones` node are native ops.

---

## Open questions for Kyle

1. **§8.3 Athabasca uranium SME data**: who owns this — Kyle, an
   external geologist contractor, or a public-source synthesis? Auto-
   generated factor weights are not appropriate for R5.
2. **Constraints v1 scope**: exclusion polygons only, or also
   regulatory restrictions (e.g., First Nations consultation areas,
   protected lands)? Suggest polygons only for v1.
3. **§8 vs §7-B (dashboards) parallelism**: §8 backend can ship while
   §7-B dashboards are pending Kyle review. Sign-off on this approach.
4. **QP credential verification mechanism**: SAML federation,
   manually-recorded license number, or third-party verification API?
   §29.6.1 doesn't specify. v1 = manual recording with audit ledger
   entry; v2 = automated verification.

---

## Recommendation

§8 work for the autonomous run:
- **§8.1** schema migrations (10 tables) — same pattern as §6.5 (RLS
  + workspace_id + project_id FKs).
- **§8.2** deposit-model loader skeleton with 10 template stubs (no SME
  content yet; populate when Kyle provides Athabasca uranium data).
- **§8.4** Target Recommendation Graph state model + node stubs.
- **§8.5** 11 target-agent skeletons.
- **§8.6** `score_targets` Hatchet workflow skeleton.

That gets §8 to roughly the same level §7 reached in doc-phases
77-83 — backbone scaffolded, behavior pending.

**§8.3, §8.7, §8.9 wait for Kyle:**
- §8.3 needs Athabasca uranium SME data.
- §8.7 weighted scoring formula needs the weights from §8.3.
- §8.9 sign-off ceremony has product-feel decisions (credential
  verification mechanism).

---

## TL;DR

§8 = Target Recommendation Engine (Athabasca uranium first); 22-30
ticks; ~85% backend. Reuses §7 Export Compliance Agent + Report Builder
Graph for the Target Recommendation Report output. Autonomous-safe
slice: schema migrations + graph state + 11 agent skeletons + Hatchet
workflow + deposit-model loader. SME-dependent slice
(Athabasca content + scoring weights + sign-off mechanism) waits
for Kyle.

Autonomous run next ticks: doc-phase 85 = §8.1 `targeting.*` schema
migrations. Doc-phase 86 = §8.4 Target Recommendation Graph state
model + node stubs. Doc-phase 87 = §8.5 11 agent skeletons.
