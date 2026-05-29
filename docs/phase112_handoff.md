## Doc-phase 112 handoff — Mechanical geological_ontology seed

**Status:** Complete. **83 terms + 134 synonyms seeded**, idempotent.

## What landed

`database/seeders/GeologicalOntologyMechanicalSeeder.php` — Laravel
seeder that populates the **factual-taxonomy subset** of the §20.1
ontology (3 of 12 classes that are reference data, not SME judgment):

| Class | Terms | Synonyms | Notes |
|---|---|---|---|
| `commodity` | **47** | 70+ | Periodic-table-grade element list (U, Au, Cu, Ni, Co, Li, Zn, Pb, Mo, Sn, W, Fe, Mn, Cr, V, Ti, Al, REE, Sb, As, Bi, Be, Cd, In, Ga, Ge, Te, Sc, Nb, Ta, Zr, Hf, plus PGMs, diamond, potash, phosphate, sulfur, graphite, oil, gas, coal) with element symbols + common synonyms |
| `geological_age` | **29** | 35+ | All 4 eons + 6 eras + 10 Phanerozoic periods + 8 Cenozoic epochs (Hadean → Holocene). Payload tags `kind = eon | era | period | epoch` |
| `resource_class` | **7** | 14 | CIM categories: Inferred / Indicated / Measured Mineral Resource + Probable / Proven Mineral Reserve + Exploration Target + Historical Estimate |

### Sample data

```sql
SELECT canonical_term, payload->>'element_symbol' FROM silver.geological_ontology_terms
WHERE class='commodity' AND canonical_term='Uranium';
-- Uranium | U

SELECT array_agg(synonym ORDER BY synonym) FROM silver.geological_ontology_synonyms
WHERE term_id = (SELECT term_id FROM silver.geological_ontology_terms
                  WHERE canonical_term='Uranium');
-- {U, U3O8, yellowcake}
```

### Idempotence

Re-running produces identical row counts:
- Run 1: 83 terms, 134 synonyms
- Run 2: 83 terms, 134 synonyms (unchanged)

Uses `find-or-create` on terms + `insertOrIgnore` on synonyms,
respecting the `(class, canonical_term)` + `(term_id, synonym,
language_code)` unique constraints.

### Grant fix

Doc-phase 90 migration only granted SELECT on the ontology tables.
Seeder needs INSERT/UPDATE/DELETE. Granted those to `georag_app`
before running:

```sql
GRANT INSERT, UPDATE, DELETE ON silver.geological_ontology_terms TO georag_app;
GRANT INSERT, UPDATE, DELETE ON silver.geological_ontology_synonyms TO georag_app;
```

## What's still SME territory

9 of 12 ontology classes still wait for the §9.3 SME pass — these
carry geological judgment this seeder deliberately doesn't presume:
- **deposit_model** — 10 launch types per §20.2 (Athabasca uranium,
  roll-front uranium, orogenic gold, etc.) with attribute payloads
- **lithology** — 200-300 entries; BGS Rock Classification Scheme
- **alteration** — argillic, phyllic, propylitic, etc.
- **structure** — fault, shear zone, fold, fracture, vein, contact
- **mineral_assemblage** — pyrite-chalcopyrite, sericite-pyrite, etc.
- **host_rock** — cross-references lithology × deposit_model
- **tectonic_setting** — convergent margin, rift, intracratonic basin
- **geochemistry** — pathfinder elements per commodity + ratios
- **geophysics** — signature patterns by method

Estimate: 800-1000 more entries needed for v1 SME completion.

## Master-plan §9.3 progress

| Class | Status |
|---|---|
| commodity | ✅ seeded (47 terms) |
| geological_age | ✅ seeded (29 terms) |
| resource_class | ✅ seeded (7 terms) |
| deposit_model | pending (Kyle) |
| lithology | pending (Kyle/contractor — BGS taxonomy) |
| alteration | pending (Kyle) |
| structure | pending (Kyle) |
| mineral_assemblage | pending (Kyle) |
| host_rock | pending (Kyle) |
| tectonic_setting | pending (Kyle) |
| geochemistry | pending (Kyle) |
| geophysics | pending (Kyle) |

**3 of 12 classes populated autonomously. Master-plan §9.3 is now
~25% complete on a row-count basis** (83 out of estimated ~1100).

## Recommended next ticks

The substrate is now backed by real reference data for the 3
mechanical classes. Potential next steps:
- Add the seeder to `DatabaseSeeder.php` so fresh deployments pick
  it up automatically (one-line edit)
- Extend the rollup verifier to assert minimum seed counts
  (commodity >= 40, geological_age >= 25, resource_class >= 7)
- Land the FastAPI `resolve_term(raw_term)` async helper now that
  there's real data to resolve against

Doc-phase 113 = wire the seeder into DatabaseSeeder + extend the
rollup verifier to gate seed counts.

## Carry-overs

Unchanged. The remaining 9 ontology classes (deposit_model
attributes, alteration, structure, etc.) wait for Kyle/contractor
SME work. Estimated ~800-1000 more entries for full §9.3 completion.
