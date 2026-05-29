# Master-plan §11 (DR + deployment topologies + performance hardening) — Scope Proposal

**Doc-phase 96** — seventh scope proposal.

---

## What §11 ships

"Production readiness across deployment patterns."

Master-plan Phase 11 deliverables (verbatim):
1. Backup strategy (§26.2) implemented across all stores
2. Cross-store consistency restore tested (§26.3)
3. Hatchet `restore_workspace` workflow
4. DR runbook for all five scenarios (§26.5)
5. Multi-tenant SaaS deployment hardened with Tenant Isolation
   Auditor in CI
6. Single-tenant cloud reference deployment (Helm chart)
7. Self-host Docker Compose + Kubernetes manifests
8. Air-gapped bundle build pipeline (signed update packages, offline
   public data bundles, offline tile bundles)
9. Performance load testing against §28 targets
10. Audit ledger cold-tier archival working

**Done test:** DR drill on staging restores a workspace cleanly
within RTO; air-gapped bundle build pipeline produces verifiable
signed package; load tests validate p95 targets.

---

## §11 is fundamentally ops/infra

Almost no application code. Mostly:
- Operator runbooks
- CI pipeline configs (Tenant Isolation Auditor)
- Helm chart + Kubernetes manifests
- Docker Compose hardening
- Signed-bundle build pipeline (Bash + GPG + tar)
- Load-test harness (k6 or Gatling against the running stack)
- Audit-ledger archival cron + cold-tier S3 sink

ONE Hatchet workflow (`restore_workspace`) is the only Python-side
deliverable. Five DR runbooks (markdown). One CI workflow file.

---

## Sub-step breakdown estimate

| # | What | Backend | Ops/Infra | Ticks |
|---|---|---|---|---|
| 11.1 | Per-store backup strategy doc + cron jobs (Postgres + Neo4j + Qdrant + Redis + SeaweedFS) | none | medium | 2-3 |
| 11.2 | Cross-store consistency restore harness | small | medium | 2 |
| 11.3 | `restore_workspace` Hatchet workflow | medium | small | 1-2 |
| 11.4 | 5 DR runbooks (Postgres loss, store divergence, ransomware, full datacenter, partial outage) | none | medium | 2 |
| 11.5 | Tenant Isolation Auditor in CI pipeline | small | medium | 1-2 |
| 11.6 | Single-tenant Helm chart | none | medium | 2 |
| 11.7 | Self-host Kubernetes manifests | none | medium | 1-2 |
| 11.8 | Air-gapped bundle pipeline (signed packages + offline public data + offline tiles) | none | heavy | 3-4 |
| 11.9 | Load test harness against §28 targets (k6 or Gatling) | none | medium | 2-3 |
| 11.10 | Audit ledger cold-tier archival + verification | small | medium | 1-2 |
| 11.11 | Acceptance: full DR drill + bundle build + load test pass | mixed | mixed | 1-2 |

**Total: 18-26 ticks.** Comparable to §6/§7/§9.

Almost ZERO frontend work. ~80% ops/infra; ~20% backend.

---

## V1.49 / current baseline overlap

What exists:
- **Phase 0** infra is in place: docker-compose dev cluster, Postgres
  base image, Neo4j, Qdrant, Redis, SeaweedFS.
- **Hatchet** for workflow scheduling — `restore_workspace` follows
  established pattern.
- **`audit_ledger`** + hash chain — §11.10 archival reads this directly.
- **GeoRAG architecture HTML** already documents deployment topology
  in §07 + §27 + §28.

What's net-new in §11:
- All cross-store backup orchestration (each store has its own
  backup tooling; §11 weaves them together).
- DR runbooks — pure docs but each is ~500-1000 lines of operational
  procedure.
- Helm chart + Kubernetes manifests.
- Air-gapped bundle pipeline — likely the highest-leverage and
  highest-risk piece (signed packages need release-engineering
  rigor).
- Load test harness — new tooling adoption (k6 or Gatling).

---

## Risks

1. **Cross-store restore consistency** — restoring Postgres + Neo4j
   + Qdrant from independent snapshots can leave referential
   inconsistencies (a chunk in Qdrant that's not in Postgres anymore).
   The `restore_workspace` workflow needs explicit consistency
   checks + repair steps. v1 = best-effort with reconciliation; v2
   = atomic snapshot guarantees via filesystem-level snapshotting
   (zfs/btrfs).
2. **Air-gapped bundle scope creep** — three sub-bundles (signed
   updates, offline public data, offline tiles) each have their own
   pipeline. Risk of "11.8 turns into its own phase." Mitigation:
   ship signed-updates first; offline-data + offline-tiles ship in
   §11-v2.
3. **DR drill scheduling** — needs Kyle to allocate a quarterly DR
   drill window. Without drills, runbooks rot.
4. **Load test target validation** — §28 targets may need adjustment
   based on actual production load shape. First load test runs
   should be exploratory, not pass/fail.

---

## Dependencies

- **k6 or Gatling** — pick one for load testing at §11.9 start.
- **Helm + kubectl** — operator tools, no Python dep.
- **GPG** for signing bundles — standard ops dep.
- **External S3-compatible cold storage** for §11.10 archival — could
  be SeaweedFS dedicated cold-tier bucket or external (Backblaze B2,
  Wasabi, etc.).

---

## Open questions for Kyle

1. **Load test tool choice**: k6 (Go-based, JS scripts, light) vs.
   Gatling (Scala, more features, heavier)? Recommend k6 for
   simplicity.
2. **Air-gapped bundle scope v1**: signed updates only (§11-v1), or
   all 3 bundles (signed + offline data + offline tiles)? Recommend
   signed-updates-only first.
3. **Cold-tier archival destination**: SeaweedFS dedicated bucket
   (single-stack) or external (Backblaze B2 etc., more durable)?
4. **DR drill cadence**: monthly, quarterly, or on-deployment-only?
   Recommend quarterly with extra drills before major releases.

---

## Recommendation

§11 is mostly NOT autonomous-safe — ops/infra work needs Kyle
review for deployment topology choices, runbook accuracy, load-test
target negotiation, and the air-gapped bundle pipeline (which
touches release engineering).

Autonomous-safe slice (smallest):
- **§11.3** `restore_workspace` Hatchet workflow skeleton
- **§11.10** audit ledger archival logic skeleton (the writer side)
- DR runbook scaffolds (markdown templates with "TODO: fill" sections)

That's ~3-4 ticks of scaffolding. After that, §11 needs Kyle's
operational pass.

---

## TL;DR

§11 = production hardening. 18-26 ticks; ~80% ops/infra; almost no
frontend. §11.3 + §11.10 skeletons are autonomous-safe; everything
else needs Kyle.

Recommended next: skip §11 from the autonomous run; do §12 scope
proposal next (smaller backend-only phase); circle back to §11 when
Kyle is available.
