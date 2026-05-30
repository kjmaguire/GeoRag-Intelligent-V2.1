# GeoRAG Intelligence — Technical Handover Index

> **Provenance.** Built from `HANDOVER_MANIFEST.md` (mechanical inventory of
> the live tree) and `PASS_1_MAPPING.md` (owning-doc routing). The four
> handover documents are thin navigational overlays that point into the
> canonical sources listed in §3. If code and canonical-source spec disagree,
> code wins; open an issue + update the spec.

---

## 1. The four documents

| File | Owns |
|---|---|
| [`SAD.md`](SAD.md) | System overview, topology, components, cross-cutting concerns (security/tenancy/observability/config/orchestration), key decisions pointer. |
| [`DFS.md`](DFS.md) | Domains, end-to-end data flows, classification, persistence + database architecture, storage / cache / pub-sub, outbound integrations + exports, reliability + retention. |
| [`API_DOCUMENTATION.md`](API_DOCUMENTATION.md) | All caller-facing surfaces — Laravel `/api/v1/*` + `/internal/*` bridge, FastAPI domain service, Reverb WebSocket channels + event classes, Martin tile API, refusal vocabulary, HMAC envelope, Trust-Summary contract, security response headers. |
| [`CICD_PIPELINE.md`](CICD_PIPELINE.md) | GitHub Actions workflows, Dockerfiles, test posture + gates, secrets/SOPS, migration sequence, Hatchet worker-pool selection, Dagster + Ofelia schedules, alert routing, operator scripts. |

This index is the navigation layer. Sections 3–6 below give you everything you need to reach the right doc + the right canonical source.

---

## 2. Reading order

- **Day 1** — this file, then `SAD.md`, then `docs/RUNBOOK.md` and `docs/OPERATOR-AFTERNOON.md` for first-deploy context.
- **Day 2** — `DFS.md` for the data plane, then `georag-architecture.html` Section 04 for schema detail.
- **Day 3** — `API_DOCUMENTATION.md` plus the live OpenAPI from the running FastAPI container (`GET /openapi.json`).
- **When deploying** — `CICD_PIPELINE.md`, then `ops/runbooks/cold-start.md` + `ops/runbooks/secret-management.md`.
- **On call** — `ops/runbooks/on-call.md` + the scenario-specific runbook under `ops/runbooks/`.

---

## 3. Canonical sources

The handover docs **route into** the following authoritative sources. Read them when the handover summary isn't enough.

### 3.1 Master architecture spec
- [`../../georag-architecture.html`](../../georag-architecture.html) — complete architecture reference.
- [`../../README.md`](../../README.md) — quick-start, tech stack summary.
- [`../../CLAUDE.md`](../../CLAUDE.md) — project context, 9 hard rules, agent delegation, code style, commit convention.
- [`../../AGENTS.md`](../../AGENTS.md) — Laravel Boost guidelines snapshot.

### 3.2 Architecture Decision Records — [`../adr/`](../adr/)

12 ADRs.

| # | Title | Status |
|---|---|---|
| 0001 | SeaweedFS replaces MinIO as the S3-compatible object store | Accepted |
| 0002 | §04p PDF stack replaces RAGFlow as the canonical parser | Accepted |
| 0003 | Defer bge-reranker-v2-m3 + GPU reranker host upgrade | Proposed (deferred) |
| 0004 | Orchestrator short-circuit for high-confidence definition queries | Proposed (gated on SME sign-off) |
| 0005 | Normalize TIFF scans to PDF and route through the §04p stack | Accepted |
| 0006 | Agentic retrieval — one LangGraph + six routed intents | Accepted |
| 0007 | Chat-embedded interactive cards + two new agentic-retrieval intents | Accepted (2026-05-25) |
| 0008 | Embedding model evaluation — Option D (domain-fine-tune `bge-small`) | Accepted (2026-05-27) |
| 0009 | §3 and §4 algorithmic-spines rollout — stage-gated, flag-gated | Accepted |
| 0010 | `silver.document_passages` is the canonical chunked-content corpus | Accepted |
| 0011 | Reranker domain adaptation — vocabulary, MLM, full fine-tune | Proposed |
| 0012 | Structured-to-NL summary corpus expansion | Proposed |

### 3.3 Operator references
- [`../RUNBOOK.md`](../RUNBOOK.md) — PII / secret rotation / encrypted-ops procedures.
- [`../OPERATOR-AFTERNOON.md`](../OPERATOR-AFTERNOON.md) — first-deploy one-afternoon checklist (O-01..O-07 preflight gates).
- [`../acceptance-criteria.md`](../acceptance-criteria.md) — "is V1 done?" canonical checklist.
- [`../SERVICE_INVENTORY.md`](../SERVICE_INVENTORY.md) — per-container talks-to + healthcheck-vs-reality notes.

### 3.4 Operational runbooks — [`../../ops/runbooks/`](../../ops/runbooks/) (38 scenarios)

`authz-audit-triage`, `backup-restore`, `citation-pipeline`, `claude-code-mcp-migration`, `cold-start`, `container-hardening`, `data-version`, `datastore-tuning`, `dem-self-host`, `deploy-rollback`, `dr-1-postgres-loss`, `dr-2-store-divergence`, `dr-3-ransomware`, `dr-4-full-datacenter`, `dr-5-partial-outage`, `drillhole-label-rename`, `evidence-model`, `hybrid-retrieval`, `ingestion-pipeline`, `llm-model-swap`, `log-retention`, `martin-tile-server`, `migration-rollback`, `neo4j-backup`, `on-call`, `qdrant-snapshot`, `redis-3-instance-rollout`, `redis-topology`, `refusal-rate-spike`, `retrieval-cache`, `retrieval-pipeline`, `retrieval-tuning`, `s3-abstraction`, `secret-management`, `secret-rotation`, `service-outage`, `validation-corpora`, `volume-migration`. Plus [`../runbooks/caddy_tls.md`](../runbooks/caddy_tls.md).

### 3.5 Specification + design docs — [`../architecture/`](../architecture/)

Files present on disk (per-spec design notes; expand as needed):

- `context_prep_spec.md` — Spine A (ADR-0009).
- `data_quality_flags_design.md` — DQ flag schema + writers.
- `document_versioning_design.md` — supersession + revision tracking.
- `golden_question_seed_loader_design.md` — YAML→`eval.golden_questions`.
- `multi_turn_resolution_spec.md` — multi-turn anaphora resolution.
- `parent_child_chunker_spec.md` — §3d parent-expansion chunker.
- `repair_loop_spec.md` — Spine B (ADR-0009).
- `reranker_v1_blockers.md` — reranker LoRA pipeline blockers.
- `shadow_telemetry_sentry_tags.md` — shadow-mode Sentry tag taxonomy.
- `six_subgraphs_spec.md` — codified by ADR-0006.
- `spatial_chat_card_audit_2026_05_29.md` — chat-card payload audit.
- `structured_answer_format_spec.md` — plan §4a 8-section format.
- `trace_logging_design.md` — `silver.query_traces` + `trace_writer.py`.
- `user_facing_error_catalog.md` — `GuardErrorCode` + i18n mapping.

> ⚠ **Flagged.** The reconciliation plan §3 references subdirs
> `docs/architecture/manual/`, `data_dict/`, `appendix/`, `notes/INDEX.md`
> as canonical. **These subdirs are not present on the live tree** at the
> time of this handover. Until reconciled, route into the files that DO
> exist (the list above + `georag-architecture.html` + `docs/adr/`).
> See §5.1 below.

### 3.6 Other doc subdirs

`docs/api/` (OpenAPI snapshot), `docs/audits/`, `docs/deployment/` (`k3s-quickstart.md`, `k8s-reference.md`), `docs/load_tests/`, `docs/parsers/`, `docs/security/`.

### 3.7 Frozen-reality evidence — [`../../ops/baselines/`](../../ops/baselines/) + [`../../ops/audit/`](../../ops/audit/)

Datastore + docker stats CSVs, PG before/after tuning, image-digest snapshots, resolved-compose YAML — operator drift-investigation artifacts.

---

## 4. Inventory totals (top-line counts)

| Surface | Count | Drill into |
|---|---|---|
| FastAPI endpoints | 109 | [`API_DOCUMENTATION.md`](API_DOCUMENTATION.md) §5 |
| FastAPI router files | 32 | [`API_DOCUMENTATION.md`](API_DOCUMENTATION.md) §5 |
| Pydantic AI agents (`@georag_agent`) | 42 | [`SAD.md`](SAD.md) §3 |
| LangGraph subgraphs | 3 | [`SAD.md`](SAD.md) §3 |
| Intent labels | 8 | [`SAD.md`](SAD.md) §3 |
| Hatchet workflow modules | 46 (live; manifest §4 still says 45) | [`DFS.md`](DFS.md) §2 + [`CICD_PIPELINE.md`](CICD_PIPELINE.md) §6 |
| Hatchet cron-triggered workflows | 30 declarations | [`CICD_PIPELINE.md`](CICD_PIPELINE.md) §6 |
| Dagster asset modules | 56 top-level + 5 `bronze_to_silver/` + 7 `silver_to_gold/` (live; manifest §5 still says 53 + 4 and omits silver_to_gold) | [`DFS.md`](DFS.md) §2 |
| Dagster schedules + sensor | 6 + 1 | [`CICD_PIPELINE.md`](CICD_PIPELINE.md) §6 |
| Dagster asset checks | 27 / 6 files | [`CICD_PIPELINE.md`](CICD_PIPELINE.md) §6 |
| Kestra flows | 3 | [`DFS.md`](DFS.md) §6 |
| PG tables | 174 / 15 schemas | [`DFS.md`](DFS.md) §4 |
| PG functions / triggers / MVs / extensions | 23 / 7 / 1 / 15 | [`DFS.md`](DFS.md) §4 |
| Neo4j labels / relationship types | 10 / 12 | [`DFS.md`](DFS.md) §4 |
| Qdrant collections | 9 | [`DFS.md`](DFS.md) §4 |
| Reverb channels / event classes | 30 / 11 | [`API_DOCUMENTATION.md`](API_DOCUMENTATION.md) §6 |
| Laravel routes (api / web) | 67 / 157 | [`API_DOCUMENTATION.md`](API_DOCUMENTATION.md) §3 + §4 |
| ADRs | 12 | §3.2 above |
| Operator runbooks | 38 | §3.4 above |
| GitHub Actions workflows | 7 | [`CICD_PIPELINE.md`](CICD_PIPELINE.md) §1 |
| Dockerfiles | 5 | [`CICD_PIPELINE.md`](CICD_PIPELINE.md) §4 |
| Compose services / overlays / volumes | 33 / 5 / 23 | [`SAD.md`](SAD.md) §2 + [`DFS.md`](DFS.md) §5 |
| Prometheus jobs / alerts / dashboards | 12 / 64 / 17 | [`SAD.md`](SAD.md) §4 |

Full mechanical inventory: [`../../HANDOVER_MANIFEST.md`](../../HANDOVER_MANIFEST.md).
Coverage checklist (loop terminator): [`../../COVERAGE.md`](../../COVERAGE.md).

---

## 5. Needs Confirmation (consolidated rollup)

Items flagged by code inspection or by drift between code and canonical spec. None auto-resolved — Kyle / operator decisions only.

### 5.1 Canonical-tree integrity
- **`docs/architecture/manual/`, `data_dict/`, `appendix/`, `notes/INDEX.md`** referenced by the reconciliation plan as canonical sources but **not present on the live tree**.
- **CLAUDE.md tech snapshot** lists `Qwen3-30B-A3B-Instruct AWQ`; live `docker-compose.yml` vLLM `command:` ships `Qwen/Qwen3-14B-AWQ` as default. Compose = build-time source of truth.
- **`SERVICE_INVENTORY.md`** references vLLM `v0.19.1`; live compose pins `v0.21.0`.
- **PgBouncer pin** — CLAUDE.md `edoburu 1.25`; compose `1.25.1-p0`.
- **Octane server** — CLAUDE.md "(Swoole/RoadRunner)"; env files `OCTANE_SERVER=swoole` everywhere.
- **Martin schema name** — `docker/martin/martin.yaml` targets `public_geo`; Phase-0 canonical is `public_geoscience`. Rename pending.
- **`HANDOVER_MANIFEST.md` §7a PG table totals** — manifest reports 174 tables / 15 schemas (migration-file scan); live `information_schema` introspection on 2026-05-29 returns 248 / 17. DFS §4.2 uses the live figures; manifest §7a needs a re-scan pass. Drift sources: silver +33 from CC-* waves + ADR-0010, audit +6 from 05-25 RLS sweep, workflow + usage growth.

### 5.2 Code observed but not exercised
- **Sentry** — `composer.json` requires it; `project_sentry_removed_2026_05_21` notes uninstall + `.env` wiring commented. Live state not re-verified.
- **Email / SMTP** — `config/mail.php` standard; grep of `app/` for `Mail::`, `Notification::`, `->notify(` returns zero. Outbound routes through Kestra (`support_packet_dispatch.yaml::MailSend`).
- **Slack** — `SLACK_BOT_USER_OAUTH_TOKEN` wired in `config/services.php`; no app code uses it. Kestra-mediated.
- **`services.basemap.styles`** referenced by `HandleInertiaRequests::share()`; no `basemap` key in `config/services.php`. Hardcoded fallback in frontend.
- **`pg_partman`** extension installed (`partman` schema); no `PARTITION OF` declarations in current migrations.
- **`pg_ivm`** extension installed; one MV (`silver.mv_collar_summary`) uses standard `REFRESH MATERIALIZED VIEW`.
- **Octane `tables.example:1000`** scaffold leftover in `config/octane.php`.
- **OpenAPI snapshot** at `docs/api/openapi.json` covers ~10 of 109 FastAPI endpoints.
- **Detailed FastAPI request/response shapes** outside the snapshot — contracts live in routers + Pydantic models + FormRequests.

### 5.3 Auth + secrets posture
- **`georag_app` + `martin_ro` PG roles never CREATEd** — `docker/postgresql/init/init-roles.sql` only creates the 3 NOLOGIN audit roles. 30+ migrations + Martin config reference `georag_app` / `martin_ro` for GRANTs but the roles themselves are not provisioned anywhere on disk. **Cold-start blocker** — fresh cluster's `artisan migrate` fails on first GRANT. Provisioning path TBD (likely SOPS-managed `CREATE ROLE … LOGIN PASSWORD …` extension to init-roles.sql). See DFS §4.1.1.
- **Sanctum `token_prefix`** empty default — set `SANCTUM_TOKEN_PREFIX=georag_` for secret-scanner detection.
- **Token expiration mismatch** — Sanctum bearer 480 min; session cookie 120 min.
- **Public-API external auth** — only Sanctum observed; no API-key / OAuth2.
- **`.sops.yaml`** referenced by O-01b preflight; not found at repo root.
- **Per-flow JWT rotation cadence** vs `flow_jwt_key_reaper` expiry window.
- **`HATCHET_CLIENT_TOKEN`** first-time provisioning path in cold-start runbook.
- **`EXTERNAL_NOTIFICATION_HMAC_SECRET`** distribution + revocation process.
- **Webhook subscription CRUD** — `/api/v1/webhooks` advertises registry; subscribe/unsubscribe surface in Kestra; endpoints not enumerated.
- **Auth methods beyond Sanctum** — architecture HTML may describe OIDC/SSO. Confirm.

### 5.4 Tenancy + multi-DB plumbing
- **Legacy `georag.workspace_id` GUC writers** — 13 Python files still set the legacy GUC instead of canonical `app.workspace_id`.
- **`georag_app` runtime role** — `SET ROLE` per transaction vs inherit?
- **`init-roles.sql` placement** — outside auto-init dir per `project_init_roles_gap`.

### 5.5 Deployment + CD
- **K3s / Helm** — `charts/georag/` + `kubernetes/manifests/` exist; `cd.yml` SSH+compose only.
- **Helm chart CD wiring** — no GitHub Actions workflow drives `helm upgrade`.
- **Migration execution location in CD** — `cd.yml` does not call `artisan migrate --database=pgsql_migrations`. Where on prod hosts?
- **`continue-on-error: true` debt in `cd.yml`** — 3 places gated on SOPS + SSH secrets.
- **Cosign image signing** — Trivy + SBOM wired; no cosign in `ci.yml`.
- **GHCR retention policy** not inspected.

### 5.6 Observability + backup
- **Backup target storage** (`backup-agent` + `compose.wal-archiving.yml`) — S3 / NFS / local target via env vars.
- **Qdrant snapshot cadence + target**.
- **Dagster persistence** (`dagster.yaml`) — Postgres vs local SQLite.
- **WAL upload destination + PITR receiver**.
- **Tempo + Loki on local filesystem** — Phase-11 SeaweedFS cutover.
- **OTel logs pipeline** dead-ends at `debug` exporter; Loki via Promtail Docker-stdout only.
- **Alertmanager production webhook endpoints** — dev placeholders in `alertmanager.yml`.
- **`PULSE_INGEST_DRIVER`** default `storage` (synchronous).

### 5.7 LLM + ML
- **Repair-loop production posture** — 4 `REPAIR_LOOP_*_ENABLED` flags default false. Staging order: SHADOW → TERMINAL → LOWCOST → FULL.
- **vLLM `--max-num-seqs` ceiling** on prod GPU.
- **vLLM `VLLM_GPU_MEM_UTIL` cohort policy** — compose default 0.93, but ≤ 0.80 required when hatchet-worker-ai co-tenants the dev A4500 (compose comment + memory note). Production GPU sizing decision needed.
- **`P04P_DUAL_WRITE_ENABLED`** live state per environment.
- **`ANTHROPIC_MODEL=claude-opus-4-7`** — internal capability-tier vs literal Anthropic id?
- **Anthropic prompt-cache** prod TTL.
- **ADR-0008 bge-small fine-tune** — accepted 2026-05-27; synthetic-MLM corpus + contrastive triplets approach. Promotion gate via `services/eval/promotion_gate.py`. Checkpoint storage location for *bge-small* not yet pinned (reranker uses `s3://reranker-checkpoints/`).
- **ADR-0011 reranker fine-tune** — Proposed but **on HOLD** after 2026-05-29 double-verdict (full FT + LoRA both lost to stock on only 27 distinct production queries). Do NOT re-pitch until real user query volume arrives. MLM-adapted backbone preserved at `s3://reranker-checkpoints/v1/run_id=2026-05-29-mlm-extended/`.
- **ADR-0012 promotion** (Proposed).
- **ADR-0004 SME sign-off** (Proposed).
- **Reranker checkpoint bucket** — `reranker-checkpoints` referenced by SAD §3.3.5 but not declared in compose / not in DFS §5.1 bucket list. Provisioning path TBD.
- **`MODEL_ROUTING_ENABLED` + `SYSTEM_PROMPT_ROUTING_ENABLED` rollout posture** — both gate routing in `app/agent/model_routing.py`. Production posture per workspace not documented.
- **`workspace.prompt_versions` pinning UX** — `Admin/AgentConfig/Prompts` Inertia surface; per-workspace version-pin policy not specified.

### 5.8 Performance + scaling
- **3-instance Redis rollout** status — `REDIS_QUEUE_HOST` env scaffolding present.
- **PHPStan level-6 trajectory** — tighten vs freeze.
- **`PARSE_SUBPROCESS_MAX_WORKERS`** unset in `.env.example`.
- **Inertia shared-prop per-render PG hits** — 4 props fire DB queries on every render.
- **Hatchet engine** `SERVER_GRPC_INSECURE=t` + `SERVER_AUTH_COOKIE_INSECURE=t` + `SERVER_AUTH_SET_EMAIL_VERIFIED=t` — prod hardening checklist.
- **Neo4j `NEO4J_AUTH=none` → real-password migration trap**.

### 5.9 Test surface
- **`chaos.yml` / `perf-baseline.yml` / `tenant-isolation-auditor.yml`** cron expressions.
- **`e2e.yml` internals** — Playwright suite + trigger conditions.
- **`release-rehearsal.yml`** full job graph.

---

## 6. Operator / contact handoff

- **Hosting target**: On-premise / private cloud. SSH + `docker compose` is the canonical production deploy primitive per `cd.yml`. Helm chart alternative exists under `charts/georag/` (vanilla / k3s / airgap overlays).
- **Image registry**: `ghcr.io/${OWNER}/georag-{fastapi,laravel,dagster}` with `:<short-sha>` + `:main` tags. Trivy-scanned, SBOM-attested 90-day retention.
- **Secrets**: SOPS + age. `SOPS_AGE_PRIVATE_KEY` per GitHub Environment. Plaintext template at `.env.production.example` (140 keys). Encrypted file `.env.production.enc` at repo root.
- **SME / domain owner**: Kyle Maguire. Per `feedback_graham_not_reviewing` — Graham is NOT in the review loop.
- **Cold-start procedure**: [`../OPERATOR-AFTERNOON.md`](../OPERATOR-AFTERNOON.md) + `bash scripts/operator/preflight.sh` (O-01..O-07 gates).
- **On-call**: [`../../ops/runbooks/on-call.md`](../../ops/runbooks/on-call.md).
- **License**: No `LICENSE` file published. All rights reserved. Source shared for review only; no permission for redistribution / commercial use without written agreement. Third-party deps restricted to MIT / BSD / Apache 2.0 / MPL-2.0.

---

*End of `HANDOVER_INDEX.md`. Update via the reconciliation flow: append new findings to `HANDOVER_MANIFEST.md`, tick `COVERAGE.md`, then revise the relevant doc — never edit this file in isolation.*
