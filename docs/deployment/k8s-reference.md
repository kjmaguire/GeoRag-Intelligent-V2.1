# GeoRAG on Kubernetes — Reference

This doc covers the §11.6/7 deployment path: install GeoRAG into a
Kubernetes cluster (K3s, vanilla, or air-gapped) using either the
Helm chart or the pre-rendered manifests.

## Choosing your install path

| You have…                                    | Use this              |
|----------------------------------------------|-----------------------|
| Linux + an SSH-able host with internet       | K3s quickstart        |
| Existing EKS / GKE / AKS / vanilla cluster   | Vanilla install       |
| Air-gapped K3s (no internet)                 | Air-gap install       |
| Want raw `kubectl apply` (no Helm)           | Pre-rendered manifests|

## Pre-requisites

### K3s quickstart
- Linux node with ≥8 cores / ≥32 GiB RAM / ≥250 GiB disk
- (Optional) NVIDIA GPU + the `nvidia-device-plugin` DaemonSet
  (only needed if you enable vLLM)
- Outbound internet to pull images

### Vanilla install
- Cluster running Kubernetes ≥1.27
- StorageClass available (defaulted or explicit via
  `global.storageClass`)
- Ingress controller installed (nginx by default; override via
  `global.ingressClass`)
- (For vLLM) NVIDIA GPU operator installed, GPU nodes labelled

### Air-gap install
- Customer-side K3s (or any K8s) running
- Customer-side container registry (Harbor / distribution) OR
  willing to load images directly into containerd via `ctr image import`
- ≥30GB free disk for the bundle tarball

## Install — Helm path

### K3s

```bash
# 1. Install K3s
curl -sfL https://get.k3s.io | sh -

# 2. Install GeoRAG
helm install georag charts/georag/ \
  -f charts/georag/values-k3s.yaml \
  --create-namespace --namespace georag \
  --set secrets.postgresPassword="$(openssl rand -base64 32)" \
  --set secrets.pgAppPassword="$(openssl rand -base64 32)" \
  --set secrets.neo4jPassword="$(openssl rand -base64 32)" \
  --set secrets.redisPassword="$(openssl rand -base64 32)" \
  --set secrets.fastapiServiceKey="$(openssl rand -base64 48)" \
  --set secrets.laravelAppKey="base64:$(openssl rand -base64 32)"
```

### Vanilla

Identical to K3s but with `-f charts/georag/values-vanilla.yaml` and
your domain in `ingress.host`. Cert-manager annotations are pre-wired.

## Install — pre-rendered manifests path

If you don't want the Helm dependency:

```bash
# 1. Edit kubernetes/manifests/k3s.yaml to replace every CHANGEME
sed -i "s/CHANGEME/$(openssl rand -base64 32)/g" kubernetes/manifests/k3s.yaml

# 2. Apply
kubectl create namespace georag
kubectl -n georag apply -f kubernetes/manifests/k3s.yaml
```

⚠️  **Manifests have placeholder secrets** — never apply them
unrotated. They are committed as `CHANGEME` so the *shape* is
reviewable in git without secrets in source control.

## Sizing tables

### Small (default — K3s preset)

Single-tenant pilot, ≤50 active users, ≤100k drill records ingested.

| Component  | Replicas | CPU req | Memory req | PVC    |
|------------|----------|---------|------------|--------|
| postgresql | 1        | 0.5     | 1 Gi       | 50 Gi  |
| neo4j      | 1        | 0.25    | 1 Gi       | 20 Gi  |
| qdrant     | 1        | 0.5     | 1 Gi       | 30 Gi  |
| redis      | 1        | 0.2     | 0.5 Gi     | 5 Gi   |
| seaweedfs  | 1        | 0.2     | 0.5 Gi     | 100 Gi |
| fastapi    | 2        | 0.5     | 1 Gi       | —      |
| laravel    | 2        | 0.5     | 1 Gi       | —      |
| hatchet    | 1+1+2    | 1.25    | 2.5 Gi     | 5 Gi   |
| martin     | 2        | 0.2     | 0.25 Gi    | —      |
| **Total**  |          | **~6**  | **~12 Gi** | **210 Gi** |

### Medium (vanilla preset)

Production, ≤200 active users, ≤1M drill records.

| Component  | Replicas | CPU req | Memory req | PVC    |
|------------|----------|---------|------------|--------|
| postgresql | 1        | 2       | 4 Gi       | 100 Gi |
| neo4j      | 1        | 1       | 4 Gi       | 50 Gi  |
| qdrant     | 1        | 1       | 2 Gi       | 100 Gi |
| (rest scaled per chart values) | | | | |
| **Total**  |          | **~16** | **~32 Gi** | **400 Gi** |

### Large

Multi-tenant SaaS — needs sharded PG + Qdrant + read-replicas. Out
of scope for §11.6-v1 (see §11.6-v3 kickoff when written).

## GPU scheduling (vLLM)

vLLM needs a GPU. The chart adds:

```yaml
nodeSelector:
  nvidia.com/gpu.product: "<your-gpu-label>"
tolerations:
  - key: "nvidia.com/gpu"
    operator: "Exists"
    effect: "NoSchedule"
```

Find your GPU label:

```bash
kubectl get nodes -L nvidia.com/gpu.product
```

Then set `vllm.nodeSelector.nvidia.com/gpu.product` in your values
override file.

## Backup / restore on K8s

The chart's nightly audit-chain-verify CronJob runs at 03:00. Backup
of the data stores themselves is via the §11.1 Hatchet workflows,
which write to the SeaweedFS bucket the chart provisions. To restore
into a fresh cluster, see `docs/RUNBOOK.md` §11.3.

## Upgrade flow

```bash
# 1. Render diff
helm diff upgrade georag charts/georag/ -f charts/georag/values-k3s.yaml

# 2. Apply
helm upgrade georag charts/georag/ -f charts/georag/values-k3s.yaml
```

The `pg-init` Job re-fires as a post-upgrade hook to pick up any new
SQL migrations. All migrations are idempotent.

## Uninstall

```bash
helm uninstall georag -n georag

# PVCs are intentionally NOT deleted — protect against fat-finger.
# To drop them too:
kubectl -n georag delete pvc --all
kubectl delete namespace georag
```

## Troubleshooting

See `charts/georag/README.md` § "Troubleshooting" for the common
gotchas (pg-init Job failures, vLLM pending Pods, multi-node PVC
scheduling).
