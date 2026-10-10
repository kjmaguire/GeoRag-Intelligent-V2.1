# GeoRAG Helm chart

Single-chart deploy for the whole GeoRAG stack. K3s-tuned by default.

## Quick start (K3s)

```bash
# 1. Install K3s (Linux only; uses ~/.kube/config automatically)
curl -sfL https://get.k3s.io | sh -

# 2. Mint required secrets. --timeout because helm waits for the two
#    install hooks (pg-init, then the schema Job, which runs every
#    migration on a fresh database); the 5m default is not enough.
#    --create-namespace makes the namespace outside the release, and Helm
#    cannot adopt it, so the chart is told not to create it as well.
helm install georag charts/georag/ \
  -f charts/georag/values-k3s.yaml \
  --create-namespace --namespace georag --timeout 15m \
  --set global.createNamespace=false \
  --set secrets.postgresPassword="$(openssl rand -base64 32)" \
  --set secrets.pgAppPassword="$(openssl rand -base64 32)" \
  --set secrets.martinDbPassword="$(openssl rand -hex 32)" \
  --set secrets.redisPassword="$(openssl rand -base64 32)" \
  --set secrets.fastapiServiceKey="$(openssl rand -base64 48)" \
  --set secrets.laravelAppKey="base64:$(openssl rand -base64 32)"

# 3. Watch pods come up
kubectl -n georag get pods -w
```

The K3s preset ships Reverb off (`laravelReverb.enabled: false`), so live
chat answers do not stream until you turn it on; see "Live chat (Reverb)".
The first install also leaves the Hatchet-facing pods unready until you
mint a token; see "Hatchet token".

### Database roles (tenant isolation)

Postgres RLS only applies to a role that is neither SUPERUSER nor
BYPASSRLS. The chart's `postgresql` image creates `georag` as a superuser;
that role is for the `pg-init` and `schema` Jobs and operator work only.
Every Laravel pod and the Hatchet worker connect as `georag_app` (password
`secrets.pgAppPassword`), Martin as `martin_readonly`
(`secrets.martinDbPassword` — hex, because it is embedded in a URL). The
`pg-init` Job creates or re-passwords both on every install/upgrade and
fails if either can bypass RLS; it also creates the Hatchet engine's own
role and database (`secrets.hatchetDbPassword`; unset, it is derived from
`secrets.postgresPassword`). Laravel talks to Postgres
directly, not through PgBouncer: its per-request `app.workspace_id`
binding is session-scoped (see `BindWorkspaceRlsContext`, which refuses to
serve when `DB_POOLED=true`). The ingress routes only to Laravel — map
tiles go through Laravel's authenticating `/tiles/...` proxy, and
`/internal` is never exposed. The schema (`php artisan migrate`, then
`db:apply-raw`, as the `georag` owner role) is the `schema` Job's, run after
`pg-init` on every install and upgrade — the same two commands, in the
same order, as the AWS schema task.

## Quick start (vanilla)

```bash
helm install georag charts/georag/ \
  -f charts/georag/values-vanilla.yaml \
  --create-namespace --namespace georag --timeout 15m \
  --set global.createNamespace=false \
  --set ingress.host=georag.your-domain.com \
  --set secrets.postgresPassword="$(openssl rand -base64 32)" \
  --set secrets.pgAppPassword="$(openssl rand -base64 32)" \
  --set secrets.martinDbPassword="$(openssl rand -hex 32)" \
  --set secrets.redisPassword="$(openssl rand -base64 32)" \
  --set secrets.fastapiServiceKey="$(openssl rand -base64 48)" \
  --set secrets.laravelAppKey="base64:$(openssl rand -base64 32)" \
  --set secrets.reverbAppKey="<the VITE_REVERB_APP_KEY the laravel image was built with>" \
  --set secrets.reverbAppSecret="$(openssl rand -base64 32)"
```

### Live chat (Reverb)

Chat answers reach the browser over a WebSocket to Reverb, so with
`laravelReverb.enabled` (the default everywhere except the K3s preset) the
chart needs two more secrets and routes `/app` on the Ingress to it.

- `secrets.reverbAppKey` is public by design — the browser receives it —
  and **must equal the `VITE_REVERB_APP_KEY` the laravel image was built
  with**. Vite inlines it at build time (`docker/laravel.Dockerfile`), so
  it cannot be changed here after the fact; a mismatch connects, is
  refused, and the chat stream hangs with nothing in any log.
  `secrets.reverbAppSecret` is the half that signs, and is random.
- The same build takes `VITE_REVERB_SCHEME` (`http` or `https`, matching
  `ingress.tls.enabled`) and, for a plain-`http` ingress, `VITE_REVERB_PORT=80`:
  `resources/js/bootstrap.ts` defaults http to the compose stack's port 8085,
  which the Ingress does not serve. HTTPS defaults to 443, which it does.
  Host is left unset on purpose: the page's own host is the Ingress.
- Octane and Horizon publish to the in-cluster Reverb Service, not the public
  host. Reverb only accepts WebSocket upgrades from `ingress.host`
  (`laravelReverb.allowedOrigins` overrides).

### Hatchet token

`secrets.hatchetClientToken` cannot exist at first install: the engine
mints it from a keyset it writes to `/config` on first boot. Until it is set
the pods that talk to the engine (fastapi, the worker) cannot authenticate to
it, and an engine that is healthy beside clients that are not reads like a
Hatchet fault when it is not one. Once `georag-hatchet-0` is Ready (the
`/config` volume is why a restart keeps the keyset):

```bash
NS=georag; REL=georag
TENANT=$(kubectl -n $NS exec statefulset/$REL-postgresql -- \
  psql -U georag -d hatchet -At -c "SELECT id FROM \"Tenant\" WHERE slug='default'")
# --config goes BEFORE the subcommand; SERVER_AUTH_COOKIE_SECRETS must be set
# even though nothing here serves a cookie (hatchet-admin builds the session
# store before any subcommand). Both are load-bearing and neither is obvious —
# deploy/aws/README.md has the failure each one produces.
TOKEN=$(kubectl -n $NS exec statefulset/$REL-hatchet -- \
  env SERVER_AUTH_COOKIE_SECRETS="mint mint" \
  /hatchet-admin --config /config token create --name georag --tenant-id "$TENANT")
helm upgrade $REL charts/georag/ -f charts/georag/values-k3s.yaml \
  --reuse-values --set secrets.hatchetClientToken="$TOKEN"
kubectl -n $NS rollout restart deployment/$REL-fastapi deployment/$REL-hatchet-worker
```

The command is the one `docker-compose.yml` and `deploy/aws/README.md` document;
it has not been run against a cluster from this chart. The token carries
`exp` — 90 days — and nothing renews it.

### Model sidecars

`embedding`, `reranker` and `sparse` are three more Deployments from the
fastapi image (`uvicorn app.embedding_service:app` and its siblings), exactly
as docker-compose.yml runs them: one copy of each model, shared by every
uvicorn process and by the worker, instead of one per process (six workers
each loading a ~2.4 GiB embedding copy OOM-killed the container in
June). The fastapi pods and the worker are pointed at them with
`EMBEDDING_SERVICE_URL`, `RERANKER_SERVICE_URL` and `SPARSE_SERVICE_URL`, and
`RERANKER_BACKEND=cross_encoder` — the code default is `bedrock`, which an
on-prem install cannot call and which fails every document query closed.
`fastapi.uvicornWorkers` caps the uvicorn processes at 3 (the image default
is 6). `fastapi.extraEnv` and `hatchet.worker.extraEnv` append any other
environment, for example `LLM_BACKEND=vllm` and `VLLM_URL` for an
OpenAI-compatible endpoint you run.

CPU only. `sparse` (SPLADE++) is baked into the image and downloads nothing.
**`embedding` and `reranker` download Qwen3 weights from huggingface.co on
first start** into a PVC, and are not Ready until they have them (the startup
probe allows 15 minutes). On a cluster with no route to the internet they
never become Ready; set `modelSidecars.offline=true` (the air-gap preset does)
and seed the cache first — `airgap/README.md`, "Model weights".

## What's included (§11.6 v1)

| Service       | Workload kind     | PVC?  | Notes |
|---------------|-------------------|-------|-------|
| postgresql    | StatefulSet × 1   | 50Gi  | PG 18.3 + PostGIS 3.6 + h3 |
| pgbouncer     | Deployment × 2    | —     | transaction-mode pool |
| qdrant        | StatefulSet × 1   | 30Gi  | vector store |
| redis         | StatefulSet × 1   | 5Gi   | AOF persistence |
| seaweedfs     | StatefulSet × 1   | 100Gi | S3-compatible object store |
| fastapi       | Deployment × 2-8  | —     | HPA on CPU |
| embedding, reranker | Deployment × 1 each | 5Gi each | model sidecars (CPU) — weights downloaded on first start |
| sparse        | Deployment × 1    | —     | SPLADE++ sidecar (CPU) — weights baked into the image |
| laravel-octane| Deployment × 2-6  | —     | HPA on CPU |
| laravel-horizon | Deployment × 2  | —     | queue workers |
| laravel-reverb  | Deployment × 1  | —     | WebSocket server |
| hatchet       | StatefulSet × 1 + Deployment × 1 (worker) | 5Gi | workflow engine — one merged worker (`WORKER_POOL`, default `all`), matching docker-compose.yml and the AWS ECS Terraform |
| martin        | Deployment × 2    | —     | MVT tile server |
| pg-init Job   | install/upgrade hook | —  | database roles (`georag_app`, `martin_readonly`) and the Hatchet engine's role + database |
| schema Job    | install/upgrade hook, after pg-init | — | `php artisan migrate` then `db:apply-raw`, as the owner role |
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
it to `ingress-nginx`). Components that call Cohere's API or S3 — and the
embedding and reranker sidecars, for their first-start model download — get
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
helm upgrade georag charts/georag/ -f charts/georag/values-k3s.yaml --timeout 15m
```

The `pg-init` and `schema` Jobs re-run as post-upgrade hooks (roles first,
then `migrate` and `db:apply-raw`); both are idempotent, and `helm upgrade`
waits for them, hence the `--timeout`. There is no nightly chain-verify
CronJob: the audit hash chain is verified by the Hatchet
`audit_ledger_verify` cron on the worker.

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
cd _stage_v1.0.0
./install.sh --namespace georag --reverb-app-key <the VITE_REVERB_APP_KEY the image was built with>
```

`airgap/README.md` (shipped in the bundle) covers the secrets file, the model
weights the sidecars need, and upgrading in place.

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

- `pg-init` Job failing? `kubectl -n georag logs job/georag-pg-init` —
  usually a connectivity issue while postgres is still booting, or a runtime
  role that is SUPERUSER/BYPASSRLS (it refuses on purpose).
- `schema` Job failing? `kubectl -n georag logs job/georag-schema` — the first
  failing migration or raw file is named. Both Jobs are idempotent: fix the
  cause and `helm upgrade` again — hooks are recreated each run
  (`before-hook-creation`), and a hook that fails or times out leaves the
  release `failed` until you do.
- embedding/reranker pod never Ready? `kubectl logs` it: a download from
  huggingface.co (no route out), or with `modelSidecars.offline` an empty cache.
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
