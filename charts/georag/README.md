# GeoRAG Helm chart

Single-chart deploy for the whole GeoRAG stack. K3s-tuned by default.

## Quick start (K3s)

```bash
# 1. Install K3s (Linux only; uses ~/.kube/config automatically)
curl -sfL https://get.k3s.io | sh -

# 2. Mint required secrets
helm install georag charts/georag/ \
  -f charts/georag/values-k3s.yaml \
  --create-namespace --namespace georag \
  --set secrets.postgresPassword="$(openssl rand -base64 32)" \
  --set secrets.pgAppPassword="$(openssl rand -base64 32)" \
  --set secrets.neo4jPassword="$(openssl rand -base64 32)" \
  --set secrets.redisPassword="$(openssl rand -base64 32)" \
  --set secrets.fastapiServiceKey="$(openssl rand -base64 48)" \
  --set secrets.laravelAppKey="base64:$(openssl rand -base64 32)"

# 3. Watch pods come up
kubectl -n georag get pods -w
```

## Quick start (vanilla)

```bash
helm install georag charts/georag/ \
  -f charts/georag/values-vanilla.yaml \
  --create-namespace --namespace georag \
  --set ingress.host=georag.your-domain.com \
  --set secrets.postgresPassword="$(openssl rand -base64 32)" \
  --set secrets.pgAppPassword="$(openssl rand -base64 32)" \
  --set secrets.neo4jPassword="$(openssl rand -base64 32)" \
  --set secrets.redisPassword="$(openssl rand -base64 32)" \
  --set secrets.fastapiServiceKey="$(openssl rand -base64 48)" \
  --set secrets.laravelAppKey="base64:$(openssl rand -base64 32)"
```

## What's included (§11.6 v1)

| Service       | Workload kind     | PVC?  | Notes |
|---------------|-------------------|-------|-------|
| postgresql    | StatefulSet × 1   | 50Gi  | PG 18.3 + PostGIS 3.6 + h3 |
| pgbouncer     | Deployment × 2    | —     | transaction-mode pool |
| neo4j         | StatefulSet × 1   | 20Gi  | Community Edition (no Enterprise) |
| qdrant        | StatefulSet × 1   | 30Gi  | vector store |
| redis         | StatefulSet × 1   | 5Gi   | AOF persistence |
| seaweedfs     | StatefulSet × 1   | 100Gi | S3-compatible object store |
| fastapi       | Deployment × 2-8  | —     | HPA on CPU |
| laravel-octane| Deployment × 2-6  | —     | HPA on CPU |
| laravel-horizon | Deployment × 2  | —     | queue workers |
| laravel-reverb  | Deployment × 1  | —     | WebSocket server |
| hatchet       | StatefulSet × 1 + 2 worker pools | 5Gi | workflow engine |
| martin        | Deployment × 2    | —     | MVT tile server |
| dagster       | webserver + daemon | 10Gi | scheduled batch ingestion |
| vllm          | Deployment × 1 (GPU) | —  | Qwen/Qwen3-14B-AWQ |
| pg-init Job   | post-install hook | —     | idempotent SQL migrations |
| audit-verify  | CronJob nightly   | —     | hash-chain integrity verifier |

## What's NOT included (§11.6 v2)

Observability stack — install separately via upstream charts:
- `kube-prometheus-stack` (Prometheus + Grafana + Alertmanager)
- `loki` + `promtail` (log aggregation)
- `tempo` + `opentelemetry-collector` (distributed tracing)
- `minio` (legacy object store — SeaweedFS is the §11-v2 default)
- `kestra` (workflow editor UI)
- `caddy` (TLS termination — use ingress + cert-manager instead)

## Operating

### Upgrade

```bash
helm upgrade georag charts/georag/ -f charts/georag/values-k3s.yaml
```

The `pg-init` Job re-runs as a post-upgrade hook; all SQL migrations
are idempotent.

### Uninstall

```bash
helm uninstall georag -n georag
# PVCs are NOT deleted by default — drop them manually:
kubectl -n georag delete pvc --all
```

### Air-gap

See `scripts/build_airgap_bundle.sh` + `airgap/install.sh` for the
single-tarball install path. Customer-side:

```bash
tar xzf georag-airgap-v1.0.0.tar.gz
cd georag-airgap-v1.0.0
./install.sh --namespace georag
```

## Sizing tiers

`global.tier` is informational — actual sizing lives in each
service's `resources` block. The included tiers:

| Tier   | Use case               | Total CPU req | Total mem req |
|--------|------------------------|---------------|---------------|
| small  | Single-tenant pilot    | ~6 cores      | ~12 Gi        |
| medium | Production (≤200 users)| ~16 cores     | ~32 Gi        |
| large  | Multi-tenant (defer)   | (§11-v3)      | (§11-v3)      |

vLLM is separate: 1× NVIDIA A100 (40GB) or equivalent.

## Troubleshooting

- `pg-init` Job failing? Check the Pod logs — usually a permission
  issue on the bundled SQL files or a stale schema constraint.
- vLLM stuck pending? Confirm the GPU device plugin is installed
  (`kubectl get nodes -L nvidia.com/gpu.product`) and matches
  `vllm.nodeSelector`.
- Pods can't reach each other? K3s uses `local-path` PVCs which
  are node-pinned; ensure all stateful services schedule onto the
  same node, or migrate to a multi-node `StorageClass`.

## Acceptance harness

```bash
bash scripts/section11_v2_acceptance.sh
```

This runs `helm lint`, `helm template` against all 3 values files,
and (when `K3S_CONTEXT` env var is set) a real install against
a test cluster.
