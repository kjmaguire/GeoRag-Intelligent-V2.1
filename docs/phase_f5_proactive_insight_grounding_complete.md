# Phase F.5 — Proactive-insight grounding fix (+ container PYTHONPATH unification)

**Status:** Complete — all 6 §04i guards now pass on the Shirley Basin deposit-type question.
**Date:** 2026-05-14
**Question fixed:** *"What type of uranium deposit is targeted by drilling in Shirley Basin, Wyoming?"*

## Symptom

After Phase F.4 cleared Layer 1, the same question still failed Layers 3, 4, and 6:

```
Layer 3: Ungrounded number 374.0 in response — not found in any tool result
Layer 4: Formation/entity name 'Proactive Insights' could not be resolved
Layer 4: Formation/entity name 'Depth' could not be resolved
Layer 4: Formation/entity name 'Consider' could not be resolved
Layer 6: Value -1065.0 violates constraint 'depth_max_m'
Layer 6: Value 445.0 violates constraint 'grade_uranium_max_pct'
Layer 6: Value 374.0 violates constraint 'grade_uranium_max_pct'
```

Every flagged token traces back to the same source: the **"Proactive Insights"** block
that `anomaly_detector` appends to the LLM answer after synthesis (orchestrator step 4b).

## Root cause

`anomaly_detector` produces deterministic statistical insights from raw `tool_results`
rows — depth σ-anomalies, grade outliers, lithology anomalies. The block looks like:

```
--- Proactive Insights ---
  1. Depth anomaly: 36-1065 is 445 m TD — 2.2σ deeper than the project average of 374 m.
     Consider whether this reflects geological targets at depth or operational constraints.
```

That paragraph is appended to `response.text` and then handed to the §04i validators
designed to catch **LLM hallucinations**. Predictable misfires followed:

* **Layer 3** flags `374.0` because the project mean isn't in any tool result verbatim
  (it's a derived statistic).
* **Layer 4** flags `Proactive`, `Insights`, `Depth`, `Consider` — common-word TitleCase
  tokens — as unresolved "formations."
* **Layer 6** flags `445`, `-1065` (parsed from `36-1065`), and `374` as depth/grade
  constraint violations — because anomaly insights are σ-outliers *by definition*.

The insights are grounded by construction (derived from real cited rows). The validators
are LLM-output graders. The two should not interact.

## Fix

A single shared helper, `strip_proactive_insights`, that removes the insights block
before each validator extracts tokens. Insights still appear in the user-facing
response text — only the validator views are pruned.

### Changes

1. **`src/fastapi/app/agent/anomaly_detector.py`** — added:
   * `PROACTIVE_INSIGHTS_HEADER` module constant
   * `strip_proactive_insights(text)` helper that partitions on the header,
     re-attaches any trailing `[DATA-N]` markers (so completeness_guard still
     sees the LLM-side citation footer), and returns the LLM-only text.

2. **`src/fastapi/app/agent/hallucination/orchestrator_validators.py`** —
   `verify_numbers`, `verify_entities`, `verify_constraints` each call
   `strip_proactive_insights(text)` at the top before extracting numbers /
   proper nouns / candidate constraint values.

3. **`src/fastapi/app/agent/hallucination/layer_completeness.py`** —
   `verify_completeness` strips the block before sentence-splitting so
   insight bullets are no longer flagged as uncited declarative sentences.

## The container PYTHONPATH gotcha (discovered during F.5 verification)

While verifying the strip helper, my edits to `orchestrator_validators.py`
appeared to have no effect — Layer 6 kept firing on `445.0`. Tracing
revealed:

* `WORKDIR /app` in the Dockerfile sets the FastAPI runtime CWD to `/app`.
* `RUN uv pip install --system --no-deps . || true` also installs the app
  tree into `/usr/local/lib/python3.13/site-packages/app`.
* The compose service bind-mounts `./src/fastapi:/app` for live editing.
* `uvicorn app.main:app` runs from `/app`, but **uvicorn does not add the
  CWD to `sys.path`** — sys.path[0] becomes empty (`""`). Site-packages is
  the next-matching entry that resolves `app.*`, so the **baked image copy
  wins** over the bind mount.

In other words: bind-mount edits had been silently no-ops since the image
was built. Every phase from B through F.4 was unknowingly running stale
baked code. The reason Phase F.4's verification "passed" with my filter
was a coincidence — the cache rehydration path happens to produce a clean
result on that question.

**Fix:** add `PYTHONPATH: /app` to the three services that mount the
fastapi tree:

* `fastapi` (uvicorn)
* `hatchet-worker-ingestion`
* `hatchet-worker-ai`

With `PYTHONPATH=/app` set, `/app` precedes site-packages in `sys.path`,
and every `app.*` import resolves to the live bind-mounted source.

This is a **production safety fix**, not just a dev convenience. Without
it, doing `docker cp` or relying on the bind mount to ship a fix could
leave the running uvicorn serving baked code indefinitely.

## Verification

```
docker compose up -d fastapi      # picks up new PYTHONPATH
docker compose exec fastapi \
    sh -c 'cd /app && python tmp/f4_verify_deposit.py'
```

**Result:**

```
citation_guard_eval: 0.05s, all_passed=True, failed_guards=[]
evaluate_guards: entity guard within tolerance — 2 unresolved entity(ies) <= tolerance=2

citations (3):
  [DATA-1] type=DATA score=1.000 title=Drill collars from PostGIS (63 records)
  [DATA-2] type=DATA score=0.500 title=Result from drill_targeting
  [DATA-3] type=DATA score=1.000 title=Neo4j knowledge graph (1 entities)

confidence: 0.833
PASS — all citations >= 0.5 (Layer 1 retrieval_quality gate clear).
```

* Layer 1 — clear (Phase F.4 fix holding)
* Layer 3 — `374.0` no longer flagged (insight numbers stripped)
* Layer 4 — only `Wyoming` / `This` remain, both within the `GUARD_TOLERANCE_ENTITY_UNRESOLVED=2` tolerance
* Layer 6 — clear (insight depth/grade outliers stripped)
* Completeness — clear (insight sentences stripped)

## Carry-overs

1. **Other test scripts in `tmp/`** likely also ran against baked code in the
   past — anything diagnosed as "passing" before today's PYTHONPATH fix should
   be re-verified once.
2. **Layer 4 still flags `Wyoming` and `This`** even from the LLM body. Tolerance
   covers it for now (≤2), but a future fix could (a) whitelist US-state
   names and (b) tighten the TitleCase heuristic to exclude single-syllable
   demonstratives like "This."
3. **The site-packages copy is now functionally dead** but still consumes ~50MB
   of image size. A future Dockerfile cleanup could drop the
   `RUN uv pip install --system --no-deps .` line entirely now that PYTHONPATH
   is canonical.
