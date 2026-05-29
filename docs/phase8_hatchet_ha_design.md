# Hatchet engine HA — design exploration

**Phase 8 Step 4 (R-P3-6 scoping).** This document scopes what
production-grade high availability for Hatchet would look like.
It does NOT ship clustering — that's a Phase 9+ decision.
Read this before reopening R-P3-6 to avoid relitigating the
trade-offs.

---

## 1. Current posture — single instance

The `docker-compose.yml` `hatchet-lite` service (lines 1695-1730)
runs a single `hatchet-lite` image bundling engine + admin + dashboard
+ DB migration. Workers connect via `HATCHET_CLIENT_HOST_PORT` (default
`hatchet-lite:7077` over gRPC). Queue + repository state lives in the
shared `hatchet` Postgres database
(`SERVER_MSGQUEUE_KIND=postgres`).

### Failure modes today

| Failure | Blast radius |
|--------|--------------|
| `hatchet-lite` OOMs / crashes | All workflow execution halts. Workers stay running but their gRPC stream errors out; they reconnect on container recovery. Cron triggers are missed for the duration. |
| `hatchet-lite` host kernel panic / reboot | Same as crash, plus any in-flight task results not yet `xack`-ed in Postgres are re-run when workers reconnect (Hatchet's at-least-once semantics handle this). |
| Postgres outage | Hatchet engine can't read/write queue tables; same as a Hatchet crash from the caller's perspective. |
| Bad config push | Engine fails healthcheck → restart loop → all execution stalls. No partial rollout. |

Recovery is fast on hot-restart (`docker compose restart`) — usually
sub-30s. The pain is in (a) the missed-cron window and (b) the lack
of a maintenance lever (no canary, no rolling restart).

### V1 acceptance posture

For a single-tenant deploy on one host, this is fine. Cron skew of
~30s a few times a year is operationally acceptable when the
alternative is multi-node clustering complexity. R-P3-6 has been
deferred FIVE times across Phases 3-7 for this reason — it's not
that we don't see the gap, it's that we never had a forcing function.

The forcing function would be:
- A second customer who needs an SLA we can't honour with single-instance.
- A workflow class whose missed-cron cost is high (audit-ledger
  hash-chain skew? per-tenant rate-limit reset drift?).
- Multi-tenant SaaS posture (each tenant expects "platform"-level
  availability, not "self-hosted dev tool" availability).

---

## 2. Multi-instance Hatchet engine

Hatchet's design supports horizontal scale of the engine. Two-plus
engine instances share:

- The `hatchet` Postgres DB (queue + repository state) — already
  shared, no change needed beyond connection-pool sizing.
- The `WorkflowTriggerCronRef` table (cron schedules) — the ticker
  has built-in leader election; only one ticker runs at a time.

What changes:

| Component | Single-instance | Multi-instance |
|-----------|-----------------|-----------------|
| gRPC clients (workers) | One target host:port | LB target (DNS round-robin OR explicit `HATCHET_CLIENT_HOST_PORT_LIST`) |
| Web admin / REST API | `:8889` direct | Behind an HTTP LB (could reuse `caddy`) |
| Ticker / scheduler | Built-in | Built-in, leader-elected via Postgres advisory lock |
| `SERVER_GRPC_BROADCAST_ADDRESS` | `localhost:7077` | LB hostname:port |
| Health checks | One container's `/api/ready` | Per-instance, aggregated |

The official Hatchet docs recommend a TCP load balancer in front of
gRPC (Envoy / HAProxy in TCP mode) — HTTP/2 LB also works if your
edge is HTTP-aware. `nginx` works for gRPC if compiled with
`--with-http_v2_module` (most modern builds are).

---

## 3. Worker-side adaptation

The Python `hatchet_sdk.Client` accepts a single `host_port`. To
target multiple engines, the cleanest path is to put a TCP LB
container between workers and engines:

```
                 ┌──────────────────┐
                 │ TCP LB (HAProxy) │  :7077
                 └────┬─────────┬───┘
              ┌───────┘         └───────┐
        ┌─────▼─────┐             ┌─────▼─────┐
        │ hatchet-1 │             │ hatchet-2 │
        └───────────┘             └───────────┘
              ▲                         ▲
              │  shared `hatchet` PG    │
              └─────────────────────────┘
```

Workers keep `HATCHET_CLIENT_HOST_PORT=hatchet-lb:7077`. The LB
distributes connections; engines coordinate via Postgres so any
engine can serve any worker's stream.

DNS round-robin (no LB container, just `hatchet-1`+`hatchet-2`
registered as `hatchet:7077`) works for cold start but doesn't
re-balance during a restart of a single engine.

---

## 4. State-loss boundaries

Hatchet's at-least-once delivery means in-flight tasks are safe in
the durable-state sense — a crashed engine's in-progress tasks get
re-dispatched when another engine picks them up. But a few subtle
corners:

| Concern | Single-instance behaviour | Multi-instance behaviour |
|---------|---------------------------|--------------------------|
| Cron miss window | Up to restart-duration | ~0 (other ticker takes over) |
| Long-running task checkpoint | None — re-runs from start on engine restart | Same — Hatchet has no built-in checkpointing |
| Worker → engine stream churn | Worker reconnects after engine restart | Worker re-routes to next available engine in seconds |
| `WorkflowTriggerCronRef` race on rapid registration | Single writer, no race | UNIQUE constraint on `(parentId, cron, name)` already prevents dup rows; first writer wins |

Checkpointing is the big asterisk. If we want "engine restart =
zero task work lost" semantics, the workflow task itself has to
implement intermediate progress writes. Hatchet doesn't give us that
for free.

---

## 5. Operational ask

| Item | Single-instance | Multi-instance ask |
|------|-----------------|---------------------|
| TLS on inter-engine comms | N/A | Postgres is the only shared channel; pg_sslmode=require + a TCP LB with TLS termination |
| Postgres replica | Shared with everything | Optional; Hatchet doesn't itself need a replica, but a primary outage takes everything down — same blast radius as today |
| Backup story | Daily pg_dump of `hatchet` DB | Same — Hatchet doesn't add storage |
| Monitoring | One container, one healthcheck | N containers, aggregated via Prometheus (existing OTel collector → Prom is fine) |
| Rolling restart | "Stop the world" maintenance | `docker compose up -d --scale hatchet-lite=2 …` style |
| Compose vs k8s | Compose | Compose works for N=2-3; beyond that, k8s is the right tool. The Hatchet team's recommended deploy IS k8s. |

---

## 6. Recommendation — Phase 9 candidates

Three paths, ranked by effort:

### Path A — accept single-instance for V1 (lowest effort, no Phase 9 work)
Document the SLA cap, monitor for the forcing functions, defer until
real customer pressure demands HA. R-P3-6 stays open.

### Path B — minimal HA on docker-compose (medium effort, ~1 phase)
Add a second `hatchet-2` service block + an `haproxy` service routing
gRPC + REST + WebUI. Update workers to point at the LB. Doesn't solve
the "compose isn't a real prod platform" problem but proves the
multi-engine architecture works.

**Phase scope estimate:** 4 steps + handoff. Mostly compose + LB
config + worker reconnect testing. Verifier: kill `hatchet-1`,
confirm in-flight smoke completes via `hatchet-2`.

### Path C — Hatchet on k8s (high effort, ~2-3 phases)
The "real" answer per Hatchet's docs. Requires building / acquiring
k8s manifests, deciding on managed-Postgres posture, and operating a
cluster the project doesn't have today. Worth the cost only if
moving more services to k8s in parallel.

### Recommendation

Until a forcing function lands, **Path A**. When it lands, **Path B**
as a stepping stone; **Path C** when the broader platform moves to
k8s.

The Phase 7 + Phase 8 operational maturation work (observability,
auto-prune, multi-kid JWT rotation, TLS edge, admin UI) is more
likely to pay back in V1 than HA work, so the deferral has been
correct in retrospect.

---

End of design doc.
