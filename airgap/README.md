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
| `images/`             | Saved Docker images (`docker save` format), one per service, and `index.txt` mapping each tarball to the image reference it carries (`install.sh` reads it) |

Typical bundle size: **15–20 GB** (images + the Helm chart). No model
weights are bundled: the chart deploys no inference server. LLM calls
go to Cohere's own API, or to an OpenAI-compatible endpoint you run
yourself (`LLM_BACKEND=vllm` with `VLLM_URL` pointing at it) — which
in an air-gapped install means one you host inside the enclave. The
embedding and reranker sidecars do need Qwen3 weights that are not in
the images; see "Model weights" below.

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
   - PVCs (postgres + qdrant + redis + seaweedfs + the Hatchet engine + the
     embedding and reranker model caches): ~200 GB
   - **Total minimum free**: 225 GB

4. **(Optional) Private container registry** if you have multiple
   nodes. For single-node K3s you can skip this — the install loads
   images directly into containerd via `ctr image import`. With
   `--registry`, `install.sh` tags the images under that prefix **on this
   node only**; it does not push. Push the `<registry>/…` tags to your
   registry yourself (`docker push`, or `k3s ctr image push`) before the
   other nodes need them.


## Install

```bash
# 1. Unpack
tar xzf georag-airgap-v1.0.0.tar.gz
cd _stage_v1.0.0

# 2. (Optional) Prepare a secrets file. Skip it and install.sh writes
#    ./georag-secrets.env with random values (mode 600). Either way KEEP the
#    file: every upgrade needs the same passwords, and a re-install over
#    existing data volumes needs them too.
cat > secrets.env <<EOF
POSTGRES_PASSWORD=$(openssl rand -base64 32)
PG_APP_PASSWORD=$(openssl rand -base64 32)
MARTIN_DB_PASSWORD=$(openssl rand -hex 32)
REDIS_PASSWORD=$(openssl rand -base64 32)
FASTAPI_SERVICE_KEY=$(openssl rand -base64 48)
LARAVEL_APP_KEY=base64:$(openssl rand -base64 32)
HATCHET_DB_PASSWORD=$(openssl rand -hex 32)
REVERB_APP_SECRET=$(openssl rand -base64 32)
# The key the laravel image was BUILT with (VITE_REVERB_APP_KEY); see "Live chat"
REVERB_APP_KEY=<the build-time value>
# Optional: QDRANT_API_KEY=  COHERE_API_KEY=  (air-gapped: leave COHERE_API_KEY out)
# HATCHET_CLIENT_TOKEN= cannot be set yet; see "Hatchet token" below
EOF
chmod 600 secrets.env

# 3. Install (the first pass imports ~20 GB of images; then helm waits for the
#    pg-init and schema hooks, so allow 10-20 min)
./install.sh --secrets-file secrets.env
```

Martin's and Hatchet's database passwords are embedded in URLs: hex only.
Keys the installer does not know are reported rather than dropped, and a
missing `--secrets-file` is an error (it used to mint random secrets
silently). Any key a first install's file lacks is generated and appended to
that file.

If you have a private registry instead of single-node K3s:

```bash
./install.sh \
    --registry harbor.your-corp.local/georag \
    --secrets-file secrets.env
```

## Post-install

```bash
# Watch pods. helm already waited for the pg-init Job (database roles) and the
# schema Job (php artisan migrate + db:apply-raw), and deletes each once it
# succeeds; if one failed it is still there: kubectl -n georag logs job/georag-schema
kubectl -n georag get pods -w

# Make the first admin
kubectl -n georag exec deploy/georag-laravel-octane -- \
    php artisan tinker --execute='\App\Models\User::factory()->create(["email" => "admin@your-corp", "is_admin" => true])'

# Find the URL
kubectl -n georag get ingress
```

## Live chat

Chat answers reach the browser over a WebSocket to Reverb. Its key is public
by design and is **baked into the laravel image when that image is built**
(`VITE_REVERB_APP_KEY`, plus `VITE_REVERB_SCHEME` and, for a plain-http
ingress, `VITE_REVERB_PORT=80`). `REVERB_APP_KEY` must be that same value; a
mismatch connects, is refused, and the chat stream hangs with nothing in any
log. The installer cannot know it: pass `--reverb-app-key` (or a
`REVERB_APP_KEY` line) with the value the image was built with. Without one it
generates a random key and says so. Details: `charts/georag/README.md`,
"Live chat (Reverb)".

## Hatchet token

The Hatchet engine mints its client token from a keyset it writes on first
boot, so it cannot be in the secrets file at install time. Until it is set,
fastapi and the worker cannot authenticate to the engine (and an engine that
is healthy beside clients that are not reads like a Hatchet fault when it is
not one). Once `georag-hatchet-0` is Ready:

```bash
NS=georag
TENANT=$(kubectl -n $NS exec statefulset/georag-postgresql -- \
  psql -U georag -d hatchet -At -c "SELECT id FROM \"Tenant\" WHERE slug='default'")
# --config goes BEFORE the subcommand, and SERVER_AUTH_COOKIE_SECRETS must be
# set even though nothing here serves a cookie: both are load-bearing.
TOKEN=$(kubectl -n $NS exec statefulset/georag-hatchet -- \
  env SERVER_AUTH_COOKIE_SECRETS="mint mint" \
  /hatchet-admin --config /config token create --name georag --tenant-id "$TENANT")
helm upgrade georag chart/georag-*.tgz --namespace $NS --reuse-values \
  --timeout 20m --set secrets.hatchetClientToken="$TOKEN"
kubectl -n $NS rollout restart deployment/georag-fastapi deployment/georag-hatchet-worker
```

This is the command `docker-compose.yml` and `deploy/aws/README.md` document;
it has not been run against a cluster from this chart. The token expires after
90 days and nothing renews it.

## Model weights

`sparse` (SPLADE++) is baked into the image. **`embedding` and `reranker`
download Qwen3 weights from huggingface.co on first start**, which an
air-gapped cluster cannot do; `values-airgap.yaml` sets
`modelSidecars.offline`, so a cold cache makes them fail at once with a clear
"not in cache" error instead of retrying the network. Seed their cache PVCs
once, from a machine that can reach the Hub:

```bash
pip install huggingface_hub
# the revisions are the pins in values.yaml (modelSidecars.embedding.revision)
# and src/fastapi/app/services/reranker.py (RERANKER_REVISION)
hf download Qwen/Qwen3-Embedding-0.6B --revision 97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3 --cache-dir hf_cache
hf download Qwen/Qwen3-Reranker-0.6B  --revision e61197ed45024b0ed8a2d74b80b4d909f1255473 --cache-dir hf_cache
tar czf hf_cache.tgz -C hf_cache .
```

Carry `hf_cache.tgz` in, then into each pod's cache (`/tmp/hf_cache`):

```bash
for c in embedding reranker; do
  kubectl -n georag cp hf_cache.tgz "$(kubectl -n georag get pod -l app.kubernetes.io/component=$c -o name | cut -d/ -f2)":/tmp/hf_cache.tgz
  kubectl -n georag exec deploy/georag-$c -- tar xzf /tmp/hf_cache.tgz -C /tmp/hf_cache
  kubectl -n georag rollout restart deploy/georag-$c
done
```

These commands have not been run against a cluster. Two limits to know:

- The cache lives on a PVC, so it survives restarts; deleting the PVC means
  seeding again.
- The Hatchet worker's ingest path loads its own copy of the dense model
  (`passage_embedder.load_embedding_model` ignores `EMBEDDING_SERVICE_URL`)
  from `/tmp/hf_cache` in the worker pod, which is not persistent and is not
  seeded by the steps above. On a cluster with no route to the Hub, document
  ingestion cannot embed passages until the worker has the weights too.
  Bundling the weights and seeding the worker is an open item.

## Upgrade in place

Ship a new air-gap bundle to the customer, then:

```bash
tar xzf georag-airgap-v1.1.0.tar.gz
cd _stage_v1.1.0
./install.sh --secrets-file ../_stage_v1.0.0/georag-secrets.env
# (install.sh sees the existing release and runs `helm upgrade --install
#  --reuse-values`; nothing is regenerated, so no password is rotated)
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
| `helm` times out in a hook               | `kubectl -n georag logs job/georag-pg-init` (database roles) or `job/georag-schema` (migrations); fix, then re-run `./install.sh` (idempotent) |
| `ctr image tag` / "not found" at load    | `sudo k3s ctr image ls` to see what containerd named the import; images/index.txt is the source of the names |
| embedding / reranker pod never Ready     | Their cache is empty: "Model weights" above |
| Free disk full                           | Trim old images: `k3s ctr image rm $(k3s ctr image ls -q | grep -v georag)` |
