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
  --set secrets.redisPassword="$(openssl rand -base64 32)" \
  --set secrets.fastapiServiceKey="$(openssl rand -base64 48)" \
  --set secrets.laravelAppKey="base64:$(openssl rand -base64 32)"
```

## What's included (§11.6 v1)

| Service       | Workload kind     | PVC?  | Notes |
|---------------|-------------------|-------|-------|
| postgresql    | StatefulSet × 1   | 50Gi  | PG 18.3 + PostGIS 3.6 + h3 |
| pgbouncer     | Deployment × 2    | —     | transaction-mode pool |
| qdrant        | StatefulSet × 1   | 30Gi  | vector store |
| redis         | StatefulSet × 1   | 5Gi   | AOF persistence |
| seaweedfs     | StatefulSet × 1   | 100Gi | S3-compatible object store |
| fastapi       | Deployment × 2-8  | —     | HPA on CPU |
| laravel-octane| Deployment × 2-6  | —     | HPA on CPU |
| laravel-horizon | Deployment × 2  | —     | queue workers |
| laravel-reverb  | Deployment × 1  | —     | WebSocket server |
| hatchet       | StatefulSet × 1 + Deployment × 1 (worker) | 5Gi | workflow engine — one merged worker (`WORKER_POOL`, default `all`), matching docker-compose.yml and the AWS ECS Terraform |
| martin        | Deployment × 2    | —     | MVT tile server |
| pg-init Job   | post-install hook | —     | idempotent SQL migrations |
| audit-verify  | CronJob nightly   | —     | hash-chain integrity verifier |
| PDB × 5       | policy/v1         | —     | fastapi, laravel-octane, laravel-horizon, pgbouncer, martin — only while ≥2 replicas |
| NetworkPolicy | opt-in            | —     | default-deny + one allow policy per component (`networkPolicy.enabled`) |

## Hardening (chart 0.2.0, 2026-09-06)

Two templates ported from the retired `ops/charts` skeleton and rewritten
for this chart's components. Neither changes a workload. (A third,
ServiceMonitor, was ported alongside these but removed 2026-09-16: this
repo has no Prometheus Operator CRDs and no scrape config anywhere —
production and dev observability are CloudWatch/marker-log alarms and
Laravel Pulse. See `docs/architecture/manual/12-observability.md`.)

**PodDisruptionBudget** — on by default. One `minAvailable: 1` budget per
horizontally scaled component, emitted only while that component runs at
least two replicas (HPA `minReplicas` when autoscaling is on), so scaling
down to one replica never leaves a budget that blocks `kubectl drain`.

**NetworkPolicy** — off by default (`networkPolicy.enabled=true` to turn on).
A default-deny policy covers every pod in the release, `allow-dns` lets all
of them reach kube-dns, and one policy per component allows exactly the
traffic in the map at the top of `templates/networkpolicy.yaml`. The
ingress controller is identified by `networkPolicy.ingressController`
(K3s Traefik in `kube-system` by default; `values-vanilla.yaml` switches
it to `ingress-nginx`). Components that call Cohere's API or S3 get
internet egress on 443 with private ranges carved out
(`networkPolicy.externalEgress`); anything else external goes in
`networkPolicy.extraEgress`. K3s enforces NetworkPolicy out of the box;
on vanilla clusters you need a CNI that does (Calico, Cilium). Turn it on
only after a first install works without it — a wrong ingress-controller
selector blackholes the web app silently.

## What's NOT included (§11.6 v2)

Observability stack — none of this is templated by this chart or present
anywhere else in the repo (no ServiceMonitor, no scrape config). Install
separately via upstream charts if you want it:
- `kube-prometheus-stack` (Prometheus + Grafana + Alertmanager + the
  Prometheus Operator CRDs, including ServiceMonitor)
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

No GPU node is required: this chart deploys no inference server. The
embedding, reranker and sparse sidecars run on CPU by default, and LLM
calls go to Cohere's own API (or an OpenAI-compatible endpoint you run).

## Troubleshooting

- `pg-init` Job failing? Check the Pod logs — usually a permission
  issue on the bundled SQL files or a stale schema constraint.
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
