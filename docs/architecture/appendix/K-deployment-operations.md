# Appendix K — Deployment + Operations

Status: **Rewritten 2026-10-06.** A short pointer page. The previous body
described a Kestra / Dagster / Neo4j / Caddy / Prometheus / Grafana stack
and a single-host Docker Compose production; none of that exists any more,
and the detail now lives with the deployment artifacts themselves.

There are three deployment surfaces.

## 1. Development — Docker Compose

`docker-compose.yml` defines 16 services. The `profiles:` key on each
service is the truth; the header comment in the file summarises it.

| Profile | Services |
|---|---|
| *(none — always starts)* | `postgresql` (PostgreSQL 18 + PostGIS 3.6), `pgbouncer`, `redis`, `martin` |
| `dev-light` | `laravel-octane`, `laravel-horizon`, `laravel-reverb` |
| `dev-data` | `fastapi`, the `embedding` / `sparse` / `reranker` model sidecars (the reranker reserves the GPU), `qdrant`, `minio` (SeaweedFS) + `minio-init`, `hatchet-lite` + `hatchet-worker` |
| `dev-full` | everything |

```bash
cp .env.example .env        # fill in secrets; HATCHET_CLIENT_TOKEN and
                            # S3_SECRET_KEY / MINIO_ROOT_PASSWORD are required
docker compose --profile dev-light --profile dev-data up -d   # a working day
docker exec georag-laravel-octane php artisan migrate --database=pgsql_migrations
```

`HATCHET_CLIENT_TOKEN` comes from `hatchet-admin token create` against the
running `hatchet-lite` (see the comment above the `hatchet-worker` service
in `docker-compose.yml`). Host ports bind to `GEORAG_BIND_ADDR`
(default `127.0.0.1`).

Optional overlays in `docker/`: `compose.langfuse.yml`,
`compose.redis-staging.yml`.

## 2. Production — AWS ECS Fargate

Production runs on Amazon ECS Fargate since 2026-09-08
([ADR-0022](../../adr/0022-aws-replaces-azure-as-the-production-cloud.md)):
ten services, no GPU, RDS for PostgreSQL 18 (no pooler), S3, Redis on ECS
with EFS. Everything is Terraform in
[`deploy/aws/terraform/`](../../../deploy/aws/terraform/), applied from
GitHub Actions (`.github/workflows/terraform.yml`, `cd.yml`).

- **Start here:** [`deploy/aws/README.md`](../../../deploy/aws/README.md) —
  preconditions, remote state, the public edge (CloudFront by default, or
  the ALB with a domain), the power switch, and Steps 0–4 for a first
  deployment.
- **Cutover gate:** `AWS_REGION=<region> bash scripts/operator/aws-preflight.sh`.
- **Nightly stop/start:** EventBridge Scheduler
  (`deploy/aws/terraform/scheduler.tf`) scales ECS and stops RDS.
- **Alerting:** CloudWatch log-marker metric filters and alarms → one SNS
  email receiver (`deploy/aws/terraform/alerts.tf`).
- **Backups:** RDS automated point-in-time restore, 35 days
  (`db_backup_retention_days`); S3 versioning for object storage. Qdrant
  is derived data, rebuilt by re-embedding.
- **On-call:** [`ops/runbooks/aws-oncall.md`](../../../ops/runbooks/aws-oncall.md),
  with `secret-rotation.md`, `refusal-rate-spike.md` and `raw-sql-layer.md`
  alongside it in `ops/runbooks/`.
- **Operator procedures** (PII decryption, `APP_KEY` rotation, secrets):
  [`docs/RUNBOOK.md`](../../RUNBOOK.md).

## 3. On-prem — Helm chart

[`charts/georag/`](../../../charts/georag/) with the
`values-vanilla.yaml`, `values-k3s.yaml` and `values-airgap.yaml` profiles;
raw manifests generated from it live in
[`kubernetes/manifests/`](../../../kubernetes/manifests/). The chart pins
`EMBEDDING_BACKEND=local` (self-hosted Qwen3 embedding). The SOPS + age
tooling in `scripts/operator/` (`bootstrap-secrets.sh`, `preflight.sh`)
belongs to this path, not to AWS. Details:
[Appendix L](L-kubernetes-and-airgap.md).
