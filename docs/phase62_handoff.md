# Phase 62 Handoff — Verifier cascade O(N²) → O(1) fix

**Document version:** 1.0
**Status:** Doc-phase 62 complete. Doc-phase 63 inheriting.
**Predecessors:** `docs/phase61_handoff.md` §5.3, `docs/phase60_handoff.md` §5.1.

A focused tooling tick. No master-plan §3 step progresses this tick;
instead the verifier infrastructure that gates every prior step gets
restructured so future ticks don't pay the cumulative cost of running
every prior cascade.

**Result: cascade time 20+ min → 1.45 sec warm, 2m 45s cold.** Roughly
800× speedup on warm runs. The remaining warm runtime is just the
doc-phase-specific checks (route registrations, file existence,
artisan reflection); cascade contribution is now negligible.

---

## 1. What doc-phase 62 delivered

### Manifest helper (~100 lines)

`scripts/_verifier_manifest.sh` — sourced by every phase3 verifier.
Two functions:

- `mark_verifier_passed <step>` — writes `{passed_at, git_sha}` entry
  to `.verifier-state/cascade-passes.json`. Atomic via `.tmp + os.replace`.
  Python-based read-modify-write (no jq dependency).

- `check_verifier_recent <step>` — returns 0 if manifest entry exists,
  is within `MANIFEST_TTL_SEC` (default 1 hour), and `git_sha` matches
  current HEAD (or current SHA unknown — accept the entry to avoid
  blocking in non-git environments).

### 13 verifiers updated

Each existing phase3 verifier (step1, step2, step3, step4, step5,
step6, step7a, step7b, step7c, step8a, step8b, step8c, step8d) now:
- Sources `_verifier_manifest.sh` near the top
- Replaces its cascade loop's `bash $prior_verifier` invocation with
  `check_verifier_recent → bash $prior_verifier` fallback
- Calls `mark_verifier_passed "stepN"` at the end on success
  (`FAIL == 0`)

### Cascade loop shape (uniform across 5+ verifiers)

```bash
for step in 1 2 3 4 5 6 7a 7b 7c; do
    if check_verifier_recent "step${step}"; then
        note "[step${step}] PASS — manifest recent (skip re-run)"
    elif bash "$SCRIPT_DIR/phase3_master_plan_step${step}_verify.sh" >/dev/null 2>&1; then
        note "[step${step}] PASS — verifier re-run green"
    else
        note "[step${step}] FAIL — verifier regressed"
        FAIL=$((FAIL + 1))
    fi
done
```

### Bug fixes uncovered en route

While running the manifest-aware cascade for the first time, three
real bugs surfaced in steps 8a/8c/8d that the manifest exposed:

1. **`php artisan route:list --path=X` truncates output when no TTY.**
   Route names got cut to `admin.ingestion-review.…` mid-string;
   grep for the full name failed. Fixed by switching to
   `--json` output + Python json parsing. The truncation only
   affected non-TTY invocations from scripts; manual runs at the
   prompt rendered the full names because the terminal was wider.

2. **`pipefail` false-positive on docker exec.** Even when the inner
   command returned non-zero, the pipe's grep ran against partial
   output. Hard to reason about. Mitigated as a side-effect of (1):
   we now capture command substitution → variable → grep against
   string, no pipe involved.

3. **No `.gitignore` entry for `.verifier-state/`.** Added.

---

## 2. Files of record

### New
- `scripts/_verifier_manifest.sh` (~110 lines)
- `.verifier-state/` directory (gitignored; created at first run)
- `.gitignore` updated

### Modified (13 verifiers)
- `scripts/phase3_master_plan_step1_verify.sh`
- `scripts/phase3_master_plan_step2_verify.sh`
- `scripts/phase3_master_plan_step3_verify.sh`
- `scripts/phase3_master_plan_step4_verify.sh`
- `scripts/phase3_master_plan_step5_verify.sh`
- `scripts/phase3_master_plan_step6_verify.sh`
- `scripts/phase3_master_plan_step7a_verify.sh`
- `scripts/phase3_master_plan_step7b_verify.sh`
- `scripts/phase3_master_plan_step7c_verify.sh`
- `scripts/phase3_master_plan_step8a_verify.sh` — also fixed route check
- `scripts/phase3_master_plan_step8b_verify.sh`
- `scripts/phase3_master_plan_step8c_verify.sh` — also fixed route check (×2)
- `scripts/phase3_master_plan_step8d_verify.sh` — also fixed route check

---

## 3. Verifier status

Doc-phase 62 doesn't have its own verifier — the fix is structural,
and every subsequent doc-phase tick validates it implicitly by
running fast. Empirical proof points:

| Run | Time | Verdict |
|---|---|---|
| Cold (no manifest) — step8d cascade | 2m 45s | 16/16 PASS |
| Warm (manifest fresh) — step8a cascade | 1.22 sec | 13/13 PASS |
| Warm — step8b cascade | 34 sec (most of which is doc-phase-specific pytest, not cascade) | 16/16 PASS |
| Warm — step8c cascade | 1.25 sec | 16/16 PASS |
| Warm — step8d cascade | 1.45 sec | 16/16 PASS |

Cold runs still re-run every prior verifier (no entries in manifest);
warm runs skip them. Across multiple ticks landing same-day, the
warm path dominates.

Manifest contents after the cold run:

```json
{
  "step1": {"git_sha": "...", "passed_at": "2026-05-13T05:..."},
  ...
  "step8d": {"git_sha": "...", "passed_at": "2026-05-13T05:..."}
}
```

13 entries; one per verifier.

---

## 4. Decisions made in this phase

### 4.1 Python-based manifest read-modify-write, not jq

`jq` is not guaranteed present on every machine that runs the
verifiers (CI base images, fresh checkouts, the WSL canonical tree
historically). Python 3 IS guaranteed because the entire OCR pipeline
depends on it. Using Python keeps the helper self-contained.

### 4.2 1-hour TTL by default

Conservative. Long enough that a single working session reuses the
manifest entries; short enough that "I ran tests this morning, then
went to a meeting, then came back and tried to ship" still re-runs
the cascade after a long lunch.

Operators can override via `MANIFEST_TTL_SEC=` env if they want a
faster expiry (e.g. `MANIFEST_TTL_SEC=300` for 5-minute caching
during active dev).

### 4.3 git_sha invalidation belt-and-suspenders

Even within the 1-hour TTL, the cascade re-runs prior verifiers if
the current git HEAD has moved since the verifier last passed.
Prevents the "I committed a change to parse_native but doc-phase 62
manifest still says step3 is green" scenario.

If git isn't available (detached state, no .git dir, etc.) the
check degrades to age-only. Better to accept-and-skip than block
work in non-git environments.

### 4.4 Manifest committed? NO

`.verifier-state/` is gitignored. Committing the manifest would:
- Generate noise in every PR (every cascade run modifies it)
- Create merge conflicts between branches
- Make CI's "fresh checkout always cold-runs" the right semantic
  (no stale manifest from another branch)

Local-only manifest is the right scope.

### 4.5 No central master-sweep script

Existing project pattern in doc-phases 1-48 has master-sweep scripts
(`phase{N}_master_sweep.sh`) that run all prior verifiers. Doc-phase
62 doesn't add one because the existing per-step verifiers cascade
themselves — `bash phase3_master_plan_step8d_verify.sh` IS the
master sweep for §3 (it cascades 1-8c plus its own checks).

If desired later, a thin `phase3_master_sweep.sh` is trivial to add
— just `bash scripts/phase3_master_plan_step8d_verify.sh`.

### 4.6 `php artisan route:list --json` over `--path` text output

Discovered during this tick: text output truncates when stdout is
not a TTY. JSON is stable across TTY/non-TTY. The new pattern is:

```bash
ROUTES_JSON=$(docker exec "$LARAVEL_CONTAINER" php artisan route:list --json --path=X 2>/dev/null || echo '[]')
if echo "$ROUTES_JSON" | python3 -c "
import json, sys
sys.exit(0 if 'route.name' in [r.get('name') for r in json.loads(sys.stdin.read() or '[]')] else 1
)"; then ...
```

Already applied in step8a/8c/8d. Future verifiers should use the
same pattern.

---

## 5. Findings carried over to doc-phase 63+

### 5.1 Doc-phase 63 inherits the fast cascade

The first tick to benefit from doc-phase 62's work. Should feel
materially nicer to develop — verifier runs are sub-second instead
of multi-minute.

### 5.2 Some warm runs aren't instant (step8b: 34 sec)

step8b runs `pytest tests/test_ocr_render_endpoint.py` as one of its
checks — that's the doc-phase 59 endpoint pytest with PaddleOCR
model cache + S3 upload + render. 30 sec is the legitimate own-work
cost, not cascade overhead. Cascade itself is sub-second within
that run.

Future optimization (not in scope here): cache pytest results per
test file using a similar manifest pattern. Probably overkill —
pytest is the source of truth and re-running it is correct.

### 5.3 `_verifier_manifest.sh` could be reused by Phase 1-48 verifiers

The existing project has dozens of phase-tagged verifiers from
prior doc-phases (1-48) under the same scripts/ directory. They all
predate this fix. Retrofitting them would speed up the master sweeps
that other workflows rely on.

Out of scope here, but worth a separate cleanup tick. Pattern: same
source + same mark_verifier_passed call at the end.

### 5.4 No automated alarm on manifest staleness

If a verifier passes today, the manifest entry persists. If the
underlying code regresses tomorrow (in a way that doesn't touch
git_sha because of `.gitignore` patterns, etc.), the cascade
silently skips.

Mitigations in current design:
- 1-hour TTL forces a re-run every hour anyway
- git_sha check forces re-run on any commit

So the failure mode is narrow: "code regressed within the last hour
AND no commit has been made since the manifest entry was written."
Acceptable risk for the speedup gain.

---

## 6. Pre-existing carry-overs (unchanged this phase)

All carry-overs from doc-phases 49-61 remain. The cascade fix doesn't
address any of them — just makes them faster to surface in future
verifier runs.

---

## 7. What doc-phase 63 will do

Per doc-phase 61 handoff §7, the choice is between:

- **Option A**: Step 8 closeout — re-OCR Hatchet workflow + Reverb
  broadcast + audit log emission. Operational polish.
- **Option B**: Step 9 — 50-PDF acceptance corpus harness. The §3
  done gate.

Doc-phase 62's tooling fix removes a previous blocker (cascade time)
for either option. Recommend Option A first per the doc-phase 61
analysis — closing Step 8 cleanly before validating §04p against
real PDFs makes the corpus pass actionable rather than informative.

---

## 8. Master-plan §3 progress

Unchanged from doc-phase 61:

| Step | Status |
|---|---|
| 1-7c, 8a-8d | ✅ DONE |
| 7d (shadow comparison) | deferred |
| 8e (re-OCR + Reverb + audit) | next option A |
| 9 (acceptance corpus) | next option B |
| 10 (RAGFlow retirement) | pending |

---

End of doc-phase 62 handoff. The cascade is fast. Future ticks land
without waiting.
