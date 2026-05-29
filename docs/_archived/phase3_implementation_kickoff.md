# Phase 3 Implementation Kickoff — Kestra migration + Activepieces sunset

**Document version:** 1.0
**Status:** Active.
**Predecessors:** `docs/phase2_handoff.md`,
`docs/phase2_implementation_kickoff.md`,
`docs/phase2_activepieces_flows.md`,
`docs/phase2_activepieces_upgrade.md`.
**Supersedes (at Phase 3 close):** the four Activepieces-specific docs above.

---

## 1. Why this phase exists

Phase 2 stood up Activepieces as the integration edge and shipped two
flows end-to-end behind feature flags. The vertical slice worked, but
the operational story has two real rough edges that combine to
recommend a swap rather than continued hardening:

1. **Flow definitions live only in Activepieces' DB.** Re-creating
   them in a fresh environment is a manual UI exercise (carry-over
   R-P2-2). Phase 3's planned answer was an export-to-JSON-and-commit
   tool — that's real engineering effort.
2. **Activepieces' fit is closer to business automation than data
   orchestration.** The integration estate we're building (geoscience
   feeds, RAG ingestion triggers, on-prem-friendly webhooks) maps
   better onto a YAML-flow data orchestrator.

**Kestra** ([kestra.io](https://kestra.io)) is the chosen replacement.
Postgres-backed, YAML-flows-as-code by default (subsumes R-P2-2 with
zero engineering effort), built around durable orchestration primitives
that match the workload, MIT-compatible licensing.

Phase 3 migrates the integration edge from Activepieces to Kestra
without disturbing anything downstream. The two existing flows
(`public_geoscience_pull`, `external_notification`) re-land as Kestra
YAML flows; the Hatchet-side workflows on the receiving end of the
trigger route stay untouched.

---

## 2. Locked decisions

These are inherited from Phase 2 (Model A integration edge) and
reaffirmed here:

| ID | Decision | Status |
|----|----------|--------|
| **D2 (Phase 2)** | Integration-edge topology — external world ⇄ orchestrator ⇄ Hatchet/Dagster/Laravel | Reaffirmed. Kestra slots into the same topology slot. |
| **D3** | Isolated admin auth (no Sanctum SSO) | Reaffirmed. Kestra ships basic auth out-of-the-box; SSO defers to Phase 4. |
| **D4** | Same Postgres server, separate logical DB + role | Reaffirmed. New `kestra` DB + `kestra` role mirror the Phase 0 + Phase 2 pattern. |
| **D5** | Feature-flag gating per flow + reuse `workspace.feature_flag_history` | Reaffirmed, but renamed: flag namespace shifts from `activepieces.<flow>.enabled` to neutral `flows.<flow>.enabled` (Step 3). |
| **D7 (NEW)** | Kestra YAML flows committed to the repo under `kestra/flows/*.yaml`. The Kestra service auto-loads them on boot via the `KESTRA_CONFIGURATION` block. **R-P2-2 closed.** | New. |
| **D8 (NEW)** | Per-flow JWT replaces shared `FASTAPI_SERVICE_KEY` for `/internal/v1/integrations/...`. **R-P2-3 closed.** | New. |
| **D9 (NEW)** | HMAC-shared-secret signature verification on inbound webhooks. **R-P2-4 closed.** | New. |

Phase 3 explicitly does NOT do: SSO (R-P2-1, deferred to Phase 4),
Hatchet engine HA (R-P1-9 / R-P2-10, deferred to Phase 4 — pre-cutover
infrastructure work), DB-driven flow registry (R-P2-5, no longer
needed since Kestra YAML flows are themselves the registry).

---

## 3. Done definition for Phase 3

Phase 3 is **done** when all of:

1. Kestra is healthy in the dev stack on a pinned image; `docker compose
   up -d` brings it up; healthcheck passes; basic-auth admin reachable.
2. Schema + role isolation matches the Phase 0/1/2 pattern (`kestra`
   role + `kestra` logical DB on the existing server, NOSUPERUSER,
   NOBYPASSRLS).
3. Both flows live under `kestra/flows/` as YAML committed to the repo.
4. The `public_geoscience_pull` Kestra flow runs end-to-end on cron;
   `external_notification` accepts an HMAC-signed webhook end-to-end.
5. `/internal/v1/integrations/<flow>/trigger` accepts per-flow JWTs
   (D8); the legacy `X-Service-Key` path is removed at Step 7.
6. `external_notification` rejects un-signed and badly-signed inbound
   payloads (D9).
7. `/admin/integrations` reads from `pgsql_kestra` instead of
   `pgsql_activepieces`; flag references shift to `flows.<flow>.enabled`.
8. Activepieces is sunset: container removed from compose, role +
   DB dropped, runbooks archived, `activepieces.*` feature flags
   migrated then dropped.
9. Per-step verifiers green; combined master sweep green.
10. Phase 3 → Phase 4 handoff written.

---

## 4. Step-by-step

### Step 1 — Kestra Postgres role + logical DB
- New role: `kestra` (LOGIN, NOSUPERUSER, NOBYPASSRLS).
- New DB: `kestra` (owner: `kestra`).
- Migration: `database/raw/phase3/10-kestra-role-and-db.sql`.
- Verifier: `scripts/phase3_step1_verify.sh` — role posture, DB
  ownership, auth + connect, no schema-level grants leaked into the
  georag DB.

### Step 2 — Kestra docker service
- Image pinned to `kestra/kestra:v1.2.18` (latest stable at kickoff).
- Single-node "standalone" mode: queue + repository = `postgres`,
  storage = `local` (volume `kestra_data`); no Kafka, no Elasticsearch.
- Basic auth enabled; admin email + password from `.env`.
- Port: `${KESTRA_PORT:-8085}` (avoid clash with Hatchet 8889 / Octane 80).
- Profile-gated to `dev-data` and `dev-full`, mirrors Activepieces.
- Healthcheck: `GET /health` (Kestra's built-in).
- Verifier: container running + healthy; Kestra's own DB tables
  (`flows`, `executions`) created on first boot; auth-required
  endpoint returns 401 without basic-auth.

### Step 3 — Generic flow trigger + per-flow JWT (R-P2-3)
- Rename the workspace.feature_flags namespace from
  `activepieces.<flow>.enabled` to `flows.<flow>.enabled`. Migration
  copies values forward + drops old rows after a smoke confirms the
  controller reads the new key.
- FastAPI `integrations_trigger.py` accepts a per-flow JWT in the
  `Authorization: Bearer …` header. JWT is minted by the existing
  `FastApiJwtMinter` with `flow:<flow_name>` scope and a 24h TTL.
- Kestra holds the JWT as a secret (Kestra's `secrets.*` namespace).
- Backward-compat: the `X-Service-Key` path stays during Step 3–6 as
  a feature-flag-gated escape hatch (`flows.legacy_service_key_accepted`,
  default true). Step 7 flips it false + removes the code.
- Verifier: legitimate per-flow JWT for the right flow ⇒ 202; same
  JWT for a different flow ⇒ 403; expired JWT ⇒ 401.

### Step 4 — `public_geoscience_pull` Kestra flow
- New file: `kestra/flows/georag/public_geoscience_pull.yaml`.
- Trigger: `io.kestra.plugin.core.trigger.Schedule` (cron `0 */6 * * *`).
- Tasks: `Http.Request` → `S3.Upload` → `Http.Request` to FastAPI's
  trigger endpoint with the per-flow JWT.
- Smoke (`scripts/phase3_step4_smoke.sh`) hits FastAPI directly with
  a synthetic GeoJSON payload — same shape as Phase 2 Step 4's smoke,
  just with the JWT path instead of the `X-Service-Key` path.
- The Hatchet `public_geoscience_pull` workflow stays unchanged.
- Verifier: YAML loads in Kestra (no errors at /flows/parse); end-to-end
  smoke passes; `bronze.provenance` row written.

### Step 5 — `external_notification` + HMAC (R-P2-4)
- New file: `kestra/flows/georag/external_notification.yaml`.
- Trigger: `io.kestra.plugin.core.trigger.Webhook` with a key.
- Task: shape envelope (`notification_id`, `source`, `kind`,
  `payload`, `received_at`) and HMAC-sign the canonical-JSON form
  with a per-sender shared secret.
- The `external_notification` Hatchet workflow grows an HMAC verify
  step at the front; rejects badly-signed inputs with `skipped=true,
  reason='hmac_verification_failed'` (no audit row written).
- Smoke covers: legitimate sig ⇒ COMPLETED + audit row; missing sig
  ⇒ rejection; tampered payload ⇒ rejection; replay (same sig +
  same notification_id) ⇒ idempotent skip (existing path, unchanged).
- Verifier: 7 cases mirroring Phase 2 Step 5a + the three HMAC paths.

### Step 6 — `/admin/integrations` dashboard pivot
- Rename `pgsql_activepieces` → `pgsql_kestra` in `config/database.php`.
- Update `IntegrationsController::loadActivepiecesFlows()` to
  `loadKestraFlows()` reading `flows` + `flows_executions` from the
  kestra DB.
- Update `Integrations.tsx` labels.
- The Hatchet rollup query (`v1_runs_olap`) is unchanged — it's
  orchestrator-agnostic.
- Verifier: 7 checks (mirrors Phase 2 Step 6).

### Step 7 — Activepieces sunset
- Stop + remove `activepieces` container from `docker-compose.yml`.
- Drop `activepieces` role + `activepieces` DB after a final
  pg_dump-to-archive snapshot (kept 90 days).
- Remove `pgsql_activepieces` connection from Laravel config.
- Remove `activepieces.*` feature flag rows.
- Remove the `X-Service-Key` legacy fallback in
  `integrations_trigger.py`.
- Archive `docs/phase2_activepieces_flows.md` and
  `docs/phase2_activepieces_upgrade.md` to `docs/_archived/`.
- Verifier: container gone, role gone, DB gone, no `activepieces`
  references in active config files, all Phase 1+2 verifiers still
  pass.

### Step 8 — Phase 3 → Phase 4 handoff
- `docs/phase3_handoff.md`. Same shape as `phase2_handoff.md`.
- Carry-overs for Phase 4: SSO (R-P2-1), Hatchet HA (R-P1-9 + R-P2-10),
  any new ones discovered in Step 1–7.

---

## 5. Engineering invariants for Phase 3

1. **Kestra is the integration edge — no orchestration drift.** Kestra
   does not run internal workflows; it only triggers them via FastAPI.
   Same rule that applied to Activepieces in Phase 2.
2. **Flows live in the repo.** `kestra/flows/*.yaml` is the source of
   truth. Kestra UI edits must be exported back to a YAML file before
   merge — checked at PR review.
3. **Per-flow JWTs.** No more shared service keys. Each flow has its
   own JWT scoped to its own flow name. Rotation procedure documented
   in Step 3.
4. **HMAC for inbound.** External senders sign their payload; the
   Hatchet workflow rejects unsigned or tampered requests before any
   side-effect runs.
5. **Sunset cleanly.** Activepieces leaves no trail. The two-runbook
   doc set archives, not deletes — they're history, not active scope.
6. **Octane-safe + RLS-aware.** All new Laravel surfaces follow Phase
   0 patterns; new Kestra-side queries go through the read-only
   `pgsql_kestra` connection.

---

## 6. Risks + mitigations

| Risk | Mitigation |
|------|-----------|
| Kestra YAML schema is unfamiliar; flows take longer than estimated | Each flow ships with a smoke that runs against FastAPI directly (bypasses Kestra), so we can validate the receiving end independently. |
| Per-flow JWT rotation is unscripted today | Step 3 ships a small `phase3_jwt_rotate.sh` helper that mints + writes the new JWT into Kestra's secret store via API, mirroring `phase1_step8_traffic.sh` operationally. |
| HMAC implementation drift between Kestra-side signing + Hatchet-side verifying | Single canonical-JSON serialisation (sorted keys, no whitespace, UTF-8). Documented + a test fixture committed alongside the verifier. |
| Activepieces DB drop loses historical flow run state | Step 7 takes a final `pg_dump activepieces` to S3 with 90d retention before dropping. |
| Both orchestrators briefly co-exist during Steps 4–6 | Acceptable. Feature-flag gates ensure each flow runs through exactly one orchestrator at a time; Step 7 is the explicit cutover. |

---

## 7. Files of record (preview)

```
database/raw/phase3/10-kestra-role-and-db.sql                   (Step 1)
database/raw/phase3/20-rename-flow-flags.sql                    (Step 3)
database/raw/phase3/90-activepieces-sunset.sql                  (Step 7)
docker-compose.yml                                                (mod — Step 2 + 7)
docker/kestra/application.yaml                                    (Step 2)
kestra/flows/georag/public_geoscience_pull.yaml                 (Step 4)
kestra/flows/georag/external_notification.yaml                  (Step 5)
src/fastapi/app/routers/integrations_trigger.py                 (mod — Step 3 + 7)
src/fastapi/app/services/flow_jwt.py                            (Step 3)
src/fastapi/app/hatchet_workflows/external_notification.py     (mod — Step 5 HMAC)
app/Http/Controllers/Admin/IntegrationsController.php          (mod — Step 6)
config/database.php                                              (mod — Step 6 + 7)
resources/js/Pages/Admin/Integrations.tsx                        (mod — Step 6)
docs/phase3_implementation_kickoff.md                            (this file — Step 0)
docs/phase3_kestra_flows.md                                      (Step 4 + 5)
docs/phase3_handoff.md                                            (Step 8)
docs/_archived/phase2_activepieces_flows.md                     (Step 7)
docs/_archived/phase2_activepieces_upgrade.md                   (Step 7)
scripts/phase3_step{1..7}_verify.sh                              (each step)
scripts/phase3_step{4,5}_smoke.sh                                (per-flow)
scripts/phase3_jwt_rotate.sh                                     (Step 3 helper)
```

---

## 8. Sign-offs + start order

Step 1 first (DB+role — fully reversible). Step 2 next (service comes
up — also reversible). Step 3 unblocks Steps 4–5 (the JWT path is
prerequisite for the new flows). Step 4 demos before Step 5. Step 6
is mostly independent. Step 7 is the explicit cutover — only after
both flows have run cleanly on Kestra for at least one cron tick.
Step 8 closes Phase 3.

End of Phase 3 implementation kickoff.
