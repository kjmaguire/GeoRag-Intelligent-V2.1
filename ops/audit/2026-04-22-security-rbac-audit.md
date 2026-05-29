# Module 9 — Security & RBAC Phase A Read-Only Audit

**Date:** 2026-04-22
**Auditor:** Claude Opus 4.7 (1M context)
**Scope:** evidence-grounded review of authentication, authorization, multi-tenant
isolation, transport security, and sensitive-data handling across the Laravel
monolith and the FastAPI domain service. Read-only. No code or config changed.
**Predecessors:** `ops/backlog/module-10-auth-bypass-sweep.md` (Haiku, 2026-04-19),
which is referenced and partially superseded.

---

## 1. Executive Summary

The cross-service identity contract is in better shape than the prior Haiku
audit suggested. The Laravel→FastAPI shared-secret + short-lived HS256 JWT
spine is implemented correctly; the per-transaction GUC plumbing the previous
audit flagged as Critical is **already wired** (`SET LOCAL georag.project_id`,
`src/fastapi/app/agent/deps.py:160`). The FastAPI `/internal/queries` route
correctly enforces JWT-vs-body project equality when
`MULTI_TENANT_ENFORCEMENT_ENABLED=True` (`src/fastapi/app/routers/queries.py:501-523`).
Qdrant retrieval has a mandatory `workspace_id` payload filter
(`src/fastapi/app/services/qdrant_service.py:110-117`), and the silver tile
proxy now performs an explicit project access check
(`app/Http/Controllers/PublicGeoscience/TileProxyController.php:257-262`).

That said, **the Laravel side has multiple confirmed IDOR primitives** that
defeat the rest of the spine: `ProjectController::show/update/destroy`
(`app/Http/Controllers/Api/V1/ProjectController.php:66-122`) and the entire
`CollarController` (`app/Http/Controllers/Api/V1/CollarController.php:22-80`)
do *no* membership check — `findOrFail($projectId)` is the only gate. Combined
with `User::hasProjectAccess()` failing **open** when the `project_user` pivot
table is missing (`app/Models/User.php:52-58`), and `MULTI_TENANT_ENFORCEMENT_ENABLED`
defaulting to `False` (`src/fastapi/app/config.py:354`), a single workspace
that has not run all migrations or has the flag off has no enforced tenant
boundary. Add to that **zero security headers** (no CSP / HSTS / X-Frame-Options
emitted anywhere — exhaustive grep returned 0 matches), no `TrustProxies`
middleware, three Dagster containers running as `root`
(`docker-compose.yml:790, 1362, 1437`), and a dormant-but-trivial
default-workspace-UUID fallback in two FastAPI public routers
(`evidence.py:91`, `answer_runs.py:83`).

**Verdict: DO NOT SHIP.** The Laravel IDOR set is exploitable today by any
authenticated user with a workspace UUID; this is multi-tenant data leakage by
default. Module 9 must close the IDOR set, harden `User::hasProjectAccess`,
flip `MULTI_TENANT_ENFORCEMENT_ENABLED` to `True` for any deployment with more
than one workspace, and ship a security-headers middleware before the platform
is presented to any external customer.

---

## 2. AUTH-04 — Correction of Prior Critical Finding

The 2026-04-19 audit (`ops/backlog/module-10-auth-bypass-sweep.md` and earlier
informal write-ups) flagged "RLS GUC plumbing pending" as **CRITICAL** based on
a comment in migration `2026_04_17_120200_replace_toothless_rls_with_guc_aware_policies.php`
that said the per-tx GUC was "future" work.

**This finding is incorrect.** The implementation is live at:

`src/fastapi/app/agent/deps.py:127-161` — `AgentDeps.acquire_scoped()` opens an
asyncpg transaction, sets `SET LOCAL statement_timeout`, validates the
project_id against a strict UUID regex (`_UUID_RE`, line 169), and issues:

```python
# src/fastapi/app/agent/deps.py:160
await conn.execute(f"SET LOCAL georag.project_id = '{pid}'")
```

The migration's GUC-aware policies (`silver.collars`, `silver.samples`) read
`current_setting('georag.project_id', true)` and admit only matching
project_id rows. The single missing piece is that the GUC is set to
`project_id`, not `workspace_id` — the policies and the FastAPI plumbing are
internally consistent but only enforce **project-level** isolation on these
two tables. See A3 below for the workspace-level RLS gap on the other 9
workspace-scoped tables.

**Status: AUTH-04 RESOLVED** as a critical. Re-classified as **MEDIUM (RLS
coverage gap)** under A3.

---

## 3. A1 — Identity & Authentication

### Evidence

**Sanctum config (`config/sanctum.php`):**
- Token TTL: 480 minutes (8 hours) via `env('SANCTUM_TOKEN_EXPIRATION', 480)` (line 49). Reasonable.
- Stateful domains: explicit allowlist of localhost/127.0.0.1 ports + `georag.local` (line 18-22). Production domains must be added via `SANCTUM_STATEFUL_DOMAINS` env.
- Guard: `web` only (line 36).
- CSRF middleware: `Illuminate\Foundation\Http\Middleware\ValidateCsrfToken` (line 80) — wired.

**Session config (`config/session.php`):**
- `http_only` defaults `true` (line 185). Good.
- `secure` is env-only with no default (line 172). **In an env that omits `SESSION_SECURE_COOKIE`, cookies will be sent over HTTP.** Acceptable for dev, dangerous in prod.
- `same_site` defaults `'lax'` (line 202). Acceptable for SPA.

**User model (`app/Models/User.php`):**
- `HasApiTokens` trait (line 21) — Sanctum personal-access tokens supported.
- `hasProjectAccess()` (line 45-61) — checks `project_user` pivot. **Lines 52-58 contain a fail-OPEN branch:**

```php
// app/Models/User.php:52-58
} catch (QueryException $e) {
    if (self::isMissingProjectUserPivot($e)) {
        Log::warning('User::hasProjectAccess: project_user pivot missing — access checks are DEGRADED (failing open)...');
        return true;   // ← FAIL OPEN
    }
    throw $e;
}
```

The migration that creates the pivot exists (`database/migrations/2026_04_11_000000_create_project_user_table.php`). On any environment where migrations have run, this branch is dead. On any environment where migrations have NOT run (fresh container, broken `php artisan migrate`, restored backup), every user has access to every project.

**Login routes (`routes/api.php:45-54`):**
- `register` — `throttle:3,1` (3/min/IP) — tight.
- `login` and `spa-login` — `throttle:auth-login` named limiter, keys on email + IP (per docblock comment lines 38-44). Good design.

**FastAPI auth (`src/fastapi/app/services/auth.py`):**
- `verify_service_key` (line 35-52): HMAC compare via `hmac.compare_digest`. Constant-time. Required on every `/internal/*` and `/v1/*` route.
- `extract_user_context` (line 78-146): decodes Bearer JWT with HS256, validates `iss="georag-laravel"`, `aud="georag-fastapi"`, requires `exp/iat/sub`. **Crucial behaviour at lines 95-100: missing or malformed Authorization → returns empty UserContext (no error).** This is the documented "graceful rollout" — but it means routes that *only* depend on `extract_user_context` for tenant scoping (not `verify_service_key`) get a `user_id=None` request through.

**JWT minter (Laravel side, `app/Services/FastApiJwtMinter.php`):**
- TTL: 60 seconds (line 31, `private const TTL_SECONDS = 60`). Excellent.
- Algorithm: HS256 (line 35). Algorithm pinned in both directions (Laravel signs with HS256, FastAPI decodes with `algorithms=["HS256"]`) — no algorithm-confusion attack surface.
- Key size enforced ≥ 32 bytes (line 38, RFC 7518 §3.2 floor).

### Findings — A1

| ID | Severity | File:line | Finding |
|---|---|---|---|
| A1-01 | **CRITICAL** | `app/Models/User.php:52-58` | `hasProjectAccess()` fails OPEN when `project_user` pivot is missing. Any environment without the 2026_04_11 migration grants every user access to every project. Convert to fail-CLOSED + alert; treat the pivot absence as a startup health-check failure rather than a per-request fall-through. |
| A1-02 | **HIGH** | `src/fastapi/app/services/auth.py:95-100` | `extract_user_context` returns an empty `UserContext` when the Authorization header is absent. The graceful-rollout window has now lasted ≥ 1 module cycle; pin every Laravel deploy to mint JWTs and tighten the empty case to raise 401. Otherwise a Laravel bug that fails to set the header silently downgrades RBAC to "X-Service-Key only" with `user_id=None`. |
| A1-03 | MEDIUM | `config/session.php:172` | `'secure' => env('SESSION_SECURE_COOKIE')` has no PHP-level default. Add `?? !app()->environment('local')` or hard-default to `true` and let dev override via env. |
| A1-04 | LOW | `config/sanctum.php:18-22` | Hardcoded localhost stateful-domain list will silently break SPA cookie auth in prod if `SANCTUM_STATEFUL_DOMAINS` isn't set. Add an explicit boot-time check that flags a non-empty production stateful-domain list. |

---

## 4. A2 — RBAC Enforcement (Route-by-Route)

### Evidence

**`POST /internal/queries` (FastAPI)** — `src/fastapi/app/routers/queries.py:494-523`:

```python
# routers/queries.py:501-523
if _settings.MULTI_TENANT_ENFORCEMENT_ENABLED:
    if not user.project_id:
        raise HTTPException(status_code=403, detail="Missing project_id claim on JWT. Re-authenticate.")
    if user.project_id != body.project_id:
        raise HTTPException(status_code=403, detail="JWT project does not match request project.")
```

**Correctly enforced when the flag is on.** When the flag is off (the
default — `src/fastapi/app/config.py:354 MULTI_TENANT_ENFORCEMENT_ENABLED: bool = False`),
the comparison is skipped and `body.project_id` (caller-supplied) wins.

**`GET /v1/evidence/{id}` (FastAPI)** — `src/fastapi/app/routers/evidence.py:67-91, 215-241`:
- Workspace resolved from `X-Workspace-Id` header (line 81-89) **OR falls back to the seeded default workspace UUID** `a0000000-0000-0000-0000-000000000001` (line 91) when the header is absent.
- Evidence fetch (line 237) joins on `workspace_id = $2` from the resolved value.
- The fallback is shipped behind `verify_service_key` (router-level, `evidence.py:52`), so it is not directly exposed to browsers — but a Laravel proxy that forwards user input as `X-Workspace-Id` would let any authenticated user request any workspace's evidence. **No Laravel route currently calls this endpoint** (grep for `/v1/evidence` in `app/`: `0` matches), so the route is dormant — but the design is broken.

**`POST /v1/answer_runs/{id}/feedback` (FastAPI)** — `src/fastapi/app/routers/answer_runs.py:69-83, 91-138`:
- Same `_resolve_workspace_id` pattern (line 69-83) — same default-workspace fallback.
- `_check_answer_run_workspace` (line 91-138) does fetch the answer_run's workspace and compares to the resolved value (line 134). Tight comparison. Good.
- But the input to the comparison is the caller-supplied X-Workspace-Id (or the default), not derived from JWT or session. **Trust boundary leaks one hop downstream** — the security depends entirely on whoever calls this endpoint setting the right header.

**`GET /v1/answer_runs/{id}/events`** — same pattern, same caveat.

**`GET /api/v1/projects/{id}` (Laravel)** — `app/Http/Controllers/Api/V1/ProjectController.php:66-83`:

```php
public function show(string $projectId): JsonResponse
{
    try {
        $project = Project::withCount('collars')->findOrFail($projectId);
        return (new ProjectResource($project))->response();
    } ...
```

**No membership check.** Any authenticated user with a project UUID gets the
record. `update()` (line 90-108) and `destroy()` (line 115-) follow the same
pattern.

**`POST/GET/DELETE /api/v1/projects/{id}/collars/...`** —
`app/Http/Controllers/Api/V1/CollarController.php:22-145`: every method does
`Project::findOrFail($projectId)` (lines 26, 65, 98, 135) with **zero
membership check**. List, create, show, destroy: all open IDOR.

**`POST /api/v1/queries`, `POST /api/v1/projects/{id}/upload`, exports** —
correctly gate via `$user->hasProjectAccess($projectId)`:
- `app/Http/Controllers/Api/V1/QueryController.php:50` — `if ($user === null || !$user->hasProjectAccess($projectId))`.
- `app/Http/Controllers/Api/V1/UploadController.php:65` — same pattern.
- `app/Http/Controllers/Api/V1/ExportController.php:198` — same pattern.

But because `hasProjectAccess` itself fails open (A1-01), this gate is only as
strong as the migration state.

**`GET /tiles/silver/{source}/{z}/{x}/{y}.pbf` (Laravel TileProxy)** —
`app/Http/Controllers/PublicGeoscience/TileProxyController.php:228-262`:

```php
// TileProxyController.php:257-262
if (! $this->userHasProjectAccess($projectId)) {
    return response()->json(['message' => 'Access denied to this project.'], 403);
}
```

`userHasProjectAccess` (line 520-528) delegates to `User::hasProjectAccess` —
inherits the fail-open branch.

**`GET /tiles/public-geoscience/...`** — `routes/web.php:71-78`. Confirmed
workspace-agnostic by design (PGEO is a single read-only public corpus). Only
auth requirement is `auth:sanctum` (the parent group). Correct.

**Document upload** — `routes/api.php:114`: `POST projects/{project}/upload` — gated by `hasProjectAccess` (UploadController.php:65). Inherits A1-01 risk.

**Export routes** — `routes/api.php:99-111`. `apiResource('projects.exports')` is `scoped()` (Laravel route-model binding scoping). The named `exports.download` route is not project-scoped (line 110), so any user with an export UUID can download. Need to verify ExportController::download enforces ownership.

### Findings — A2

| ID | Severity | File:line | Finding |
|---|---|---|---|
| A2-01 | **CRITICAL** | `app/Http/Controllers/Api/V1/ProjectController.php:66-122` | `show/update/destroy` use `findOrFail($projectId)` with no membership gate. Any authenticated user can read/update/delete any project by UUID. Add `if (!$request->user()->hasProjectAccess($projectId)) abort(403);` to all three (and call it before findOrFail to avoid existence oracles). |
| A2-02 | **CRITICAL** | `app/Http/Controllers/Api/V1/CollarController.php:22-145` | All four methods (index/store/show/destroy) leak collar records cross-tenant. The `scoped()` route binding doesn't enforce parent-pivot ownership — only that the child belongs to the parent UUID. Fix: gate every method with `hasProjectAccess` like QueryController does. |
| A2-03 | **HIGH** | `src/fastapi/app/config.py:354` | `MULTI_TENANT_ENFORCEMENT_ENABLED: bool = False` is the default. Any deployment that does not explicitly set this to `True` runs with the JWT-vs-body equality check disabled. Recommend flipping the default and gating "single-tenant escape hatch" behind an explicit `SINGLE_TENANT_MODE` env flag. |
| A2-04 | **HIGH** | `src/fastapi/app/routers/evidence.py:81-91`, `src/fastapi/app/routers/answer_runs.py:74-83` | Workspace resolution falls back to the default-workspace UUID when `X-Workspace-Id` is absent. Service-to-service trust assumes Laravel always sets the header — confirmed by exhaustive grep that **Laravel does not currently set this header on any outbound call** (`grep -rn 'X-Workspace-Id' app/` → 0 matches). Routes are dormant today; if any Laravel proxy is added in Module 9 without setting the header, every request collapses to the default workspace silently. Either remove the fallback or derive workspace from the JWT `project_id` via DB lookup. |
| A2-05 | MEDIUM | `app/Http/Controllers/Api/V1/ExportController.php` (`download` action) | The `exports.download` route (`routes/api.php:110`) is not under the `apiResource(...)->scoped()` parent and the controller may not re-check ownership on the export ID. Read the `download` method and confirm. |
| A2-06 | MEDIUM | `app/Http/Controllers/Api/V1/ColumnMappingController.php`, `VendorProfileController.php` | Vendor profiles + column mappings are described as "global" in the route comment (`routes/api.php:117`). Verify they are not workspace-scoped data; if they are, add a tenant filter. |

---

## 5. A3 — Data Store Isolation

### PostgreSQL RLS

Search: `grep -rn "ENABLE ROW LEVEL SECURITY\|CREATE POLICY" database/migrations/`
returns matches in **only two files** — `2026_04_13_200000_production_hardening_final.php`
and `2026_04_17_120200_replace_toothless_rls_with_guc_aware_policies.php`.

**Tables with RLS (2):** `silver.collars`, `silver.samples` — both via the GUC-aware
policy that reads `current_setting('georag.project_id', true)`.

**Tables with `workspace_id` column but no RLS (≥ 9, by `grep -lrn "workspace_id\s*UUID" database/migrations/`):**
- `silver.workspaces` (self)
- `silver.projects.workspace_id`
- `silver.evidence_items`
- `silver.answer_runs`
- `silver.answer_retrieval_items`
- `silver.answer_citation_items`
- `silver.answer_citation_spans`
- `silver.document_revisions`
- `silver.document_passages`
- `silver.message_feedback`
- `silver.drill_traces`
- `silver.boundary` / `silver.formation` / `silver.working_geochem`

**RLS coverage is ≈ 15%.** Application-layer WHERE clauses are the only
safety net for the rest. A single missing `WHERE workspace_id = ?` on any of
those tables = cross-tenant leak.

### Qdrant

`src/fastapi/app/services/qdrant_service.py:107-128`:

```python
ws_str = str(workspace_id)
ws_filter = Filter(must=[FieldCondition(key="workspace_id", match=MatchValue(value=ws_str))])
```

**Mandatory.** Every `query_points` call composes `branch_filter` from
`ws_filter` + optional additional conditions. Good.

### Neo4j

`src/fastapi/app/agent/tools.py:1303-1310`:

```cypher
MATCH (start) WHERE start.project_id = $project_id ...
```

Cypher queries filter on `project_id` (not `workspace_id`). Consistent with
the project-level isolation model used by Postgres RLS. Acceptable as long as
project_id is treated as a tenant-isolation boundary (single-workspace projects
only).

**However**, the `project_id` parameter flows from the `deps.project_id`
field, which in turn flows from `body.project_id` in
`routers/queries.py:199`. With `MULTI_TENANT_ENFORCEMENT_ENABLED=False`, this
is caller-controlled.

### Redis

`src/fastapi/app/agent/orchestrator.py:2711-2720`:

```python
# georag:rag_cache:v6:{sha256[:16](q|wid|pid|wdv|pdv|rsv|spv|fh|rh|cats)}
return f"georag:rag_cache:v6:{h}"
```

Cache key includes `wid` (workspace) and `pid` (project) in the hash input.
Workspace-scoped. Good.

`src/fastapi/app/agent/event_stamper.py:102` — `georag:answer_run_events:<answer_run_id>` —
keyed on the answer_run UUID, which is itself tenant-bound via the
`silver.answer_runs.workspace_id` FK. Indirectly scoped. Acceptable.

`src/fastapi/app/agent/orchestrator.py:439` — `f"georag:graph_entities:v1:{project_id}"` —
project-scoped key. Acceptable.

### SeaweedFS / object storage

Out of audit scope this round (need to read storage client code; deferred to
Module 9 chunk dedicated to bronze/silver bucket layout).

### Findings — A3

| ID | Severity | File:line | Finding |
|---|---|---|---|
| A3-01 | **HIGH** | `database/migrations/*` | RLS covers 2 of ≥ 11 workspace-scoped tables. Extend GUC-aware policies to: `evidence_items`, `answer_runs`, `answer_retrieval_items`, `answer_citation_items`, `answer_citation_spans`, `document_revisions`, `document_passages`, `message_feedback`, `drill_traces`. Also extend the `acquire_scoped` GUC to set `georag.workspace_id` alongside `georag.project_id` so workspace-level policies can fire. |
| A3-02 | MEDIUM | `src/fastapi/app/agent/tools.py:1303` | Neo4j Cypher uses `project_id` for tenant scoping. Document this as the official boundary or add a `workspace_id` property + filter to every node label that holds workspace-scoped data. |

---

## 6. A4 — JWT / Token Security

### Evidence

**Algorithm pinning** — `src/fastapi/app/services/auth.py:117`:
```python
algorithms=["HS256"],
```

Single algorithm in the allow-list. No `none` accepted. No RS/HS confusion
surface.

**Issuer / audience** — same call site, lines 118-119: `issuer="georag-laravel"`,
`audience="georag-fastapi"`. PyJWT enforces both. Good.

**Required claims** — `options={"require": ["exp", "iat", "sub"]}` (line 120). Good.

**TTL** — Laravel side, `app/Services/FastApiJwtMinter.php:31`: `TTL_SECONDS = 60`.
60-second window. Strong.

**Skew tolerance** — PyJWT default (no `leeway` set). Tight.

**Key rotation** — no rotation logic in either side. `FASTAPI_SERVICE_KEY` is
read once from env. `docs/RUNBOOK.md` (per CLAUDE.md note) is the documented
rotation source.

**Key strength** — `FastApiJwtMinter.php:38, 75`: enforces ≥ 32-byte key. Per RFC 7518.

**Token payload** — `FastApiJwtMinter.php` claims: `sub` (user_id), `project_id`,
`roles`, `iss`, `aud`, `iat`, `exp`. **No PII** (no email, no name).

### Findings — A4

| ID | Severity | File:line | Finding |
|---|---|---|---|
| A4-01 | LOW | `src/fastapi/app/services/auth.py:117-120` | No `leeway` parameter; PyJWT defaults to 0. If server clocks drift > 1 s the 60-s tokens may reject prematurely. Add `leeway=2` to absorb NTP jitter. |
| A4-02 | LOW | `app/Services/FastApiJwtMinter.php` (whole file) | No `kid` (key ID) in JWT header. Key rotation today requires a flag-day swap. Add `kid` + accept multiple keys on the FastAPI side for zero-downtime rotation (deferrable to Module 11). |

---

## 7. A5 — CSRF / CORS / Headers

### CORS — `config/cors.php`

- `paths`: `['api/*', 'sanctum/csrf-cookie']` (line 16). Tight.
- `allowed_origins`: env-driven, fallback list of localhost ports + `georag.local` (line 20-22).
- `supports_credentials: true` (line 32). Required for SPA cookie mode.
- `allowed_methods: ['*']`, `allowed_headers: ['*']` (lines 18, 26).

### CSRF

- `config/sanctum.php:80` registers `ValidateCsrfToken` middleware.
- Not separately verified that Inertia's `XSRF-TOKEN` round-trip works under Octane (Octane's worker model can leak the token across requests if the middleware isn't octane-aware — defer verification to Module 9 chunk 3).

### Security headers

Exhaustive search across `app/`, `bootstrap/`, `config/`, `resources/`:

```
$ grep -rn "X-Frame-Options\|Content-Security-Policy\|Strict-Transport-Security\|X-Content-Type-Options\|Referrer-Policy" app/ bootstrap/
(0 matches)

$ grep -rln "Strict-Transport-Security\|Content-Security-Policy" config/ resources/
(0 matches)

$ grep -c "addHeader\|->header(" app/Http/Middleware/HandleInertiaRequests.php
0
```

**No security headers are emitted on any response.** No HSTS. No CSP. No
X-Frame-Options. No X-Content-Type-Options. No Referrer-Policy. No
Permissions-Policy.

### Trusted proxies

```
$ ls app/Http/Middleware/TrustProxies.php
ls: cannot access 'app/Http/Middleware/TrustProxies.php': No such file or directory

$ grep -rn "TrustProxies\|TrustedProxy" app/ bootstrap/
(0 matches)
```

**No `TrustProxies` middleware is registered.** Behind a reverse proxy that
sets `X-Forwarded-For`, Laravel will see the proxy IP as the client, breaking
rate-limit keys (`auth-login` keys on email + IP) and access logs.

### Cookie flags

- `config/session.php:185` `http_only` → `true` (default).
- `config/session.php:202` `same_site` → `'lax'` (default).
- `config/session.php:172` `secure` → env, no PHP default. (See A1-03.)

### Findings — A5

| ID | Severity | File:line | Finding |
|---|---|---|---|
| A5-01 | **HIGH** | (no file — middleware absent) | No security-headers middleware. Add `SecurityHeadersMiddleware` emitting at minimum: HSTS (when `request->isSecure()`), `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: strict-origin-when-cross-origin`, and a CSP that covers Inertia + Vite + MapLibre + tile endpoints. |
| A5-02 | **HIGH** | `app/Http/Middleware/TrustProxies.php` (missing) | No `TrustProxies` middleware. Behind nginx/Traefik the `auth-login` rate limiter (which keys on email + IP, `routes/api.php:38-44`) will see only the proxy IP — collapsing the per-IP component to a single bucket. Create `App\Http\Middleware\TrustProxies` with `protected $proxies = '*';` (or the explicit proxy CIDR) and register it in `bootstrap/app.php`. |
| A5-03 | MEDIUM | `config/cors.php:18,26` | `allowed_methods: ['*']` and `allowed_headers: ['*']` are broader than necessary. Tighten to the actual surface: methods to `['GET','POST','PUT','PATCH','DELETE','OPTIONS']`, headers to the explicit list (`Authorization, Content-Type, X-XSRF-TOKEN, X-Inertia, X-Inertia-Version, X-Requested-With, X-Request-ID`). |

---

## 8. A6 — Sensitive Data Handling

### Evidence

**PII encryption at rest** — `app/Models/QueryAuditLog.php:51-52`:

```php
'query_text'    => 'encrypted',
'response_text' => 'encrypted',
```

`encrypted` cast is Laravel's `Crypt::` envelope. Reversible with `APP_KEY`.
Good for query content.

**User table** — `app/Models/User.php:101-108`:

```php
'email_verified_at' => 'datetime',
'password'          => 'hashed',
'is_admin'          => 'boolean',
```

`email` is **plaintext**. Acceptable per industry norm but worth noting if
clients require GDPR right-to-erasure / encryption-at-rest for PII.

**Audit log table** — exists (`database/migrations/2026_04_12_000000_create_query_audit_log_table.php`) and has `query_text_hash` (lookup-without-decrypt) added by `2026_04_16_140000_add_query_text_hash_to_query_audit_log.php`. Schema correct.

**Logging redaction** — FastAPI access log middleware (`src/fastapi/app/middleware.py:135-164`) logs `method, path, status, duration_ms, client` and skips query string explicitly ("no query string — may have PII", line 124). Authorization header is not logged. Good.

**Error responses in prod** — `config/app.php:42`: `'debug' => (bool) env('APP_DEBUG', false)`. Defaults to `false`. **However** the prior audit
flagged `docker-compose.yml:441,518,585` as `APP_DEBUG: ${APP_DEBUG:-true}` — the compose default is `true`. Any deploy that doesn't override this leaks stack traces. Carry forward.

### Findings — A6

| ID | Severity | File:line | Finding |
|---|---|---|---|
| A6-01 | MEDIUM | `app/Models/User.php:101-108` | `email` is plaintext. If GDPR-class deployments are in scope, add `'email' => 'encrypted'` cast + a `email_hash` shadow column for login lookups (parallel design to `query_text_hash`). |
| A6-02 | MEDIUM | `docker-compose.yml:441,518,585` | (Carried from `module-10-auth-bypass-sweep.md`.) `APP_DEBUG: ${APP_DEBUG:-true}` defaults true in compose. Flip the default to false; require explicit opt-in for dev. |

---

## 9. A7 — Multi-Tenancy Attack Surface

### 1. Workspace switching

`/api/v1/queries` POST → `QueryController::store` reads `$request->validated()['project_id']` (caller-supplied) and gates with `hasProjectAccess`. **Active workspace is derived per-request from the body.** No server-side "current workspace" session state. Good design — but pivots ALL of multi-tenancy onto `hasProjectAccess` (A1-01).

### 2. Default workspace UUID `a0000000-0000-0000-0000-000000000001`

Two FastAPI routers (`evidence.py:91`, `answer_runs.py:83`) silently fall back
to this UUID when `X-Workspace-Id` is absent. As noted in A2-04, Laravel
doesn't currently set this header → any future Laravel proxy that forwards
without it will resolve every request to default workspace.

`src/fastapi/app/agent/tools.py:1088` also hard-codes the default UUID as a
fallback for `workspace_id` lookup in `search_documents` — will silently
mis-attribute graph results to the default workspace if the SQL lookup fails.

### 3. IDOR: `GET /api/v1/projects/<other-tenant-uuid>`

**Confirmed exploitable** — `ProjectController::show` line 66-83 returns the
project record on `findOrFail($projectId)` with no membership check. See A2-01.

### 4. Soft deletes

`grep -n "softDeletes\|deleted_at" app/Models/Project.php app/Models/Collar.php app/Models/User.php` → 0 matches. None of the workspace-scoped models use soft deletes. Cross-tenant resurrection via stale `whereNull('deleted_at')` is not a concern.

### 5. Cache key collision

RAG cache keys (`orchestrator.py:2711`) include both `wid` and `pid` in the SHA-256 hash input. Cross-workspace identical queries do NOT collide. Good.

### Findings — A7

(All five scenarios are already represented in A1, A2, A3 findings — no new
unique findings here. Folded into the threat model in §11.)

---

## 10. A8 — Test Coverage for Security

```
$ grep -rln "assertStatus(401)\|assertStatus(403)\|assertForbidden" tests/ | wc -l
4
$ grep -rln "status_code == 40[13]\|HTTP_40[13]" src/fastapi/tests/ | wc -l
3
```

**Laravel** (4 files): `TileProxyTest.php`, `Api/V1/ExportControllerTest.php`,
`Api/V1/VendorProfileApiTest.php`, `Api/V1/ColumnMappingApiTest.php`.

**FastAPI** (3 files): `test_jwt_auth.py`, `test_multi_tenant_enforcement.py`,
`test_projects_router.py`.

**No IDOR tests** matching the pattern "user A authenticates → fetches user B's
project → expect 403" exist on Laravel side for `ProjectController` or
`CollarController`. The vacancy of these tests is itself a finding.

### Container hardening

```
$ grep -B1 "user: root" docker-compose.yml
TRANSFORMERS_CACHE: /tmp/hf_cache
user: root
--
command: dagster-daemon run
user: root
--
command: dagster-webserver --host 0.0.0.0 --port 3001
user: root
```

Three services (`embeddings_worker` or similar with `TRANSFORMERS_CACHE`,
`dagster-daemon`, `dagster-webserver`) run as `root`. Other services
(presumably) inherit image defaults — needs a sweep to confirm.

### Findings — A8

| ID | Severity | File:line | Finding |
|---|---|---|---|
| A8-01 | **HIGH** | `tests/Feature/Api/V1/` (missing) | No IDOR test on `ProjectController` or `CollarController`. Add: user A authenticates, requests user B's project_id, asserts 403. Until A2-01 / A2-02 are fixed, the test will fail; that's the point. |
| A8-02 | MEDIUM | `docker-compose.yml:790,1362,1437` | Three Dagster + embeddings services run as `user: root`. Move to a non-root UID (matching the bronze/silver bucket ownership) or document why root is required. |

---

## 11. Consolidated Critical + High Findings

| ID | Severity | One-liner | Fix-where |
|---|---|---|---|
| A1-01 | **CRITICAL** | `User::hasProjectAccess` fails OPEN when pivot is missing | `app/Models/User.php:52-58` |
| A2-01 | **CRITICAL** | `ProjectController::show/update/destroy` IDOR | `app/Http/Controllers/Api/V1/ProjectController.php:66-122` |
| A2-02 | **CRITICAL** | `CollarController` (all methods) IDOR | `app/Http/Controllers/Api/V1/CollarController.php:22-145` |
| A1-02 | HIGH | FastAPI `extract_user_context` returns empty UserContext on missing Authorization | `src/fastapi/app/services/auth.py:95-100` |
| A2-03 | HIGH | `MULTI_TENANT_ENFORCEMENT_ENABLED` default = `False` | `src/fastapi/app/config.py:354` |
| A2-04 | HIGH | Evidence + answer_runs default to default-workspace UUID when header missing | `evidence.py:91`, `answer_runs.py:83` |
| A3-01 | HIGH | RLS coverage = 2 of ≥ 11 workspace-scoped tables | `database/migrations/*` |
| A5-01 | HIGH | No security headers emitted on any response | (middleware absent) |
| A5-02 | HIGH | No `TrustProxies` middleware | `app/Http/Middleware/TrustProxies.php` (missing) |
| A8-01 | HIGH | No IDOR tests | `tests/Feature/Api/V1/` |

---

## 12. Findings Count by Severity

| Severity | Count |
|---|---|
| **CRITICAL** | 3 |
| **HIGH** | 7 |
| **MEDIUM** | 8 |
| **LOW** | 4 |
| **TOTAL** | **22** |

---

## 13. Module 9 Chunk Plan

Dependency-ordered. Each chunk is justified by a finding above. No padding.

**Chunk 9.1 — Laravel IDOR remediation** (closes A2-01, A2-02, partial A8-01)
- Add `hasProjectAccess` gate to `ProjectController::show/update/destroy`.
- Add `hasProjectAccess` gate to all `CollarController` methods.
- Add 4 PHPUnit IDOR tests (one per affected controller method): user A → user B's UUID → 403.
- Acceptance: tests fail before, pass after.

**Chunk 9.2 — Harden `User::hasProjectAccess`** (closes A1-01)
- Convert fail-open to fail-closed. Boot-time health check that asserts the `project_user` table exists; abort startup with a loud error otherwise.
- Add a unit test that simulates the QueryException and asserts `false` is returned.
- Acceptance: removing the migration → app refuses to boot.

**Chunk 9.3 — RLS coverage extension** (closes A3-01, hardens AUTH-04 envelope)
- Extend GUC-aware policies to all 9 remaining workspace-scoped silver tables.
- Add `georag.workspace_id` GUC alongside `georag.project_id` in `AgentDeps.acquire_scoped`.
- Migrate every `WHERE workspace_id = ?` in tools to rely on RLS as a backstop (defense in depth — keep the WHERE clause).
- pgTAP tests: assert each table rejects cross-tenant SELECT under a wrong-GUC session.
- Acceptance: 9 new RLS-enforcement tests green.

**Chunk 9.4 — FastAPI auth tightening** (closes A1-02, A2-03, A2-04)
- Flip `MULTI_TENANT_ENFORCEMENT_ENABLED` default to `True`. Add `SINGLE_TENANT_MODE` opt-out.
- Tighten `extract_user_context`: missing Authorization on `/internal/*` and `/v1/*` paths → 401 (kept lenient on `/health`, `/ready`, `/metrics` only).
- Replace `_resolve_workspace_id` default-fallback with: derive workspace from JWT `project_id` via DB lookup, no fallback.
- Acceptance: FastAPI integration tests fail without JWT; pass with JWT; cross-project request returns 403.

**Chunk 9.5 — Security headers + TrustProxies** (closes A5-01, A5-02)
- New `App\Http\Middleware\SecurityHeadersMiddleware` (CSP, HSTS, XFO, XCTO, Referrer-Policy, Permissions-Policy).
- New `App\Http\Middleware\TrustProxies` registered in `bootstrap/app.php`.
- CSP that covers Inertia/Vite/MapLibre/tiles/SSE.
- Acceptance: response headers visible in CI snapshot test.

**Chunk 9.6 — Cookie + CORS hardening** (closes A1-03, A5-03)
- `config/session.php`: default `secure` to `true` outside local env.
- `config/cors.php`: replace `allowed_methods: ['*']` and `allowed_headers: ['*']` with explicit allowlists.
- Acceptance: request with disallowed method returns 405 / disallowed header rejected by CORS.

**Chunk 9.7 — Container hardening** (closes A8-02, carries A6-02)
- Move three `user: root` services to non-root UIDs.
- Flip `APP_DEBUG` compose default from `true` to `false`.
- Acceptance: `docker compose ps` shows no root processes; stack traces no longer surface in error responses.

**Chunk 9.8 — JWT polish + audit trail** (closes A4-01, A4-02 when prioritized)
- Add `leeway=2` to PyJWT decode.
- Add `kid` header for rotation (deferrable to Module 11 if Module 9 pressure is real).
- Audit log: ensure every 403 from chunks 9.1, 9.4 is logged with `actor_user_id, target_workspace_id, target_resource, reason`. Wire into existing Pulse / Loki pipeline.
- Acceptance: 403 events appear in audit log table.

---

## 14. Carry Forward from `ops/backlog/module-10-auth-bypass-sweep.md`

The 2026-04-19 sweep had 1 HIGH (RESOLVED), 2 MEDIUM (open), 1 LOW (open). The
two open MEDIUM items belong to Module 9:

1. **`.env.example:34` `APP_DEBUG=true`** — flag for deployment runbook. Fold into Chunk 9.7.
2. **`docker-compose.yml:441,518,585` `APP_DEBUG: ${APP_DEBUG:-true}`** — flip default. Fold into Chunk 9.7.
3. (LOW) **`.env.example:100` Redis no-auth comment** — leave as-is for dev profile; Module 9 doesn't need to touch it. Carry to Module 10 doc sweep.

The "AUTH-04 RLS GUC plumbing pending" finding in any informal channel must be
explicitly retracted — see §2 above.

---

## 15. Threat Model — Top 5 Attack Scenarios

### T1 — Cross-tenant project read via direct UUID guess (CRITICAL, exploitable today)

Attacker: any authenticated user of any workspace.
Path: `GET /api/v1/projects/{victim-project-uuid}` →
`routes/api.php:64` → `ProjectController::show`
(`app/Http/Controllers/Api/V1/ProjectController.php:66`) →
`Project::findOrFail($projectId)` (line 70) → returns `ProjectResource`.
**No `hasProjectAccess` check.** Closed by Chunk 9.1.

### T2 — Cross-tenant collar enumeration (CRITICAL, exploitable today)

Path: `GET /api/v1/projects/{victim-project-uuid}/collars` →
`routes/api.php:67` → `CollarController::index`
(`app/Http/Controllers/Api/V1/CollarController.php:22`) →
`Project::findOrFail($projectId)` (line 26) → returns paginated collars
matching `project_id` (line 30). **No membership gate.**

Even if T1 were fixed, T2 is independently exploitable. Closed by Chunk 9.1.

### T3 — Migration-state-dependent RBAC bypass (CRITICAL, env-dependent)

Path: any environment where `2026_04_11_000000_create_project_user_table.php`
hasn't run (fresh container, restored backup, first boot before
`migrate`) → any call into `User::hasProjectAccess` → catches `QueryException`
with SQLSTATE `42P01` → logs warning → returns `true`
(`app/Models/User.php:57`) → every gate downstream (UploadController:65,
QueryController:50, ExportController:198, TileProxyController:527) admits
the request.

The fail-open warning log is the only sign — easy to miss. Closed by Chunk 9.2.

### T4 — Default-workspace silent collapse (HIGH, dormant today, latent)

Path: any future Laravel proxy or external client that calls
`/v1/evidence/{id}` or `/v1/answer_runs/{id}/...` with `X-Service-Key` but
without `X-Workspace-Id` →
`evidence.py:_resolve_workspace_id` line 91 / `answer_runs.py:_resolve_workspace_id`
line 83 → returns `UUID("a0000000-0000-0000-0000-000000000001")` →
all access checks resolve against the default workspace.

If the default workspace contains a user's data (single-tenant deploy), every
attacker request returns that data. Closed by Chunk 9.4.

### T5 — Reverse-proxy IP spoofing → rate-limit defeat (HIGH, latent)

Path: deployment behind nginx/Traefik that sets `X-Forwarded-For` →
Laravel does not have `TrustProxies` registered → `request->ip()` returns
the proxy's IP (single value) → `auth-login` rate limiter (which keys on
email + IP, `routes/api.php:38-44`) collapses the IP component → all
attackers share the single IP bucket → effective rate limit is the
shared 5/min, not 5/min/attacker.

Combined with absent CAPTCHA, this enables credential-stuffing at scale.
Closed by Chunk 9.5.

---

## 16. Items I Could Not Verify (Follow-Up Required)

1. **`ExportController::download` ownership check** — A2-05. Need to read the
   `download` method body to confirm whether it re-checks the export's
   `project_id` against the caller. Quick to verify; deferred from this audit
   to keep scope on cross-cutting evidence rather than per-method drilldowns.
2. **Inertia + Octane CSRF interaction** — A5 mentions but did not test. Octane
   workers can leak request-scoped state if middleware isn't octane-aware.
3. **SeaweedFS bucket layout** — A3 deferred. Need to read the storage client
   to confirm bronze/silver bucket names and whether keys are workspace-prefixed.
4. **`ColumnMapping` / `VendorProfile` global vs workspace-scoped** — A2-06.
   The route comment says "global" but the data model wasn't audited.
5. **Reverb / Horizon channels** — broadcasting auth (`routes/channels.php`)
   was not opened this round. Confirm `Broadcast::channel('queries.{queryId}',
   ...)` validates ownership.
6. **All non-Dagster docker-compose services' user directives** — only the
   three `user: root` overrides were inspected. Confirm other services don't
   inherit `root` from their base images.

These are scoped for the test-engineer + senior-reviewer follow-up at the
Module 9 milestone gate.

---

*End of Phase A audit. Read-only. No code or config changed.*
