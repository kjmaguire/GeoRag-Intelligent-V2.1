# authz_audit Triage

**Module 10 Chunk 10.8** — investigate spikes in 403 events from Module 9
Chunk 9.8's structured `authz.deny` channel.

## When to use this runbook

The `LaravelAuthzDenyBurst` alert fires when `rate(laravel_authz_deny_total[5m]) > 1`
sustained 5 min. That's roughly "more than 60 denials in five minutes" —
above the noise floor but well below realistic load.

Other triggers:
- Operator-flagged spike from the `GeoRAG — Authorization` dashboard.
- Customer report of repeated "Project not found" 404s.
- Post-incident review surfaces a reason="cross_workspace" cluster.

## Quick triage (3 minutes)

```bash
# 1. What reason codes are firing?
curl -sG http://localhost:9090/api/v1/query \
    --data-urlencode 'query=sum by (reason) (rate(laravel_authz_deny_total[5m]))'
```

| Reason | Likely cause |
|--------|--------------|
| `no_pivot_row` | Real user hitting a project they don't own. Could be UI bug (stale URL) or probing. |
| `cross_workspace` | JWT-vs-header mismatch. **Investigate immediately** — possible token spoofing. |
| `unauthenticated` | Missing JWT. Probably a misbehaving client; could be an attacker probing. |
| `cross_user` | User A trying to read user B's chat conversation. Same shape as no_pivot_row. |
| `admin_only` | Non-admin user hitting an admin-scoped controller (column mappings, vendor profiles). |

## Drill into Loki

The Prometheus counter shows "how many" per reason; Loki shows "who"
and "what":

```
{channel="authz_audit"}
  | json
  | event="authz.deny"
  | reason="cross_workspace"
  | line_format "{{.actor_user_id}} -> {{.target_resource}} ({{.target_workspace_id}}) action={{.action}} path={{.path}}"
```

Open Grafana → Explore → Loki datasource → paste the query above. The
dashboard `GeoRAG — Authorization (authz.deny)` has a documented panel
that constructs this query at the click of a button.

## Triage by reason

### cross_workspace

**Treat as a security incident until proven otherwise.**

```
{channel="authz_audit"} | json | reason="cross_workspace" [last 1h]
```

For each event, capture:
- `actor_user_id` — who's making the request.
- `target_workspace_id` — what workspace the JWT vs header disagrees on.
- `path` — which endpoint.
- Time clustering — single user repeatedly? Or distributed?

**If single user, distributed clicks:** stale JWT after a workspace
switch. Force the user to re-login (revoke their Sanctum tokens):

```bash
docker compose exec laravel-octane php artisan tinker
>>> User::find(<actor_user_id>)->tokens()->delete();
>>> exit
```

**If multiple users from one IP/CIDR:** investigate as probing. Capture
IP, time, request bodies (if logged) and escalate to security.

**If clustered around a deploy SHA:** likely a regression in workspace
resolution. See `git log` for changes to
`src/fastapi/app/services/workspace_resolution.py` or the `InjectTraceparent`
middleware. Roll back if the cluster is wide.

### no_pivot_row + cross_user

Most common reason. Triage:

```
{channel="authz_audit"} | json | reason=~"no_pivot_row|cross_user"
```

Look for:
- Same user hitting multiple distinct project UUIDs in a short window
  → enumeration / scraping. Rate-limit + investigate.
- Same target_resource hit by many users → broken UI link OR shared
  bookmark. Check `path` patterns.

If the actor is a known internal user and the target is a project they
DO have access to (verify via project_user pivot), the bug is in
`hasProjectAccess()` or the cache layer. File a ticket; don't roll back
unless impact is wide.

### unauthenticated

Anonymous traffic hitting `/v1/*`. Most common cause: misbehaving
client / forgotten Authorization header. Less common: probing.

```
{channel="authz_audit"} | json | reason="unauthenticated"
  | line_format "{{.path}} from={{.client_ip}}"
```

Cluster by IP. Rate-limit upstream if a single IP exceeds 100/min.

### admin_only

Non-admin user hit `/api/v1/column-mappings` or
`/api/v1/vendor-profiles`. Usually:
- Operator tested a route in dev, forgot to grant admin in prod.
- Power user discovered admin-only routes via API exploration.

Either way: check the user's `is_admin` flag is correct. Update if needed.
The route itself is correctly defended — this is typically a permission
issue, not a security incident.

## What to NOT do

- **Don't disable the audit channel** to silence the noise. The signal is
  the point.
- **Don't grant blanket access** to "fix" a no_pivot_row spike. Investigate
  whether the user *should* have access; if yes, add the pivot row; if no,
  the gate is working as intended.
- **Don't roll back the deploy** unless the spike correlates with the
  deploy SHA AND is sustained > 30 min. Spikes from operator backfills,
  testing, or batch jobs are normal.

## Audit trail

Every triage action should leave a breadcrumb:

```bash
docker compose exec laravel-octane php artisan tinker
>>> Log::channel('authz_audit')->info('triage_action', [
...     'incident' => 'cross_workspace_spike',
...     'actor' => 'kyle@example.com',
...     'action_taken' => 'revoked_tokens_for_user_42',
...     'reason' => 'stale_jwt_after_workspace_switch',
... ]);
```

The breadcrumb shows up in the same dashboard the spike does — useful
for the post-mortem.

## Cross-references

- `ops/runbooks/on-call.md` — escalation script.
- `ops/runbooks/log-retention.md` — how long the events stay queryable.
- `app/Support/AuthorizationAuditLogger.php` — the writer side.
- `app/Providers/AppServiceProvider.php` — the listener that bridges to
  Prometheus counter (Module 10 Chunk 10.4).
- `docker/grafana/dashboards/georag-authz.json` — visualisation.
- Grafana panel "Loki transition path" in the authz dashboard documents
  the LogQL queries this runbook reuses.
