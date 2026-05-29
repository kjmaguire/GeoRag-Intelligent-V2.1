# GeoRAG Helm chart (skeleton)

**v1.5-14** — Kubernetes/Helm starting point. **Not production-ready.**

## What this chart deploys

- `laravel-octane` Deployment + Service
- `fastapi` Deployment + Service
- ConfigMap + Secret with the env surface needed by both
- (Stubs for `horizon`, `reverb`, `dagster` exist in `values.yaml` but
  are not yet templated)

## What this chart does NOT deploy

The datastores. PostgreSQL, Redis, Neo4j, Qdrant, SeaweedFS, Ollama,
vLLM, and Martin are intentionally external — point at your preferred
community charts (bitnami, neo4j-helm, etc.) and pass connection
details under `externalServices.*` in `values.yaml`.

## Prerequisites

- Helm 3.14+
- A Kubernetes cluster with:
  - The georag-* container images available (build via `cd.yml` in
    this repo, push to your registry, set `global.imageRegistry` and
    `global.imageRepoOwner`)
  - PostgreSQL 18 + PostGIS 3.6 (with the schema from
    `database/migrations/` applied)
  - Redis 8.x, Neo4j Community 2026.03+, Qdrant 1.17+
  - SeaweedFS S3-compatible endpoint
  - Ollama (or vLLM on x86_64) reachable for LLM calls
- helm-secrets plugin if `global.useSops=true`

## Install

```bash
helm install georag ops/charts/georag \
    --namespace georag --create-namespace \
    --values your-values.yaml
```

## Maturity gates before this is production

| Gate | Owner | Status |
|------|-------|--------|
| Network policies for tenant isolation | Module 9 / DevOps | **TEMPLATED** — toggle `networkPolicy.enabled=true` |
| HorizontalPodAutoscaler | DevOps | **TEMPLATED** — toggle `autoscaling.enabled=true` |
| PodDisruptionBudget | DevOps | **TEMPLATED** — toggle `podDisruptionBudget.enabled=true` |
| Ingress + cert-manager TLS | DevOps | **TEMPLATED** — toggle `ingress.enabled=true` + `ingress.tls.enabled=true` |
| Prometheus ServiceMonitor | DevOps | **TEMPLATED** — toggle `serviceMonitor.enabled=true` (needs Prometheus Operator CRDs) |
| PVC strategy for SeaweedFS | DevOps | TODO — externalised; bring via SeaweedFS chart |
| Loki / Promtail / Alertmanager subcharts | DevOps | TODO — bring via grafana/loki-stack chart |
| SOPS-encrypted secret values | DevOps | TODO — install helm-secrets plugin; switch `global.useSops=true` |
| FluentBit / sidecar log forwarding | DevOps | TODO — depends on chosen log aggregator |
| Cluster-autoscaler config for GPU nodes (vLLM) | DevOps | TODO — cluster-level, not chart-level |

**Templated** items render Kubernetes resources from this chart and need
only env-specific values. **TODO** items are deliberately externalised
to community charts because they're not GeoRAG-specific.

### Production rollout sequence

```bash
# 1. Bring up datastore charts in your data namespace.
helm install postgres bitnami/postgresql --namespace data ...
helm install redis    bitnami/redis      --namespace data ...
helm install neo4j    neo4j/neo4j        --namespace data ...
helm install qdrant   qdrant/qdrant      --namespace data ...

# 2. Install Prometheus Operator (provides ServiceMonitor CRDs).
helm install prometheus prometheus-community/kube-prometheus-stack \
    --namespace monitoring --create-namespace

# 3. Install cert-manager for TLS.
helm install cert-manager jetstack/cert-manager \
    --namespace cert-manager --create-namespace --set installCRDs=true

# 4. Install GeoRAG with all production toggles flipped.
helm install georag ops/charts/georag \
    --namespace georag --create-namespace \
    --set networkPolicy.enabled=true \
    --set autoscaling.enabled=true \
    --set podDisruptionBudget.enabled=true \
    --set ingress.enabled=true \
    --set ingress.tls.enabled=true \
    --set serviceMonitor.enabled=true \
    --values your-prod-values.yaml
```

Until these gates close, this chart is for client demos and
evaluation, **not** prod deployment. Production GeoRAG should run via
the Compose stack with `cd.yml` until the chart graduates.

## arm64 deployment caveat

The georag-* application images build for `linux/amd64` and
`linux/arm64` (see `.github/workflows/ci.yml` v1.5-13). The vLLM prod
LLM tier is x86_64-only — set `externalServices.vllm.enabled=false`
on arm64 nodes and use Ollama instead.

## See also

- `docker-compose.yml` — authoritative service definition for V1
- `.env.production.example` — full env surface (143 keys)
- `ops/runbooks/secret-rotation.md` — credential management
- `docs/acceptance-criteria.md` — V1 ship checklist
