# Query Class Routing

Documents the six spec query classes, their precedence, how to add a new class, and how to test the classifier. Use this when adding a new retrieval behaviour, debugging a misclassified query, or onboarding.

---

## The two classifiers

There are two classifiers in the orchestrator. They are separate and both run on every query.

1. **`_classify_query()` in `orchestrator.py`** — Internal routing buckets: `{spatial: bool, documents: bool, downhole: bool, assay: bool, graph: bool, targeting: bool, public_geoscience: bool}`. This drives which store tools actually execute.

2. **`classify_query()` in `src/fastapi/app/services/query_classifier.py`** — Spec-class label: one of the six `QueryClassLiteral` values below. This drives `answer_runs.query_class`, the reranker top-k selection, and the cache key.

This document describes classifier 2 (the spec-class classifier). Changing it does not change which stores are queried — it changes the metadata label, reranker behavior, and cache key. Both classifiers must stay aligned in intent; see "Adding a new class" below for where to touch both.

---

## Current 6 classes — definitions and precedence

Precedence is strict and descending. The first class that matches wins.

### 1. `viz` (highest precedence)

**Definition:** The user wants a rendered output — a map, chart, plot, stereonet, or diagram. The intent is visualization, not data retrieval per se.

**Token signal:** `plot`, `chart`, `visualize`, `render`, `stereonet`, `rosette`, `heatmap`, `contour`, `isopach`, `draw`, `generate`, `wireframe`, `downhole-plot`, `fence-diagram`

**Phrase signal:** `show me a`, `plot the`, `render a map`, `generate a map`, `plan view map`, `cross section view`, `heat map`, `rose diagram`, `fence diagram`, `stereonet plot`

**Why highest:** Render verbs (`plot`, `draw`, `render`) are unambiguous explicit user signals. A user who says "plot a map of collars within 50 km" is canonically requesting a visualization, not a spatial data retrieval — even though the query contains spatial keywords. Viz must fire before spatial to capture this intent correctly. Calibrated against the 54-test corpus.

**Reranker top-k:** 30

### 2. `spatial`

**Definition:** The user is asking about drill-hole locations, geographic proximity, coordinate systems, or geographic coverage.

**Token signal:** `drill`, `hole`, `holes`, `collar`, `collars`, `azimuth`, `dip`, `inclination`, `easting`, `northing`, `coordinate`, `coordinates`, `ddh`, `hq`, `bq`, `nq`, `near`, `within`, `radius`, `bbox`, `intersect`, `polygon`, `utm`, `epsg`, `crs`, `srid`, `collar-location`, `drillhole-location`

**Phrase signal:** `drill hole`, `drillholes`, `how many drill`, `within radius`, `within km`, `cross section`, `plan view`, `diamond drill`, `active holes`, etc.

**Why second:** Drill-hole location questions are the most frequent concrete retrieval task and need direct PostGIS dispatch. Spatial signals are strong geological jargon (azimuth, dip, collar) that rarely appear outside a location context.

**Reranker top-k:** 30 (wider pool — many collars can be relevant to a spatial query)

### 3. `computation`

**Definition:** The user wants a numerical calculation, statistical aggregation, or resource estimate.

**Token signal:** `grade`, `grades`, `tonnage`, `tonnes`, `reserve`, `reserves`, `ppm`, `ppb`, `pct`, `u3o8`, `calculate`, `compute`, `average`, `mean`, `median`, `maximum`, `minimum`, `aggregate`, `distribution`, `correlation`, `weighted`, `interpolate`, `idw`, `assay`, `assays`, `geochemistry`, `geochem`, `accumulation`, `cutoff`, `cut-off`, `break-even`, `mlb`, `mlbs`

**Phrase signal:** `resource estimate`, `average grade`, `highest grade`, `grade distribution`, `weighted average grade`, `indicated resource`, `cutoff grade`, `best intercept`, etc.

**Reranker top-k:** 10 (tight — computation needs the top few exact numerical matches)

### 4. `document`

**Definition:** The user is asking about a report, publication, NI 43-101 filing, or a specific section/appendix within a document.

**Token signal:** `report`, `reports`, `ni43`, `43-101`, `pdf`, `filing`, `publication`, `publications`, `journal`, `appendix`, `jorc`, `authored`, `published`, `qp`

**Phrase signal:** `ni 43-101`, `technical report`, `qualified person`, `geological setting`, `geological study`, `property description`, `filed on sedar`, `what does the report`, `according to the report`, etc.

**Reranker top-k:** 15 (higher precision for report-section synthesis)

### 5. `factual`

**Definition:** Knowledge-graph or entity questions not covered by document retrieval. Covers geological entity relationships, deposit styles, and provenance questions.

**Token signal:** `entity`, `entities`, `relationship`, `relationships`, `pathfinder`, `vein`, `fault`, `fold`, `intrusion`, `mineralogy`, `petrology`

**Phrase signal:** `related to the`, `connected to the`, `hosted by`, `knowledge graph`, `deposit style`, `uranium deposit style`, etc.

**Note:** The factual token set is deliberately conservative. Most factual geological questions are routed to `document` by the geological context keywords in that class. `factual` fires only when the query has explicit graph/entity vocabulary without document-retrieval markers.

**Reranker top-k:** 20

### 6. `unknown` (lowest — fallback)

**Definition:** No keyword in any class matched. The query is off-topic, too vague to classify, or uses no geological vocabulary.

**Reranker top-k:** 20 (safe default)

The orchestrator's LLM classifier fallback (`classify_via_llm`) fires before spec-class classification when the internal routing buckets all return False. An all-False routing dict triggers a polite refusal response without querying any store. The spec-class `unknown` is for queries where some routing fired but no class label matched.

---

## Adding a new class

Six steps. Do all six in the same PR.

**Step 1 — Add token set and update `QueryClassLiteral` in `query_classifier.py`.**

```python
# src/fastapi/app/services/query_classifier.py

QueryClassLiteral = Literal[
    "factual", "spatial", "document", "computation", "viz", "unknown",
    "your_new_class",   # add here
]

_YOUR_NEW_CLASS_TOKENS: set[str] = {"token1", "token2", ...}
_YOUR_NEW_CLASS_PHRASES: set[str] = {"phrase one", "phrase two", ...}
```

All tokens must be geological-domain-specific. Generic English words (`what`, `how`, `list`, `section`) must not appear in any set — they produce false positives.

**Step 2 — Insert the new class at the correct precedence position in `classify_query()`.**

```python
def classify_query(query: str) -> QueryClassLiteral:
    ...
    if _matches(lower, words, _VIZ_TOKENS, _VIZ_PHRASES):
        return "viz"
    if _matches(lower, words, _SPATIAL_TOKENS, _SPATIAL_PHRASES):
        return "spatial"
    # Insert your new class here if it has higher precedence than computation:
    if _matches(lower, words, _YOUR_NEW_CLASS_TOKENS, _YOUR_NEW_CLASS_PHRASES):
        return "your_new_class"
    ...
```

Document why the new class sits at its precedence position in the module docstring.

**Step 3 — Add per-class top-k in `reranker.py`.**

```python
# src/fastapi/app/services/reranker.py
RERANKER_TOP_K_BY_CLASS: dict[str, int] = {
    "factual":         20,
    "spatial":         30,
    "document":        15,
    "computation":     10,
    "viz":             30,
    "unknown":         20,
    "your_new_class":  <N>,   # add here
}
```

**Step 4 — Add at least 5 unit tests per new class.**

```python
# src/fastapi/tests/test_query_classifier.py

@pytest.mark.parametrize("query", [
    "your new class query 1",
    "your new class query 2",
    "your new class query 3",
    "your new class query 4",
    "your new class query 5",
])
def test_your_new_class(query: str) -> None:
    assert classify_query(query) == "your_new_class"
```

Also add precedence tests if the new class could conflict with an adjacent class.

**Step 5 — Update the database CHECK constraint.**

```sql
-- Run this as a migration (new migration file in database/migrations/):
ALTER TABLE silver.answer_runs
    DROP CONSTRAINT answer_runs_query_class_valid,
    ADD CONSTRAINT answer_runs_query_class_valid
        CHECK (query_class IN (
            'factual', 'spatial', 'document', 'computation', 'viz', 'unknown',
            'your_new_class'
        ));
```

**Step 6 — Update `QueryClassLiteral` in the Pydantic model and bump the strategy version.**

```python
# src/fastapi/app/models/answer_run.py
QueryClassLiteral = Literal[
    "factual", "spatial", "document", "computation", "viz", "unknown",
    "your_new_class",
]

# src/fastapi/app/services/query_classifier.py
RETRIEVAL_STRATEGY_VERSION = "v1-hybrid-YYYY-MM-DD"   # today's date
```

The version bump invalidates all existing cache entries (new class semantics means prior cached answers under the old routing are stale).

---

## Modifying per workspace

Per-workspace query class overrides are not yet supported. The classifier is global.

**TODO (Module 9 scope):** When `silver.workspace_settings` gains a `query_class_overrides JSONB` column, the orchestrator should load workspace overrides before calling `classify_query()` and apply them as pre-classification rules. Example shape:

```json
{
  "force_class_if_keyword": [
    {"keyword": "tailing", "class": "spatial"}
  ],
  "disable_class": ["viz"]
}
```

Until this is implemented, contact Kyle (SME) if a workspace needs classification tuning.

---

## Testing the classifier

```bash
# Run all 54 classifier tests:
docker exec georag-fastapi pytest src/fastapi/tests/test_query_classifier.py -v

# Run a specific class:
docker exec georag-fastapi pytest src/fastapi/tests/test_query_classifier.py -v -k "spatial"

# Run with coverage:
docker exec georag-fastapi pytest src/fastapi/tests/test_query_classifier.py --cov=app.services.query_classifier
```

Current test count: 54 (8 viz, 8 spatial, 8 computation, 8 document, 6 factual, 5 unknown, 6 precedence, 3 constant/alias/type, 2 edge cases). All 54 pass as of 2026-04-21. Grow this count with every new class or token set change.

To test a single query interactively:

```bash
docker exec georag-fastapi python3 -c \
  "from app.services.query_classifier import classify_query; print(classify_query('your query here'))"
```

---

## Performance

The classifier is pure-Python regex with `frozenset` intersection for single-token matching (O(1) average). The phrase check is a linear scan over phrase sets with `re.search()` per phrase. Total time per query is in the microsecond range — not a bottleneck at any realistic load. No caching of classifier results is needed.

---

*Written 2026-04-21 during Module 4 Phase D. Update whenever the underlying procedure changes.*
