# GeoRAG FastAPI — Security posture

This file documents the security assumptions the FastAPI domain service
makes and the configuration flags that control them. Read it before
deploying in any multi-customer or multi-project environment.

---

## ⚠️  MULTI-TENANT RBAC — OFF BY DEFAULT

```
settings.MULTI_TENANT_ENFORCEMENT_ENABLED: bool = False   # DEFAULT
```

The default build ships with **single-tenant / graceful-rollout** posture:

- Requests authenticated with `X-Service-Key` alone are accepted.
- JWT `project_id` mismatches against the request body are **logged as
  warnings** but the request still proceeds using the body's project_id.
- Requests without a JWT at all are accepted so long as `X-Service-Key`
  validates.

This is safe when:
1. The deployment is single-customer. Every Laravel user shares the same
   trust boundary and there is no business need to prevent a user on
   project A from asking about project B — the Laravel front door
   already enforces project membership before minting the JWT.
2. You are in the middle of rolling out the `FastApiJwtMinter` to every
   Laravel deploy and cannot tolerate 403s from callers that haven't
   shipped the minter yet.

It is **NOT safe** when:
1. The same FastAPI instance serves multiple customer organisations.
2. A user on project A could in any way be induced to send a request
   with `body.project_id = <project belonging to customer B>` — the
   FastAPI service will happily retrieve project B's data because the
   flag is off.

### Turning it on

When every Laravel deployment hitting the FastAPI service signs requests
through `FastApiJwtMinter`:

1. Set `MULTI_TENANT_ENFORCEMENT_ENABLED=true` in the FastAPI env file.
2. Restart FastAPI. The startup log will print a confirmation banner.
3. From that point on, requests where the JWT `project_id` doesn't equal
   the body `project_id` — or where the JWT is missing a project_id —
   will receive HTTP 403 and never hit the retrieval pipeline.

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
