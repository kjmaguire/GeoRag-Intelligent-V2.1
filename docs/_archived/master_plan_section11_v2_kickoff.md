# Master-plan §11.6/7/8 (Helm + K8s + Air-gapped bundle) — Kickoff

**Doc-phase:** 180 (shipped)
**Status:** SHIPPED — 4-tick batch landed 2026-05-16
**Predecessor:** `docs/master_plan_section11_kickoff.md` (§11-v1 shipped)
**Locked defaults:** K3s target + tarball+install.sh air-gap shape
**Acceptance:** `scripts/section11_v2_acceptance.sh` — 45/45 green
**Defer:** real K3s install test (per Kyle: "ship chart + manifests now, defer install test")

---

## TL;DR

§11-v1 covered backup ops + DR + load testing on the existing
docker-compose deployment. §11-v2 covers **how customers actually
install GeoRAG into their own infra** — three ticks:

1. **§11.6** — Helm chart that installs the whole GeoRAG stack on
   K3s (also works on vanilla K8s; K3s is the test target)
2. **§11.7** — Kubernetes manifests + values reference + per-store
   PVC / Secret / NodeSelector defaults
3. **§11.8** — Air-gapped bundle: single tarball with all images
   + the chart + `install.sh` for customers with no internet

Estimated effort: **12-16 hours** spread across one or two sessions.

---

## Locked decisions

| Decision                       | Value                                                                     |
|--------------------------------|---------------------------------------------------------------------------|
| Reference K8s distribution     | **K3s** — single-binary, default for on-prem mining co's                  |
| Chart shape                    | **Single mega-chart** (`charts/georag/`) with subchart-style values       |
| StorageClass default           | `local-path` (K3s built-in); overridable for vanilla K8s                  |
| Ingress                        | Traefik (K3s default); chart exposes ingress.className for override       |
| Secrets management             | Plain `Secret` resources with values from `--set-file` / sealed-secrets   |
| GPU scheduling                 | NodeSelector + toleration for `nvidia.com/gpu` (vLLM pod only)            |
| Air-gap bundle shape           | **Single tar.gz** (~30GB) with images + chart + install.sh                |
| Bundle build pipeline          | `scripts/build_airgap_bundle.sh` — Docker save + chart-package + tar      |
| Install command                | `./install.sh --values custom.yaml` (mirrors `helm install --values`)     |

---

## Sub-step detail

### §11.6 — Helm chart (`charts/georag/`)

**Directory layout:**

```
charts/georag/
├── Chart.yaml
├── values.yaml                  # all knobs, heavily commented
├── values-k3s.yaml              # K3s-tuned overrides
├── values-vanilla.yaml          # vanilla K8s assumptions
├── values-airgap.yaml           # air-gap registry overrides
├── README.md                    # install + upgrade + uninstall
└── templates/
    ├── _helpers.tpl
    ├── namespace.yaml
    ├── secrets.yaml
    ├── configmap.yaml
    ├── postgresql/              # StatefulSet + Service + PVC
    ├── pgbouncer/               # Deployment + Service
    ├── neo4j/                   # StatefulSet + Service + PVC
    ├── qdrant/                  # StatefulSet + Service + PVC
    ├── redis/                   # StatefulSet + Service + PVC
    ├── seaweedfs/               # StatefulSet (master + volume) + Service
    ├── fastapi/                 # Deployment + Service + HPA
    ├── laravel/                 # Deployment + Service + HPA + cron
    ├── hatchet/                 # StatefulSet (engine) + Deployments (workers)
    ├── vllm/                    # Deployment (1x) + NodeSelector gpu
    ├── martin/                  # Deployment + Service
    ├── dagster/                 # Deployment + UI Service
    ├── ingress.yaml             # Traefik default; overridable
    └── jobs/
        ├── pg-init.yaml         # runs database/raw/phase0/*.sql on first install
        └── audit-chain-verify.yaml  # CronJob: nightly hash-chain verify
```

**Acceptance:**
- `helm template charts/georag/ -f values-k3s.yaml` produces valid YAML
- `helm install georag charts/georag/ -f values-k3s.yaml` succeeds
  on a fresh K3s
- All §11-v1 acceptance checks pass against the K3s install
- All §6 + §10 acceptance checks pass against the K3s install

### §11.7 — K8s manifests + reference values

**What ships:**
- `kubernetes/manifests/` flat YAML directory rendered from the
  chart, for customers who don't want Helm at all
  - Generated via `helm template > kubernetes/manifests/all.yaml`
  - Per-store directories: `kubernetes/manifests/postgresql/`, etc.
- `docs/deployment/k8s-reference.md` — values reference, sizing
  table (small/medium/large), upgrade flow, backup/restore on K8s
- `docs/deployment/k3s-quickstart.md` — single-page install guide
  for the simplest K3s case

**Acceptance:**
- `kubectl apply -f kubernetes/manifests/all.yaml` succeeds on a
  fresh K3s and reaches Ready
- The two docs render correctly in MkDocs (if used) or as plain
  Markdown

### §11.8 — Air-gapped install bundle

**What ships:**
- `scripts/build_airgap_bundle.sh` — orchestrates:
  1. `docker pull` each image listed in `docker-compose.yml` +
     vLLM model image
  2. `docker save -o images/<name>.tar` each one
  3. `helm package charts/georag/` → `georag-<version>.tgz`
  4. Copy `install.sh` + `values-airgap.yaml`
  5. `tar czf georag-airgap-<version>.tar.gz <staged dir>`
- `airgap/install.sh` — single-command installer that runs on the
  customer's air-gapped K3s node:
  1. Detect K3s runtime (`k3s ctr` vs `docker`)
  2. `ctr image import images/*.tar` (K3s) or `docker load`
     (vanilla) for each saved image
  3. Tag images for the local registry path (if one)
  4. `helm install georag georag-<version>.tgz -f values-airgap.yaml`
- `airgap/README.md` — operator runbook for the air-gap install
  (pre-reqs, sizing, troubleshooting)
- `scripts/verify_airgap_bundle.sh` — validates a built bundle
  before shipping (checksums, image count, chart validity)

**Acceptance:**
- `bash scripts/build_airgap_bundle.sh` produces a single
  `georag-airgap-<version>.tar.gz` (~30GB)
- `bash scripts/verify_airgap_bundle.sh georag-airgap-<version>.tar.gz`
  reports OK
- Test install: unpack on a fresh K3s VM with no internet, run
  `./install.sh`, observe a healthy stack
- Test install reaches `helm status georag` → `STATUS: deployed`

### §11.6/7/8 acceptance harness

**What ships:**
- `scripts/section11_v2_acceptance.sh` — runs against a live K3s
  cluster (env var `K3S_CONTEXT`)
- Checks:
  - `helm template` produces valid YAML for all 3 values files
  - All pods reach Ready within 5 min after `helm install`
  - All §11-v1 checks pass against K3s install
  - `build_airgap_bundle.sh` produces a verifiable tarball
  - Documented install commands in the 3 docs are byte-identical
    to what `install.sh` actually runs (no doc drift)

---

## Out of scope (deferred to §11-v3)

- Helm chart published to a public Helm repository
- Multi-cluster deployments (sharded across regions)
- StatefulSet → Operator migration (cloudnative-pg for PG,
  Neo4j Operator) — adds complexity without obvious customer ROI
- Auto-scaling beyond HPA (no KEDA, no Karpenter)
- Service mesh (no Istio, no Linkerd) — customers can bolt one on

---

## Open risks

1. **vLLM image size** — the model weights bundled in the image
   could push the air-gap tarball past 50GB. Mitigation: keep the
   model out of the image, ship a separate `models/` directory
   that gets mounted as a PVC + side-loaded.

2. **K3s + GPU** — K3s supports NVIDIA GPUs but needs the GPU
   operator installed first. The docs need a clear "before you
   install GeoRAG" prereq section.

3. **PG18 + PostGIS3.6 + h3_postgis** — these are container
   images we pull from our own registry today. Air-gap bundle
   needs to include them; verify they're tagged + reproducible.

---

## Sign-off

If approved as-written:

- [ ] §11.6 = Helm chart (`charts/georag/`) installable on K3s
- [ ] §11.7 = generated K8s manifests + reference docs
- [ ] §11.8 = air-gapped bundle script + install.sh + verifier
- [ ] One acceptance harness covers all three
- [ ] §11-v3 (registry publishing, multi-cluster, operators)
      deferred to a future kickoff
