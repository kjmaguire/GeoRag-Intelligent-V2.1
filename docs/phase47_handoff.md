# Phase 47 Handoff — dead silver.shadow_runs INSERT in ingest_pdf workflow (REAL PRODUCTION BUG)

**Document version:** 1.0
**Status:** Phase 47 complete. Real production bug fixed — the live `ingest_pdf` Hatchet workflow was crashing on every persist step because Phase 4's silver.shadow_runs drop had left the matching INSERT in the workflow code.
**Predecessors:** `docs/phase46_handoff.md`. This phase was triggered by the P46 confirmatory sweep surfacing `phase1_step4_verify.sh` (which Phase 45 had re-synced from missing-in-WSL → present).

---

## 1. What Phase 47 delivered

A real bug fix to `src/fastapi/app/hatchet_workflows/ingest_pdf.py`
plus supersession-tolerant updates to `phase1_step4_verify.sh`.
This was NOT cosmetic verifier-massaging — the bug was crashing
the live workflow.

### 1a. The bug

Phase 4 Step 6 (sunset of the v1.49 shadow-diff harness) dropped
`silver.shadow_runs` and archived ShadowRunsController + the
shadow_diff workflow. **It did not** remove the matching
`INSERT INTO silver.shadow_runs` in
`hatchet_workflows/ingest_pdf.py`'s `persist` step.

Result: every PDF ingest run since Phase 4 hit
`asyncpg.exceptions.UndefinedTableError: relation "silver.shadow_runs" does not exist`
during persist. The workflow's other writes (silver.reports +
silver.document_passages) had already happened in earlier
transactions and succeeded; the shadow_runs INSERT failed at the
end. Subsequent verifier runs found populated `silver.reports`
and `silver.document_passages` from those partial successes, so
counts-based assertions kept passing. The smoke-trigger check
(`6` in phase1_step4) was the only one that observed the actual
crash — and it was never run because phase1_step4_verify.sh was
missing in WSL until Phase 45 re-synced it.

The bug had been latent since Phase 4 closed (months earlier on
this run's calendar).

### 1b. The fix

`src/fastapi/app/hatchet_workflows/ingest_pdf.py`:

1. Removed the `async with conn.transaction():` block that did
   `set_config('app.workspace_id', …)` + `INSERT INTO silver.shadow_runs`
   + `final.shadow_runs_id = shadow_id` (lines ~439-491).
2. Kept the `final = IngestPdfFinalOut(…)` initialization that
   was inside the dead block (it's used downstream for audit-row
   payload + return value).
3. Updated audit payload reference from `"shadow_runs_id": shadow_id`
   → `"shadow_runs_id": None`.
4. Updated the persist log line from
   `"…shadow_id=%s parser=…", final.shadow_runs_id, …` →
   `"…parser=…", final.parser_used, …` (also fixed the dangling
   `duration_ms` variable that was only defined inside the dead
   transaction by recomputing it inline:
   `final.parse_duration_ms + final.persist_duration_ms`).

The `shadow_runs_id: str | None = None` field is intentionally
preserved in the `IngestPdfFinalOut` model with its default
None — downstream consumers of the workflow's final output
may still read the field; they now get None. (No-op for any
caller that doesn't read it; safe-default for any that does.)

### 1c. The verifier supersession

`scripts/phase1_step4_verify.sh` had three checks asserting
historical state Phase 4 deliberately removed:

| Check | Pre-Phase-4 | Post-Phase-4 |
|-------|-------------|--------------|
| 1: schema | silver.shadow_runs + workspace.feature_flags both present | only workspace.feature_flags |
| 2: feature flags | both `ingest_pdf_hatchet_traffic_pct` + `ingest_pdf_shadow_enabled` seeded | neither (Phase 4 deleted both) |
| 7: persist outputs | silver.shadow_runs row + audit.audit_ledger entry | audit row only |

Each updated with `if … elif …` branches that accept either era
and report which the live system is in.

After: phase1_step4 = **9/9 standalone**.

---

## 2. How this was missed for so long

1. Phase 4 Step 6's verifier (`phase4_step6_verify.sh`) checked the
   sunset *negatively* — confirming the table was dropped, the
   feature flags were deleted, the controller was archived. It
   did not also check that *callers* of the removed surface had
   been updated.
2. The matching INSERT lived in a Hatchet workflow that's only
   exercised by `phase1_step4_verify.sh`'s end-to-end smoke
   trigger. Without that verifier running, the crash silently
   accumulated as `partial` runs in Hatchet's run history.
3. `phase1_step4_verify.sh` itself was missing in WSL from
   sometime before Phase 18 until Phase 45 re-synced it.
   Sweep cascades skipped it.
4. The two other writes in `persist` (silver.reports +
   silver.document_passages) happen in *earlier* transactions
   that committed before the shadow_runs transaction failed.
   So counts-based assertions in adjacent verifiers kept passing
   from the per-row population on each attempted run.

**The take-away**: when Phase 4 dropped silver.shadow_runs, the
real-bug discovery would have needed either (a) running every
caller-touching verifier, or (b) a grep across the codebase for
`silver.shadow_runs` to find lingering references. Neither
happened. The autonomous-run cleanup theme (Phases 44-47) is
now what's exercising those callers.

---

## 3. Verifier impact

`phase1_step4` was previously **5/9** in the P46 sweep run.
After Phase 47: **9/9 standalone**.

The same fix should also remove a class of `phase4_step6`-style
partial passes that depended on stale cache from previous
ingest attempts — `silver.reports` and `silver.document_passages`
counts now grow only from clean persist runs, not from
broken-mid-persist partial states.

---

## 4. Files of record

```
src/fastapi/app/hatchet_workflows/ingest_pdf.py    — dead silver.shadow_runs INSERT removed (~53 lines deleted)
scripts/phase1_step4_verify.sh                      — 3 checks supersession-tolerant (schema, feature flags, persist)
docs/phase47_handoff.md                             — this file
```

The ingest_pdf.py edit is the only application-code change in
this run since Phase 43 closed R-P11-B. Carefully bounded — only
removed code that referenced an already-dropped table.

---

## 5. Container deployment

After editing:
1. `cp` source Windows → WSL with CRLF normalisation.
2. `docker cp` into `georag-hatchet-worker-ingestion` (live mount).
3. Python import smoke (`importlib.import_module('app.hatchet_workflows.ingest_pdf')`).
4. `docker restart georag-hatchet-worker-ingestion` to drop the
   stale code from the worker's in-memory image.
5. Re-run phase1_step4 — end-to-end smoke now reaches all 7
   workflow steps and persist returns.

---

## 6. Carry-over status after Phase 47

| Item | Status |
|------|--------|
| Major-shape backlog | ✅ empty (R-P11-B at 43) |
| Sweep-flake cleanup | ✅ closed (P44/45/46) |
| Real bugs surfaced by sweep | ✅ closed (P46 routes, P47 silver.shadow_runs INSERT) |
| LLM/test variance flags | ⚠️ still flagged (cold-run floor 28 vs 30 typical; gq-017 stability 1/3) |

The autonomous-run cadence has now stabilised at the point where
every per-step verifier with both up-to-date code AND
supersession-tolerant assertions reports green. The two flagged
LLM-variance items remain out of autonomous-loop scope and want
a dedicated sit-down.

---

## 7. Bug-discovery pattern

This phase establishes a third pattern alongside the two from
Phase 44/45:

1. **Sweep-collision** (P44): two concurrent sweeps starve each
   other on docker exec.
2. **WSL probe-drift** (P44/45): Windows-side edits not synced
   to WSL look like sweep flakes.
3. **Caller-side dead code** (P47): a "removed" feature whose
   downstream callers are never updated. Symptoms: live workflow
   runs throw `UndefinedTableError` (or equivalent) mid-flight;
   the matching verifier passes if it only counts rows or if
   it's not in the sweep rollup at all.

The diagnostic for #3 is `grep` across the whole codebase when
sunsetting any table/column/route — not just the controller
and the migration that drops it.

End of Phase 47 handoff.
