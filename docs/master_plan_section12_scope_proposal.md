# Master-plan §12 (XGBoost + source trust + advanced learning) — Scope Proposal

**Doc-phase 96** — final scope proposal. With this the entire
master plan (§3-§12) is scope-proposed.

---

## What §12 ships

"Advanced ML capabilities once enough field outcomes have
accumulated."

Master-plan Phase 12 deliverables (verbatim):
1. XGBoost target scoring trained on accumulated `target_outcomes`
2. SHAP explanations on every XGBoost score (mandatory)
3. A/B comparison between weighted and XGBoost scoring; both stay
   in production
4. Source trust scoring (§21.5) trained
5. Source trust feeding into retrieval ranking
6. LLM Incident Diagnosis Graph fully wired
7. Continuous learning loop running on schedule
8. Storage Tiering Agent (§10.8) live
9. Lineage Reporter Agent (§10.9) live

**Done test:** target rankings include both weighted and XGBoost
scores with SHAP-explained per-target factor contributions; source
trust scores influence retrieval ranking; field feedback loop
demonstrably improves model performance over a quarter of operation.

---

## §12 has a hard prerequisite

The whole phase depends on **`targeting.target_outcomes` having
enough labeled rows to train on**. Per master plan §8's "geological
learning loop" (§20.8), this fills as:
- Targets recommended (§8 ships)
- Geologist signs off
- Drilling happens (external)
- Assays imported
- `target_outcomes` rows written

Realistic threshold for first useful XGBoost training: **50-100
labeled outcomes per deposit model**. For Athabasca uranium alone,
that's months to years of field-feedback accumulation.

So §12 is a "wait until field outcomes accumulate" phase. The
scaffolding (model training workflow, SHAP integration, A/B harness)
can land NOW; the training itself waits for data.

---

## Sub-step breakdown estimate

| # | What | Backend | Frontend | SME | Ticks |
|---|---|---|---|---|---|
| 12.1 | `xgboost` + `shap` deps + image rebuild | small | none | none | 1 |
| 12.2 | `targeting.target_models.scoring_kind` extend to support 'xgboost' (already in §8.1 enum) | none (done) | none | none | 0 |
| 12.3 | `train_target_model` Hatchet workflow (reads target_outcomes, writes new target_model_version) | medium | none | small | 2-3 |
| 12.4 | XGBoost inference path: alternate code branch in `target_scoring` agent | medium | none | none | 1-2 |
| 12.5 | SHAP-per-target explanation writer (replaces deterministic decomposition for xgboost rows in `target_score_factors`) | small | none | none | 1 |
| 12.6 | A/B comparison ranking + UI (existing `scoring_kind = ensemble`) | small | small | none | 1-2 |
| 12.7 | Source trust scoring training (Phase 12 §21.5) | medium | none | none | 2-3 |
| 12.8 | Source trust feeding retrieval ranking (fusion layer extension) | medium | none | none | 1-2 |
| 12.9 | LLM Incident Diagnosis Graph wiring | medium | none | none | 2 |
| 12.10 | Continuous learning loop (cron `train_target_model` + `train_source_trust`) | small | none | none | 1 |
| 12.11 | Storage Tiering Agent live (was skeleton from earlier phase) | medium | none | none | 1-2 |
| 12.12 | Lineage Reporter Agent live | medium | none | none | 1-2 |
| 12.13 | Acceptance: weighted + XGBoost A/B + SHAP on both scoring kinds + source trust impacts retrieval | mixed | mixed | small | 2 |

**Total: 16-22 ticks** (excluding the multi-month wait for outcome
data to accumulate).

Frontend skew: ~10% frontend (A/B comparison UI is the only piece;
everything else is backend ML). Almost the most backend-heavy phase
in the plan.

---

## V1.49 / current baseline overlap

What exists:
- **`targeting.target_outcomes`** table (§8.1, doc-phase 85) — the
  training data source. Currently empty (no field outcomes yet).
- **`targeting.target_model_versions.scoring_kind`** already supports
  'xgboost' + 'ensemble' (per §8.1 CHECK enum).
- **`targeting.target_score_factors`** writes per-factor breakdown —
  SHAP rows fit the same schema (factor_name = SHAP-derived feature
  name).
- **Hatchet workflow pattern** — `train_target_model` follows
  established mold.
- **Phase 0 LLM Incident Diagnosis stub** likely exists (per Phase 0
  AI agent set in `worker.py` POOLS).

What's net-new:
- `xgboost` + `shap` Python deps (~50 MB combined; image rebuild).
- Source trust training infrastructure.
- Retrieval ranking extension to consume source trust scores.
- Storage Tiering + Lineage Reporter agents (were probably stubbed
  earlier; need live behavior here).

---

## Risks

1. **No data, no model.** Without enough `target_outcomes` rows,
   XGBoost training fails or produces a useless model. §12 should
   ship with a synthetic-outcomes mode (generated from analogue
   deposits with declared synthetic flag) to allow validation
   without waiting on field feedback.
2. **A/B between weighted + XGBoost.** Master plan says both stay
   in production. Need a clear UX for showing both scores side-by-
   side without confusing geologists. UX work waits for Kyle.
3. **Source trust circularity.** Source trust scores feed retrieval;
   retrieval feeds claim ledger; claim ledger feeds new source
   evaluations. Risk of feedback loops where popular sources get
   trusted more. Mitigation: trust scores use sliding-window
   freshness + decay.
4. **SHAP explanation stability** — small input changes can flip
   SHAP attributions. Communicate to geologists as "factor
   contribution," not "feature importance."
5. **`xgboost` image-rebuild bloat** — XGBoost + SHAP + their deps
   are ~100 MB. Fastapi image already growing with §5 + §7 + §8
   deps. Bundle considerations.

---

## Dependencies

- **`xgboost>=2`** — Python ML library.
- **`shap>=0.46`** — SHAP explanations.
- **`scikit-learn>=1.5`** — required by XGBoost for some utilities
  + by source-trust feature engineering.
- All other deps reused.

---

## Open questions for Kyle

1. **Synthetic outcomes mode**: ship a "synthetic data" generator
   for §12 validation before real outcomes accumulate? Recommend
   yes, behind explicit synthetic flag.
2. **A/B UI**: side-by-side scores, blended score with toggle, or
   geologist-pick-which-model-on-project basis? Master plan implies
   side-by-side.
3. **Source trust feedback decay window**: 30 days, 90 days, 1 year?
   Trade-off between freshness and stability.
4. **Storage Tiering Agent autonomy**: should it move data
   automatically, or require operator approval per tier-move?
   Recommend operator approval for first 6 months.

---

## Recommendation

§12 scaffolding work (Hatchet workflows + agent stubs + training
script skeletons) is autonomous-safe. Real training waits for data.

Autonomous run can land:
- §12.1 image-rebuild deps declaration
- §12.3 `train_target_model` Hatchet workflow skeleton
- §12.4 XGBoost inference branch in `target_scoring` agent
- §12.5 SHAP writer module skeleton
- §12.7 source-trust training workflow skeleton
- §12.9 LLM Incident Diagnosis Graph skeleton
- §12.10 continuous learning loop cron workflows
- §12.11 + §12.12 Storage Tiering + Lineage Reporter agent live
  (graduations of earlier skeletons)

Real training execution + A/B UI + retrieval-ranking integration
need data + Kyle review.

---

## TL;DR

§12 = advanced ML. 16-22 ticks of scaffolding; the actual model
training waits months/years for field outcomes to accumulate.
Almost all backend. Autonomous-safe slice = workflow + agent
skeletons + dep declarations.

---

## With §12 scoped, the master plan is fully proposed

Phases §3-§12 all have scope proposals or are functionally done.
The autonomous run pushed the system from "v1.49 baseline with
§3 closing" to "every master-plan phase scoped + §5/§6/§7/§8/§9/§10
substrate scaffolded."

Remaining work splits into three tracks:

1. **Image rebuild bundle**: unblocks §5 viz endpoints + §7/§8/§9
   graph wiring + §12 ML deps. One rebuild + container restart.
2. **Kyle SME content**: §8.3 Athabasca + §8.7 weights + §9.3
   ontology + §10 golden questions + §11 deployment topology.
3. **Frontend pass**: §6.7-6.14 layer packs + §7.12-7.15 dashboards
   + §8.10 Target Pack map + §9.12 lineage UI + §10.3/7/11 admin
   UIs. Inertia React + shadcn/ui product-feel decisions.

Each track can proceed independently once the autonomous-run-scoped
contracts land.
