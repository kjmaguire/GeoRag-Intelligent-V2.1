# Phase 1 Step 8 — `ingest_pdf` Cutover Runbook

**Document version:** 1.0
**Status:** Active runbook for the Hatchet `ingest_pdf` cutover window.
**Owner:** Platform on-call.
**Companions:**
- `docs/phase1_implementation_kickoff.md` — Phase 1 plan
- `docs/phase1_v149_ingest_pdf_survey.md` — Diff contract (§10)

---

## 1. Goal

Move 100% of PDF ingestion traffic from the v1.49 Dagster path to the
Hatchet `ingest_pdf` workflow with **zero regression** observable in
`silver.shadow_runs`. The cutover happens in two halves:

1. **Ramp window (~14 calendar days)** — `traffic_pct` walks 0 → 1 → 10
   → 50 → 100, gated on diff classifications.
2. **Cutover** — once 100% has held a 7-day clean streak, disable the
   v1.49 Dagster asset and remove the dual-write hook.

The ramp is conservative on purpose: 14 days lets us catch input shapes
that the smoke fixtures don't cover (large scanned reports, exotic
encodings, vendor-profile edge cases).

---

## 2. Ramp schedule

The schedule below is the **default cadence**. Each step requires the
previous step's gate (§4) to be green.

| Day | `ingest_pdf_hatchet_traffic_pct` | What's running |
|-----|----------------------------------|----------------|
| 0 (cutover start) | **0%** | v1.49 only. Dual-write code paths idle. |
| 1   | **1%**   | Dual-write fires for ~1% of uploads. Diff worker + dashboards exercise. |
| 4   | **10%**  | Wider sample. First "real" diff signal. |
| 8   | **50%**  | Half of all uploads dual-write. Full diff harness coverage. |
| 11  | **100%** | All uploads dual-write. Begin 7-day clean-streak count. |
| 18  | **Cutover** | If streak ≥ 7, disable v1.49. See §6. |

Calendar dates fill in at ramp-start time and live in the
[Hatchet workers + Shadow runs admin pages](#) (no separate calendar
artefact).

---

## 3. How to bump traffic

The traffic flag is `workspace.feature_flags.ingest_pdf_hatchet_traffic_pct`
(integer, 0–100). It's set per-workspace OR platform-wide
(`workspace_id IS NULL`).

**Preferred path: Shadow Runs dashboard**

1. Open `/admin/shadow-runs`.
2. Confirm the *Clean streak* tile and *24h counts* tile look healthy
   (see §4).
3. Set the new value in the **Platform default** row (or per-workspace
   override if you're staging a single workspace first).
4. The change is live immediately — `ShadowRouter` reads the flag on
   every upload.

**Backup path: psql**

```sql
INSERT INTO workspace.feature_flags
    (workspace_id, flag_name, int_value, updated_at)
VALUES (NULL, 'ingest_pdf_hatchet_traffic_pct', 10, now())
ON CONFLICT (workspace_id, flag_name) DO UPDATE
    SET int_value = EXCLUDED.int_value,
        updated_at = now();
```

(The constraint is `UNIQUE NULLS NOT DISTINCT` per the migration
forward-fix — a `NULL` `workspace_id` is a real conflict key, so the
UPSERT is safe for the platform default row.)

---

## 4. Daily check (5 minutes)

Run **once per UTC day** during the ramp window. The dashboard does the
heavy lifting; the checklist is a forcing function.

1. **Open `/admin/shadow-runs`.**
   - *24h counts* tile: confirm `clean` is the dominant bucket.
   - *Clean streak (days)* tile: green when ≥ 1; red signal if it just
     reset to 0.
   - Skim the most recent 20 rows — any non-`clean` row gets a
     drill-down click (Show page).

2. **Open `/admin/hatchet-workers`.**
   - Both `georag-hatchet-worker-ingestion` and
     `georag-hatchet-worker-ai` show **Live ≥ 1**.
   - *Last 24h workflow runs*: `ingest_pdf` and `shadow_diff` rows
     present; `Failed` column ≤ 5% of `Succeeded`. `shadow_diff_scan`
     fires every minute (1440 succeeded/day expected).

3. **Confirm v1.49 is still healthy.** Dagster asset
   `silver_reports` should still produce silver rows for the same input
   keys; this is what makes the dual-write a *shadow* and not a
   replacement.

4. **Log the check.** A one-line note in the on-call channel:
   `2026-05-12 ramp@10% clean=98.2% streak=2d` is enough.

If any item fails, follow §5 (pause / rollback).

---

## 5. Pause / rollback criteria

Set `traffic_pct` back to the prior step (or 0) immediately if any of
the following fires:

| Trigger | Action |
|---|---|
| Any **`fatal`** classification in the last hour | **Hard stop** — set traffic_pct to 0; open an incident; preserve the row(s) for diff inspection. |
| **`divergent`** rate > 5% over a rolling 24h | Roll back one step (e.g. 50% → 10%); investigate before resuming. |
| `shadow_diff_scan` not running (no row in last 5 minutes) | Restart `georag-hatchet-worker-ai`; verify scan resumes; do **not** advance ramp until it has. |
| `silver_reports` Dagster asset fails ≥ 3× consecutively | Pause the ramp at the current step; investigate. v1.49 outage during shadow ⇒ shadow_runs partial pile-up but no user impact. |
| Hatchet engine unreachable (`/admin/hatchet-workers` shows 0 live workers) | Roll back to 0%; engine outage means dual-write would silently fail. v1.49 path keeps serving. |

The rollback path is identical to §3 — just write a smaller (or 0)
value to `ingest_pdf_hatchet_traffic_pct`. There's no schema migration,
no service restart.

`ingest_pdf_shadow_enabled = false` is the **master kill switch**:

```sql
UPDATE workspace.feature_flags
   SET bool_value = false, updated_at = now()
 WHERE workspace_id IS NULL
   AND flag_name = 'ingest_pdf_shadow_enabled';
```

This bypasses the per-workspace + traffic_pct logic entirely; every
upload goes through v1.49 only. Use this if you need to disable the
shadow path before the dashboard responds.

---

## 6. Cutover gate (Day 18-ish)

The cutover is permitted **only when ALL of the following are true:**

1. `ingest_pdf_hatchet_traffic_pct = 100` (platform default).
2. The *Clean streak* tile reads **≥ 7 days** in `/admin/shadow-runs`.
3. No `fatal` classification in the last 30 days.
4. No `divergent` row in the last 7 days that wasn't explained + closed.
5. Both worker pools have been Live for the entire 7-day window
   (no rolling restarts inside the streak — restarts reset the streak
   if a `partial` row hangs).

The dashboard's clean-streak query is the canonical source of truth:

```sql
WITH days AS (
    SELECT date_trunc('day', started_at) AS day,
           bool_or(classification IN ('minor','divergent','fatal','partial'))
               AS has_non_clean
    FROM silver.shadow_runs
    WHERE workflow_kind = 'ingest_pdf'
      AND started_at >= now() - interval '30 days'
    GROUP BY 1
    ORDER BY 1 DESC
)
SELECT count(*) AS streak_days
FROM (
    SELECT day, has_non_clean,
           sum(has_non_clean::int) OVER (ORDER BY day DESC) AS bad_so_far
    FROM days
) s
WHERE bad_so_far = 0;
```

If the gate is green, proceed to §7.

---

## 7. Cutover steps

These run in order; each is reversible until step 4.

1. **Disable the v1.49 Dagster asset auto-trigger.**
   In the Dagster UI (or via `dagster asset wipe` for the bronze
   sensor): turn off the bronze-upload sensor's `silver_reports` job
   trigger. New uploads no longer auto-fire v1.49.

   *Reversible:* re-enable the sensor.

2. **Watch for 24 hours at single-write Hatchet.**
   New uploads now run only through Hatchet `ingest_pdf`. The
   v1.49 Dagster hook in `src/dagster/georag_dagster/assets/silver_reports.py`
   becomes dormant (no `silver_reports` materializations means the
   hook never fires).

   `silver.shadow_runs` rows will pile up as `partial` because v1.49
   no longer writes back. **Expected.** The diff scanner classifies
   them as `fatal` after 24h via the `started_at < now() - 24h` rule
   in `shadow_diff_scan` — also expected, those are the orphans of the
   previous shadow regime, not regressions.

3. **Drop dual-write from `UploadController`.**
   Edit `app/Http/Controllers/Api/V1/UploadController.php` and remove
   the `dispatchShadowIfPdf` call (or set
   `ingest_pdf_shadow_enabled = false` permanently to keep the code
   path but mute it).

   *Reversible:* re-add the call.

4. **Remove the v1.49 hook + retire the survey doc.**
   - Delete `src/dagster/georag_dagster/hooks/shadow_v149.py` and the
     hook invocation block in `silver_reports.py`.
   - Mark `docs/phase1_v149_ingest_pdf_survey.md` as
     `Status: ARCHIVED — superseded by Phase 1 cutover 2026-MM-DD`.
   - Optionally: drop `silver.shadow_runs` once the team's content
     team has finished any post-mortem queries against historical
     rows. Keep the table for at least 30 days post-cutover.

   *Not reversible* — these are the "remove the scaffolding" steps.

5. **Update CLAUDE.md** under §"Don't duplicate orchestration":
   add `ingest_pdf` to the list of workflows owned by Hatchet (it's
   already on the Hatchet side; this is to record that v1.49 is no
   longer a parallel option).

---

## 8. Open questions / non-blocking work

These are *known* and don't gate cutover, but are worth tracking:

- **Audit action_type asymmetry.** Today the Hatchet path emits
  `ingest_pdf.parse.complete` but not `silver.reports.write`. The
  classifier flags this as `divergent` even when v1.49 + Hatchet
  produce identical JSON. Until both sides emit both types, expect
  the dominant classification to be `divergent` not `clean`. A pre-
  cutover task is to add a `silver.reports.write` audit emission on
  both sides; that flips the dominant classification to `clean` and
  unblocks the streak gate.

- **Token-Jaccard section similarity.** The classifier uses Jaccard
  on word tokens, not SBERT cosine (see
  `app/services/shadow_diff/classifier.py` module docstring for the
  reasoning). If `minor` rates trend higher than expected, this is the
  first lever — promote to SBERT.

- **Outbox propagation parity.** Phase 1 treats outbox count diffs as
  `minor` only; Phase 2 promotes to `divergent`. Don't tune outbox
  emission to match v1.49 just to get clean rows — Phase 2's outbox
  contract supersedes v1.49.

- **Dashboard-side traffic-flag history.** Today the dashboard shows
  the *current* value; we don't keep a per-flag audit trail. Step 9's
  Phase 2 handoff captures this as a Phase 2 hardening task.

---

## 9. Reference: dashboards + scripts

| Surface | Purpose |
|---|---|
| `/admin/shadow-runs` | Per-row classifications + traffic-pct editor + clean-streak tile |
| `/admin/shadow-runs/{id}` | Per-row diff drilldown (per-field check outcomes + raw payloads) |
| `/admin/hatchet-workers` | Engine-side worker liveness + recent-run rollup |
| `/admin/workflow-runs` | All orchestrator runs (Phase 0 dashboard — broader scope) |
| `scripts/phase1_step5b_smoke.sh` | End-to-end shadow + diff smoke (run before bumping traffic) |
| `scripts/phase1_step6_verify.sh` | Shadow dashboard done-definition check |
| `scripts/phase1_step7_verify.sh` | Worker dashboard done-definition check |
| `scripts/phase1_step8_traffic.sh` | Helper to set platform-default traffic_pct from CLI (see §10) |

---

## 10. CLI helper — `phase1_step8_traffic.sh`

A small wrapper around the UPSERT in §3 for ramp-day automation. See
`scripts/phase1_step8_traffic.sh`. Usage:

```bash
# Read current value
scripts/phase1_step8_traffic.sh get

# Set platform-default to 10
scripts/phase1_step8_traffic.sh set 10

# Disable shadow path entirely (kill switch)
scripts/phase1_step8_traffic.sh disable

# Re-enable after disable
scripts/phase1_step8_traffic.sh enable
```

The helper writes through psql (admin role) so it works even if the
Octane container is down.

---

End of cutover runbook.
