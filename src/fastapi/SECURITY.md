# GeoRAG FastAPI — Security posture

This file documents the security assumptions the FastAPI domain service
makes and the configuration flags that control them. Read it before
deploying in any multi-customer or multi-project environment.

---

## ✅ MULTI-TENANT RBAC — ON BY DEFAULT

```
settings.MULTI_TENANT_ENFORCEMENT_ENABLED: bool = True   # DEFAULT (config.py)
```

> **Audit 2026-06-27 correction.** This section previously claimed the flag was
> `False` / OFF by default. That was stale and asserted the **opposite** of the
> live default. Enforcement is **ON out of the box**; the single-tenant posture
> below is now an explicit, validated opt-out.

The default build ships **enforcing** tenant isolation:

- Requests where the JWT `project_id` does not equal the body `project_id` —
  or where the JWT is missing a `project_id` — receive HTTP 403 and never
  reach the retrieval pipeline.
- `X-Service-Key` authenticates the service-to-service hop; the JWT carries the
  per-request identity (`project_id`, and `workspace_id` as of the 2026-06
  streaming-JWT fix) used by the RLS GUC binding.

### Explicit single-tenant / graceful-rollout opt-out

For a single-customer deployment — or while rolling `FastApiJwtMinter` out to
every Laravel deploy — you may relax enforcement, but config.py **requires you
to acknowledge it explicitly** (`MULTI_TENANT_ENFORCEMENT_ENABLED=False`
without `SINGLE_TENANT_MODE=True` raises at startup; you cannot silently
disable isolation):

1. Set `MULTI_TENANT_ENFORCEMENT_ENABLED=false` AND `SINGLE_TENANT_MODE=true`
   in the FastAPI env file.
2. In that relaxed posture: `X-Service-Key`-only requests are accepted; JWT
   `project_id` mismatches are logged as warnings but the request proceeds
   using the body's `project_id`; requests without a JWT are accepted so long
   as `X-Service-Key` validates.

The relaxed posture is safe **only** when the deployment is single-customer
(every Laravel user shares one trust boundary and the Laravel front door
already enforces project membership before minting the JWT). It is **NOT safe**
when the same FastAPI instance serves multiple customer organisations — a user
induced to send `body.project_id = <another customer's project>` would have it
honoured. Leave the default (enforcement ON) for any multi-customer deployment.

### Defence-in-depth: row-level security in PostGIS

The migration `2026_04_17_120200_replace_toothless_rls_with_guc_aware_policies.php`
installs RLS policies on `silver.collars` and `silver.samples` that read
`current_setting('georag.project_id', true)` and admit:

- **Every row** when the GUC is unset (single-tenant deployments and
  Dagster ingestion — backwards-compatible with everything that exists
  today).
- **Only matching project_id rows** when the GUC is set.

To activate the GUC path on the FastAPI side, switch tool code from:

```python
async with deps.pg_pool.acquire() as conn:
    rows = await conn.fetch(sql, *args)
```

to the project-scoped helper that ships in `app.agent.deps.AgentDeps`:

```python
async with deps.acquire_scoped() as conn:
    rows = await conn.fetch(sql, *args)
```

`acquire_scoped` opens a transaction, runs `SET LOCAL georag.project_id =
'<uuid>'` when `MULTI_TENANT_ENFORCEMENT_ENABLED` is True, and yields the
connection. The `SET LOCAL` is transaction-scoped — safe under PgBouncer
transaction pooling because the backend connection isn't returned to the
pool until COMMIT / ROLLBACK fires.

Net effect when the flag is on: even if a tool query forgets to include
`WHERE project_id = $1`, the RLS policy silently filters cross-project
rows out before they reach the LLM. That's the defence the previous
`USING (true)` policy was pretending to provide.

### Tests

See `tests/test_multi_tenant_enforcement.py` for the contract.

---

## Qdrant document-scope filter (P0 #1)

```
settings.QDRANT_DOCUMENT_PROJECT_SCOPE: str = "cross_project"   # DEFAULT
```

Controls whether `search_documents` applies a Qdrant payload filter on
`project_id` when retrieving chunks from the `georag_reports` collection.

- `cross_project` — **default**, no filter. Safe only if the collection
  holds purely public NI 43-101 filings.
- `project_or_public` — admit chunks whose `project_id` matches the
  caller OR is missing (legacy) OR equals `"public"`. Recommended after
  running the `index_reports` asset with `config.project_id` stamped on
  every report.
- `strict` — admit only chunks whose `project_id` equals the caller.
  Will return zero results until the indexer has stamped `project_id`
  on every point in the collection.

Bump `DOCUMENT_SCOPE_VERSION` when flipping this setting so cached RAG
responses from the previous policy are invalidated.

---

## Query-plaintext logging (P0 #3)

User query text is never emitted to logs in plaintext. Every log line
that used to carry `query='%.80s'` now emits `query_hash=<16 hex>` via
`app.agent.log_safe.query_hash()`, which is HMAC-SHA256 keyed on
`FASTAPI_SERVICE_KEY`. The audit log in Laravel stores the same HMAC in
the `query_text_hash` column, so you can correlate a log line to the
encrypted audit row without exposing the query content.

The encrypted full query text is still available in
`query_audit_logs.query_text` for administrators with DB access; it is
AES-GCM encrypted via Laravel's `encrypted` cast.

---

## Column whitelist (P0 #2)

`app.agent.tools.verify_numerical_claim` uses a hard-coded per-table
column allowlist instead of raw string interpolation. Any claim citing a
column not on the allowlist returns `BLOCKED` immediately. See
`tests/test_verify_numerical_claim_whitelist.py` for the contract.

Do NOT widen the allowlist without a test update.

---

## Reporting issues

Security issues should be reported to the project maintainer directly
(see `CODEOWNERS`). Do not file public GitHub issues for unpatched
vulnerabilities.
