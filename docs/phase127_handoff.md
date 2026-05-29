## Doc-phase 127 handoff — Track 1 image rebuild LANDED + 45 golden questions seeded

**Status:** Track 1 complete. **Substrate verifier 72/72 PASS.** 45
mechanical golden questions live in `eval.golden_questions`. Build #4
(baking in `pyjwt`) running in background.

## What landed in this tick

### Image rebuild healthy

After 3 builds + the §122 incident remediation, the new
`georag/fastapi:latest` image is fully healthy with:

- **6 §5/§7/§12 dep additions:** geopandas, rasterio, mplstereonet,
  weasyprint, python-docx, openpyxl, xgboost, shap, scikit-learn
- **4 fix-up pip additions** that surfaced during recovery:
  sentry-sdk[fastapi] (restored), hatchet-sdk, aioboto3, pymupdf
- **1 volatile pip dep** (pyjwt) — will bake in via build #4
- **2 system libs** added to runtime stage: libgl1 + libglib2.0-0
  for OpenCV/paddleocr, plus 9 WeasyPrint libs (Pango/Cairo/GLib/fonts)
- **langgraph extra** baked into runtime image (was opt-in)
- **§04p PDF stack preserved** through the wholesale-drift recovery

### Post-rebuild smoke — 15/15 PASS

```
docker exec georag-fastapi python -c "import weasyprint; ..."
PDF bytes generated: 6218     # WeasyPrint Pango/Cairo bond test
```

All 15 checks green. Image confirmed runnable.

### Substrate verifier — 72/72 PASS

Up from 65/65 pre-rebuild + 2 doc-phase 125+126 gates +
2 doc-phase 124 mechanical-questions gates + post-rebuild gains:

```
bash scripts/autonomous_run_substrate_verify.sh
# → 72/72 checks passed
```

### Bug caught + fixed mid-flight: seeder idempotence

The mechanical-questions live DB tests failed on re-seed
(`assert r2.updated == 0` → `assert 37 == 0`). Root cause:
`json.dumps()` produced a different field order than Postgres'
JSONB-stored representation, so text-equality returned false for
identical payloads. Fixed by parsing both sides before comparing
(structural equality via `json.loads(stored) == new_obj`).

After fix: re-seed reports `unchanged=45` as expected.

### 45 mechanical golden questions LIVE in `eval.golden_questions`

```
SELECT question_set, count(*) FROM eval.golden_questions
WHERE status='active' GROUP BY question_set;
```

| Question set | Count |
|---|---|
| numeric_grounding | 15 |
| schema_mapping | 10 |
| ocr_triage | 10 |
| report_section | 10 |
| **Total active** | **45** |

Idempotent — re-running the CLI reports `unchanged=45`. The 50
SME-authored questions follow the same template once authored.

## Cumulative skeleton-graduation surface

With this rebuild landing, the following skeleton bodies can NOW
graduate to live behavior (their underlying deps are present):

- §5 viz endpoints (geopandas/rasterio/mplstereonet for strip log,
  cross section, stereonet)
- §7 Report Builder Graph (langgraph for nodes + weasyprint for PDF)
- §7.10 `generate_report` Hatchet task body (composes the above)
- §8.4 Target Recommendation Graph (langgraph)
- §8.6 `score_targets` Hatchet task body
- §8.7 weighted scoring formula (depends on §8.3 SME content)
- §9.5 Hypothesis Generator integration into Answer Graph (langgraph)
- §9.7 LLM Incident Diagnosis Graph wiring (langgraph)
- §12.3-§12.5 XGBoost training + inference + SHAP attribution
  (xgboost + shap + scikit-learn)

~20 graduations now unblocked. Track 1 is genuinely complete; the
remaining backlog is implementation, not unblock.

## Current session state — recap

Cumulative across the §3-§12 master-plan substrate work plus tracks
1+2 prep + the §125-§126 CI gates:

- **Doc-phase ticks this run:** 127 (5 ticks ago we were at 122 = rebuild prep)
- **Live helpers shipped:** 8 across `geological_ontology`,
  `decision_intelligence`, `support_cockpit`, `audit`
- **Live pytest cases:** 52 + 14 mechanical questions = **66 total**
- **Substrate verifier:** **72/72 PASS**
- **Database tables added:** 26 across §6/§7/§8/§9/§10
- **Hatchet workflows:** 10 long-running registered in AI pool
- **Eloquent models:** 14 + 5 factories
- **Ontology terms seeded:** 83 + 134 synonyms (mechanical 3 of 12 classes)
- **Golden questions seeded:** 45 mechanical (0 of 50 SME-authored)

## Next track activations

### Track 2 — §8.3 Athabasca uranium SME content

Kyle owns. Edit
`src/fastapi/app/services/target_recommendation/sme_content/athabasca_uranium.py`,
fill in the 13 TODO blocks, then:

```bash
docker exec georag-fastapi python -m \
    app.services.target_recommendation.sme_content \
    --slug athabasca_uranium --user-id 731 --activate
```

Protective rail (test_athabasca_uranium_module_currently_blocked)
will surface remaining blockers if any.

### Track 3 — frontend pass

Backend is now end-to-end ready. The first surfaces with live data
behind them:
- **Eval Dashboard (§10.7)** — backed by `get_workspace_decision_summary` +
  `get_ontology_class_stats` + the 45 just-seeded golden questions
- **Decision History (§9.12)** — backed by `get_workspace_audit_excerpt`
- **Support Cockpit (§10.11)** — backed by `emit_support_access_audit` +
  `open_trace_with_audit` + the ops.* tables

Each Inertia React page surface is your call on product-feel.

## Carry-overs

- **Build #4** running in background — bakes `pyjwt` in permanently
  (currently installed volatilely; would disappear on next
  `docker compose up --force-recreate fastapi`).
- **§04p source URI** — `silver.parser_run_artifacts.raw_output_uri`
  references unchanged; ingest paths intact.
- **Image size** — base layer now ~12-13 GB (xgboost + shap add ~500 MB;
  paddlepaddle ~500 MB; OpenCV variants ~300 MB; nvidia CUDA libs).
  Production deploys can strip CUDA libs via a runtime-only stage later.
