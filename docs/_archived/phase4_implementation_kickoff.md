# Phase 4 Implementation Kickoff — Operational maturation

**Document version:** 1.0
**Status:** Active.
**Predecessors:** `docs/phase3_handoff.md`,
`docs/phase3_implementation_kickoff.md`.

---

## 1. Theme

Phase 1+2+3 built the integration edge end-to-end with a single sender,
single shared HMAC secret, code-level flow registry, and one user who
logs into Kestra separately from Laravel. Phase 4 closes those gaps so
the platform is ready for multi-sender / multi-operator real-world use.

**Phase 4 is NOT** about new features or new orchestrators — it's about
hardening what's already standing. Three ingestion-side carry-overs
(R-P3-7 per-step OTel, R-P3-8 SBERT, R-P3-9 vendor profiles) defer to
Phase 5 because they're tuning items, not maturation ones. Hatchet HA
(R-P3-6) defers to Phase 5 too — it's infrastructure-shaped and big.

---

## 2. Locked decisions (recap from Phase 3 handoff §4)

| ID | Item | Phase 4 status |
|----|------|---------------|
| **R-P3-1** | Per-sender HMAC registry | **In scope (Step 1)** |
| **R-P3-2** | Kestra SSO via Sanctum | **In scope (Step 2)** |
| **R-P3-3** | Restart-on-input-model-change CI check | **In scope (Step 3)** |
| **R-P3-4** | DB-driven flow registry | **In scope (Step 4)** |
| **R-P3-5** | Generalised dual-write harness | Deferred to Phase 5+ |
| **R-P3-6** | Hatchet engine HA | **Deferred to Phase 5** — infrastructure-shaped |
| **R-P3-7** | Per-step OTel spans inside parse_pdf_report | Deferred to Phase 5+ |
| **R-P3-8** | SBERT promotion in shadow_diff | Deferred to Phase 5+ |
| **R-P3-9** | Vendor-profile column-mapping | Deferred to Phase 5+ (ingestion scope) |
| **R-P3-10** | Drop `silver.shadow_runs` | **In scope (Step 6)** — Phase 1 cleanup |

---

## 3. Done definition for Phase 4

Phase 4 is **done** when all of:

1. The `external_notification` flow accepts payloads from N senders, each
   with their own HMAC secret + rotation; one sender's secret leak doesn't
   compromise the others.
2. Operators log into Kestra via the same Sanctum session as `/admin/...`
   (no second password to remember; revocation cascades).
3. The flow registry (FastAPI's `FLOW_REGISTRY` + Laravel's
   `REGISTERED_FLOWS`) reads from a single Postgres table — adding a flow
   is a row insert, not a code deploy.
4. CI catches the "fastapi container needs restart after Hatchet input
   model change" footgun automatically (R-P3-3).
5. `silver.shadow_runs` is archived to S3 and dropped (Phase 1 R-P1-10
   completes its 30-day post-cutover window).
6. Per-step verifiers + master regression sweep all green.
7. Phase 4 → Phase 5 handoff written.

---

## 4. Step-by-step

### Step 1 — Per-sender HMAC registry (R-P3-1)
- New table: `usage.external_notification_senders` keyed by `source`
  with columns `secret_kid`, `secret_value` (encrypted via existing
  pgcrypto pattern), `created_at`, `disabled_at`.
- `external_notification` workflow looks up the secret by `source`
  rather than reading the env var; falls back to env for the legacy
  single-sender path during a co-existence window.
- Operator helper: `scripts/phase4_sender_register.sh` adds a sender +
  prints the secret once.
- Verifier: 3 senders with distinct secrets all sign-and-verify
  cleanly; one sender's secret rejected for another sender's payload;
  disabled sender → 401-equivalent (skipped reason).

### Step 2 — Kestra SSO via Sanctum (R-P3-2)
- New Laravel route: `GET /admin/integrations/kestra-redirect` —
  validates the admin gate, mints a short-lived Kestra basic-auth
  cookie or proxy token, redirects to Kestra UI.
- Kestra config gains a Sanctum-aware reverse-proxy header check.
  (Phase 4 keeps Kestra's basic-auth as a fallback admin-only path
  during co-existence; Step 7-equivalent removes it.)
- Verifier: admin-gated session reaches Kestra UI without the basic
  auth prompt; non-admin gets 403 at the Laravel boundary.

### Step 3 — CI check for stale fastapi/Pydantic input models (R-P3-3)
- New script: `scripts/check_fastapi_pydantic_freshness.sh`. Compares
  the FastAPI container's loaded module mtimes against the on-disk
  source mtime; fails if any Hatchet workflow file is newer than the
  container's import.
- Wires into `composer test` (or equivalent) so a stale fastapi shows
  up locally before it shows up in a smoke.
- Documented in `docs/RUNBOOK.md` as the canonical "you forgot to
  restart fastapi" check.

### Step 4 — DB-driven flow registry (R-P3-4)
- New table: `workflow.flow_registry` with `flow_name`, `kind`,
  `description`, `hatchet_workflow_module`, `pydantic_input_class`,
  `enabled`. Populated from a seed migration that mirrors the current
  hard-coded entries.
- FastAPI's `FLOW_REGISTRY` becomes a runtime dict loaded from the
  table on app start; cache TTL 60s.
- Laravel's `REGISTERED_FLOWS` constant becomes a runtime SELECT.
- Verifier: registry add (DB row) → flow becomes triggerable without
  code deploy.

### Step 5 — Multi-sender dashboard polish
- `/admin/integrations` gains a "Senders" panel: per-sender 24h
  receive count, last-seen, disabled state. Uses the new sender
  registry from Step 1.
- Existing flow-card UI stays unchanged; Step 5 is purely additive.
- Verifier: panel renders; sender disable from the UI takes effect
  in the next inbound webhook.

### Step 6 — Drop `silver.shadow_runs` (R-P1-10)
- Pre-flight: archive the table to S3 as a `pg_dump --table=...`
  one-shot in the bronze bucket under `archive/phase1/shadow_runs/`.
- Migration drops the table + the `shadow_diff` workflow + the diff
  classifier.
- Update `/admin/shadow-runs` route to redirect to a "Phase 1
  cutover archive" notice page (or remove — TBD per Step 6 retro).
- Verifier: pg_dump file in S3 (size > 0); table gone; route returns
  410 Gone (or 301 to a redirect page).

### Step 7 — Migration housekeeping
- Compress the 4 phase migration directories (`database/raw/phaseN/`)
  into a single `database/raw/phase1-4-rollup.sql` for greenfield
  installs while keeping the original per-phase files as the
  historical record.
- One pass through `.env` to remove now-unused variables (anything
  referenced only in archived files).
- Verifier: rollup script applies cleanly to a fresh Postgres; matches
  the live schema bit-for-bit (no drift).

### Step 8 — Phase 4 → Phase 5 handoff
- Same shape as previous handoffs.

---

## 5. Engineering invariants for Phase 4

1. **Backward-compat during each step.** Every Step 1-5 adds a new
   capability without removing the old one; cleanup steps (6, 7) are
   explicit cutovers with rollback paths.
2. **Encrypted-at-rest for the new sender secrets table** (R-P3-1).
   Reuse Phase 0's pgcrypto pattern, not a new crypto primitive.
3. **No new auth surfaces.** Step 2 (SSO) routes everything through
   the existing Sanctum session; we don't introduce OAuth flows.
4. **Single source of truth for flow definitions** — Step 4 makes the
   DB authoritative; Kestra YAML files and FastAPI registry both
   reference the DB row (or get auto-synced in Step 4's migration).
5. **Octane-safe + RLS-aware.** All new Laravel surfaces follow Phase 0
   patterns.

---

## 6. Files of record (preview)

```
database/raw/phase4/10-external-notification-senders.sql        (Step 1)
database/raw/phase4/20-flow-registry-table.sql                  (Step 4)
database/raw/phase4/90-drop-shadow-runs.sql                     (Step 6)
database/raw/phase1-4-rollup.sql                                 (Step 7)
src/fastapi/app/services/sender_secrets.py                       (Step 1)
src/fastapi/app/services/flow_registry.py                       (Step 4)
src/fastapi/app/hatchet_workflows/external_notification.py     (mod — Step 1)
src/fastapi/app/routers/integrations_trigger.py                  (mod — Step 4)
app/Http/Controllers/Admin/KestraSsoController.php              (Step 2)
app/Http/Controllers/Admin/IntegrationsController.php           (mod — Steps 4, 5)
config/database.php                                               (mod, possible)
docker-compose.yml                                                (mod — Steps 2, 7)
resources/js/Pages/Admin/Integrations.tsx                        (mod — Step 5)
routes/web.php                                                     (mod — Step 2)
docs/phase4_handoff.md                                            (Step 8)
scripts/phase4_step{1..7}_verify.sh                              (each step)
scripts/phase4_sender_register.sh                                 (Step 1)
scripts/check_fastapi_pydantic_freshness.sh                       (Step 3)
```

End of Phase 4 implementation kickoff.
