# GeoRAG K8s manifests

These three files are pre-rendered from the Helm chart at
`charts/georag/`. Use them when you don't want a Helm dependency
(`kubectl apply -f`).

| File           | Source values            | Resources | Use case |
|----------------|--------------------------|-----------|----------|
| `k3s.yaml`     | `values-k3s.yaml`        | 32        | Single-node K3s install |
| `vanilla.yaml` | `values-vanilla.yaml`    | 34        | EKS / GKE / kubeadm / OpenShift (with adjustments) |
| `airgap.yaml`  | `values-airgap.yaml`     | 34        | Customer-side air-gap install (consumed by `airgap/install.sh`) |

Counts are with the chart defaults: PodDisruptionBudgets included,
NetworkPolicy off (an opt-in value — see `charts/georag/values.yaml`,
"Hardening"). Enable it through Helm rather than by hand-editing these
files. There is no ServiceMonitor here, on, or off — the chart has never
deployed Prometheus Operator CRDs or scrape config of any kind; production
and dev observability are CloudWatch/marker-log alarms and Laravel Pulse
(see `docs/architecture/manual/12-observability.md`). Hatchet ships as one
merged `hatchet-worker` Deployment (`WORKER_POOL=all`), not separate
`-ai` / `-ingestion` pools.

## CRITICAL — Rotate secrets before applying

Every secret value in these files is `CHANGEME`. The chart renders
to `kubectl apply` shape with placeholder values so you can read the
shape without secrets in source control. Before deploying, run
`bash kubernetes/rotate_secrets.sh <flavor>` or edit by hand:

```bash
sed -i 's/CHANGEME/<your-base64-pass>/g' kubernetes/manifests/k3s.yaml
```

Or — strongly recommended — use the Helm chart and pass secrets via
`--set-file`:

```bash
helm install georag charts/georag/ \
  -f charts/georag/values-k3s.yaml \
  --set-file secrets.postgresPassword=secrets/pg.txt \
  --set-file secrets.fastapiServiceKey=secrets/fastapi.txt
```

## Regenerating these manifests

```bash
# After editing values-*.yaml or any template
bash scripts/regenerate_k8s_manifests.sh
```

The script passes `--kube-version` (default 1.30.0) because `Chart.yaml`
pins `kubeVersion: >=1.27.0` and an offline `helm template` otherwise
assumes 1.20 and refuses to render.

The §11-v2 acceptance harness asserts the rendered files match the
chart output — drift is a hard fail.
