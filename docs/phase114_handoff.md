## Doc-phase 114 handoff — First LIVE ontology helper (`resolve_term`)

**Status:** Complete. **10/10 pytest tests pass.** First non-skeleton
helper of the autonomous run.

## What landed

`src/fastapi/app/services/geological_ontology/resolver.py` —
**actual working code** (not a skeleton):

### `resolve_term(conn, *, raw_term, restrict_to_class)`

Async function that resolves a raw user/LLM string to a canonical
ontology term. Returns `ResolvedTerm` dataclass or `None`.

Lookup order:
1. Case-insensitive exact match against
   `silver.geological_ontology_synonyms.synonym`.
2. Fall back to case-insensitive match against
   `silver.geological_ontology_terms.canonical_term`.

Optional `restrict_to_class` parameter limits the lookup to a single
ontology class (commodity / geological_age / resource_class, plus
the 9 SME-pending classes when they fill in).

### `find_synonyms(conn, *, canonical_term, ontology_class)`

Returns every synonym for a canonical term — useful for query
expansion (e.g., a Qdrant search for "Uranium" should also match
"U3O8" or "yellowcake").

### `ResolvedTerm` dataclass

```
ResolvedTerm(
    canonical_term: str,
    ontology_class: "commodity" | "geological_age" | ...,
    payload: dict,                # passes through schema payload (e.g., element_symbol)
    matched_via: "synonym" | "canonical_term",
)
```

`matched_via` is exposed so retrieval downstream can know whether a
match was via the more-permissive synonym layer vs the strict
canonical_term — useful for ranking + telemetry.

### Smoke verification

```
docker exec georag-fastapi python -m pytest tests/test_ontology_resolver.py -q
# 10 passed, 1 warning in 0.48s
```

Verified scenarios:
- ✅ `U3O8` → `Uranium` via synonym (with payload)
- ✅ `Gold` → `Gold` via canonical_term fallback
- ✅ Case-insensitive (`YELLOWCAKE` + `yellowcake` both work)
- ✅ Class restriction blocks cross-class matches
- ✅ Empty / whitespace input returns None
- ✅ Garbage input returns None
- ✅ `Cretaceous` → `Cretaceous Period` / `geological_age`
- ✅ `inferred` → `Inferred Mineral Resource` / `resource_class`
- ✅ `find_synonyms("Uranium")` → `[U, U3O8, yellowcake]`
- ✅ Unknown canonical returns empty list (not None)

## Why this is significant

Of the ~85 sub-steps closed this run, this is the **first one with
live behavior**, not just a locked-interface skeleton. The resolver:
- Runs against real seeded data (doc-phase 112: 83 terms + 134 synonyms)
- Has permanent pytest coverage
- Returns Pydantic-friendly dataclasses
- Honors the §9.2 contract from doc-phase 90
- Plugs into existing async patterns (asyncpg + asyncio)

When the §9.3 SME pass populates the remaining 9 classes (alteration,
structure, lithology, etc.), this resolver Just Works — no code
changes needed.

## Master-plan progress

§9.2 ontology infrastructure is now **functionally complete**:
- Schema (§9.1, doc-phase 90) — done
- Seed loader stubs (§9.2, doc-phase 90) — done
- Mechanical seed (§9.3 partial, doc-phase 112) — 83 terms live
- DatabaseSeeder integration (doc-phase 113) — done
- `resolve_term` + `find_synonyms` (§9.2, doc-phase 114) — **live + tested**

What's still pending: the §9.3 SME pass for the remaining 9 classes
(deposit_model, lithology, alteration, structure, mineral_assemblage,
host_rock, tectonic_setting, geochemistry, geophysics).

## Cumulative session state — 41 ticks (74-114)

The autonomous run has now produced its first **working live code**
that consumes the autonomous-run substrate. Pattern:
1. Schema migrations land tables (76, 85, 90, 91, 92, 97, 102, 150000)
2. Mechanical seed lands data (112)
3. DatabaseSeeder + verifier integration locks the floor (113)
4. **Live helper** consumes data + ships with tests (114) ← here

Same pattern can apply when the SME pass lands more terms:
- §9.3 SME content lands → resolver continues working with no
  code changes
- §8.3 SME deposit-model attributes land → §8.7 weighted scoring
  formula uses them via the same pattern

## Recommended next ticks

Continued ontology coverage:
- Doc-phase 115 = add `resolve_term` calls into the existing
  chat retrieval entity-resolution layer (where the LLM extracts
  "U3O8" from a question, the resolver now canonicalizes that to
  "Uranium" before Qdrant lookup)
- Doc-phase 116 = expose `resolve_term` via a small FastAPI debug
  endpoint (handy for Kyle to test SME content as it lands)

Or pivot:
- Land a similar "first live helper" for another autonomous-run
  service — e.g., `app.services.decision_intelligence.record_decision`
  can become live now that the schema is in place and `emit_audit`
  exists.

## Carry-overs

Unchanged plus:
- Verifier doesn't yet include the new pytest file. Could extend
  rollup verifier to also gate `pytest tests/test_ontology_resolver.py`
  exit code 0.
