# Phase 45 Handoff — broader Windows↔WSL drift sweep

**Document version:** 1.0
**Status:** Phase 45 complete. Six diverging check.php scripts re-synced; six missing verifiers/probes/sweeps re-synced; one missing seaweedfs probe re-synced; phase1_step6 verifier given full supersession treatment for the Phase 4 ShadowRunsController removal.
**Predecessors:** `docs/phase44_handoff.md`. R-P11-B is closed; this phase continues the cleanup theme that Phase 44 opened.

---

## 1. What Phase 45 delivered

Phase 44 fixed 6 stale WSL probe scripts. Phase 45 follows up
with a systematic Windows↔WSL audit of the whole `scripts/`
directory and addresses three follow-on classes of drift:

### 1a. Diverging check.php files (6 files)

Same root cause as Phase 44's probe drift: Windows-side already
had the fully-qualified `\Illuminate\Contracts\Console\Kernel::class`
form at the boot line, WSL still had stale `Kernel::class`
relying on a `use` statement placed after the call.

| File | Re-synced |
|------|----------:|
| `scripts/_phase1_step6_check.php` | ✅ |
| `scripts/_phase1_step7_check.php` | ✅ |
| `scripts/_phase2_rp26_check.php` | ✅ |
| `scripts/_phase2_step6_check.php` | ✅ (was missing in WSL entirely) |
| `scripts/_phase3_step6_check.php` | ✅ |
| `scripts/_phase4_step2_check.php` | ✅ |

### 1b. Missing scripts in WSL (8 files)

These existed in the Windows tree but had never been copied into
the WSL tree. The early-phase verifiers and the phase41 master
sweep were the most consequential:

| File | Re-synced |
|------|----------:|
| `scripts/phase1_step4_verify.sh` | ✅ |
| `scripts/phase1_step5a_smoke.sh` | ✅ |
| `scripts/phase1_step5b_smoke.sh` | ✅ |
| `scripts/phase1_step5b_verify.sh` | ✅ |
| `scripts/phase1_step6_verify.sh` | ✅ |
| `scripts/phase2_rp28_backups_verify.sh` | ✅ |
| `scripts/phase2_step6_verify.sh` | ✅ |
| `scripts/phase41_master_sweep.sh` | ✅ |
| `scripts/_seaweedfs_s3_probe.py` | ✅ |

(One-off sync helpers like `_p17_cold_run.sh`, `_p18_sync*.sh`,
`_p1X_sync.sh`, `_p3X_sync.sh` were intentionally NOT synced —
they're session-local Windows-only helpers, not load-bearing
verifier infrastructure.)

### 1c. phase1_step6 supersession (Phase 4 ShadowRunsController removal)

After the bulk sync the verifier ran 1/5 → standalone diagnosis
found three real check failures rooted in Phase 4's deliberate
removal of `ShadowRunsController`, its routes, its
`Pages/Admin/ShadowRuns/{Index,Show}.tsx`, and `silver.shadow_runs`.

Updated `scripts/phase1_step6_verify.sh` with supersession-tolerant
checks for all four: either the historical surface is present, or
the post-Phase-4 removal is confirmed (controller class missing,
zero shadow-runs routes registered, TSX files absent, table not
in `information_schema.tables`).

A secondary fix: the verifier's `grep 'controller_class='` was
matching Laravel's stack-trace source-code excerpt (which prints
the offending PHP line) rather than the intended check.php
output line. Anchored the three relevant greps to `^…=` so the
trace's indented source-excerpt can't false-positive.

Final state: **phase1_step6 = 5/5 standalone.**

---

## 2. Audit methodology

```bash
for f in /mnt/c/Users/GeoRAG/Herd/georag/scripts/*.sh \
         /mnt/c/Users/GeoRAG/Herd/georag/scripts/*.py \
         /mnt/c/Users/GeoRAG/Herd/georag/scripts/*.php; do
    base=$(basename "$f")
    wsl=/home/georag/projects/georag/scripts/"$base"
    if [ ! -f "$wsl" ]; then
        echo "MISSING IN WSL: $base"
    elif ! diff <(tr -d '\r' < "$f") <(tr -d '\r' < "$wsl") > /dev/null 2>&1; then
        echo "DIFFERS: $base"
    fi
done
```

CRLF-normalized diff to avoid false positives from line-ending
drift. Surfaced 30 missing + 6 diverging files. After filtering
out 21 one-off session-local sync helpers, 15 files (6 diverging
+ 9 missing) warranted re-sync.

---

## 3. Why some files were missing in WSL

The pre-Phase-18 development cadence used a different sync
discipline: files were created Windows-side first and copied to
WSL only when needed for a verifier run. Some early-phase
verifiers (phase1_step{4,5a,5b,6}, phase2_step6) were created and
exercised once during their phase but never re-copied. Once those
phases closed, no later step touched the WSL copy, so a refresh
would only have happened if a sweep ran them — which the sweep
loop wouldn't try because they weren't in the awk-driven verifier
list from `phase19_master_sweep.sh`.

`phase41_master_sweep.sh` was created during Phase 41 but I
intentionally deferred launching it because the Phase 40 sweep
was still mid-flight. The sync got forgotten in the handover.

---

## 4. Verifier results after Phase 45

Each affected verifier re-checked standalone (the ones that don't
require the in-flight P44 sweep's docker-exec slot):

```
phase1_step6_verify.sh         5/5
phase1_step7_verify.sh         6/6
phase3_step6_verify.sh         6/6
phase4_step2_verify.sh         8/8
phase4_step2 (post probe sync) 8/8
phase8_step2 (P44)             5/5
phase9_step2 (P44)             6/6
phase10_step1 (P44)            6/6
phase10_step3 (P44)            7/7
phase12_step4 (P44)            8/8
phase14_step2 (P44)            6/6
```

`_phase2_rp26_check.php` was synced but doesn't have a
corresponding `phase2_rp26_verify.sh` — it's an inline check
called from another verifier. No standalone test.

`phase2_step6_verify.sh` and `phase2_rp28_backups_verify.sh` were
synced but not re-run in this phase to avoid colliding with the
in-flight P44 sweep.

---

## 5. Files of record

```
scripts/phase1_step6_verify.sh                  — supersession update (3 checks + grep anchoring)
scripts/_phase1_step6_check.php                 — re-synced from Windows
scripts/_phase1_step7_check.php                 — re-synced
scripts/_phase2_rp26_check.php                  — re-synced
scripts/_phase2_step6_check.php                 — re-synced (was missing)
scripts/_phase3_step6_check.php                 — re-synced
scripts/_phase4_step2_check.php                 — re-synced
scripts/phase1_step4_verify.sh                  — re-synced (was missing)
scripts/phase1_step5a_smoke.sh                  — re-synced (was missing)
scripts/phase1_step5b_smoke.sh                  — re-synced (was missing)
scripts/phase1_step5b_verify.sh                 — re-synced (was missing)
scripts/phase1_step6_verify.sh                  — (same file as above)
scripts/phase2_rp28_backups_verify.sh           — re-synced (was missing)
scripts/phase2_step6_verify.sh                  — re-synced (was missing)
scripts/phase41_master_sweep.sh                 — re-synced (was missing)
scripts/_seaweedfs_s3_probe.py                  — re-synced (was missing)
docs/phase45_handoff.md                         — this file
```

15 sync operations + 1 supersession edit. No app code, no tests,
no schema changes.

---

## 6. Sweep status

No new master sweep launched. Per the agreed cadence: the in-flight
clean P44 sweep continues; Phase 45 contributes to its eventual
greenness without firing a competing P45 sweep. When P44 reports
final, a single consolidated sweep can be launched in a later
session.

---

## 7. Carry-over status after Phase 45

- ✅ R-P11-B closed (Phase 43)
- ✅ R-P21 closed (Phase 38)
- ✅ R-P15-1 closed (Phase 36)
- ✅ R-P32 closed (Phase 32)
- ✅ phase9_step1 docker-network (Phase 44)
- ✅ 6 stale WSL probe scripts (Phase 44)
- ✅ 6 diverging check.php files (Phase 45)
- ✅ 8 missing WSL scripts including phase41_master_sweep (Phase 45)
- ✅ phase1_step6 supersession + grep anchoring (Phase 45)

Major-shape backlog and the cleanup theme are both at zero.

---

## 8. Pattern reinforcement

Phase 44 documented the **WSL probe-drift pattern**: Windows-side
edits not synced to WSL look like sweep flakes. Phase 45 extends
this: the drift wasn't limited to probes. Any `.php`, `.sh`, or
`.py` helper script edited Windows-side and not synced to WSL
behaves identically. The audit methodology in §2 is the
diagnostic going forward — run before launching any future sweep
where ad-hoc Windows edits have happened since the last sync.

End of Phase 45 handoff.
