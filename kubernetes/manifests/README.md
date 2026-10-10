# GeoRAG K8s manifests

These three files are pre-rendered from the Helm chart at
`charts/georag/`. Use them when you don't want a Helm dependency
(`kubectl apply -f`).

| File           | Source values            | Resources | Use case |
|----------------|--------------------------|-----------|----------|
| `k3s.yaml`     | `values-k3s.yaml`        | 41        | Single-node K3s install |
| `vanilla.yaml` | `values-vanilla.yaml`    | 43        | EKS / GKE / kubeadm / OpenShift (with adjustments) |
| `airgap.yaml`  | `values-airgap.yaml`     | 43        | The pre-rendered counterpart of the air-gap values. `airgap/install.sh` does not apply it: it drives the bundled chart through Helm |

Counts are with the chart defaults: PodDisruptionBudgets included,
NetworkPolicy off (an opt-in value — see `charts/georag/values.yaml`,
"Hardening"). Enable it through Helm rather than by hand-editing these
files. The `pg-init` and `schema` Jobs are Helm hooks; applied with
`kubectl` they are plain Jobs that start together, which is why the schema
Job waits for the database roles before it migrates. There is no ServiceMonitor here, on, or off — the chart has never
deployed Prometheus Operator CRDs or scrape config of any kind; production
and dev observability are CloudWatch/marker-log alarms and Laravel Pulse
(see `docs/architecture/manual/12-observability.md`). Hatchet ships as one
merged `hatchet-worker` Deployment (`WORKER_POOL=all`), not separate
`-ai` / `-ingestion` pools.

## CRITICAL — Rotate secrets before applying

The `georag-secrets` Secret in each file carries placeholders. Edit it by
hand, key by key. Do not run one blanket `sed` over `CHANGEME`: it would
give every password the same value, and some keys cannot simply be random
(`REVERB_APP_KEY`, `HATCHET_CLIENT_TOKEN`, see below).

| Key | Set it to |
|-----|-----------|
| `POSTGRES_PASSWORD`, `PG_APP_PASSWORD`, `REDIS_PASSWORD` | independent random values (`openssl rand -base64 32`) |
| `MARTIN_DB_PASSWORD` | hex, because it is embedded in Martin's `DATABASE_URL` (`openssl rand -hex 32`) |
| `HATCHET_DB_PASSWORD` | hex, same reason (`openssl rand -hex 32`) |
| `FASTAPI_SERVICE_KEY` | 32+ characters (`openssl rand -base64 48`) |
| `LARAVEL_APP_KEY` | `base64:` + 32 random bytes, base64-encoded |
| `REVERB_APP_KEY` | **not random**: the `VITE_REVERB_APP_KEY` the laravel image was built with. A mismatch connects, is refused, and chat hangs with nothing in any log |
| `REVERB_APP_SECRET` | random (`openssl rand -base64 32`) |
| `QDRANT_API_KEY` | empty leaves Qdrant unauthenticated; set it to turn auth on |
| `HATCHET_CLIENT_TOKEN` | empty until you mint one; see `charts/georag/README.md`, "Hatchet token" |

`k3s.yaml` ships Reverb off, so its Secret has no `REVERB_*` keys.

Or — strongly recommended — use the Helm chart and pass secrets via
`--set-file` (the chart also needs `--set global.createNamespace=false` when
Helm makes the namespace with `--create-namespace`):

```bash
helm install georag charts/georag/ \
  -f charts/georag/values-k3s.yaml \
  --create-namespace --namespace georag --timeout 15m \
  --set global.createNamespace=false \
  --set-file secrets.postgresPassword=secrets/pg.txt \
  --set-file secrets.fastapiServiceKey=secrets/fastapi.txt
  # ...and every other required secret; see charts/georag/README.md
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
