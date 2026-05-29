# GeoRAG Intelligence V1.0 — Technical Handover Index

> Handover documentation generated 2026-05-28 from a static inspection
> of the codebase. Where the implementation is opinionated, this
> documentation captures the opinion. Where the architecture spec is
> the only source, that is marked as **Assumption**. Where files were
> missing or ambiguous, that is captured under "Missing / Needs
> Confirmation" inside each document.

This index is the starting point for a new engineer or operator.

---

## 1. Documents in this folder

| # | Document                                       | Purpose                                                                           |
| - | ---------------------------------------------- | --------------------------------------------------------------------------------- |
| 1 | [`SAD.md`](SAD.md)                             | Solution Architecture Document — system overview, components, Mermaid topology.   |
| 2 | [`DFS.md`](DFS.md)                             | Data Flow Specification — domain inventory, ingestion / RAG / map flows.          |
| 3 | [`API_DOCUMENTATION.md`](API_DOCUMENTATION.md) | API surfaces — Laravel SPA, `/api/v1/*`, `/internal/*` bridge, FastAPI `/internal/*`, WebSocket channels, tile API. |
| 4 | [`CICD_PIPELINE.md`](CICD_PIPELINE.md)         | CI/CD — GitHub Actions workflow inventory, CI job graph, three-stage SSH CD.      |
| 5 | `HANDOVER_INDEX.md`                            | This file.                                                                        |

---

## 2. Authoritative source documents (already in the repo)

These exist independently of the handover pack and are the long-form
single source of truth.

| Source                                                              | What it covers                                                                  |
| ------------------------------------------------------------------- | ------------------------------------------------------------------------------- |
| [`../../georag-architecture.html`](../../georag-architecture.html) | Complete architecture spec — every technology decision, schema, interface contract, deployment detail, performance tuning, and acceptance criterion. Section 00 (README) → 04 (schemas + pipelines) → 05-06 (deployment + tuning). |
| [`../../README.md`](../../README.md)                                | Quick-start and tech stack summary.                                            |
| [`../../CLAUDE.md`](../../CLAUDE.md)                                | Project context, hard rules, agent delegation, code style, commit conventions. |
| [`../../AGENTS.md`](../../AGENTS.md)                                | Agent inventory.                                                                |
| [`../RUNBOOK.md`](../RUNBOOK.md)                                    | Operator procedures (PII handling, secret rotation, DB maintenance).            |
| [`../OPERATOR-AFTERNOON.md`](../OPERATOR-AFTERNOON.md)              | One-afternoon checklist for first production deploy.                            |
| [`../acceptance-criteria.md`](../acceptance-criteria.md)            | "Is V1 done?" checklist.                                                        |
| [`../adr/`](../adr/)                                                | Architecture Decision Records (ADR-0001 SeaweedFS, ADR-0002 §04p replaces RAGFlow, ADR-0005 TIFF normalise, ADR-0007 chat cards, ADR-0010 canonical passages corpus, …). |
| [`../api/openapi.json`](../api/openapi.json)                        | Partial OpenAPI snapshot — covers some FastAPI `/internal/*` paths.            |
| [`../../ops/runbooks/`](../../ops/runbooks/)                        | 28 operational runbooks (deploy-rollback, on-call, authz-audit-triage, refusal-rate-spike, llm-model-swap, secret-management, …). |
| [`../../ops/baselines/`](../../ops/baselines/)                      | API latency + capacity-planning baselines.                                      |
| [`../../ops/backlog/v1.5-followups.md`](../../ops/backlog/v1.5-followups.md) | V1.5 follow-up tracker.                                                |
| [`../SERVICE_INVENTORY.md`](../SERVICE_INVENTORY.md)                | Per-service inventory.                                                          |

---

## 3. Suggested reading order

1. **Day 1** — Read this index, then `SAD.md` for the big picture and
   `README.md` for the dev-loop quick-start.
2. **Day 2** — `DFS.md` for the data plane, then `architecture.html`
   Section 04 for schema detail.
3. **Day 3** — `API_DOCUMENTATION.md` plus the live OpenAPI from the
   running FastAPI container (`GET /openapi.json`).
4. **When deploying** — `CICD_PIPELINE.md`, `OPERATOR-AFTERNOON.md`,
   `RUNBOOK.md`, and the `ops/runbooks/` scenario index.

---

## 4. What this handover deliberately omits

- **Git repository internals** — branching strategy, commit conventions,
  PR review process. (User instruction excluded these.)
- **Per-feature implementation walkthroughs** — covered by ADRs and
  per-phase plan documents in `docs/`.
- **SQL schema row-by-row** — see `database/migrations/` (188 files)
  and `architecture.html` §04e.
- **Frontend component catalogue** — see `resources/js/Pages/` and
  `resources/js/components/`.

---

## 5. Known unknowns (consolidated)

These are the items the handover pack flagged as needing operator
confirmation. They are documented inline in each file; consolidated
here for triage.

- **SeaweedFS vs MinIO** — ADR-0001 selects SeaweedFS, compose still
  ships `minio`. Which is canonical in current deployment?
- **Sentry status** — package required in `composer.json`, but
  `project_sentry_removed_2026_05_21` records uninstall. Current state?
- **Outbound email** — no SMTP / mail driver observed. Intentional?
- **K3s** — reference-only or future deploy target?
- **Migrations in CD** — `cd.yml` does not call `artisan migrate`. Where
  is it run on production hosts?
- **`continue-on-error` debt in `cd.yml`** — gated on
  `SOPS_AGE_PRIVATE_KEY` + SSH secrets being provisioned.
- **OpenAPI completeness** — on-disk snapshot covers ~10 of 35+ FastAPI
  routes; regenerate from the running container.
- **Public API auth for external callers** — only Sanctum bearer
  observed; no API-key or OAuth2 client-credentials.
- **Webhook subscription CRUD** — `GET /api/v1/webhooks` advertises a
  registry; the subscribe/unsubscribe surface lives in Kestra.
- **Image signing (cosign)** — not present in `ci.yml`. Planned?
- **`e2e.yml` / `release-rehearsal.yml` job details** — not enumerated
  in this pass.

---

## 6. Contact handoff (operator info)

- **Project SME / domain owner** — Kyle (geological domain decisions).
  Per `feedback_graham_not_reviewing`, Graham is *not* in the review
  loop.
- **Hosting** — on-prem / private cloud. SSH to each environment host.
- **Image registry** — GHCR under `${github.repository_owner}`.
- **Secrets** — SOPS + age. Public-key in repo; private key per
  environment in GitHub Secrets.

---

*End of index.*
