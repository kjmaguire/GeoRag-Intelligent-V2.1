# GeoRAG Operations Runbook

Operator-facing runbook for security-sensitive and data-affecting procedures.
Referenced from `CLAUDE.md` so agents know where to send operators for
one-shot tasks that don't belong in code.

Sections below are stable URL fragments — link to them from PR descriptions,
incident post-mortems, and the internal wiki.

---

## A4 PII at rest — how to read `query_audit_log` safely

**Context.** `query_audit_log.query_text` and `.response_text` are
transparently encrypted via Laravel's `encrypted` cast (A4). In-process
reads through the `QueryAuditLog` Eloquent model return plaintext; raw
DB reads return Laravel's base64-wrapped `{"iv":...,"value":...,"mac":...}`
ciphertext envelope. Getting this wrong in new code silently either leaks
ciphertext to a consumer that expects plaintext, or writes plaintext to a
column that's supposed to be encrypted.

### Do this

```php
// Eloquent — plaintext round-trip, cast handles everything.
$row = \App\Models\QueryAuditLog::find($auditId);
echo $row->query_text;          // plaintext
$row->query_text = 'new value'; // mutator encrypts + refreshes query_text_hash
$row->save();
```

### If you must bypass Eloquent

Use `DB::table()` only when the aggregation genuinely can't go through the
model (for example, `GROUP BY query_text_hash` in the analytics endpoint).
Decrypt with `Crypt::decryptString()` **not** `Crypt::decrypt()`:

```php
use Illuminate\Support\Facades\Crypt;
use Illuminate\Support\Facades\DB;

$raw = DB::table('query_audit_log')->where('audit_id', $id)->value('query_text');
$plain = $raw === null ? null : Crypt::decryptString($raw);
```

**Why `decryptString` and not `decrypt`:** `Crypt::decrypt()` calls
`unserialize()` on the payload. Our model's mutator uses
`Crypt::encryptString()` which does **not** serialize, so a `decrypt()`
call on our ciphertext unserializes a non-serialized string and returns
garbage. Matching write/read primitives is a hard requirement.

### Writing encrypted values from raw queries

Avoid it — write through the model so the mutator also refreshes
`query_text_hash`. If you can't (large bulk migration), replicate the
mutator inline:

```php
use Illuminate\Support\Facades\Crypt;
use App\Models\QueryAuditLog;

DB::table('query_audit_log')->where('audit_id', $id)->update([
    'query_text'      => Crypt::encryptString($plaintext),
    'query_text_hash' => QueryAuditLog::hashQueryText($plaintext),
]);
```

### Never do this

- `DB::table('query_audit_log')->value('query_text')` without decrypting.
- `DB::table()->update(['query_text' => $plaintext])` — writes plaintext to
  an encrypted column, silently breaking A4 PII-at-rest.
- `Crypt::decrypt()` on our ciphertext — see above.

---

## query_text_hash cross-install caveat

`query_text_hash` is a deterministic HMAC-SHA256 of the normalised
(lower-cased, trimmed) query text, salted with `APP_KEY`. Analytics uses
it to group semantically-equal queries for `top_queries` aggregation
without decrypting the whole 30-day window.

**Consequence today:** hashes are **not comparable across installs**. A
query hashed on staging and a query hashed on prod produce different
values even for identical input because `APP_KEY` differs. Nothing in the
current single-tenant deployment depends on cross-install comparability,
so no action needed.

**Consequence if we ever consolidate tenants into one DB:** an observer
with DB read access could determine whether a given plaintext query
exists in the audit log by computing its HMAC and looking up the hash.
With a **shared** `APP_KEY` across tenants, they could also test whether
tenant A and tenant B ran the same query — a small side-channel but one
that would violate tenant isolation.

**If/when you go multi-tenant-in-one-DB:**

1. Add a `tenant_id` column to `query_audit_log` (in A4 it doesn't exist
   because every install is its own tenant).
2. Change `QueryAuditLog::hashQueryText()` to HMAC with a per-tenant
   salt (e.g., `hash_hmac('sha256', $normalized, $tenant_secret)`).
3. Backfill via a tenant-aware version of `audit:encrypt-pii`.

Don't change this pre-emptively — it's only a problem when tenant
isolation is required on a shared DB.

---

## APP_KEY rotation checklist

`APP_KEY` is the HMAC key for the `encrypted` cast AND for
`query_text_hash`. Rotating it without a data-migration step will:

1. Make every previously-encrypted `query_text` / `response_text` row
   unreadable (decryption fails with `Illuminate\Contracts\Encryption\DecryptException`).
2. Change every `query_text_hash` value for the same input — breaking
   analytics' `top_queries` aggregation until a full re-hash runs.

### When you need to rotate

- Key disclosure (leaked commit, compromised backup, departing operator).
- Policy-driven rotation (compliance requirement, e.g. annual).
- Migrating to a new deployment environment.

### Rotation procedure (preferred: one-shot)

```bash
php artisan audit:rotate-key --dump-dir=/secure
```

That's it. The orchestrator handles all seven steps: maintenance mode →
dump with old key → generate new key → rebind the in-process encrypter →
restore with new key (integrity-checked) → shred the plaintext dump →
lift maintenance. Add `--force` to skip the "Proceed?" confirmation,
`--keep-dump` to preserve the plaintext (shred it yourself; don't forget),
`--no-maintenance` if you're running against a non-HTTP environment.

On any failure the command preserves the dump and reports the recovery
path. NEVER shreds the dump on failure — it's the only recovery asset.

### Rotation procedure (manual, for partial recovery)

If the orchestrator fails mid-run you run the same four commands by hand:

```bash
# 1. Maintenance mode — pauses writes.
php artisan down

# 2. With the OLD APP_KEY still active, dump plaintext audit rows to a
#    JSONL file on a secured path. The dump carries an integrity trailer
#    (SHA-256 of concatenated audit_ids + row count) that the restore
#    step verifies.
php artisan audit:dump-pii --output /secure/audit-pii.jsonl
# (Put /secure on a KMS-encrypted volume or in-memory tmpfs. The command
#  refuses to write to a world-writable dir without the sticky bit, warns
#  on world-readable dirs, and chmods the output to 0600.)

# 3. Generate + install the new APP_KEY.
php artisan key:generate --force

# 4. Re-encrypt under the new APP_KEY from the JSONL dump. The restore
#    refuses when the trailer's row count or ids_sha256 doesn't match
#    what it actually read — catches truncation + tampering.
php artisan audit:restore-pii --input /secure/audit-pii.jsonl

# 5. Shred the plaintext dump immediately.
shred -u /secure/audit-pii.jsonl

# 6. Lift maintenance mode.
php artisan up
```

Both commands stream in chunks, so even a million-row audit log runs in
flat memory. Use `--dry-run` on either to preview without touching data.

If the rotation happens without steps 2 and 4, you have two recovery options:

- **Restore old `APP_KEY` from backup.** Every encrypted column becomes
  readable again. This is almost always the right answer.
- **Accept the loss.** For audit data you're happy to forget (dev-only
  install, retention policy expired), `TRUNCATE query_audit_log` and
  move on.

### Prevention

- Store `APP_KEY` alongside the DB backup, always encrypted with a
  separate KMS. Losing the DB without the key is the same as deleting the
  data.
- Never rotate `APP_KEY` out-of-band (e.g., via `php artisan key:generate`
  on a whim). Always run the rotation procedure above.
- Add `APP_KEY` to whatever secret-rotation calendar your org uses so
  it's a scheduled operation, not a reactive one.

### Never rotate via

- Committing a new `APP_KEY` to `.env.example`. Production pulls from
  `.env`, but any operator running `cp .env.example .env` on a stale
  install will replace their real key. `.env.example` uses placeholders.
- `docker compose down && docker compose up` without preserving the
  volume that holds `.env` and the Postgres data directory.

---

## FASTAPI_SERVICE_KEY rotation (R13 follow-up)

Shared secret between Laravel and FastAPI. Signs JWTs for per-request
identity propagation (B7) AND gates the legacy `X-Service-Key` path
during graceful rollout.

### Minimum length

**≥ 32 bytes.** Both sides enforce this at startup:

- FastAPI: `Settings.FASTAPI_SERVICE_KEY` Pydantic validator (see
  `src/fastapi/app/config.py`).
- Laravel: `FastApiJwtMinter::mint()` throws on a short secret.

### Generate

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
# yields 64 URL-safe characters ≈ 48 bytes of entropy.
```

### Rotate

1. Update `.env` (the variable feeds both services via docker-compose env
   interpolation).
2. **Recreate the containers that read it** — `docker restart` keeps the
   stale env; only `docker compose up -d --force-recreate` or an explicit
   `docker compose up -d --no-deps fastapi laravel-octane laravel-horizon`
   re-injects the new value.
3. Reload Horizon so in-flight jobs don't run with a JWT minted under the
   old secret: `docker exec georag-laravel-horizon php artisan horizon:terminate`.

No data-migration step — JWTs are short-TTL (60 s) so any in-flight
tokens simply expire.

---

## Golden-set flywheel (weekly)

The RAG quality-improvement flywheel: every week, surface the top-N
queries the pipeline struggled with, promote the real ones into the
test suite, fix until green, repeat.

```bash
# Default: 7-day lookback, top 20 candidates, min 2 occurrences, max 0.5 confidence.
docker exec georag-laravel-octane php artisan audit:golden-set-report \
    --output /app/storage/reports/golden-$(date +%Y%m%d).md

# Wider lookback for a quarterly review:
docker exec georag-laravel-octane php artisan audit:golden-set-report \
    --since=90d --limit=50 --min-count=3 \
    --output /app/storage/reports/golden-quarterly-$(date +%Y%m%d).md
```

Scoring: `count × (1 − avg_confidence)`. A query that fails 10 times at
confidence 0.2 outweighs one that succeeds 10 times at 0.9.

Each candidate carries a suggested action:
- **< 0.2 avg + ≥ 5 occurrences** → promote to `test_hallucination_failures.py`
- **< 0.4 avg** → promote to `test_golden_queries.py`
- **≥ 5 occurrences, higher avg** → investigate the confidence scorer
- Otherwise → check `classifier_escalation_signal` logs for that hash

### Schedule it

Add to the ops cron, Dagster schedule, or GitHub Actions workflow:

```cron
# Weekly Monday 06:00 UTC — golden-set review.
0 6 * * MON  docker exec georag-laravel-octane php artisan audit:golden-set-report \
             --output /gold/golden-set-$(date +\%Y\%m\%d).md
```

---

## Query escalation tiers + signal-harvesting dashboard

GeoRAG answers queries through three tiers that escalate only when the
earlier tier returns empty. Every tier emits a Prometheus metric so the
`GeoRAG — Signal Harvesting` Grafana dashboard can tell you when the
next tier needs to be enabled or built.

### The three tiers

| Tier | Signal to fire | Cost | Flag |
|---|---|---|---|
| **1. Deterministic keyword dispatch** | All queries start here | base latency | always on |
| **2. Bounded rephrasing retry** | Tier-1 `classifier_fallback + all_tools_empty` | +1 LLM round-trip (Haiku), +≤2 retrieval passes | `AGENTIC_ESCALATION_ENABLED=true` (default) |
| **3. Full Pydantic AI agent** | Tier-2 rephrasing also empty | +1 agent run w/ up to `AGENTIC_MAX_TOOL_CALLS` tool calls | `AGENTIC_FULL_ESCALATION_ENABLED=false` (default off) |

### When to enable tier 3

Watch the **Escalation success rate (1h)** panel on the signal-harvesting
dashboard:

- **>0.5** — tier-2 rephrasing is earning its keep. Leave tier 3 off.
- **0.2–0.5** — tier-2 is marginal. Turn tier 3 on in one deploy and
  compare success rates.
- **<0.2** — tier 3 is the right investment. Set
  `AGENTIC_FULL_ESCALATION_ENABLED=true` in `.env` and recreate the
  FastAPI container.

### Dashboard at a glance

Grafana URL (dev): `http://localhost:3000/d/georag-signals`
(requires `--profile dev-monitor` on `docker compose up`).

| Panel | What it tells you |
|---|---|
| Escalation rate (30m) | Fraction of queries hitting tier-2. Green = healthy. Yellow/red = classifier widening. |
| Escalation success rate (1h) | Gates the tier-3 decision above. |
| Avg rephrasings per escalation | If <1.5, the rephrase prompt is underperforming. |
| Cache hit rate over time (per backend) | Anthropic ephemeral + vLLM prefix cache both report here. |
| Routing decisions per second by tier | Healthy: mostly FAST/STANDARD. DEEP dominance = classifier over-escalating. |
| Failovers per second | >0.01 rps sustained = Anthropic capacity/rate-limit pressure. |
| Chunks returned per query | Mode 0 = starved retrieval; mode ~5 = full context. |
| Query duration p50/p95 | p95 spikes = retries or escalation. |

### Operator toggle to turn tier 3 on

```bash
# 1. Update .env
echo 'AGENTIC_FULL_ESCALATION_ENABLED=true' >> .env

# 2. Recreate fastapi so env is re-read
docker compose up -d --no-deps --force-recreate fastapi

# 3. Watch the dashboard for 30 minutes. Key:
#    - escalation success rate should jump (from tier-2 alone)
#    - avg duration p95 may rise 3-10s on escalated queries
#    - if p95 on the general case rises, rollback
```

### Rollback

```bash
# Flip the flag off; recreate.
sed -i 's/^AGENTIC_FULL_ESCALATION_ENABLED=.*/AGENTIC_FULL_ESCALATION_ENABLED=false/' .env
docker compose up -d --no-deps --force-recreate fastapi
```

No data migration needed — the flag controls a runtime escalation branch;
nothing is persisted.

---

## vLLM prefix caching (production OpenAI-compatible path)

The FastAPI orchestrator structures OpenAI-compatible calls as two
messages — a stable `system` (prompt variant + per-project preamble) and
a per-turn `user` (CONTEXT + question). That's the structural prerequisite
for vLLM's automatic prefix cache to reuse KV across requests.

**The engine flag must also be set at vLLM startup:**

```bash
# docker/compose.vllm.yml (overlay) or equivalent
services:
  vllm:
    command:
      - --model=Qwen/Qwen3-14B-AWQ
      - --quantization=awq_marlin
      - --enable-prefix-caching         # ← required for the cache to exist
      - --max-num-batched-tokens=4096
      - ...
```

Without `--enable-prefix-caching`, the prefix is still stable but vLLM
doesn't cache it — you lose the latency/cost win but the orchestrator
code remains correct.

**How to verify the cache is hitting:** the orchestrator logs
`cached_tokens` and `cache_hit_rate` per call when the backend reports
them. Expect ~0.0 on the first query in a session (cold cache) and
~0.85–0.95 thereafter within the same project.

---

## Test environment gotchas

- **SQLite is the default, and `phpunit.xml` forces it.** `phpunit.xml`
  declares `<env name="DB_CONNECTION" value="sqlite" force="true"/>`
  (same for `DB_DATABASE=:memory:`). Because `force="true"` overrides the
  container's shell env, `docker exec georag-laravel-octane php artisan
  test` always runs under SQLite — passing `-e DB_CONNECTION=pgsql` does
  nothing. `tests/TestCase.php` installs a sqlite compatibility hook that
  rewrites PG-specific DDL (TIMESTAMPTZ, JSONB, TEXT[], CREATE
  MATERIALIZED VIEW, etc.) into noops or sqlite-friendly equivalents so
  migrations can still run.
- **To run tests that need real Postgres, use `phpunit.pgsql.xml`.**
  Feature tests that inspect PostGIS types, `information_schema`,
  `pg_constraint`, MVT views, or use PostGIS functions (ST_X,
  ST_Transform, etc.) call `$this->skipIfSqlite()` in `setUp()` and
  self-skip under the default config. Run them against the dedicated
  `georag_test` database with:

      docker exec georag-laravel-octane php artisan test -c phpunit.pgsql.xml

  `phpunit.pgsql.xml` connects directly to `postgresql:5432` (bypassing
  PgBouncer, which is pinned to the `georag` DB and uses transaction
  pooling incompatible with `migrate:fresh`), forces `DB_DATABASE=georag_test`,
  and expands `DB_SEARCH_PATH` to all application schemas so
  `RefreshDatabase::migrate:fresh` can correctly wipe silver/bronze/gold/
  index/public_geoscience between suite runs.

  The testsuites in the config are ordered so `RefreshDatabase` suites
  run FIRST — that way their first test's `migrate:fresh` populates the
  DB before the read-only schema/catalog tests try to inspect it.
- **Provisioning `georag_test` on a fresh PG volume.** The
  `docker/postgresql/init/init-test-db.sh` entrypoint script creates
  the DB and installs PostGIS + the medallion schemas automatically on
  first container boot. If your PG volume already exists (init scripts
  don't re-run against an existing volume), apply the script's SQL
  manually — the file header has the exact recipe. `RefreshDatabase`'s
  `migrate:fresh` handles the migrations; you don't need to run
  `artisan migrate` against `georag_test` yourself.
- **Adding a new `@dataProvider` test?** PHPUnit 12 dropped annotation
  metadata — use the `#[DataProvider('providerMethod')]` attribute from
  `PHPUnit\Framework\Attributes\DataProvider` instead. Annotation-based
  providers silently collapse to a single test call with zero arguments
  (it looks like "1 skipped" under SQLite's setUp-skip, which is how
  `BedrockGeologyMigrationTest` hid a broken provider for a while).
- **Analytics endpoint needs PostGIS.** `ProjectAnalyticsController`
  touches the `geom` column which sqlite doesn't understand. Tests that
  hit this endpoint should follow the Collar test pattern:
  `RefreshDatabase` + `$this->skipIfSqlite()` + listed in
  `phpunit.pgsql.xml`'s `Postgres (RefreshDatabase)` testsuite.
- **Broadcasting auth is `BROADCAST_CONNECTION=null` in tests.** The null
  driver's `/broadcasting/auth` endpoint always returns 200 regardless of
  the channel callback's return value. Tests that validate channel
  authorization must invoke the `channels.php` callback directly (see
  `QueryChannelAuthorizationTest` for the pattern).

---

## JWT claims policy — Laravel → FastAPI internal auth

**Context.** Internal traffic from Laravel to FastAPI carries TWO auth
credentials per request (B7 graceful rollout):

1. `X-Service-Key: <FASTAPI_SERVICE_KEY>` — symmetric shared secret;
   constant-time-compared in `app/services/auth.py:verify_service_key`.
2. `Authorization: Bearer <JWT>` — short-TTL HS256 JWT minted by
   `app/Services/FastApiJwtMinter.php` and decoded in
   `app/services/auth.py:extract_user_context`.

The two are belt-and-braces: the X-Service-Key proves the request came
from a trusted Laravel deploy; the JWT proves *which user* on that deploy
authorised this specific call.

### Required JWT claims

Mint every internal JWT with these claims — `extract_user_context`
rejects 401 on any missing required field:

| Claim         | Type   | Source                                              |
|---------------|--------|-----------------------------------------------------|
| `iss`         | string | Always `"georag-laravel"`                           |
| `aud`         | string | Always `"georag-fastapi"`                           |
| `sub`         | string | Authenticated `users.id` (UUID)                     |
| `project_id`  | string | The project UUID the user is currently scoped to    |
| `roles`       | array  | User's role strings — `["member"]` / `["admin"]`    |
| `iat`         | int    | Token issue epoch                                   |
| `exp`         | int    | `iat + 60` — 60-second TTL                          |

**Why 60 seconds.** The JWT is minted just-in-time before each FastAPI
call. Long TTLs widen the replay window; 60 s is enough to cover the
slowest synchronous request path and short enough that a leaked token
expires before it's useful.

### Algorithm + signing key

- HS256 only. The asymmetric algorithms (RS256 etc.) require a separate
  keypair-management workflow we don't run today.
- Signing key = `FASTAPI_SERVICE_KEY` (shared with the X-Service-Key
  header). One env var, two purposes — see "Key separation note" below
  for why this is acceptable.
- Minimum key length: 32 bytes (RFC 7518 §3.2 for SHA-256). FastAPI
  fails startup with `_SERVICE_KEY_MIN_BYTES = 32` enforcement
  (R13 — `app/config.py`).

### Multi-tenant enforcement gate

`MULTI_TENANT_ENFORCEMENT_ENABLED` controls whether
`POST /internal/queries` rejects `project_id` mismatch (JWT claim vs
request body) with HTTP 403, or just logs a warning and proceeds. Default
is False — read `src/fastapi/SECURITY.md` BEFORE flipping it on; any
Laravel deploy still using X-Service-Key alone (no JWT) will start
hitting 403s immediately.

The matching FastAPI-side defence-in-depth is the GUC-aware RLS policy
on `silver.collars` and `silver.samples` — when the multi-tenant flag is
on, tools migrate to `AgentDeps.acquire_scoped()` which sets
`SET LOCAL georag.project_id = '<uuid>'` per transaction so even a
WHERE-clause-forgetting tool query gets RLS-filtered. Migration
`2026_04_17_120200_replace_toothless_rls_with_guc_aware_policies.php`
installs the policy (admits all rows when GUC unset — backwards
compatible).

---

## Key separation note — `FASTAPI_SERVICE_KEY` does double duty

**Context.** A single env var, `FASTAPI_SERVICE_KEY`, is currently used
for:

1. The `X-Service-Key` shared secret (Laravel ↔ FastAPI HMAC).
2. The HS256 signing key for JWTs minted by `FastApiJwtMinter.php`.
3. The HMAC key for `app.agent.log_safe.query_hash` (P0 #3 query
   plaintext scrub — keyed so a Loki-with-read-access insider can't
   brute-force the plaintext from a known-query dictionary).

This is **deliberately** a single key for now, not three rotated
independently, for two reasons:

- **Single-tenant deployment posture.** All three uses cross the same
  Laravel ↔ FastAPI trust boundary. An attacker who compromises the key
  can already issue valid `X-Service-Key` requests; their ability to
  forge JWTs or correlate query hashes adds no new privilege.
- **Operational simplicity.** Rotating one secret in `.env` and
  restarting both services is a 30-second runbook step; rotating three
  independent secrets requires choreography to avoid a window where
  Laravel's signing key disagrees with FastAPI's verifying key.

### When to split the keys

Split into three independent env vars when:

1. You go multi-customer (separate `FASTAPI_SERVICE_KEY` per tenant
   isn't enough — the JWT signing key and the audit-log HMAC key need
   different rotation cadences once one customer compromises theirs).
2. You add an external system that needs to verify the audit-log HMAC
   without holding the FastAPI service key.
3. Compliance audit (SOC 2 type II, ISO 27001) explicitly flags
   key-reuse-across-purposes as a finding.

### Rotation procedure (single-key, current posture)

Same as the existing "Secret rotation" section earlier in this file.
The post-rotation verification step should additionally include:

```bash
# Confirm a fresh JWT minted by Laravel decodes cleanly on FastAPI
docker compose exec laravel-octane php artisan tinker --execute "
  echo App\\Services\\FastApiJwtMinter::mint('user-id-here', 'project-uuid-here', ['member']);
"
# Take the printed token, hit FastAPI:
curl -i http://localhost:8888/internal/queries \
  -H "X-Service-Key: $NEW_KEY" \
  -H "Authorization: Bearer $TOKEN_FROM_TINKER" \
  -H "Content-Type: application/json" \
  -d '{"project_id":"<same uuid>","query":"smoke test"}'
# Expect 200 SSE stream. 401 means JWT verification failed → key drift.
```

---

## Observability rollout — Logfire / OTel toggles

**Context.** P1 #9 added gated Logfire instrumentation on the Pydantic AI
agent. Default is OFF — no surprise outbound traffic. There are three
exporters; pick the one that matches your trace-aggregation stack.

### Decision tree

```
                ┌── LOGFIRE_TOKEN set?
       Yes ─────│
                └── Spans go to logfire.pydantic.dev (hosted backend).
                    Requires outbound HTTPS to *.pydantic.dev.

                ┌── LOGFIRE_OTEL_ENDPOINT set (e.g. http://tempo:4317)?
       Yes ─────│
                └── Spans go to your local OTLP collector (Tempo, Jaeger,
                    Honeycomb, Grafana Cloud). Container must reach it.

       Neither? Logfire runs in LOCAL-ONLY mode (send_to_logfire=False).
                Spans created in-process; useful for `logfire.span(...)`
                debugging without shipping data anywhere.
```

### Enable for one-off triage

```bash
# In .env:
LOGFIRE_ENABLED=true
LOGFIRE_TOKEN=pylf_xxx       # or LOGFIRE_OTEL_ENDPOINT=...
LOGFIRE_SERVICE_NAME=georag-fastapi
LOGFIRE_ENVIRONMENT=prod     # or dev / staging

# Restart fastapi only — Logfire init runs in lifespan section 0.
docker compose restart fastapi
docker compose logs -f fastapi | grep -i logfire
# Expect:  "Logfire configured (hosted backend, service=...)"
```

### What you get

Every `agent.run()` produces a span tree showing:
- System prompt + classifier output
- Each tool call (parameters, return, latency)
- Retries (with `audit_label=retry`)
- LLM call (input/output tokens, cache hit/miss)
- Final assembled GeoRAGResponse

Combined with the per-tool latency metrics (P1 #16 —
`georag_tool_duration_seconds`) this gives end-to-end attribution
without grep-spelunking through Loki.

### Disable

```bash
LOGFIRE_ENABLED=false
docker compose restart fastapi
```

Init failures never block startup — they're `logger.exception()`-logged
and the app continues.

---

## Cypher allowlist update procedure

**Context.** `traverse_knowledge_graph` and `query_graph_by_label`
interpolate the LLM-supplied `relationship_type` and `label` parameters
directly into Cypher (Neo4j's API doesn't support parameterising those).
P2 #28 installed two allowlists in
`src/fastapi/app/agent/tools.py`:

- `_ALLOWED_GRAPH_LABELS` — node labels the LLM may filter on
- `_ALLOWED_GRAPH_RELATIONSHIPS` — relationship types the LLM may filter on

### When you add a new graph entity / relationship

If you extend the Dagster Neo4j ingestion to MERGE a new label or
relationship type, you MUST add it to BOTH allowlists in tools.py and
ship a test in `test_cypher_allowlist.py`. Otherwise the LLM will
silently lose the ability to filter by it.

### Symptom of a missed update

A Loki search for `_validate_cypher_label: rejected label=` or
`_validate_cypher_relationship: rejected rel_type=` shows hits with a
label/type name that IS in your indexer code. That's the discrepancy.
Add the identifier to the allowlist; restart fastapi.

### Why not just trust the LLM

Cypher injection is the same shape as SQL injection. A malicious or
hallucinated `relationship_type="HOSTS] WITH 1 AS x MATCH (n) DETACH DELETE n //"`
would execute as a destructive query. The allowlist is the cheapest
defence — same pattern as P0 #2's per-table column allowlist for
`verify_numerical_claim`.

---

## Neo4j Community Edition backup + restore

**Context.** Neo4j Community Edition does NOT have online backup
(`neo4j-admin backup` is Enterprise-only). The two viable strategies for
GeoRAG are an offline `database dump` (preferred — produces a portable
artifact) and a volume-level snapshot (faster but storage-driver
specific).

### Offline dump (recommended — runs nightly during low-traffic window)

The graph fits in < 100 MB on disk today, so the stop-restart window is
typically 30–90 s. This procedure ships a portable `.dump` file that can
be restored on any host with the same Neo4j major version.

```bash
# 1. Stop Neo4j cleanly so the dump captures a consistent state.
docker compose stop neo4j

# 2. Run neo4j-admin in a one-shot container against the same data
#    volume. The --to-path mount writes the dump to a host-side
#    `backups/` folder we can ship to S3/MinIO/cold storage later.
mkdir -p backups
docker compose run --rm \
  -v georag_neo4j_data:/data \
  -v $(pwd)/backups:/backups \
  --entrypoint neo4j-admin \
  neo4j:2026-community \
  database dump --to-path=/backups neo4j

# 3. Restart Neo4j.
docker compose start neo4j
docker compose ps neo4j   # wait for healthy

# 4. Ship the dump off-host. Naming convention:
#    georag-neo4j-YYYYMMDD-HHMM.dump
mv backups/neo4j.dump backups/georag-neo4j-$(date -u +%Y%m%d-%H%M).dump
mc cp backups/georag-neo4j-*.dump georag/backups/neo4j/
```

### Restore from dump

```bash
docker compose stop neo4j

# Wipe the existing data volume — the load is destructive.
docker volume rm georag_neo4j_data
docker volume create georag_neo4j_data

docker compose run --rm \
  -v georag_neo4j_data:/data \
  -v $(pwd)/backups:/backups \
  --entrypoint neo4j-admin \
  neo4j:2026-community \
  database load --from-path=/backups --overwrite-destination=true neo4j

docker compose start neo4j
```

### Verification after restore

```bash
PASS=$(grep ^NEO4J_PASSWORD .env | cut -d= -f2)
docker compose exec -T neo4j cypher-shell -u neo4j -p "$PASS" \
  "MATCH (n) RETURN labels(n)[0] AS label, count(*) AS n ORDER BY n DESC;"
# Compare counts against the pre-backup snapshot.
```

### What NOT to do

- **Don't `docker cp` from a running container.** The data files are
  open and you'll get a torn read.
- **Don't `tar` the volume from a running container.** Same problem.
- **Don't restore a dump from a different Neo4j major version.** 2026.x
  dumps load only into 2026.x. For cross-major restore, dump as Cypher
  (`apoc.export.cypher.all`) instead — see APOC docs.

### Backup cadence

| Environment | Frequency | Retention |
|---|---|---|
| Dev | On-demand only | Last 3 dumps |
| Staging | Nightly 02:00 UTC | 14 days |
| Prod | Nightly 02:00 UTC + before any schema migration | 30 days nightly + 12 monthly |

Automation lives in-container: `docker/neo4j/backup.sh` is mounted into
`georag-backup-agent` and triggered by Ofelia on the schedule defined in
the compose `ofelia.job-exec.neo4j-backup.*` labels. See
`ops/runbooks/neo4j-backup.md` for operator procedures (DRY_RUN, manual
trigger, restore).

---

## LLM backend selection — Ollama / vLLM / Anthropic

**Context.** The FastAPI orchestrator supports three LLM backends. They
are mutually exclusive for the PRIMARY path; one of them (vLLM) can also
serve as a failover target when the primary is Anthropic.

### Current state (verify with `docker compose exec fastapi env | grep LLM`)

| Variable | Dev default | Notes |
|---|---|---|
| `LLM_BACKEND` | `vllm` | GPU inference via the `vllm` container (Ollama cutover complete) |
| `LLM_PRIMARY_URL` | `http://vllm:8000/v1` | OpenAI-compat endpoint |
| `LLM_PRIMARY_MODEL` | `Qwen/Qwen3-14B-AWQ` | Qwen 3 14B dense AWQ (reverted from Qwen3-30B-A3B MoE in 2026-05 to free A4500 VRAM for hatchet-worker-ai embed/rerank/sparse models). Stays resident via vLLM. |
| `VLLM_MODEL` | `Qwen/Qwen3-14B-AWQ` | The value actually sent to the OpenAI-compat API by FastAPI when `LLM_BACKEND=vllm` — must match `served-model-name` on the container. |
| `OLLAMA_NUM_CTX` | _(unused)_ | Ollama is deprecated; legacy Modelfiles archived under `docker/_deprecated/ollama/`. |
| `ANTHROPIC_API_KEY` | empty | Set + flip `LLM_BACKEND=anthropic` to activate |
| `LLM_BACKEND_FALLBACK` | `downshift` | Only fires when LLM_BACKEND=anthropic |

### Flip to Anthropic (cloud) primary

This activates the prompt-cache + tier-routing + real-streaming +
multi-turn-correction code paths that ship this codebase but are
dormant under Ollama.

```bash
# 1. Get an API key
# https://console.anthropic.com/account/keys

# 2. Edit .env
sed -i 's/^ANTHROPIC_API_KEY=.*/ANTHROPIC_API_KEY=sk-ant-api03-XXXX/' .env
sed -i 's/^LLM_BACKEND=.*/LLM_BACKEND=anthropic/' .env

# 3. Restart fastapi (lifespan re-creates the AsyncAnthropic pool)
docker compose restart fastapi

# 4. Confirm
docker compose logs fastapi | grep -i "Anthropic client ready"
# Expect: "Anthropic client ready (pooled)"

# 5. Smoke-test a chat query — should land in
#    `_call_anthropic_llm` not `_call_openai_compatible_llm`
docker compose logs -f fastapi | grep -E "_call_anthropic|_call_openai"
```

### Flip to vLLM primary

vLLM is the canonical inference path for both dev and prod (Ollama
cutover complete — see `docs/model_migration.md`). The dev workstation
(RTX A4500, 20 GB) serves `Qwen/Qwen3-14B-AWQ` with `awq_marlin` kernels
at `--max-model-len=8192` and `--gpu-memory-utilization=0.80` (leaves
VRAM headroom for the co-tenant hatchet-worker-ai embed/rerank/sparse
models); prod hardware sizes are set by the production-readiness doc.

```bash
# 1. Confirm AWQ Qwen3-14B fits + serves at acceptable throughput
./ops/validation/vllm_a4500_smoke.sh
# 2. (gated models) provide HF token
sed -i 's/^HF_TOKEN=.*/HF_TOKEN=hf_XXXX/' .env
# 3. Start vLLM (overlay; mutually exclusive with the dev-llm Ollama
#    profile during the cutover window)
docker compose -f <canonical>.yml -f docker/compose.vllm.yml \
  --profile gpu-llm up -d vllm
# 4. Wait for healthy (cold weight-load takes 60-90 s on the A4500;
#    cached afterwards via the named HF cache volume)
docker compose ps vllm
# 5. Flip the backend
sed -i 's|^LLM_BACKEND=.*|LLM_BACKEND=vllm|' .env
sed -i 's|^LLM_PRIMARY_URL=.*|LLM_PRIMARY_URL=http://vllm:8000/v1|' .env
docker compose restart fastapi
```

### Anthropic + local-LLM failover (belt-and-braces production)

When Anthropic returns 429/529 the orchestrator can either downshift
(cheaper Anthropic tier) OR cross-backend-failover to the local vLLM.
The cross-backend path captures a truly independent failure domain:

```bash
# Run BOTH services — Anthropic primary, vLLM as failover target
docker compose -f <canonical>.yml -f docker/compose.vllm.yml \
  --profile gpu-llm up -d vllm
sed -i 's/^LLM_BACKEND_FALLBACK=.*/LLM_BACKEND_FALLBACK=local_llm/' .env
docker compose restart fastapi
```

The orchestrator's `LLM_BACKEND_FALLBACK=local_llm` branch routes
`anthropic.APIStatusError(429|529)` through `_call_openai_compatible_llm`
against `VLLM_URL` / `VLLM_MODEL` (see
`app/agent/orchestrator.py::run_deterministic_rag`). Failover events
emit the `georag_llm_failovers_total{from_tier=…, to=…}` counter.

The legacy alias `LLM_BACKEND_FALLBACK=deepseek` is still accepted by
the orchestrator and routes to the same code path — kept for back-compat
through the cutover window; will be removed in Phase 2.

### Sanity checks after any flip

```bash
# Effective settings the FastAPI process sees after restart
docker compose exec fastapi python -c "
from app.config import settings
print('LLM_BACKEND:', settings.LLM_BACKEND)
print('effective_llm_model:', settings.effective_llm_model)
print('ANTHROPIC_API_KEY set:', bool(settings.ANTHROPIC_API_KEY))
"

# Per-attempt audit log shows which model handled each call
docker compose logs fastapi --tail=200 | grep "_call_llm: attempt"
# Expect: "_call_llm: attempt=1/8 label=primary model=claude-sonnet-4-5..."
```

---

## Ollama model-upgrade decision matrix

> **⚠️ HISTORICAL CONTEXT (pre-Module-5 migration, 2026-04-21)**
>
> This section was written when `qwen2.5:14b` was the primary. The stack
> has since migrated to **`qwen3:30b-a3b` (MoE)** via the Module 5 Qwen
> migration. The VRAM math in this section is correct for dense-model
> sizing but doesn't reflect MoE (A3B = 3B active params, different KV
> cache math) or the Qwen 3 thinking-mode budget behavior.
>
> Hardware refresh 2026-05-08 — the table below was sized for the prior
> RTX 4080 16 GB. Actual dev hardware is now NVIDIA RTX A4500 20 GB
> (Ampere) on a Threadripper Pro 5955WX. The +4 GB on the new card lets
> qwen3:30b-a3b run cleanly at 24K context with `OLLAMA_NUM_PARALLEL=1`,
> and Q5_K_M with a small Threadripper-friendly CPU offload.
>
> For the current primary model, context, and thinking-mode state, see:
>   - `docs/model_migration.md` (final-state table at the bottom)
>   - `ops/baselines/capacity-planning.md` (refresh 2026-05-08)
>   - `ops/audit/2026-04-21-tool-call-01-investigation.md` (thinking-mode
>     budget discovery)
>   - `memory/project_module_5_status.md` (current state summary)
>
> This section preserved unchanged below as the historical dense-model
> reference. Use when evaluating a fallback-to-dense scenario.

**Context.** The chat backend defaults to `qwen2.5:14b` Q4_K_M because
that's the largest model that fits cleanly on RTX 4080 (16 GB VRAM)
with 24 K context AND `OLLAMA_NUM_PARALLEL=2`. Disk size is rarely the
binding constraint — VRAM is.

### VRAM budget math (verify with `nvidia-smi --query-gpu=memory.total`)

For any candidate model:

```
total_vram_needed
  = model_weights_size                                  # Q4_K_M ≈ ~param_count × 0.6 bytes
  + (n_layers × kv_heads × head_dim × 2 × ctx × 2)      # KV cache @ FP16
        × num_parallel_sessions
```

With `OLLAMA_KV_CACHE_TYPE=q8_0`, KV cache size is halved.
With `OLLAMA_FLASH_ATTENTION=1`, attention compute is faster but doesn't
materially change memory.

### Models we care about, on RTX 4080 (16 GB)

| Model | Q4 weights | KV/session @ 24K, q8_0 | Total @ parallel=2 | Fits? | Quality vs current |
|---|---|---|---|---|---|
| `qwen2.5:14b` (current) | 9 GB | 2.3 GB | **~13.6 GB** | ✅ | baseline |
| `qwen2.5:14b-georag` (current + bakes ctx + drops Qwen SYSTEM) | 9 GB | 2.3 GB | ~13.6 GB | ✅ | + identity-leak fix |
| `qwen2.5:32b` Q4_K_M | 19 GB | 3.2 GB | **~25 GB** | ❌ overflows → measured **0.28 tok/s** (see below) | unusable |
| `qwen2.5:32b` Q3_K_M | 14 GB | 3.2 GB | ~20 GB | ❌ overflows by 4 GB | likely also CPU-offload |
| `deepseek-r1-distill-qwen-14b` | 9 GB | 2.3 GB | ~13.6 GB | ✅ | better reasoning per benchmarks; same VRAM |
| `gpt-oss:20b` Q4 | 12 GB | 2.8 GB | ~17.6 GB | ❌ overflows by 1.6 GB | likely CPU-offload |
| `mistral-small:24b` Q4 | 14 GB | 2.5 GB | ~19 GB | ❌ overflows by 3 GB | likely CPU-offload |
| `llama3.1:8b` Q4 | 5 GB | 1.5 GB | ~8 GB | ✅ huge headroom | smaller / less agent-tuned |

**Anything that overflows DOES load** — Ollama partially CPU-offloads
rather than refusing. Live measurement against `qwen2.5:32b` on this
hardware (RTX 4080 16 GB), 2026-04-17:

```
qwen2.5:32b    21 GB    46%/54% CPU/GPU    4096    ← Ollama auto-shrunk context from 24K
nvidia-smi:    14,988 MiB used / 16,376 MiB total — 1 GB free
Generation:    48 completion tokens in 173.9 s = 0.28 tok/s
```

That's **~125× slower** than `qwen2.5:14b` on full-GPU. A 1000-token
RAG answer would take ≈60 minutes. The FastAPI `TIMEOUT_GATHER_S=8`
fires on every query well before the LLM produces anything useful.

`qwen2.5:32b` is on disk in `ollama_models` for independent
verification but **must not be set as `LLM_PRIMARY_MODEL` on this
hardware**. Remove with `docker compose exec ollama ollama rm qwen2.5:32b`
to reclaim 19 GB of disk if you've satisfied yourself with the result.

### What to do

1. **Today (16 GB VRAM)**: stay on `qwen2.5:14b-georag` (the custom
   variant built from `docker/ollama/Modelfile.qwen2.5-14b-georag`).
   Switch `LLM_PRIMARY_MODEL=qwen2.5:14b-georag` in `.env` once you've
   built the variant — it strips the baked-in Qwen SYSTEM, bakes the
   24 K context, and bakes the 4 K output cap.
2. **Same hardware, different model**: try
   `deepseek-r1-distill-qwen-14b` — same VRAM footprint as current,
   often better reasoning on multi-hop RAG. Pull and A/B against the
   golden query set:
   ```bash
   docker compose exec ollama ollama pull deepseek-r1:14b
   ```
3. **Hardware refresh path**: 24 GB VRAM (RTX 4090, A5000) unlocks
   `qwen2.5:32b` Q4 cleanly with 2 parallel sessions. 48 GB VRAM
   (A6000) unlocks `qwen2.5:72b` Q4 + a 70b distill of DeepSeek R1.

### Build the GeoRAG variant + switch primary

```bash
# Build (one-shot — image stays in ollama_models volume)
docker compose exec -T ollama bash -c '
cat > /tmp/Modelfile.georag <<EOF
FROM qwen2.5:14b
SYSTEM ""
PARAMETER num_ctx 24576
PARAMETER num_predict 4096
PARAMETER temperature 0.1
EOF
ollama create qwen2.5:14b-georag -f /tmp/Modelfile.georag
'

# Switch primary
sed -i 's/^LLM_PRIMARY_MODEL=.*/LLM_PRIMARY_MODEL=qwen2.5:14b-georag/' .env
docker compose restart fastapi
```

### Verify which model just answered a query

```bash
docker compose exec ollama ollama ps
# NAME                  ID  SIZE    PROCESSOR    CONTEXT  UNTIL
# qwen2.5:14b-georag   ... 11.5 GB 100% GPU     24576    5 min
```

`PROCESSOR=100% GPU` is what you want. Anything with `% CPU` in the
mix means the model didn't fit and you're paying ~5× latency penalty.

---

## Qdrant snapshots + zero-downtime re-index

### Online snapshot (Community Edition)

Unlike Neo4j Community (which needs the DB stopped to dump), Qdrant's
snapshot API works while the collection is online. A 100 MB collection
snapshots in ~1 s on NVMe.

```bash
# Snapshot one collection
curl -X POST http://localhost:6333/collections/georag_reports/snapshots

# Snapshot every collection
for c in $(curl -s http://localhost:6333/collections | \
           python3 -c "import sys,json; [print(c['name']) for c in json.load(sys.stdin)['result']['collections']]") ; do
  curl -X POST "http://localhost:6333/collections/$c/snapshots"
done

# List snapshots for a collection
curl http://localhost:6333/collections/georag_reports/snapshots

# Download (snapshot file lives under /qdrant/storage/snapshots/<collection>/)
SNAP=$(curl -s http://localhost:6333/collections/georag_reports/snapshots | \
       python3 -c "import sys,json; print(json.load(sys.stdin)['result'][-1]['name'])")
curl -o backups/georag_reports.snapshot \
  "http://localhost:6333/collections/georag_reports/snapshots/$SNAP"
```

### Restore from snapshot

```bash
# Upload + recover into a (possibly-wiped) collection
curl -X PUT "http://localhost:6333/collections/georag_reports/snapshots/upload" \
  -F "snapshot=@./backups/georag_reports.snapshot"
```

### Backup cadence (recommended)

| Environment | Frequency | Retention | Storage |
|---|---|---|---|
| Dev | On-demand | Latest 3 per collection | Host `backups/` dir |
| Staging | Nightly 02:00 UTC | 14 days | S3/MinIO |
| Prod | Nightly + before any reindex | 30 days daily + 12 monthly | S3/MinIO + offsite |

Automate with a host cron entry that iterates collections, posts
snapshots, downloads each, ships to MinIO. Skeleton lives in
`scripts/qdrant_snapshot.sh` — write when the prod path is on the
roadmap.

---

## Qdrant zero-downtime re-index via collection alias

HNSW `m` is IMMUTABLE after collection creation. Same for `distance`
and `vector_size`. To change any of those you must create a fresh
collection + copy data + swap.

```bash
# 1. Create v2 with new config
curl -X PUT http://localhost:6333/collections/georag_reports_v2 \
  -H "Content-Type: application/json" \
  -d '{"vectors":{"size":384,"distance":"Cosine"},"hnsw_config":{"m":64}}'

# 2. Re-ingest from source (Dagster re-run)
docker compose exec dagster-webserver dagster asset materialize \
  --select index_reports --config '{...}'

# 3. Verify the new collection's health
curl http://localhost:6333/collections/georag_reports_v2

# 4. Atomic alias swap. Reads on `georag_reports` flip to v2 instantly
#    without any downtime; any in-flight request completes against the
#    old collection because Qdrant doesn't rescope in-flight queries.
curl -X POST http://localhost:6333/collections/aliases \
  -H "Content-Type: application/json" \
  -d '{
    "actions": [
      {"create_alias": {"collection_name": "georag_reports_v2", "alias_name": "georag_reports"}},
      {"delete_alias": {"alias_name": "georag_reports_old"}}
    ]
  }'

# 5. Keep the old collection as "v1" for a week before deleting, so a
#    bad migration can be rolled back instantly.
```

FastAPI `search_documents` targets the alias name `georag_reports`, so
no code change is needed for the swap.

---

## Qdrant access control

**Dev posture (current).** Qdrant listens on port 6333 but the port is
not exposed externally — only the internal `georag` Docker network
reaches it. `QDRANT_API_KEY` in `.env` is empty. This is fine because
an attacker who can already reach the internal network has bypassed a
much bigger security boundary.

**Prod posture.** When FastAPI is behind a reverse proxy and the
Qdrant port MAY be reachable from outside the service mesh, enable the
API key:

```bash
# 1. Generate a random value
QDRANT_API_KEY=$(openssl rand -base64 32 | tr -d '/+' | cut -c1-32)
sed -i "s|^QDRANT_API_KEY=.*|QDRANT_API_KEY=$QDRANT_API_KEY|" .env

# 2. Both services pick it up via compose env forwarding
docker compose up -d qdrant fastapi

# 3. Verify the FastAPI client attached the key
docker compose logs fastapi --tail=50 | grep "Qdrant client ready"
# Expect: "Qdrant client ready (api_key_set=True)"

# 4. Verify Qdrant is rejecting unauth traffic
docker compose exec qdrant curl -s -o /dev/null -w "%{http_code}\n" \
  http://localhost:6333/collections
# Expect: 401 (or 403)
```

---

## Redis — persistence, access control, and database schema

**Context.** One Redis 8.4 instance backs four concerns:

| db | Purpose | Loss-tolerance |
|----|---------|-----------------|
| db0 | Horizon supervisor state + Laravel sessions + queue jobs | **NOT tolerant** — restart without persistence = logged-out users + lost queued jobs |
| db1 | Laravel application cache | Tolerant (re-populates) |
| db2 | FastAPI chat response cache + project-graph-entities cache | Tolerant |
| db3 | Reserved for future / operator use | — |

### Persistence

Default (Redis review #1 onwards) uses **AOF with `appendfsync everysec`** — durability within 1 s, ~5 % perf cost. Controlled via `.env`:

```bash
REDIS_APPENDONLY=yes       # "no" to flip back to cache-only mode
REDIS_APPENDFSYNC=everysec # "always" = 0 s window but 10× slower
                          # "no"      = kernel decides (risky)
```

AOF file lives in the `redis_data` Docker volume (`/data/appendonly.aof`). On unclean shutdown Redis auto-truncates to the last valid `appendonly-truncate-to-timestamp`.

If you want to split into a durable queue Redis + a fast cache Redis (the split the compose comment originally suggested), add a second `redis-cache` service with `REDIS_APPENDONLY=no` and point the Laravel `cache.redis` connection at it.

### Prod password + network posture

**Current dev posture is NOT production-safe:**

```
REDIS_PASSWORD = georag_redis_dev   # weak well-known
protected-mode = no                 # only the pass defends
port = 6379 → 0.0.0.0:6380          # bound to every host interface
```

Before promoting to prod:

```bash
# 1. Strong random password
NEW_PASS=$(openssl rand -base64 24 | tr -d '/+' | cut -c1-32)
sed -i "s|^REDIS_PASSWORD=.*|REDIS_PASSWORD=$NEW_PASS|" .env

# 2. Close the host port — services reach Redis via the internal
#    Docker network hostname `redis:6379`. The host binding is only
#    useful for `docker compose exec redis redis-cli` during triage.
#    In prod, delete the `ports:` block from the redis service
#    entirely OR bind to 127.0.0.1:6379 only:
#    ports:
#      - "127.0.0.1:${REDIS_PORT:-6379}:6379"

# 3. Restart
docker compose up -d redis

# 4. Verify every client can still reach Redis
docker compose exec laravel-octane php artisan tinker --execute \
  "echo Illuminate\\Support\\Facades\\Redis::connection()->ping();"
docker compose exec fastapi curl -sf http://localhost:8000/ready | python3 -m json.tool
```

### FastAPI-side pool knobs

See `app/main.py::lifespan` — the FastAPI client is:

```
max_connections=32         # 4 workers × 32 = 128 ≪ Redis maxclients=10000
health_check_interval=30   # PING every 30s on idle conns
client_name=georag-fastapi # visible in CLIENT LIST
db=2                       # isolated from Laravel db0/db1
```

### Observability

Hit ratio by db (watch for sustained < 50 % on db1 / db2 — indicates TTLs too short or cache key churn):

```bash
docker compose exec redis redis-cli -a "$REDIS_PASSWORD" --no-auth-warning INFO stats \
  | grep -E "^keyspace_(hits|misses)"
```

Slowlog (threshold now 1 ms — anything logged is worth investigating):

```bash
docker compose exec redis redis-cli -a "$REDIS_PASSWORD" --no-auth-warning SLOWLOG GET 10
```

---

## Martin tile server — config changes and grant audit

See `ops/runbooks/martin-tile-server.md` for:

- Why `docker compose restart martin` is wrong on WSL2 and the correct
  `docker rm -f georag-martin && docker compose up -d martin` workaround.
- `cache_size_mb` division math (Martin 1.x splits it across four caches —
  set `cache_size_mb: 512` to get 256 MB of tile cache).
- `martin_readonly` role: what it can access, how to audit grants, smoke tests.
- Diagnosing `db error` tile failures (missing schema USAGE vs missing SELECT).
- Prometheus alert rules are DORMANT — Martin 1.5.0 has no `/metrics` endpoint.

---

## Phase H4 — FastAPI ↔ Laravel internal callback channel

**Context.** §7 Report Builder cockpit shows real-time build progress
fed by Laravel Reverb. The producer is FastAPI's `generate_report`
workflow; the consumer is the React cockpit subscribed to
`private-admin.reports.{build_id}`. Bridging them requires FastAPI to
push events INTO Laravel — the reverse direction of the normal
Laravel → FastAPI service-key call.

**Shared secret.** The same `FASTAPI_SERVICE_KEY` env var is reused
symmetrically. Laravel-side `App\Http\Middleware\VerifyServiceKey`
mirrors the FastAPI `verify_service_key` dep — constant-time compare
via `hash_equals`. Endpoint mounted under `/api/internal/*`.

### Routes covered

- `POST /api/internal/admin/reports/{build_id}/progress` — fires
  `App\Events\Admin\ReportBuildProgress` on
  `private-admin.reports.{build_id}`. Body:
  `{stage, section_id?, message?, sections_completed?, sections_total?}`.

### Rotating the shared key

The key must match between Laravel and FastAPI services. Steps:

1. Generate a new random key on a workstation:
   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(48))"
   ```
2. Update `FASTAPI_SERVICE_KEY` in `.env` (Laravel) and the FastAPI
   container's env (docker-compose `services.fastapi.environment` or
   the secrets store). Both names are identical.
3. Roll the FastAPI container first (`docker compose restart fastapi`),
   then `php artisan config:clear && docker compose restart laravel-octane`.
   This window is ~5 s where outbound progress posts to the old key
   return 401; the Hatchet workflow continues — the broadcast is
   best-effort and only the cockpit's live strip is affected.
4. Verify with `curl -H "X-Service-Key: $NEW" ...` against both
   directions.

### Failure mode — broadcast bridge unreachable

When FastAPI calls Laravel and the call fails (Laravel down, key
mismatch, network), `app.services.laravel_bridge.post_report_build_progress`
logs `WARNING laravel_bridge: progress post failed` and continues. The
workflow output is unaffected; only the live progress strip stops
updating. The cockpit falls back to a stale "idle" indicator after no
events for ~5 s; operators can refresh to get the audit-anchored
build state via the `GET /admin/reports/{build_id}` path.

---

## Phase H4 — alerts inbox + acknowledge audit trail

**Context.** `/admin/alerts-inbox` surfaces every audit row where
`action_type LIKE '%.alert'` (cost burn, vLLM security, ingestion
breaches, future SLA misses). Operators click Acknowledge; that
writes an immutable `<original_action_type>.acknowledged` counter row
keyed on the same `target_id`. Both rows are part of the audit hash
chain — you cannot retroactively un-acknowledge.

### How to insert an alert (programmatic)

Alerts are emitted by application code via `app.audit.emit_audit`
with `action_type` ending in `.alert`. The convention:

| action_type             | source                                    | payload required                                   |
|-------------------------|-------------------------------------------|----------------------------------------------------|
| `cost.burn.alert`       | §5 LLM cost tracker (per-workspace 1h)    | `severity, spent_usd, window, workspace_id`        |
| `vllm_security.alert`   | Phase 0 §29 vLLM security gate            | `severity, model, finding`                         |
| `ingestion.breach.alert`| Phase 1 ingestion quality gate            | `severity, table, breach_kind, run_id`             |

The `severity` field in `payload` drives the UI badge. Accepted values:
`critical`, `high`, `medium`, `low`. When absent the inbox infers
medium and the badge falls back to a neutral yellow.

### Indexes

Two partial indexes back the listing query — see
`database/raw/phase0/102-phase-h4-alerts-index.sql`:

- `audit_ledger_alerts_idx` — `(created_at DESC) WHERE action_type LIKE '%.alert'`
- `audit_ledger_acks_idx`   — `(action_type, target_id) WHERE action_type LIKE '%.acknowledged'`

Production deploys with an existing populated `audit_ledger` should
build these `CONCURRENTLY` rather than via the BEGIN…COMMIT migration:

```sql
CREATE INDEX CONCURRENTLY audit_ledger_alerts_idx
    ON audit.audit_ledger (created_at DESC)
    WHERE action_type LIKE '%.alert';
```

### Acknowledging from psql (break-glass)

If the FastAPI endpoint is down, an ack can be written directly:

```sql
-- Resolve the alert
SELECT id, action_type, workspace_id, target_schema, target_table, target_id
  FROM audit.audit_ledger
 WHERE id = '<audit_id>'::uuid;

-- Insert the counter row
INSERT INTO audit.audit_ledger (workspace_id, actor_id, actor_kind,
                                action_type, target_schema, target_table,
                                target_id, payload)
SELECT workspace_id,
       <operator_user_id>,
       'user',
       action_type || '.acknowledged',
       target_schema,
       target_table,
       target_id,
       jsonb_build_object('original_audit_id', id::text,
                          'note', 'break-glass ack — see incident #NNN')
  FROM audit.audit_ledger
 WHERE id = '<audit_id>'::uuid;
```

The hash-chain trigger writes `previous_hash` automatically.

---

## Where to add sections to this file

Add a new H2 section when:
- A procedure is >5 lines AND
- It touches secrets, encrypted data, PII, or ops-visible state AND
- Getting it wrong has a blast radius ≥ one tenant.

Skip this file for:
- Dev-only setup (use `README.md`).
- Code architecture (use `georag-architecture.html`).
- Per-feature behaviour (use the relevant code's docblock).
