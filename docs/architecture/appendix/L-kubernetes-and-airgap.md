# Appendix L — Kubernetes, Helm Chart, and Air-Gap Install

Status: **Draft.** Pulls in the Helm-chart + air-gap deployment surface
that the earlier appendices missed. Companion to
[Appendix K](K-deployment-operations.md) (Docker Compose deployment).

> **Two deployment surfaces coexist:**
> - **Docker Compose** — primary dev workstation flow + small single-host
>   prod ([Appendix K](K-deployment-operations.md)).
> - **Kubernetes** — prod + on-prem / air-gap, via the Helm chart in
>   [charts/georag/](../../../charts/georag/) and / or the raw manifests
>   in [kubernetes/manifests/](../../../kubernetes/manifests/) (this
>   appendix).

## 1. Layout

```
charts/georag/
├── Chart.yaml
├── README.md
├── templates/                 (the Helm templates)
├── values.yaml                (defaults — production-ish)
├── values-vanilla.yaml        (stock k8s flavour)
├── values-k3s.yaml            (k3s flavour — air-gap target)
└── values-airgap.yaml         (additional air-gap overrides)

kubernetes/manifests/
├── README.md
├── vanilla.yaml               (stock k8s)
├── k3s.yaml                   (k3s)
└── airgap.yaml                (air-gapped k3s)

airgap/
├── README.md
└── install.sh                 (§11.8 — single-command installer)
```

## 2. The air-gap installer

Source: [airgap/install.sh](../../../airgap/install.sh).

**What it does:**
1. Detects the K3s container runtime (containerd vs Docker).
2. Loads every image in `images/*.tar` (shipped in the air-gap tarball).
3. Re-tags each image with the configured private-registry prefix
   (`registry.internal.local/georag/` by default; override via
   `--registry`).
4. Installs the chart via Helm using the bundled `.tgz` — no internet.
5. Prints a status summary.

**Usage:**
```bash
./install.sh \
  [--namespace georag] \
  [--registry my.registry.local/georag] \
  [--secrets-file path/to/secrets.env]
```

The tarball must include:
- `images/*.tar` (every pinned image as `docker save` archives)
- `charts/georag-*.tgz` (packaged Helm chart)
- `secrets.env.template` (operator-filled)

## 3. Helm values flavours

| Flavour | When | Differences from `values.yaml` |
|---|---|---|
| `values-vanilla.yaml` | Generic upstream Kubernetes cluster | Standard Ingress (NGINX assumed), default StorageClass |
| `values-k3s.yaml` | Single-node k3s host | Uses `traefik` Ingress, `local-path` StorageClass, smaller resource requests |
| `values-airgap.yaml` | Air-gapped k3s (no internet) | All `image.registry` set to `registry.internal.local/georag`, `imagePullPolicy=IfNotPresent`, ACME disabled |

Bring up:
```bash
helm install georag charts/georag \
  -f charts/georag/values-vanilla.yaml \
  -f my-overrides.yaml \
  --namespace georag --create-namespace
```

## 4. Raw manifests (chart-free path)

[kubernetes/manifests/](../../../kubernetes/manifests/) hosts pre-rendered
manifests for the same three flavours. Used when Helm is unavailable on
the target cluster (some hardened on-prem environments don't allow
runtime template rendering).

```bash
kubectl apply -f kubernetes/manifests/vanilla.yaml
```

## 5. What changes vs Docker Compose deployment

| Concern | Docker Compose ([Appendix K](K-deployment-operations.md)) | Kubernetes (this appendix) |
|---|---|---|
| Service composition | `docker-compose.yml` profiles | Helm chart `templates/` |
| Stop-grace | `stop_grace_period:` per service | `terminationGracePeriodSeconds:` per Pod |
| Healthchecks | `healthcheck:` per service | `livenessProbe` + `readinessProbe` + `startupProbe` per container |
| Resource limits | `deploy.resources.limits` | `resources.requests/limits` |
| Secrets | `.env` + `:?` enforcement | `Secret` resources + `secrets-file` to the installer |
| Storage | Named volumes | `PersistentVolumeClaim`s (one per stateful service) |
| Network isolation | Single `georag` bridge | `NetworkPolicy` per namespace |
| TLS | Caddy edge (internal CA or ACME) | Ingress controller TLS (cert-manager planned, manual for air-gap) |
| GPU | `deploy.resources.devices: nvidia` | NVIDIA device plugin → `resources.limits.nvidia.com/gpu` |
| Reverb scaling | Single container | StatefulSet, n=1 (Reverb's broadcast does not horizontally scale yet) |

## 6. Stateful workloads — PVC sizing per flavour

| Workload | PVC name | Vanilla | k3s | Air-gap k3s |
|---|---|---|---|---|
| postgresql | `postgres-data` | 500 Gi | 100 Gi | 100 Gi |
| postgres WAL archive | `pg-wal-archive` | 100 Gi | 20 Gi | 20 Gi |
| neo4j | `neo4j-data` | 200 Gi | 50 Gi | 50 Gi |
| qdrant | `qdrant-data` | 200 Gi | 50 Gi | 50 Gi |
| redis | `redis-data` | 10 Gi | 5 Gi | 5 Gi |
| seaweedfs | `seaweedfs-data` | 2 Ti | 200 Gi | 200 Gi |
| vllm HF cache | `vllm-hf-cache` | 100 Gi | 50 Gi | 50 Gi |
| fastapi HF cache | `fastapi-hf-cache` | 20 Gi | 10 Gi | 10 Gi |
| dagster home | `dagster-home` | 20 Gi | 5 Gi | 5 Gi |
| grafana | `grafana-data` | 10 Gi | 5 Gi | 5 Gi |
| tempo | `tempo-data` | 100 Gi | 20 Gi | 20 Gi |
| loki | `loki-data` | 100 Gi | 20 Gi | 20 Gi |
| caddy | `caddy-data` | 1 Gi | 1 Gi | 1 Gi |
| rapidocr models | `rapidocr-models` | 5 Gi | 5 Gi | 5 Gi |

Exact values live in the `values-*.yaml` files; this table is the
contract.

## 7. GPU on Kubernetes

- Install the **NVIDIA device plugin** DaemonSet
  (`nvidia/k8s-device-plugin`).
- Annotate the GPU node:
  `kubectl label node <node> nvidia.com/gpu.present=true`.
- vLLM Deployment requests `resources.limits.nvidia.com/gpu: 1` with
  `nodeSelector: nvidia.com/gpu.present=true`.
- The Hatchet-worker-ai Deployment shares the same node (anti-affinity
  toggles whether co-locate or split).

## 8. Air-gap image bundle

Build the bundle on an internet-connected host that mirrors the prod
deployment:

```bash
mkdir -p airgap/dist/images
# pull every pinned image (lockfile = .image-digests, see compose top
# header). Then:
for img in $(cat .image-digests); do
  docker pull "$img"
  docker save "$img" -o "airgap/dist/images/$(echo $img | tr '/:@' '___').tar"
done
helm package charts/georag -d airgap/dist/charts/
tar czf georag-airgap-$(date +%F).tar.gz airgap/dist/
```

Ship to the air-gap host; unpack; run `install.sh`.

## 9. Cross-references to runbooks

The deep operational detail lives in [ops/runbooks/](../../../ops/runbooks/) —
that directory has been part of the repo from day one but was missing
from this manual until now. Indexed below; promote any of these into a
chapter when they age into doctrine.

| Runbook | Topic |
|---|---|
| [authz-audit-triage.md](../../../ops/runbooks/authz-audit-triage.md) | Investigating authz audit log spikes |
| [backup-restore.md](../../../ops/runbooks/backup-restore.md) | End-to-end backup + restore drill |
| [citation-pipeline.md](../../../ops/runbooks/citation-pipeline.md) | Citation lifecycle debugging |
| [claude-code-mcp-migration.md](../../../ops/runbooks/claude-code-mcp-migration.md) | Migrating Claude Code MCP servers |
| [cold-start.md](../../../ops/runbooks/cold-start.md) | Full-stack cold start |
| [container-hardening.md](../../../ops/runbooks/container-hardening.md) | Non-root user migration per service |
| [data-version.md](../../../ops/runbooks/data-version.md) | `workspaces.data_version` semantics |
| [datastore-tuning.md](../../../ops/runbooks/datastore-tuning.md) | PG / Neo4j / Qdrant tuning |
| [dem-self-host.md](../../../ops/runbooks/dem-self-host.md) | Self-hosted DEM tile service |
| [deploy-rollback.md](../../../ops/runbooks/deploy-rollback.md) | Rolling back a bad deploy |
| [dr-1-postgres-loss.md](../../../ops/runbooks/dr-1-postgres-loss.md) | DR drill 1: Postgres total loss |
| [dr-2-store-divergence.md](../../../ops/runbooks/dr-2-store-divergence.md) | DR drill 2: PG ↔ Qdrant ↔ Neo4j divergence |
| [dr-3-ransomware.md](../../../ops/runbooks/dr-3-ransomware.md) | DR drill 3: ransomware response |
| [dr-4-full-datacenter.md](../../../ops/runbooks/dr-4-full-datacenter.md) | DR drill 4: full datacenter loss |
| [dr-5-partial-outage.md](../../../ops/runbooks/dr-5-partial-outage.md) | DR drill 5: partial outage |
| [drillhole-label-rename.md](../../../ops/runbooks/drillhole-label-rename.md) | Renaming the canonical `:DrillHole` label |
| [evidence-model.md](../../../ops/runbooks/evidence-model.md) | Evidence-item lifecycle |
| [hybrid-retrieval.md](../../../ops/runbooks/hybrid-retrieval.md) | Debugging hybrid retrieval results |
| [ingestion-pipeline.md](../../../ops/runbooks/ingestion-pipeline.md) | Ingest pipeline triage |
| [llm-model-swap.md](../../../ops/runbooks/llm-model-swap.md) | Swapping the served LLM model |
| [log-retention.md](../../../ops/runbooks/log-retention.md) | Log retention policy + Loki cleanup |
| [martin-tile-server.md](../../../ops/runbooks/martin-tile-server.md) | Martin tile server triage |
| [migration-rollback.md](../../../ops/runbooks/migration-rollback.md) | Rolling back a Laravel migration |
| [neo4j-backup.md](../../../ops/runbooks/neo4j-backup.md) | Neo4j online-dump procedure |
| [on-call.md](../../../ops/runbooks/on-call.md) | On-call rotation contract |
| [qdrant-snapshot.md](../../../ops/runbooks/qdrant-snapshot.md) | Qdrant snapshot + restore |
| [redis-3-instance-rollout.md](../../../ops/runbooks/redis-3-instance-rollout.md) | Future 3-instance Redis topology rollout |
| [redis-topology.md](../../../ops/runbooks/redis-topology.md) | Current Redis topology decisions |
| [refusal-rate-spike.md](../../../ops/runbooks/refusal-rate-spike.md) | Investigating sudden refusal-rate climbs |
| [retrieval-cache.md](../../../ops/runbooks/retrieval-cache.md) | Retrieval cache layer triage |
| [retrieval-pipeline.md](../../../ops/runbooks/retrieval-pipeline.md) | Retrieval pipeline triage |
| [retrieval-tuning.md](../../../ops/runbooks/retrieval-tuning.md) | Retrieval / reranker tuning loop |
| [s3-abstraction.md](../../../ops/runbooks/s3-abstraction.md) | S3 / SeaweedFS abstraction layer |
| [secret-management.md](../../../ops/runbooks/secret-management.md) | Secret management overview |
| [secret-rotation.md](../../../ops/runbooks/secret-rotation.md) | Per-secret rotation procedure |
| [service-outage.md](../../../ops/runbooks/service-outage.md) | Generic service-outage triage |
| [validation-corpora.md](../../../ops/runbooks/validation-corpora.md) | Validation corpus maintenance |
| [volume-migration.md](../../../ops/runbooks/volume-migration.md) | Moving a named volume between hosts |

## 10. Other operational artifacts in `ops/`

| Directory | Holds |
|---|---|
| [ops/audit/](../../../ops/audit/) | Dated infrastructure / datastore audit reports (2026-04-19 et al.) — these are the post-hoc evidence that backs the §B fix history |
| [ops/backlog/](../../../ops/backlog/) | Per-module intake documents driving the phased build-out (module-3 through module-10) |
| [ops/baselines/](../../../ops/baselines/) | Performance baselines (idle, post-tuning) for PG / Neo4j / Qdrant / Docker |
| [ops/decisions/](../../../ops/decisions/) | Smaller-than-ADR decisions (e.g., Neo4j 2026.03.1 image pin) |
| [ops/migrations/](../../../ops/migrations/) | Historical migration plans (evidence-model, Neo4j label renames) |
| [ops/observability/](../../../ops/observability/) | Observability-specific operator notes (currently sparse) |
| [ops/setup/](../../../ops/setup/) | Setup helper scripts: `apply_n8n_langfuse_secrets.sh`, `bump_postgres_limits.py`, `fix-gpu-passthrough.sh`, `verify-gpu.sh`, `sync_windows_to_wsl.sh` |
| [ops/tests/](../../../ops/tests/) | Operational test artifacts (separate from `tests/`) |
| [ops/validation/](../../../ops/validation/) | Validation reports |
| [ops/reviews/](../../../ops/reviews/) | Dated review writeups |
| [ops/postgis/](../../../ops/postgis/) | PostGIS-specific operational notes |
| [ops/neo4j/](../../../ops/neo4j/) | Neo4j-specific operational notes |
| [ops/charts/](../../../ops/charts/) | Helm chart auxiliary notes (parallel to `charts/georag/`) |

## 11. `scripts/` — 264 utility scripts

`scripts/` holds ~264 utility scripts (`_p17_*`, `_p18_*`, … per
overnight-run sweeps, plus general utilities). Treat the whole
directory as **experimental scaffolding**: anything still needed
graduates into a Make target, a Hatchet workflow, or a CLI artisan
command. The `_archived/` subdirectory is the cemetery.
