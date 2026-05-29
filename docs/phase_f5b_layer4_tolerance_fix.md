# Phase F.5b — Layer 4 entity-resolution tolerance fix

**Status:** Complete. Layer 4 now produces **zero warnings** on the Shirley Basin deposit-type question.

## Symptom

Even after Phase F.5 stripped the proactive-insights block, Layer 4
(`verify_entities` → Neo4j Formation lookup) still flagged 2 tokens
per run:

```
Layer 4: Formation/entity name 'Wyoming' could not be resolved
Layer 4: Formation/entity name 'This' could not be resolved
```

`GUARD_TOLERANCE_ENTITY_UNRESOLVED=2` covered them, but they're noise —
"Wyoming" is a US state and "This" is a demonstrative pronoun. Earlier
runs (pre-F.5) also flagged "Cameco Shirley Basin Uranium" and
"Knowledge Graph", which the LLM picks up directly from the system
prompt and tool result payloads.

## Root cause

`_TITLE_CASE_RE = r"\b([A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,})*)\b"` matches
any sentence-start TitleCase word ≥4 chars: every "This deposit is…",
every "Wyoming targets…", every "Knowledge Graph" reference. The only
guard at extraction time was `if len(name) >= 4`, which barely filters
anything (the regex already requires ≥3 lowercase chars after the
uppercase, so `>= 4` only drops 3-char matches — there shouldn't be any).

Then every extracted token was checked against Neo4j Formation node
names. Common English words and US states don't appear there → flagged.

## Fix

Two whitelists + a compound-grounding helper in
`src/fastapi/app/agent/hallucination/orchestrator_validators.py`:

1. **`_TITLE_CASE_STOPWORDS`** — common English words that pass the
   TitleCase regex at sentence starts: demonstratives ("This", "That"),
   pronouns ("They", "Their"), transitional words ("However",
   "Therefore"), wh-words ("Where", "When"), modal verbs
   ("Could", "Should"), imperative cues ("Consider", "Note"), plus
   GeoRAG-system terminology that surfaces in answers ("Knowledge",
   "Graph", "Report", "Proactive", "Insights", "Anomaly", etc.).

2. **`_GEOGRAPHIC_PROPER_NOUNS`** — 50 US states + DC + territories +
   13 Canadian provinces/territories + country names + compass
   qualifiers. These are grounded in geography itself; we don't require
   them to appear as Formation nodes.

3. **`_is_grounded_name(name, formations, tool_tokens)`** — the unified
   grounding check. A name is grounded when it (or every non-stopword
   word of a compound name) appears in formations OR in the tool-result
   token bag OR in the stopword/geographic whitelists. The compound
   path means "Cameco Shirley Basin Uranium" passes if `cameco`,
   `shirley`, `basin`, and `uranium` each appear in tool_results, even
   if no Formation node carries the literal four-word compound.

The TitleCase extraction filter now reads:

```python
proper_nouns = list(dict.fromkeys(
    m.group(1) for m in _TITLE_CASE_RE.finditer(clean)
    if m.group(1).lower() not in _TITLE_CASE_STOPWORDS
    and m.group(1).lower() not in _GEOGRAPHIC_PROPER_NOUNS
))
```

and the per-name check uses `_is_grounded_name`.

## Verification

Smoke test against the failing-and-grounded cases:

| Input | Result | Why |
|---|---|---|
| `Wyoming` | grounded | geographic |
| `This` | grounded | stopword |
| `Knowledge Graph` | grounded | both words are stopwords |
| `Cameco Shirley Basin Uranium` | grounded | every word in tool_tokens |
| `Saskatchewan` | grounded | geographic |
| `Skookumchuck Formation` | **flagged** | made-up name, no grounding |
| `Bogus Pluton` | **flagged** | made-up name, no grounding |

End-to-end on the Shirley Basin deposit-type question:

```
citation_guard_eval: all_passed=True, failed_guards=[]
evaluate_guards: completeness guard within tolerance — 1 uncited sentence(s) <= tolerance=2
```

**No** `post_assembly_validation: N warning(s)` log line at all — Layers
3, 4, and 6 each return zero warnings. Compare to pre-fix output:

```
post_assembly_validation: 9 warning(s) (critical=6, high=3, advisory=0)
  Layer 4: ... 'Wyoming' ...        ← gone
  Layer 4: ... 'This' ...           ← gone
  Layer 4: ... 'Knowledge Graph'    ← gone (when it appeared)
  Layer 4: ... 'Cameco Shirley...'  ← gone
evaluate_guards: entity guard within tolerance — 2 unresolved entity(ies)
```

The retry loop, which previously fired 2× per query due to Layer 4 warnings, no longer triggers — the answer commits on attempt 1.

## Test impact

Pytest baseline: 128 passed / 5 pre-existing Ollama-tier failures —
**identical to the F.6 baseline**. The completeness-guard test failure
in `test_hallucination_layers.py::TestGuardBundle::test_completeness_failure_propagates`
predates this phase (it asserts strict failure but the
`GUARD_TOLERANCE_COMPLETENESS_UNCITED=2` setting allows 2 uncited
sentences). Out of scope for this fix.

## Carry-overs

* **Stopword list will need pruning as the corpus grows.** Geological
  formations with real names like "Basin" / "Group" / "Sandstone" /
  "Granite" could theoretically be tokens in someone's project; the
  current stopword set is conservative on the common-English side and
  doesn't include those geological-but-also-generic words. If a future
  project reports false-negatives on real formation names like
  "Knowledge Park Formation," update `_TITLE_CASE_STOPWORDS` and add a
  regression test.
* **Compound grounding could be hardened.** Today it accepts the
  compound when every constituent word is grounded somewhere; it does
  not require those words to appear *together*. A future tightening
  could check `name.lower() in tool_tokens_phrases` (a phrase-level
  index) before falling back to word-by-word.
* **Strict-completeness test debt.** The pre-existing
  `test_completeness_failure_propagates` test asserts pre-tolerance
  behavior. Should be updated when E.3.1 tolerance work is fully
  retrofitted into the test suite.
