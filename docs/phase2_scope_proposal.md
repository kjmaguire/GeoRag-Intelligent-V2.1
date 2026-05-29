# Phase 2 Scope Proposal — Activepieces adoption + integration boundary

**Status:** DRAFT — pending sign-off from Kyle.
**Author:** Phase 1 → 2 transition session.
**Replaces:** Nothing yet. The full kickoff (`docs/phase2_implementation_kickoff.md`)
will land after this proposal is signed off.

---

## 1. Why this doc exists

The Phase 1 kickoff doc names Phase 2 as "Activepieces adoption + integration
boundary migration" and that's the entire pre-existing spec. There's no
architecture-doc section for Activepieces, no integration list, no
deployment notes. Before writing a step-by-step kickoff (mirroring Phase
1's), we need scoping decisions from Kyle.

This doc enumerates:
- **(§2)** What Activepieces is, recap.
- **(§3)** What it could plausibly own in the GeoRAG architecture.
- **(§4)** Decisions Kyle needs to make.
- **(§5)** A proposed default-shape for Phase 2 if Kyle has no strong
  preference — used as a fallback so we're not blocked.

---

## 2. Activepieces recap

[Activepieces](https://www.activepieces.com/) is an open-source workflow
automation engine in the Zapier / n8n / Make.com category:

- Visual flow builder for **user-facing, integration-shaped** work.
- ~250+ pre-built "pieces" (Slack, GitHub, GMail, HTTP, Postgres, S3…).
- Self-hostable via Docker; Postgres-backed; PG-licensed AGPL community
  edition.
- Conceptually adjacent to Hatchet but with a different sweet spot:
  Hatchet excels at **durable code-driven workflows owned by engineers**;
  Activepieces excels at **user-buildable integration flows owned by
  operators / SMEs**.

The architecture doc nods to it indirectly via the Slack-notifications
mention but doesn't define an integration boundary.

---

## 3. Plausible Activepieces ownership areas

Sorted roughly by "how naturally it fits Activepieces":

| Area | Fit | Why |
|------|-----|-----|
| **Slack notifications** (RAG answer ready, ingestion failures, oncall pages) | **Strong** | Direct piece exists; operator-owned; doesn't need code-level durability. |
| **Email alerts / digest reports** | **Strong** | Same shape. |
| **External CRM / ERP sync** (Salesforce, HubSpot for company-name normalisation) | **Strong** | Standard piece-driven integrations. |
| **Webhook ingest from external geological-data providers** (USGS, BGS, GA, GSC feeds) | **Medium** | Could be Activepieces "HTTP webhook → trigger Hatchet `ingest_*`"; or could be FastAPI route → Dagster sensor. Trade-off below. |
| **Scheduled imports** of public-geoscience tilesets (NRCAN, NOAA bathymetry) | **Medium** | Either Activepieces cron → Dagster trigger, or Dagster's own sensor. |
| **User-triggered exports** (PDF report → S3, GIS layer → Geopackage) | **Weak/medium** | Already in Laravel Horizon scope; moving to Activepieces would duplicate. |
| **Operator runbook automation** (rotate APP_KEY, run `phase1_step8_traffic.sh set 10`) | **Medium** | Tempting but blast-radius high for visual-flow ownership. |
| **RAG answer pipelines** (query → retrieve → cite → respond) | **Anti-fit** | Stays in FastAPI + Pydantic AI. Not Activepieces-shaped. |

---

## 4. Decisions Kyle needs to make

### D1. What's the **first** integration to migrate?

Phase 1 picked `ingest_pdf` as a vertical slice with a shadow harness.
Phase 2 needs an equivalent first slice. Candidates:

- **D1a — Slack notification on ingestion completion** (lowest risk;
  thinnest end-to-end exercise: trigger fires from `ingest_pdf.persist`
  → Activepieces flow → Slack webhook).
- **D1b — Public-geoscience scheduled import** (more substantial;
  Activepieces takes ownership of a recurring data flow Dagster used to
  drive).
- **D1c — Webhook ingest** (e.g. external tenant sends us a "new report
  filed" event; we route into Hatchet `ingest_pdf` via Activepieces).

**Recommendation:** **D1a** as the smoke-test slice (Phase 2 Step 5
equivalent), then **D1b** or **D1c** as the second slice once auth +
secret-store + observability are wired.

### D2. **Where does Activepieces sit relative to Hatchet?**

Two viable models:

- **Model A — Activepieces as the integration edge.** External world ⇄
  Activepieces ⇄ Hatchet/Dagster/Laravel. Activepieces is the only thing
  that talks to third-party SaaS; internal workflows use Hatchet.
- **Model B — Activepieces as a parallel orchestrator for SME-owned
  flows.** Both Hatchet and Activepieces talk to the outside world;
  ownership boundary is "code engineers write" vs "ops/SMEs assemble".

**Recommendation:** **Model A**. Cleaner blast-radius story; matches
the CLAUDE.md "Don't duplicate orchestration" rule (each engine has a
distinct ownership lane).

### D3. **Auth + secret-store integration**

Activepieces has its own user store, its own connections (OAuth tokens,
API keys). Two paths:

- **D3a** — Ship Activepieces with isolated auth (its own admin login,
  no SSO). Operators log into Activepieces directly.
- **D3b** — Front it with the same Sanctum session as Laravel admin.

**Recommendation:** **D3a** for Phase 2 (separate admin surface,
read-only embed in `/admin/integrations` if useful). **D3b** is Phase 3+
hardening.

### D4. **Persistence + dependency footprint**

Activepieces needs Postgres. Choices:

- **D4a** — A new logical DB on the existing `postgresql` server (next
  to `georag` and `hatchet`). Same approach we took for Hatchet.
- **D4b** — A separate Postgres instance entirely.

**Recommendation:** **D4a** (matches the Hatchet pattern). Same role
isolation strategy (`activepieces` user, separate logical DB).

### D5. **Shadow / cutover strategy**

Phase 1 used shadow + ramp + diff for `ingest_pdf` because there was an
existing v1.49 implementation to replace. Phase 2 most likely **builds
new flows** rather than replacing existing ones — there's no v1.49
Slack-notification path to shadow against.

**Recommendation:** Phase 2 doesn't need a shadow harness for greenfield
flows. Use a feature-flag gate (`activepieces.<flow_name>.enabled`,
default false) per flow and ramp via that. Reuse the
`workspace.feature_flag_history` sidecar (R-P1-6) for the audit trail.

### D6. **Phase 2 scope ceiling**

Without a scope ceiling we'll iterate forever. Proposed:

- 1 piece of infra (Activepieces service + Postgres logical DB + auth)
- 2 user-visible flows (D1a Slack notify + one of D1b/D1c)
- 1 admin surface (`/admin/integrations` listing flows + run history)
- 1 observability slice (flow runs visible in OTel collector)
- Handoff to Phase 3

That's roughly the same footprint Phase 1 had.

---

## 5. Default-shape kickoff (used if D1–D6 sign-off blocks)

If you sign off "go with the recommendations", Phase 2 looks like:

| Step | Title |
|------|-------|
| 1 | Activepieces docker service + `activepieces` Postgres logical DB + role |
| 2 | Auth wiring (admin login, isolated; SSO deferred to Phase 3) |
| 3 | Slack-notification piece — `ingest_pdf.persist` fires `ai:slack_notify` Hatchet workflow → posts to Activepieces webhook → Slack channel |
| 4 | `/admin/integrations` Inertia page — flow list + recent run rollup (from Activepieces' Postgres) |
| 5 | Second flow (TBD: scheduled public-geoscience import, OR webhook ingest from external feed) |
| 6 | Feature-flag gating for every Activepieces flow + reuse R-P1-6 audit trail |
| 7 | OTel + Grafana panel for Activepieces flow runs |
| 8 | Phase 2 → Phase 3 handoff |

8 steps, mirrors Phase 1's shape.

---

## 6. Asks for Kyle

Please respond to these (or just say "go default" and I'll use §5):

- **D1** — Which first integration: Slack notify (D1a), scheduled import (D1b), or webhook ingest (D1c)?
- **D2** — Activepieces edge (Model A) or parallel orchestrator (Model B)?
- **D3** — Isolated auth (D3a) or Sanctum-fronted (D3b)?
- **D4** — Same Postgres server (D4a) or separate (D4b)?
- **D5** — OK to skip shadow harness for greenfield flows + use feature-flag gating instead?
- **D6** — Is the §4.6 scope ceiling acceptable, or should we trim/expand?

Once these are answered I'll land `docs/phase2_implementation_kickoff.md`
in the same shape as Phase 1's and start work.

End of scope proposal.
