## Doc-phase 122 handoff — Track 1 image-rebuild PRE-WORK

**Status:** Pre-work landed. **Kyle triggers the rebuild + monitors.**

## What changed

### `src/fastapi/pyproject.toml` — 6 new deps added to `dependencies`

§7 (report rendering):
- `weasyprint>=63.0,<64.0` — PDF rendering from HTML+CSS
- `python-docx>=1.1,<2.0` — DOCX generator
- `openpyxl>=3.1,<4.0` — XLSX generator

§12 (XGBoost + advanced learning):
- `xgboost>=2.1,<3.0` — gradient-boosted scoring
- `shap>=0.46,<1.0` — SHAP explanations
- `scikit-learn>=1.5,<2.0` — utilities + train/test split + metrics

Existing §5 deps (`geopandas`/`rasterio`/`mplstereonet`) already
declared (doc-phase 71) — will install on the same rebuild.

### `docker/fastapi.Dockerfile`

**Builder stage:**
- Added `libffi-dev` (cffi compile-time dep for WeasyPrint).

**Builder install command:**
- Now also installs the `langgraph` extra explicitly (was opt-in via
  `--extra langgraph`; now baked into the runtime image because §7 / §8
  / §9 graphs all need it).

**Runtime stage** — 9 new system packages for WeasyPrint:
- `libpango-1.0-0`, `libpangoft2-1.0-0` — Pango text layout
- `libcairo2` — Cairo 2D graphics
- `libgdk-pixbuf-2.0-0` — image decoder
- `libharfbuzz0b` — text shaping (transitive but explicit)
- `libffi8` — cffi runtime
- `shared-mime-info` — file-type detection
- `fonts-liberation` — Liberation Sans/Serif/Mono (Arial/Times metrics)
- `fonts-dejavu-core` — DejaVu fallbacks for non-ASCII glyphs

### `scripts/post_image_rebuild_smoke.sh` — new file

Post-rebuild smoke test that asserts:
1. All 9 new Python deps import + report a version
2. langgraph stack imports (4 libs)
3. WeasyPrint can render a trivial HTML→PDF (proves Pango/Cairo bond)
4. The substrate verifier still passes (currently 68/68)

Dry-run today reports 4/15 PASS (sklearn is somehow already present;
substrate verifier still 68/68 because the container hasn't been
rebuilt yet). After rebuild, expect **15/15 PASS**.

## How to trigger the rebuild

```bash
# From WSL — repo root /home/georag/projects/georag
cd ~/projects/georag

# 1) Build the new fastapi image (~10-20 min depending on cache state)
docker compose build fastapi

# 2) Restart fastapi + hatchet workers to pick up the new image
docker compose up -d --force-recreate fastapi hatchet-worker-ingestion hatchet-worker-ai

# 3) Wait for fastapi to come healthy (~30s)
docker compose ps fastapi
# wait for "healthy" in STATUS

# 4) Run the post-rebuild smoke test
bash scripts/post_image_rebuild_smoke.sh
# → expect 15/15 PASS
```

If the build fails partway through, common causes:
- **WeasyPrint compile error** — the runtime libs above should prevent
  this. If it happens anyway, paste the error and we'll add the
  missing pkg-config / lib package.
- **xgboost wheel not found** — XGBoost 2.x ships wheels for
  linux x86_64 Python 3.13. If your host is on an unsupported arch,
  pin to an older version or skip the §12 deps for now.
- **langgraph version conflict** — the extra was pinned `>=0.2.50,<0.3`.
  If `langchain` pulls a different langgraph version, we may need to
  loosen the pin.

## What graduates from skeleton AFTER the rebuild

Following modules currently raise `NotImplementedError` because their
deps weren't in the image. After rebuild they're code-ready (just
need their callers wired):

**§5 viz endpoints** (geopandas/rasterio/mplstereonet now installed):
- `app/routers/strip_log_render.py` (if/when authored)
- `app/routers/cross_section_render.py`
- `app/routers/stereonet_render.py`

**§7 Report Builder Graph** (langgraph + weasyprint now installed):
- `app.services.report_builder.nodes` — all 13 node functions
- `app.hatchet_workflows.generate_report.execute` task body

**§8 Target Recommendation Graph** (langgraph now installed):
- `app.services.target_recommendation.nodes` — all 12 nodes
- `app.hatchet_workflows.score_targets.execute` task body

**§9 Hypothesis + Reasoning** (langgraph now installed):
- `app.agents.phase9.hypothesis_generator` body
- LLM Incident Diagnosis Graph state-machine wiring

**§12 ML stack** (xgboost + shap + scikit-learn now installed):
- `app.services.target_scoring_ml.score_zone_xgboost`
- `app.services.target_scoring_ml.write_shap_factors`
- `app.services.target_scoring_ml.compute_ab_scores`
- `app.hatchet_workflows.train_target_model.execute`
- `app.hatchet_workflows.train_source_trust.execute`

~20 skeleton bodies become graduate-ready in one rebuild.

## Next session steps (after rebuild lands)

1. Re-run `bash scripts/autonomous_run_substrate_verify.sh` → should
   stay 68/68 + the 5 live-pytest gates still pass.
2. Pick 1-2 skeletons to graduate first. Recommendation: start with
   `train_target_model` Hatchet workflow body — it has the cleanest
   contract + I already drafted pytest fixtures that work for it.
3. Kick off Track 2 (§8.3 Athabasca uranium SME content) — I have a
   seed-template ready to scaffold (next handoff).

## Carry-overs

Same as prior plus:
- Image size will grow from ~1.4 GB to estimated ~2.2 GB. xgboost is
  the largest single addition (~250 MB).
- If you want to keep prod images leaner, the `[prod]` extra exists.
  Could move xgboost/shap/scikit-learn there once §12 graduates
  and we know the inference-only path.
