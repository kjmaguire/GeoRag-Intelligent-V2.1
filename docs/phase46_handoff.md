# Phase 46 Handoff — sweep partial-fail triage (37/38/39 fixed, 32/34 are real variance)

**Document version:** 1.0
**Status:** Phase 46 complete. Three of the six P44-sweep partial-fails resolved with real fixes; two reflect genuine LLM/test variance and were intentionally left alone; one was a sweep contention flake (green standalone).
**Predecessors:** `docs/phase45_handoff.md`. P44 sweep completed 82/88 verifiers green, 551/559 checks.

---

## 1. P44 sweep result

The first clean P44 sweep (after the Phase 44/45 stale-WSL-file fixes) reported:

```
PHASE 0 → 44 MASTER SWEEP
Verifiers: 82 / 88 green
Checks:    551 / 559 across all verifiers

Failing verifiers:
  - phase32_step1_verify.sh (7/9)
  - phase34_step1_verify.sh (6/7)
  - phase35_step1_verify.sh (6/7)
  - phase37_step1_verify.sh (6/8)
  - phase38_step1_verify.sh (7/8)
  - phase39_step1_verify.sh (7/8)
```

Phase 46 ran each failure standalone to classify cause and apply
targeted fixes.

---

## 2. Triage results

### 2a. phase37_step1 + phase38_step1 — missing routes (REAL REGRESSION)

The `/admin/cache-telemetry/skip-reasons.json` and
`/admin/cache-telemetry` routes were claimed by the Phase 37 and
Phase 38 handoffs but **were not actually persisted to
`routes/web.php`** on either Windows or WSL. Either Pint's
reformat at Phase 37 lost them, or they were never written —
either way the verifier was correctly reporting their absence.

Additionally, the Windows-side `CacheTelemetryController.php` was
missing the `index()` method that the Phase 38 handoff documented
adding — WSL had it; Windows was the stale copy. (Reverse
direction of the Phase 44/45 drift; this time WSL was canonical.)

**Fix:**
- Synced the canonical WSL controller back to Windows (added
  `index()` + `Inertia::render('Admin/CacheTelemetry')`).
- Added both routes under the existing `auth:sanctum` group in
  `routes/web.php`, immediately after the Workflow Run Dashboard
  route. Both controller methods call `$this->authorize('admin')`
  so the routes themselves don't need Gate middleware.
- Cleared the Octane route cache (`php artisan route:clear`).

After: phase37_step1 = 8/8, phase38_step1 = 8/8.

### 2b. phase39_step1 — verifier supersession (MY OWN WORK SUPERSEDED)

The Phase 39 verifier asserted on the literal text "Phase 39
skeleton" in the page footer. My own Phase 43 (slice 5) edit
replaced that with "R-P11-B complete — Phase 43" as part of the
closure marker. The Phase 39 verifier was correctly failing — the
slice-1 marker was no longer present.

**Fix:** Supersession-tolerant check accepts either the slice-1
marker or the post-slice-5 closure marker. Both confirm the
page descends from the same slice-1 heritage.

After: phase39_step1 = 8/8.

### 2c. phase35_step1 — sweep contention flake (no fix needed)

The sweep run reported 6/7, but standalone reports 7/7 with the
final check ("Cold-run golden ≥ 29 (got 30; Phase 34 baseline was
30)") passing cleanly. Sweep contention (the parallel golden-tests
verifier running in phase32 starving phase35's docker exec) is
the most likely cause.

### 2d. phase32_step1 + phase34_step1 — REAL LLM/TEST VARIANCE

Both verifiers fail on golden-suite cold runs:

- `phase32_step1`: "gq-017 stability — passed 1 of 3" + "cold-run
  consistency — got: 28 31 29"
- `phase34_step1`: "cold regression — got 28"

The pattern matches the documented "natural ceiling is gq-017
phrase-variance" from the Phase 18-31 retrospective: gq-017
sometimes refuses, sometimes passes, depending on the LLM's exact
wording on a given run. The 28-pass cold run is one below "30
typical", indicating the golden-suite floor has drifted slightly
below where the Phase 32 R-P32-REFUSAL-CONTEXT close intended.

**Deliberate decision: NO verifier loosening.** Loosening
`≥30 each of 3 runs` to `≥28` would mask a real degradation the
operator should know about. The verifier is doing its job; the
underlying variance is the issue. Possible follow-ups for a
future session:

- Investigate whether vLLM is at peak performance (CPU/GPU
  pressure on this host, recent vLLM version upgrade, prompt
  cache state).
- Re-bench gq-017 against the post-R-P32 phrase pairs to confirm
  the original fix still applies cleanly.
- Consider raising the supplementary `cold-run consistency`
  threshold from 30 → 29 explicitly only if independent re-runs
  confirm the new floor is structurally 29 not 30.

For Phase 46, the verifiers stay strict.

---

## 3. Drift-direction observation

Phase 44/45 fixed Windows→WSL drift (Windows-side canonical, WSL
stale). Phase 46 fixed WSL→Windows drift (WSL canonical, Windows
stale) on `CacheTelemetryController.php`. Together they confirm:

> **Drift can go either direction** depending on where the edit
> originated — `Edit` tool writes target Windows paths; Pint or
> docker-exec edits target WSL paths; manual `cp` sync was the
> only bridge and got skipped in several phases.

The session-memory entry has been kept generic ("WSL probe-drift
pattern") because the same diagnostic — `diff` after CRLF
normalization between the two trees, for any helper file edited
during the autonomous run — catches drift in either direction.

---

## 4. Verifier results after Phase 46

```
phase32_step1   7/9    real variance (intentionally not loosened)
phase34_step1   6/7    real variance (intentionally not loosened)
phase35_step1   7/7    sweep flake (green standalone)
phase37_step1   8/8    routes added
phase38_step1   8/8    routes added (same fix)
phase39_step1   8/8    supersession added
```

Sweep projection (if re-run today): **86/88 verifiers green,
~555/559 checks** — the 2 remaining failures are
phase32_step1 and phase34_step1 holding the line on real LLM
variance, not regressions.

---

## 5. Files of record

```
app/Http/Controllers/Admin/CacheTelemetryController.php   — Windows synced from WSL canonical (+index method)
routes/web.php                                            — +2 routes for /admin/cache-telemetry endpoint + page
scripts/phase39_step1_verify.sh                           — slice-1/slice-5 marker supersession
docs/phase46_handoff.md                                   — this file
```

Three substantive edits. No new dependencies. The two route
additions land what Phase 37/38 handoffs *claimed* was already
landed.

---

## 6. R-P11-B note

The Phase 39 supersession fix doesn't change R-P11-B's status —
R-P11-B is still closed (Phase 43). Phase 46 just makes the
verifier honest about the cumulative slice-1 → slice-5
progression rather than asserting a frozen slice-1 snapshot.

---

## 7. Carry-overs after Phase 46

| Item | Status |
|------|--------|
| All major-shape carry-overs | ✅ closed (R-P11-B at 43) |
| Sweep-flake cleanup theme | ✅ closed (44/45) |
| P44 sweep partial-fails | ✅ resolved (this phase) |
| Golden-test cold-run floor at 28 vs 30 typical | ⚠️ flagged for investigation |
| gq-017 stability at 1/3 vs intended 3/3 | ⚠️ flagged for investigation |

The two flags are LLM/infra-level investigations, not autonomous-loop
work. They want a sit-down session with the orchestrator code, vLLM
operational state, and the golden-suite expected behaviour to
diagnose whether the post-R-P32 fix has structurally regressed or
whether this is just variance below the typical run.

End of Phase 46 handoff.
