# Master-plan §11 (DR + deployment + performance hardening) — Kickoff

**Doc-phase:** TBD on Kyle approval
**Status:** PROPOSAL — no code lands until Kyle signs off
**Predecessor:** `docs/master_plan_section11_scope_proposal.md` (doc-phase 96)
**Authored:** 2026-05-16, post-Phase-H4 handoff

---

## Why now

Phase H4 closed the operator-facing UI surfaces with full
production-readiness wiring (15/15 acceptance, 25/25 IT, audit
chain verifier, RLS proofs). §11 is the natural sequel — it
takes the same production-readiness pass to the **deployment**
layer rather than the application layer.

§11 is also the most autonomous-safe of the open scope proposals:
~80% ops/infra, zero frontend, no product-design judgment calls.
The §6 alternative (60% frontend) gates on Kyle for the visual
work; §11 doesn't.

---

## Reality-calibrated scope

Per the original scope proposal §11 was 11 sub-steps. After
inventorying what's already in `ops/runbooks/` + prior commits,
the open work is smaller than originally estimated:

### Already done (from prior runs)

| #     | Item                                          | Evidence                                           |
|-------|-----------------------------------------------|----------------------------------------------------|
| 11.4  | 5 DR runbooks                                 | `ops/runbooks/dr-{1..5}-*.md`                      |
| 11.5  | Tenant Isolation Auditor in CI                | 4 strict-pass commits (e69ce0e, fb780c0, ca95b7a, 88ad34c) + 7-gate auditor |
| 11.9  | Load test harness — starter                   | k6 starter in commit 34c818c                       |
| (partial) 11.10 | Cold-tier archival CODE                | `app/audit/cold_tier_archive.py` + 7 tests         |
| —     | Backup-restore runbook                        | `ops/runbooks/backup-restore.md`                   |
| —     | Container hardening runbook                   | `ops/runbooks/container-hardening.md`              |
| —     | Deploy-rollback runbook                       | `ops/runbooks/deploy-rollback.md`                  |

### Open — autonomous-safe (this kickoff covers these)

| #     | Item                                          | Estimated ticks |
|-------|-----------------------------------------------|-----------------|
| 11.1  | Per-store backup cron orchestration           | 2-3             |
| 11.2  | Cross-store consistency restore harness       | 2               |
| 11.3  | `restore_workspace` Hatchet workflow          | 1-2             |
| 11.9b | Load test harness — fuller battery vs §28     | 2               |
| 11.10 | Cold-tier archival — scheduled trigger + e2e  | 1               |
| 11.11 | Acceptance drill (full §11 done-criterion)    | 1-2             |
| **Total** |                                           | **9-12 ticks**  |

### Open — Kyle-gated (deferred to §11-v2)

| #     | Item                                          | Why deferred                          |
|-------|-----------------------------------------------|---------------------------------------|
| 11.6  | Single-tenant Helm chart                      | needs release-engineering judgment    |
| 11.7  | Self-host Kubernetes manifests                | needs target-cluster decisions        |
| 11.8  | Air-gapped bundle pipeline                    | signing-key custody is a Kyle call    |

§11-v2 can land after Kyle picks the K8s distro + signing approach.

---

## Sub-step detail (the 9-12 tick batch)

### §11.1 — Backup cron orchestration

**What ships:**

- One Hatchet cron workflow per store (Postgres, Neo4j, Qdrant, Redis, SeaweedFS) — runs on schedule, writes the snapshot to a configurable bucket
- A `backups.snapshot_runs` audit table tracking start/end/sha256/size
- Verification step that re-reads the snapshot manifest and asserts byte count

**Acceptance:**
- 5 cron workflows registered + visible in Hatchet dashboard
- Manual trigger of each produces a snapshot in the bucket within RTO
- `audit.audit_ledger` carries one `backup.<store>.snapshot.completed` row per run

**Risks:**
- Postgres pg_dump can hit pg_bouncer pool issues — must bypass the pooler (route through direct connection)
- SeaweedFS already-versioned — snapshot equivalent is bucket clone, not pg_dump-style

### §11.2 — Cross-store consistency restore harness

**What ships:**
- `tests/integration/test_restore_consistency.py` — pytest-marker `restore` (new marker) that:
  - drops a test workspace
  - restores it from snapshots
  - asserts: Postgres row count matches, every Qdrant vector resolves to a Postgres chunk, Neo4j node count consistent with PG entity count

**Acceptance:**
- Test runs against a synthetic 100-document corpus and passes inside 5 minutes
- Documented in `docs/RUNBOOK.md` for operator use

### §11.3 — `restore_workspace` Hatchet workflow

**What ships:**
- `src/fastapi/app/hatchet_workflows/restore_workspace.py` — graduated workflow (not skeleton) that:
  - Takes `workspace_id`, `snapshot_set_id`, `target_db_uri`
  - Sequences: PG restore → Neo4j restore → Qdrant restore → Redis warm → SeaweedFS metadata reconciliation
  - Emits `workspace.restore.*` audit rows at each step
  - Idempotent on re-run (uses snapshot_set_id as natural key)

**Acceptance:**
- Workflow registered + smoke-tested via `aio_mock_run`
- Cross-store consistency check (§11.2) passes after a `restore_workspace` invocation

### §11.9b — Load test fuller battery

**What ships:**
- Extend the existing k6 starter to cover the §28 SLOs:
  - p95 chat answer < 8s
  - p95 map tile fetch < 200ms
  - p95 report build (small) < 30s
  - 100 concurrent users with no 5xx
- Results captured to a markdown report committed alongside the runs

**Acceptance:**
- One full run on the dev stack lands the report in `docs/load_tests/`
- Each SLO has a labelled k6 threshold so failures are explicit

### §11.10 — Cold-tier archival scheduled trigger

**What ships:**
- Hatchet cron workflow at 02:00 UTC daily that calls the existing `archive_window` function with a 90-day cutoff
- `audit.audit_ledger.prune_archived_window` runs in a second step after the operator-confirmation gate
- New `/admin/cold-tier-status` admin endpoint (Tier 4) listing recent archive runs

**Acceptance:**
- Cron registered + visible in Hatchet
- Manual trigger produces a SeaweedFS object + verification audit row
- The admin endpoint shows the run in the inbox-style list pattern

### §11.11 — Acceptance drill

**What ships:**
- `scripts/section11_acceptance.sh` mirroring `phase_h4_acceptance.sh`:
  - Run a backup cron manually
  - Snapshot a workspace, restore it via `restore_workspace`, verify cross-store consistency
  - Run the k6 SLO battery
  - Trigger the cold-tier archival workflow
- Exit 0 = §11 (autonomous portion) done

**Acceptance:**
- Script runs end-to-end against the dev stack in <30 min
- All four sub-checks PASS

---

## Cadence proposal

Same overnight-batch model that worked for Phase H4:

| Wave | Focus                                                    | Time est |
|------|----------------------------------------------------------|----------|
| 1    | §11.1 backup crons (Postgres + Neo4j first)              | 60-90 min |
| 2    | §11.1 backup crons (Qdrant + Redis + SeaweedFS)          | 60-90 min |
| 3    | §11.10 cold-tier scheduled trigger + admin endpoint      | 60 min    |
| 4    | §11.3 `restore_workspace` workflow                       | 60-90 min |
| 5    | §11.2 cross-store consistency harness                    | 90-120 min|
| 6    | §11.9b load test fuller battery                          | 60-90 min |
| 7    | §11.11 acceptance script + final regression              | 45-60 min |
| 8    | Handoff doc                                              | 15-20 min |

Total estimate: **8-10 hours of focused work** spread across multiple
runs. Doable as one overnight (Phase H4 fit comparable scope in ~9 hours).

---

## Hard constraints (won't violate)

- **No SME-level decisions** — if a tick needs Kyle's judgment on
  retention/policy/signing-key custody, the tick stops and the
  question goes back to Kyle.
- **No release-engineering** — Helm/K8s/air-gapped bundle deferred.
- **Existing audit/hash-chain invariants preserved** — cold-tier
  archive must not break the chain; chain verifier must still PASS
  after each archive run.
- **No breaking schema changes** — additive only (new tables for
  `backups.snapshot_runs`, no alterations to existing).

---

## Locked decisions (Kyle, 2026-05-16)

All 4 open questions answered with the recommended defaults:

| Decision               | Value                                                                 |
|------------------------|-----------------------------------------------------------------------|
| Snapshot retention     | **30 days hot / 90 days warm / indefinite cold**                      |
| Cold-tier bucket       | **SeaweedFS bucket `audit-cold-tier`** (same instance as bronze)      |
| k6 SLOs                | **§28 defaults**: p95 chat <8s, p95 tile <200ms, p95 small report <30s, 100 concurrent users no 5xx |
| Cron stagger           | **15 min apart from 02:00 UTC** — PG@02:00, Neo4j@02:15, Qdrant@02:30, Redis@02:45, SeaweedFS@03:00 |

These are the working defaults for all §11-v1 sub-steps. Any
deviation requires a kickoff amendment + Kyle re-sign-off.

---

## Sign-off

If Kyle approves this kickoff as-written:

- [ ] §11-v1 = the 9-12 tick autonomous batch above
- [ ] §11-v2 = Helm + K8s + air-gapped bundle (later, with Kyle)
- [ ] First wave fires next autonomous run
- [ ] Acceptance script `scripts/section11_acceptance.sh` is the
      done-test (analogous to `phase_h4_acceptance.sh`)

If kicked back: I read whichever of the open questions blocks, redraft, re-pitch.
