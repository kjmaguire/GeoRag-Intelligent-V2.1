# GeoRAG — migrate the deployment from Azure to AWS

## Context

GeoRAG currently runs on Azure Container Apps. The Azure credits are gone, so the
platform needs to run on AWS instead. **No data migration is required** — this is a
fresh deployment of the code. Nothing in the Azure Postgres, Qdrant, Redis or Blob
storage needs to come across.

That constraint removes the scariest item in a normal migration: switching the embedding
backend usually forces a full re-embed of every passage, but with no corpus to preserve
there is nothing to re-embed. It also means the Qdrant collection can be recreated at
whatever vector dimension the chosen embedding model needs, for free, right now.

**Cohere stays.** Every LLM-family capability — chat, embeddings, reranking, OCR —
continues on Cohere models. The question is only how they are reached from AWS.

## Read these first — they are trustworthy

`docs/architecture/manual/` was fully reconciled against the code on 2026-09-07.
Every chapter opens with a dated note saying what was verified and what was not.
Prefer it over `georag-architecture.html`, which is design intent and describes a
system substantially larger than what exists.

Start with: Ch 00 §5 and §7 (production topology; what the reconciliation did and did
not verify), Ch 01 (the 16 compose services and 9 Azure Container Apps), Ch 02 (data
stores, roles, extensions, backup posture), Ch 07 (Horizon's 3 jobs, Hatchet's 51
workflows and every cron, the Azure scheduler jobs), Ch 08 (the Cohere backends and the
one model with no managed equivalent), Ch 12 (Log Analytics, alert rules, what is and is
not watched). Then `deploy/azure/README.md` and `ops/runbooks/azure-oncall.md`.

## Cohere on AWS — the key structural insight

Read `src/fastapi/app/config.py`, `app/services/embedding.py`, `app/services/reranker.py`
and `app/services/ingest/cohere_parse_client.py` before designing anything here.

Foundry was acting as a proxy, and **three of the four adapters already speak Cohere's
native v2 API**, just with an Azure base URL and Azure auth:

  - embeddings  →  `{endpoint}/providers/cohere/v2/embed`
  - reranking   →  `{endpoint}/providers/cohere/v2/rerank`
  - OCR/parse   →  `{endpoint}/providers/cohere/v2/parse`   (ADR-0019)

For those three, moving to Cohere's own API is plausibly a **base URL and credential
change**, not a rewrite. Confirm that against the actual request/response handling
before relying on it.

**The chat path is the exception.** It goes through Foundry's OpenAI-compatible surface
at `{endpoint}/openai/v1/chat/completions`, not Cohere's native `/v2/chat`. Three
behaviours were empirically confirmed against a live deployment on 2026-07-30 and are
documented in `app/config.py`'s `AZURE_FOUNDRY_*` block: JSON `response_format` is
supported, reasoning arrives in a **separate `reasoning_content` field**, and Cohere
wraps JSON output in `<|START_TEXT|>` / `<|END_TEXT|>` sentinel tokens that the client
strips. Whatever route you choose, these three behaviours must be re-verified — do not
assume they carry over.

### The routes, to put to Kyle as a decision

  (a) **Cohere API direct** (`api.cohere.com`). Least change for embed/rerank/parse.
      Chat needs either a Cohere-native `/v2/chat` adapter or an OpenAI-compatible
      shim. Cloud-agnostic, which is worth something given why this migration is
      happening at all. Separate vendor billing; needs egress.

  (b) **Amazon Bedrock.** AWS-native, IAM/SigV4 auth instead of API keys, billing
      through AWS. But every adapter needs rework to Bedrock's API shape, and
      **model version parity must be checked first**: this deployment uses
      Command A+ (`Cohere-command-a-plus-05-2026`), Embed v4, Rerank v4 and Parse v5.
      Bedrock's Cohere catalogue may lag those versions, may not carry Parse at all,
      and availability varies by region. Verify what is actually offered in the target
      region before committing. If the embedding model changes, keep the output at
      **1024 dimensions** to match the `georag_chunks` schema — or recreate the
      collection, which is free here since there is no data.

  (c) **Cohere on SageMaker.** Most control, most ops burden. Probably only worth it
      if (a) and (b) both fall short.

`LLM_BACKEND` currently accepts `azure | vllm | anthropic` — there is no `bedrock` or
`cohere` value, so a new backend adapter is new code in every route except a
proxy-shaped one. `EMBEDDING_BACKEND` and `RERANKER_BACKEND` both default to `foundry`,
in code *and* in compose, so an unset value silently selects a host that will not exist
on AWS. Rename or repoint those values deliberately and loudly.

**SPLADE++ has no managed equivalent anywhere, including on Cohere.** It runs in the
`sparse` sidecar and had no Foundry counterpart; it will have no Bedrock counterpart.
It is self-hosted on AWS or the sparse leg of hybrid retrieval does not exist. Decide
deliberately and state which you chose — this is the one place where "we use Cohere for
everything LLM" does not cover the requirement.

Note also that the Parse v5 wire shape was **never empirically verified** —
`ops/validation/cohere_parse_probe.sh` exists for exactly this. `app/services/ingest/
ocr_engine.py` deliberately makes retired engine values fail loudly; extend that
pattern rather than adding a silent new path.

## The rest of the Azure coupling

**Object storage — already abstracted, the easy one.** `STORAGE_BACKEND` is a two-value
switch (`s3_compatible` | `azure_blob`) honoured by *both* the Python
`src/georag_object_storage/` package and Laravel's `config/filesystems.php:54`. The
`s3_compatible` path is what dev uses against SeaweedFS, so pointing it at real S3
should be configuration. Verify endpoint/region handling in `s3_config.py`,
`sync_client.py` and `async_client.py`. Dropping `league/flysystem-azure-blob-storage`
and the Azure clients is cleanup, not blocking. Bonus: Azure's `allowSharedKeyAccess`
is still enabled purely because Laravel's `temporaryUrl()` signs with the account key
and the Azure SDK has no user-delegation SAS — S3 presigned URLs are native, so that
wart disappears.

**Postgres extensions — the biggest technical risk. Investigate before choosing a
service.** `docker/postgresql/init/*.sql` requires postgis, postgis_topology,
postgis_raster, h3, h3_postgis, pg_partman, pg_trgm, pg_stat_statements, uuid-ossp,
hypopg, pg_ivm, pg_repack, pg_stat_kcache and auto_explain. On Azure Flexible Server
`h3` sat outside the `azure.extensions` allow-list and was a known problem. RDS and
Aurora have their own allow-lists and several of these may be unavailable. **Check the
real supported-extension list for the target engine version before committing to
managed Postgres.** If it falls short, self-managed Postgres on EC2 or a container is
the fallback — a cost and ops decision for Kyle, not one to make silently. Separate the
load-bearing extensions from the diagnostics (`auto_explain`, `pg_stat_kcache`).

**Compute.** Nine Container Apps → ECS Fargate, App Runner or EKS: `laravel-octane-cc`
(the only public ingress, currently 1/1 replicas), `laravel-horizon-cc`,
`laravel-reverb-cc`, `fastapi-cc`, `hatchet-cc`, `hatchet-worker-cc` (4 vCPU / 8 GiB,
maxReplicas 1), `qdrant-cc`, `redis-cc`, `martin-cc`. Reverb needs sticky WebSocket
support at the load balancer. Qdrant needs persistent storage — it had *no* persistent
volume on Azure, which is a bug to fix in the move, not replicate. If SPLADE++ or any
model sidecar is self-hosted, size for it here.

**Scheduled shutdown/startup.** `deploy/azure/containerapps/{shutdown,startup}-job.yaml`
fire at `0 6,7 * * *` and `0 13,14 * * *` UTC with an in-script DST guard, because ACA
Jobs have no timezone support. **EventBridge Scheduler supports timezones natively**, so
the double-fire-plus-guard mechanism collapses to one schedule.
`scripts/check_scheduler_job_parity.py` and the "Nightly scheduler jobs" CI check
enforce the Azure shape and need rewriting or retiring.

**Observability — a rewrite, not a port.** Everything is Azure Monitor and Log
Analytics: `ContainerAppConsoleLogs_CL`, KQL, ~15 baseline metric alerts plus the 12
rules in `deploy/azure/alerts/create-alerts.sh`. Several match *marker log lines*
(`ANSWER_QUALITY_REGRESSION`, `COST_BURN_THRESHOLD_EXCEEDED`, `QDRANT_PARTIAL_LOSS`)
because no metrics are scraped — Ch 12 §1.3. CloudWatch Logs Insights can do the
equivalent, but every query and threshold must be rewritten. Read Ch 12 before assuming
what is monitored: two `/metrics` endpoints exist that nothing scrapes, and Laravel
Pulse collects data nobody can view in production.

**CD and registry.** `.github/workflows/cd.yml` builds and rolls out the fastapi and
laravel images and runs `laravel-migrate-job` — **migrations only**. `php artisan
db:apply-raw` is a manual operator step, so anything created only in `database/raw/`
has never existed on Azure; see `ops/runbooks/raw-sql-layer.md` and
`scripts/raw-parity-baseline.txt`. Do not let that trap repeat on AWS. ACR → ECR,
managed identity → IAM task roles, Container Apps secrets → Secrets Manager or SSM.

**Also Azure-referencing:** `.env.production.example`, `charts/georag/`,
`ops/runbooks/azure-oncall.md`, `ops/runbooks/secret-rotation.md`, ADRs 0019/0020/0021,
and the "Deployment manifest invariants" CI check. `GEORAG_ENV=production` gates
`main.py::_assert_production_posture`, the only thing that reports a security control
being off — make sure it is set.

## How to work

1. **Work in this repository on a branch — do not create a new repo.** The Azure
   coupling is a thin perimeter and the provider abstractions already exist; a second
   copy of the codebase would fork every future migration, guard and fix. Build
   `deploy/aws/` alongside `deploy/azure/`, then remove the Azure tree in a final
   commit, carrying its embedded reasoning (probe rationale, the DST double-fire trap,
   the measured alert thresholds) into the AWS equivalents or an ADR rather than
   discarding it.
2. **Plan before building.** Produce a migration plan naming the AWS service for each
   tier, and put the open decisions to Kyle before implementing them: compute platform;
   managed vs self-managed Postgres; Cohere route (a), (b) or (c); whether SPLADE++
   sparse retrieval survives.
3. **Verify against the code, not the architecture doc.** The reconciliation found the
   docs described a system considerably larger than the one that exists.
4. **Do not silently drop capability.** If something has no AWS equivalent, say so
   plainly and record it, the way Ch 12 records the observability gaps.
5. Follow CLAUDE.md's hard rules — unchanged by the cloud move. Async-native drivers
   only in FastAPI, Octane-safe Laravel, citations mandatory, no Streamlit, MapLibre
   not Mapbox, no knowledge graph.
6. Conventional-commit messages, open a draft PR, and do not un-draft or merge unless
   Kyle asks.
7. Write an ADR for the cloud move. ADR-0020 and ADR-0021 cover the Azure storage and
   Foundry decisions and will be superseded — follow the `.claude/skills/adr-template`
   style.

## Known-broken things you will trip over — pre-existing, not caused by the move

- `gold.h3_density_mineral` has no writer at all.
- The `integration`, `golden`, `hallucination` and `live` markers (377 tests, ~12% of
  the Python suite) run in no workflow at all — see `release-rehearsal.yml`'s header.
  Do not read a green PR suite as full coverage.
- Nothing in production measures answer quality.
- Blob storage was the one irreplaceable copy, with no backup workflow and no restore
  procedure. **This is a chance to fix that properly on S3 rather than carry it over.**
</content>
</invoke>
