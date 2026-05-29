## Doc-phase 90 handoff — §9.1 + §9.2 ontology schema + seed loader

**Status:** Complete. Schema + 2 tables live + seed loader imports clean.

## What landed

### §9.1 — ontology schema

`database/migrations/2026_05_13_110000_create_geological_ontology_schema.php`
creates two tables:

- `silver.geological_ontology_terms` — term_id + class + canonical_term
  + description + payload JSONB. 12-class CHECK enum:
  deposit_model | commodity | lithology | alteration | structure |
  mineral_assemblage | host_rock | geological_age | tectonic_setting |
  geochemistry | geophysics | resource_class.
- `silver.geological_ontology_synonyms` — synonym_id + term_id +
  synonym + language_code (default 'en') + source.

**No RLS** — ontology is GLOBAL reference data, not workspace-scoped.
Future workspace-customization overlay can sit on top.

Indexes: `class`, `synonym`. Unique constraints on (class,
canonical_term) and (term_id, synonym, language_code).

Applied via superuser `georag` (same pattern as §6.5, §8.1).

### §9.2 — seed loader

New package `app/services/geological_ontology/`:
- `seeds.py` — `ONTOLOGY_CLASS_SEEDS` (12 empty lists) +
  `ONTOLOGY_CLASS_NOTES` (per-class SME guidance — e.g. "10 launch
  deposit-model types per §20.2", "BGS Rock Classification Scheme
  mapping for lithology", "argillic/phyllic/propylitic/etc. for
  alteration"). `OntologyClass` Literal type for type safety.
- `__init__.py` — re-exports.

Each class's SME notes give an external contractor or Kyle a
ready-to-fill outline. Estimated total per scope proposal: ~1100
ontology entries for v1 (100/class average).

### Smoke test

    docker exec georag-fastapi python -c "
        from app.services.geological_ontology import seed_classes
        print(len(seed_classes()))   # 12
    "

## Master-plan §9 progress

| Sub-step | Status |
|---|---|
| 9.0 scope proposal | ✅ DONE |
| 9.1 ontology schema | ✅ DONE |
| 9.2 ontology seed loader (empty stubs) | ✅ DONE |
| 9.3 SME population (1100 entries) | pending (Kyle / contractor) |
| 9.4 hypotheses schema | pending (next tick) |
| 9.5 hypothesis agent + Answer Graph integration | pending |
| 9.6 spatial relationship engine | pending |
| 9.7 next-best-data recommendations | pending |
| 9.8 analogue finder | pending |
| 9.9 decision intelligence schema | pending |
| 9.10 decision capture hooks (8 types) | pending |
| 9.11 field_outcome_learning workflow | pending |
| 9.12 data lineage graph UI | pending (frontend) |
| 9.13 What Changed delta detection | pending |
| 9.14 acceptance test | pending |

**3 of 14 §9 sub-steps closed.**

## Recommended next tick

Doc-phase 91 = §9.4 + §9.5 (hypotheses schema + hypothesis agent
skeleton). Hypotheses schema is small (1-2 tables); agent follows
§7/§8 skeleton pattern. Builds on §9.1's ontology + the existing
citation contract.

## Carry-overs

Same as prior ticks plus:
- Kyle SME ontology population (§9.3) — multi-week external work.
- `app.services.geological_ontology` will add `resolve_term()` and
  `find_synonyms()` async helpers once §9.3 SME data lands.
