## Doc-phase 123 handoff — Track 2 prep: SME content scaffolding for §8.3

**Status:** Complete. **6/6 pytest cases pass; verifier 69/69.**

## What landed

### Scaffolding package

`src/fastapi/app/services/target_recommendation/sme_content/`:
- `__init__.py` — re-exports seeder
- `athabasca_uranium.py` — **the file Kyle edits**. Structured TODO
  placeholders for every §20.2 field
- `seed_runner.py` — idempotent seeder + audit ledger anchor
- `__main__.py` — CLI: `python -m ... .sme_content --slug ... --user-id ... --activate`

### Content module — `athabasca_uranium.py`

Mirrors §20.2 verbatim. Sections Kyle fills in:

| Section | Type | Purpose |
|---|---|---|
| `HOST_ROCKS` | `list[str]` | Host rock types where mineralization occurs |
| `STRUCTURES` | `list[str]` | Structural controls (faults, shears, etc.) |
| `ALTERATION` | `list[str]` | Alteration assemblages (clay, illite, hematite, etc.) |
| `GEOCHEMISTRY_PATHFINDER_ELEMENTS` | `list[str]` | Element symbols ("U", "Pb", "B", etc.) |
| `GEOCHEMISTRY_ELEMENT_RATIOS` | `list[str]` | Diagnostic ratios ("Pb/U > 1") |
| `GEOCHEMISTRY_ANOMALY_THRESHOLDS` | `dict[str, float]` | `{"U_ppm_min": 100, ...}` |
| `GEOPHYSICS_*_SIGNATURE` | `str` | Per-method signature (5 methods) |
| `TECTONIC_SETTING` | `list[str]` | Tectonic settings |
| `POSITIVE_INDICATORS` | `list[str]` | Features that raise target score |
| `NEGATIVE_INDICATORS` | `list[str]` | Features that lower target score |
| `ANALOGUES` | `list[dict]` | Known deposits with `{name, location, year, resource_summary, key_features, notes}` |
| `RECOMMENDED_NEXT_DATA` | `list[dict]` | §20.5 entries with cost/time/uncertainty estimates |
| `SCORING_WEIGHTS` | `dict[str, float]` | §18.3 per-factor weights (alteration / structural / geochemistry / proximity / geophysics / analogue) |

Each field has a TODO comment + example shape (not geological guidance —
shape-only examples so Kyle knows the data structure expected).

### Protective rail — `is_populated()` guard

The module exports `is_populated() -> (ready, blockers)`. The seeder
calls it BEFORE writing — refuses to land half-curated content. Per
§8.3 R5 sign-off story, never run a Target Recommendation off
incomplete reference data.

Current state: 9 blockers reported (every required block is empty).

### Idempotent seeder — `seed_deposit_model_from_module()`

In one transaction:
1. Find-or-create `targeting.target_models` row keyed on slug
2. Deactivate any prior active `target_model_versions` (when
   `activate_new_version=True`)
3. Insert new `target_model_versions` row with incremented `version`
   number + `is_active=true`
4. Emit `audit.audit_ledger` row with `action_type='deposit_model.seed'`
   + payload summarizing the content (host_rock_count, analogue_count,
   scoring_weights, etc.)

Re-running preserves history — each iteration creates a new version,
no overwriting. Per §18.3 "the two approaches coexist" — every
geologist iteration is a checkpoint, regulators can audit the
version history.

### CLI

```bash
docker exec georag-fastapi python -m \
    app.services.target_recommendation.sme_content \
    --slug athabasca_uranium \
    --user-id 1 \
    --activate
```

The user-id is the geologist running the seed — recorded as the
audit ledger actor_id.

### Pytest — `tests/test_sme_seeders.py` (6 cases)

| Test | Verifies |
|---|---|
| `test_refuses_when_content_not_populated` | Module with blockers raises `SmeContentNotReadyError`; nothing lands in DB |
| `test_missing_module_raises_clear_error` | Bad module path raises `ValueError` clearly |
| `test_first_seed_creates_model_and_active_version` | First seed → target_models row + v1 (active) + audit anchor |
| `test_reseed_updates_model_and_creates_new_version` | Re-seed → v2 + v1 deactivated; target_model_id stable |
| `test_inactive_seed_does_not_deactivate_prior` | `activate_new_version=False` leaves prior version active |
| `test_athabasca_uranium_module_currently_blocked` | **Protective rail** — confirms the real module is unfillable today, so an accidental deploy can't land empty reference data |

### Substrate verifier — 9 live pytest gates now

| Gate | Tests |
|---|---|
| `pytest:ontology_resolver` | 10 |
| `pytest:decision_recorder` | 5 |
| `pytest:support_access_audit` | 4 |
| `pytest:hash_chain_proof` | 5 |
| `pytest:langfuse_link` | 6 |
| `pytest:decision_summary` | 6 |
| `pytest:ontology_stats` | 8 |
| `pytest:workspace_audit_excerpt` | 8 |
| **`pytest:sme_seeders`** | **6 (new)** |
| **Total** | **58** |

Verifier: **69/69 PASS**.

## Workflow for Kyle

1. Open `src/fastapi/app/services/target_recommendation/sme_content/athabasca_uranium.py`
   in your editor.
2. Replace TODO blocks with Athabasca-specific content. The shape
   examples in each comment are placeholder data, not guidance —
   write what you'd write in a NI 43-101 deposit-model section.
3. Save the file.
4. Re-run the seeder:
   ```bash
   docker exec georag-fastapi python -m \
       app.services.target_recommendation.sme_content \
       --slug athabasca_uranium --user-id 1 --activate
   ```
5. The seeder reports either:
   - `SmeContentNotReadyError` with the remaining blockers → fix +
     re-run, OR
   - `Created/Updated deposit model: athabasca_uranium` with the new
     `target_model_id` + version + audit ledger anchor → done.

Iterations land as new versions automatically. The audit ledger
records every seed event — full §29.2 traceability for free.

## Subsequent SME content modules

The same pattern fits the other 9 deposit models from §20.2.
Duplicate `athabasca_uranium.py` → `roll_front_uranium.py`, fill
in, run the seeder with the new slug. Two-file pattern: content +
seeder reuse.

## Carry-overs

Unchanged plus:
- Kyle owns the §8.3 content. Seeder + scaffolding ship today.
- Once §8.3 is populated, §8.7 weighted scoring formula has its
  weights from `target_model_versions.factor_weights` — a meaningful
  scoring run becomes possible.
- A future enhancement (§8.x v2): a CSV-import path for SMEs who
  prefer spreadsheets over Python.
