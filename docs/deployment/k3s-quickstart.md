# GeoRAG on K3s — 10-minute quickstart

For Linux nodes with internet. For air-gap, see
`airgap/README.md` instead.

## 1. Install K3s

```bash
curl -sfL https://get.k3s.io | sh -
sudo chmod a+r /etc/rancher/k3s/k3s.yaml
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
kubectl get nodes  # expect: NotReady → Ready within 30s
```

## 2. Install Helm

```bash
curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
```

## 3. Install GeoRAG

```bash
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
```

`--timeout 15m` because `helm install` waits for the two install hooks, and
the second runs every migration on a fresh database. `global.createNamespace=false`
because `--create-namespace` already makes the namespace, and Helm cannot adopt
a namespace it did not create.

## 4. Watch it come up

```bash
kubectl -n georag get pods -w
```

You should see (in order):
1. `georag-postgresql-0` Ready (~30s)
2. `georag-redis-0`, `georag-qdrant-0`, `georag-seaweedfs-0` Ready (~1-2min)
3. `georag-pg-init-...` Completed (database roles), then `georag-schema-...`
   Completed (`migrate` and `db:apply-raw`)
4. `georag-pgbouncer-...`, `georag-fastapi-...`, `georag-laravel-octane-...` Ready
5. `georag-embedding-...` and `georag-reranker-...` Ready last: they download
   their Qwen3 weights from huggingface.co on first start (a few GB, minutes)

Total cold-start: ~5-10 minutes on a 4-core box. `georag-fastapi` and
`georag-hatchet-worker` cannot authenticate to the Hatchet engine until you
mint a token; see `charts/georag/README.md`, "Hatchet token".

## 5. Hit it

```bash
# K3s routes /etc/hosts override on the node:
echo "127.0.0.1 georag.local" | sudo tee -a /etc/hosts

# Browse
curl http://georag.local/up    # expect "Application up" Laravel response
```

## 6. Make yourself an admin

```bash
kubectl -n georag exec deploy/georag-laravel-octane -- \
    php artisan tinker --execute='\App\Models\User::factory()->create(["email" => "you@local", "is_admin" => true])'
```

Then `http://georag.local/admin/eval/questions` will load the §10-v2
authoring UI.

## Common K3s gotchas

| Symptom                                | Fix                                                              |
|----------------------------------------|------------------------------------------------------------------|
| PVC stuck `Pending`                    | `kubectl get sc` — confirm `local-path` exists as `(default)`     |
| Pod stuck `ImagePullBackOff`           | K3s containerd doesn't see images; `sudo k3s ctr image ls` to check |
| Ingress not routing                    | `kubectl -n kube-system get pods -l app.kubernetes.io/name=traefik` |
| embedding / reranker Pod not Ready     | First start is downloading weights from huggingface.co; `kubectl -n georag logs` it |
| `helm install` timed out in a hook     | `kubectl -n georag logs job/georag-schema`; fix and `helm upgrade` |

## Uninstall

```bash
helm uninstall georag -n georag
kubectl -n georag delete pvc --all  # only if you really want to drop data
kubectl delete namespace georag
```

For full reference: `docs/deployment/k8s-reference.md`.
