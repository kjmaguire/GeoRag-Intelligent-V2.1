# GeoRAG — Air-gapped install

This bundle contains everything you need to install GeoRAG on a
Kubernetes cluster with no internet access.

## Contents

| File / dir            | What it is                                          |
|-----------------------|------------------------------------------------------|
| `install.sh`          | Single-command installer (this is what you run)     |
| `README.md`           | This file                                            |
| `MANIFEST.yaml`       | List of bundled images + checksums                  |
| `values-airgap.yaml`  | Helm values overrides for air-gap deployment        |
| `chart/`              | Packaged Helm chart (`georag-X.Y.Z.tgz`)            |
| `images/`             | Saved Docker images (`docker save` format), one per service |

Typical bundle size: **25–35 GB** (15–20 GB images + the Helm chart).
The vLLM model weights (~17 GB) are NOT bundled by default; ship
them separately as a PVC mount and set
`vllm.model.useLocalPvc=true`.

## Pre-requisites on the target node

1. **K3s** installed (or any K8s cluster). For K3s:
   ```bash
   # On a host with internet (one-time):
   curl -sfL https://get.k3s.io > k3s-install.sh
   # Transfer to air-gapped host, then:
   INSTALL_K3S_SKIP_DOWNLOAD=true ./k3s-install.sh
   ```

2. **helm** binary (~50 MB). Download once on an internet-connected
   box, transfer the binary to the target.

3. **Sufficient disk** for the images:
   - Images load into containerd: ~20 GB
   - PVCs (postgres + neo4j + qdrant + redis + seaweedfs): ~210 GB
   - **Total minimum free**: 250 GB

4. **(Optional) Private container registry** if you have multiple
   nodes. For single-node K3s you can skip this — the install loads
   images directly into containerd via `ctr image import`.

5. **(For vLLM only) NVIDIA GPU + the device-plugin DaemonSet**.

## Install

```bash
# 1. Unpack
tar xzf georag-airgap-v1.0.0.tar.gz
cd _stage_v1.0.0

# 2. (Optional) Prepare secrets file
cat > secrets.env <<EOF
POSTGRES_PASSWORD=$(openssl rand -base64 32)
PG_APP_PASSWORD=$(openssl rand -base64 32)
NEO4J_PASSWORD=neo4j/$(openssl rand -base64 24 | tr -d '/+=')
REDIS_PASSWORD=$(openssl rand -base64 32)
FASTAPI_SERVICE_KEY=$(openssl rand -base64 48)
LARAVEL_APP_KEY=base64:$(openssl rand -base64 32)
EOF
chmod 600 secrets.env

# 3. Install (~5-10 min depending on disk speed)
./install.sh --secrets-file secrets.env
```

If you have a private registry instead of single-node K3s:

```bash
./install.sh \
    --registry harbor.your-corp.local/georag \
    --secrets-file secrets.env
```

## Post-install

```bash
# Watch pods
kubectl -n georag get pods -w

# Wait for the pg-init Job to complete (runs the SQL migrations)
kubectl -n georag wait --for=condition=complete job/georag-pg-init --timeout=5m

# Make the first admin
kubectl -n georag exec deploy/georag-laravel-octane -- \
    php artisan tinker --execute='\App\Models\User::factory()->create(["email" => "admin@your-corp", "is_admin" => true])'

# Find the URL
kubectl -n georag get ingress
```

## Upgrade in place

Ship a new air-gap bundle to the customer, then:

```bash
tar xzf georag-airgap-v1.1.0.tar.gz
cd _stage_v1.1.0
./install.sh --secrets-file ../v1.0.0/secrets.env
# (install.sh detects existing release and runs `helm upgrade`)
```

Existing PVCs + data are preserved.

## Uninstall

```bash
helm uninstall georag -n georag
# PVCs are NOT deleted by default — drop them only if you really mean it:
kubectl -n georag delete pvc --all
kubectl delete namespace georag
```

## Verifying a bundle BEFORE you ship it

On the build host:

```bash
bash scripts/verify_airgap_bundle.sh dist/georag-airgap-v1.0.0.tar.gz
```

This checks file shape, image count, MANIFEST sanity, install.sh
bash syntax, and spot-checks image tarball integrity.

## Troubleshooting

| Symptom                                  | Fix                                                  |
|------------------------------------------|------------------------------------------------------|
| `install.sh` fails on `k3s ctr` command  | Re-run with sudo, or `chmod +r /etc/rancher/k3s/k3s.yaml` first |
| Pods stuck `ImagePullBackOff`            | Re-tag mismatch; `k3s ctr image ls` to confirm names |
| `pg-init` Job fails                      | Check `kubectl logs job/georag-pg-init` — usually a connectivity issue while postgres is still booting |
| Free disk full                           | Trim old images: `k3s ctr image rm $(k3s ctr image ls -q | grep -v georag)` |
