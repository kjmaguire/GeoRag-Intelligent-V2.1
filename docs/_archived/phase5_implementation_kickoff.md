# Phase 5 Implementation Kickoff — Receive-path hardening + ingestion observability

**Document version:** 1.0
**Status:** Active.
**Predecessors:** `docs/phase4_handoff.md`.

---

## 1. Theme

Phase 4 hardened the integration edge for multi-sender / multi-operator
use. Phase 5 finishes the receive-path security story (per-sender rate
limits, per-flow JWT keys), automates the staleness CI gate end-to-end,
and finally lights up per-step OTel inside the PDF parser — the
ingestion-side observability gap that's been open since Phase 1.

Phase 5 is deliberately **all code, no new infrastructure**. The two
big infrastructure items (R-P4-2 nginx-edge Kestra SSO, R-P3-6 Hatchet
engine HA) are Phase 6+ work — each warrants its own phase.

---

## 2. Locked decisions

| ID | Item | Phase 5 status |
|----|------|---------------|
| **R-P4-1** | Per-sender rate limits | **In scope (Step 1)** |
| **R-P4-4** | Per-flow JWT signing key rotation | **In scope (Step 2)** |
| **R-P4-3** | Pre-commit hook for Pydantic staleness | **In scope (Step 3)** |
| **R-P4-5** | `.env` housekeeping (orphan vars) | **In scope (Step 3)** — paired with hook |
| **R-P3-7** | Per-step OTel spans in `parse_pdf_report` | **In scope (Step 4)** |
| **R-P4-2** | Nginx-edge Kestra SSO | Deferred to Phase 6 (infrastructure) |
| **R-P3-6** | Hatchet engine HA | Deferred to Phase 6 (infrastructure) |
| **R-P3-9** | Vendor-profile column-mapping | Deferred (ingestion pipeline scope) |
| **R-P3-5** | Generalised dual-write harness | Re-evaluate when 2nd migration target lands |

---

## 3. Done definition

Phase 5 is done when:

1. `external_notification` enforces a per-sender rate limit (token
   bucket in Redis); a noisy sender can't DoS the receive path.
2. Per-flow JWT signing keys replace the shared `KESTRA_FLOW_JWT_SECRET`
   — leaking one flow's key doesn't compromise the others; key
   rotation is documented + scripted.
3. A pre-commit hook runs `check_fastapi_pydantic_freshness.sh`
   automatically; `.env` is cleaned up.
4. Every stage of `parse_pdf_report` emits an OTel span via the
   collector → Tempo path; the Hatchet `ingest_pdf` workflow run
   becomes spelunk-able down to PDF parsing sub-stages.
5. Per-step verifiers green; master regression sweep green.
6. Phase 5 → Phase 6 handoff written.

---

## 4. Step-by-step

### Step 1 — Per-sender rate limits (R-P4-1)
- Redis token bucket keyed on `external_notification:{source}` with
  capacity 60 / refill 60 per minute (configurable per sender).
- New column `usage.external_notification_senders.rate_limit_per_minute`
  (default 60).
- `external_notification` workflow checks the bucket BEFORE flag +
  HMAC. Over-limit → `skipped=true, reason='rate_limited'`. Audit row
  written for rate-limit events so the dashboard surfaces them.
- Verifier: 60 requests/min pass; 61st fails; minute later passes
  again; sender-specific (sender A's limit doesn't affect sender B).

### Step 2 — Per-flow JWT signing keys (R-P4-4)
- New table column `workflow.flow_registry.jwt_secret_kid` plus a
  pgcrypto-encrypted `jwt_secret_ciphertext`.
- `flow_jwt.mint_flow_jwt()` reads the per-flow key from the registry;
  `verify_flow_jwt_token()` looks up by `kid` claim.
- The global `KESTRA_FLOW_JWT_SECRET` env var becomes the fallback
  signing key only for flows without a per-flow secret.
- `scripts/phase3_jwt_rotate.sh` gains a `--per-flow` mode that
  generates + writes a new secret to the registry.
- Verifier: per-flow key signs cleanly; same JWT against a different
  flow rejects on scope check (existing) AND on kid mismatch (new).

### Step 3 — Pre-commit hook + .env housekeeping
- Template at `scripts/git-hooks/pre-commit` that runs
  `check_fastapi_pydantic_freshness.sh --quiet` plus other guards
  (pint --test, lint).
- Installer at `scripts/install-git-hooks.sh` copies the template into
  `.git/hooks/` (idempotent; never overwrites without `--force`).
- Strip the 7 orphan vars identified in Phase 4 Step 7 from `.env`
  unless they're documented elsewhere as runtime-configurable.
- Verifier: hook installs; running it on a stale state exits 1; on a
  clean state exits 0.

### Step 4 — Per-step OTel spans in `parse_pdf_report` (R-P3-7)
- Add an OTel tracer scope to each of the 7 parse stages in the
  Dagster parser (preflight / unstructured / pdfplumber / OCR /
  metadata / sections / resource_tables).
- Spans propagate via the existing `OTEL_*` env vars on the Dagster
  + Hatchet workers (both call `parse_pdf_report`).
- Verifier: an ingest_pdf workflow run lands ≥6 child spans under
  the parse task in Tempo (probed via Tempo's search API).

### Step 5 — Phase 5 → Phase 6 handoff
- Same shape as previous handoffs.

---

## 5. Engineering invariants

1. **No new services.** Phase 5 stays inside the existing containers.
2. **Backward-compat at each step.** Rate limit defaults to 60/min so
   existing single-sender deployments don't trip it. Per-flow JWT keys
   are optional; the env-var fallback covers flows without a per-flow
   key.
3. **OTel via existing collector**, not a new exporter.
4. **Octane-safe** for any new Laravel surface.

---

## 6. Files of record (preview)

```
database/raw/phase5/10-sender-rate-limits.sql                   (Step 1)
database/raw/phase5/20-per-flow-jwt-keys.sql                    (Step 2)
src/fastapi/app/hatchet_workflows/external_notification.py     (mod — Step 1)
src/fastapi/app/services/flow_jwt.py                           (mod — Step 2)
src/fastapi/app/services/flow_registry.py                       (mod — Step 2)
src/dagster/georag_dagster/parsers/pdf_report.py                (mod — Step 4)
scripts/phase3_jwt_rotate.sh                                     (mod — Step 2)
scripts/phase4_sender_register.sh                                (mod — Step 1)
scripts/git-hooks/pre-commit                                     (Step 3)
scripts/install-git-hooks.sh                                     (Step 3)
scripts/phase5_step{1..4}_verify.sh                              (each step)
docs/phase5_handoff.md                                            (Step 5)
.env                                                               (mod — Step 3)
```

End of Phase 5 implementation kickoff.
