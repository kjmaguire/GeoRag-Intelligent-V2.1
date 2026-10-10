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

### ⚠️ On the production deployment, use the script, not this page

```bash
bash deploy/aws/rotation/rotate-app-key.sh            # preflight + plan
bash deploy/aws/rotation/rotate-app-key.sh --apply    # do it
```

`ops/runbooks/secret-rotation.md` §2 has the full procedure and the
findings behind it. This page is the Laravel-level mechanics, which the
script executes; it is still the right page for a dev or on-prem install,
and for understanding what the script is doing.

**Neither of the two procedures below works on ECS as written.**
`audit:rotate-key` runs `key:generate --force`, which writes to `.env` —
and the production image ships none (`.dockerignore` excludes it; `APP_KEY`
arrives as an env var), so it fails *after* the dump and *before* the
restore. And `php artisan down` puts one container into maintenance, while
`laravel-octane` runs two tasks and neither of them is where a rotation
would run; the script scales the writers to zero instead. Running either
one against production by hand is how you get a half-rotated audit ledger.

### Rotation procedure (preferred: one-shot — dev and on-prem only)

Requires a writable `.env`, so it is unusable on the production image; see
the warning above.

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

If the orchestrator fails mid-run you run the same four commands by hand.
On the production deployment the equivalent is
`deploy/aws/rotation/rotate-app-key-inside.sh`, which runs steps 2, 4 and 5
inside a one-off task — read that rather than retyping these, and note that
it replaces step 3 with a key minted outside the container and step 1 and 6
with scaling the writers to zero and back:

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
  readable again. This is almost always the right answer. It is NOT the
  answer for a ledger that is *half* rotated, where some rows are under
  each key: no key reads that, and `audit:dump-pii` cannot even re-dump it
  (it fails the whole dump on the first row it cannot decrypt). That case
  is why the production script takes an RDS snapshot before it starts.
- **Accept the loss.** For audit data you're happy to forget (dev-only
  install, retention policy expired), `TRUNCATE query_audit_log` and
  move on.

### Prevention

- Store `APP_KEY` alongside the DB backup, always encrypted with a
  separate KMS. Losing the DB without the key is the same as deleting the
  data.
- Never rotate `APP_KEY` out-of-band (e.g., via `php artisan key:generate`
  on a whim). Always run the rotation procedure above — and on production,
  the script.
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

## COHERE_API_KEY rotation — interrupts chat, OCR AND embedding (ADR-0025)

One key, three capabilities, since 2026-10-04: Command A+ chat
(`LLM_BACKEND=cohere`), Parse 5 OCR (`OCR_ENGINE=cohere_parse`) and, with
`EMBEDDING_BACKEND=cohere`, Embed 5 for **both** the query path (`fastapi`)
and the ingest sweep (`hatchet-worker`). Rate limits are per key too, not per
capability: a large ingest that 429s shares its ceiling with chat.

What a rotation window costs, because it used to be "chat and scanned pages"
and is now more:

| While the key is wrong or the task is mid-rollout | Effect |
|---|---|
| chat | the query fails: `LLM_BACKEND` selects exactly one backend and nothing fails over to another (see "LLM backend selection") |
| **query embedding** | `search_documents` returns an empty result (`retrieval_failure='error'`), so any answer that needs documents **refuses**; structured-data questions can still answer |
| OCR | scanned pages fall back to `tesseract` (no tables), silently |
| ingest embedding | `embed_pending_passages` records errors; passages keep `embedding_id IS NULL` and the next sweep retries them |

There is no `COHERE_API_KEY_PREVIOUS`, so none of this is zero-downtime. The
procedure is `ops/runbooks/secret-rotation.md` §8 (Secrets Manager
`put-secret-value`, then `force-new-deployment` of **both** `fastapi` and
`hatchet-worker` — a task that kept the old key keeps failing). Rotate off
hours, revoke the old key in Cohere's dashboard only after both services are
`stable`, and re-run `ops/validation/cohere_probe.sh`: its `embed` section is
how you learn the new key is entitled to Embed, not just chat.

Locally the key is `COHERE_API_KEY` in `.env`; recreate `fastapi` and
`hatchet-worker` (`docker compose up -d --no-deps --force-recreate`) so both
re-read it.

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

Add to the ops cron or a GitHub Actions workflow:

```cron
# Weekly Monday 06:00 UTC — golden-set review.
0 6 * * MON  docker exec georag-laravel-octane php artisan audit:golden-set-report \
             --output /gold/golden-set-$(date +\%Y\%m\%d).md
```

---

## vLLM prefix caching (operator-run vLLM endpoint only)

This applies only when `LLM_BACKEND=vllm` points at an OpenAI-compatible
endpoint you run yourself (`VLLM_URL`) — there is no bundled vLLM service and
no `docker/compose.vllm.yml` overlay. The orchestrator sends that call as a
stable `system` message (prompt variant + per-project preamble) and a per-turn
`user` message (CONTEXT + question), which is the structural prerequisite for
vLLM's automatic prefix cache. Start your vLLM with `--enable-prefix-caching`;
without it the prefix is stable but never cached.
`_call_openai_compatible_llm` logs `cached_tokens` and `cache_hit_rate` per
call when the backend reports them. See "LLM backend selection" below.

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
- **`docker exec -e DB_CONNECTION=pgsql ... php artisan test` (no `-c`)
  can throw a confusing hybrid PDOException** — "connection to server at
  postgresql... FATAL: database ":memory:" does not exist". This is NOT
  `phpunit.xml`'s `force="true"` failing; it's a SEPARATE local-dev-only
  interaction: any container with `MIGRATE_DB_CONNECTION=pgsql_migrations`
  set (see `config/database.php`'s `pgsql_migrations` connection —
  deploy-time env, real host `postgresql`) makes `artisan migrate`/
  `migrate:fresh` **always** run on that connection, regardless of
  `database.default` — but that connection's `database` key still reads
  `env('DB_DATABASE', 'georag')`, which `phpunit.xml` DOES force to
  `:memory:`. The result: real Postgres host + a SQLite-only database
  name. CI never sets `MIGRATE_DB_CONNECTION`, so this never happens
  there. Locally, either unset `MIGRATE_DB_CONNECTION` for the shell
  running SQLite-suite tests, or just use `composer run test` /
  `-c phpunit.pgsql.xml` as documented above instead of ad hoc `-e`
  overrides.

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
is **True** (`Settings` in `src/fastapi/app/config.py`), and neither the AWS
task definitions, the Helm chart nor `docker-compose.yml` overrides it, so
production enforces; `.env.production.example` pins it to true explicitly.
Only the dev `.env.example` opts out (`MULTI_TENANT_ENFORCEMENT_ENABLED=false`
with `SINGLE_TENANT_MODE=true`). The opt-out is validated: FastAPI refuses to
start with enforcement off unless `SINGLE_TENANT_MODE=true` is also set. Read
`src/fastapi/SECURITY.md` BEFORE relaxing it — the relaxed posture is safe
only for a single-customer deployment.

The matching FastAPI-side defence-in-depth is the GUC-aware RLS policy
on `silver.collars` and `silver.samples` — when the multi-tenant flag is
on, tools migrate to `AgentDeps.acquire_scoped()` which sets
`SET LOCAL georag.project_id = '<uuid>'` per transaction so even a
WHERE-clause-forgetting tool query gets RLS-filtered. Migration
`2026_04_17_120200_replace_toothless_rls_with_guc_aware_policies.php`
installs the policy (admits all rows when GUC unset — backwards
compatible).

---

## `LARAVEL_INTERNAL_URL` — the FastAPI → Laravel callback host

**Incident 2026-08-18.** This variable was never set on `fastapi-cc` or
`hatchet-worker-cc` (the Azure Container Apps that ran FastAPI and the Hatchet
worker before production moved to AWS ECS, ADR-0022).
`laravel_bridge._laravel_base()` therefore fell back to
its Herd-local default, `http://laravel.test`, which resolves to nothing
inside a container. Every callback into Laravel had been failing in
production, silently:

| Helper | What was dead |
| --- | --- |
| `post_ingestion_progress` | IngestionRuns rows never flipped state in real time |
| `post_workspace_data_updated` | No live refresh of Reader / Lakehouse / Overview |
| `post_admin_surface_updated` | `cost_burn_watcher` + all admin surfaces |
| `post_report_build_progress` | §15 report-build progress bar |
| `post_workspace_activity`, `post_user_inbox_updated` | Activity feed, inbox badge |

(`post_report_build_progress`, `post_workspace_activity` and
`post_user_inbox_updated` have since lost their Laravel routes — see
"FastAPI → Laravel callback channel" below.)

Every helper swallows its own exception by design (a broadcast must never
fail the workflow that made real progress), so the only symptom was a
`log.warning` per call:

```
laravel_bridge: admin.surface_updated broadcast failed surface=llm-cost err=[Errno -2] Name or service not known
```

That reads like a transient network blip, not a missing environment variable.
`_laravel_base()` now logs **ERROR once per process, naming the variable**,
when it takes the fallback — see `test_laravel_bridge_base_url.py`.

### The correct value

On ECS, `deploy/aws/terraform/config.tf` sets it on both `fastapi` and
`hatchet-worker` to the Cloud Map name of the `laravel-octane` service:

```
LARAVEL_INTERNAL_URL=http://laravel-octane.<namespace>:80
```

`<namespace>` is the stack's private DNS namespace
(`aws_service_discovery_private_dns_namespace.this.name`). The fully qualified
`<service>.<namespace>` form is required: an awsvpc task gets no search domain
for that namespace, so a bare `laravel-octane` does not resolve (the header of
`scripts/check-internal-urls.py` explains it). Compose defaults it to
`http://laravel-octane`, the service name on its Docker network.

It lives in Terraform, so it cannot silently drift out of a task definition,
and CI fails when a service-to-service URL falls back to a compose hostname
(`scripts/check-internal-urls.py`, run by `ci.yml`). To change it, edit
`config.tf` and apply Terraform; the `-cc` Container Apps, `az containerapp
update` and a deploy step that re-asserts the variable no longer exist. If the
"falling back to http://laravel.test" error ever reappears, read the worker's
and FastAPI's log streams (`ops/runbooks/aws-oncall.md` §6):

```bash
aws logs tail /ecs/georag --since 1h --log-stream-name-prefix hatchet-worker \
  --filter-pattern laravel_bridge
```

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

## Observability rollout — Logfire toggles

**Context.** P1 #9 added gated Logfire instrumentation to FastAPI. Default is
OFF (`LOGFIRE_ENABLED=false`) — no surprise outbound traffic.

**The `logfire` package is not installed.** It is a dependency of neither
`src/fastapi/pyproject.toml` nor `uv.lock` (only `logfire-api`, which Pydantic
AI pulls in transitively), so `LOGFIRE_ENABLED=true` currently fails at
`import logfire`: FastAPI logs `Logfire init failed — proceeding without OTel
instrumentation` (`app/main.py`, lifespan section 0) and starts without it.
Add `logfire` to the FastAPI dependencies and refresh `uv.lock` before turning
it on; until then the toggles below do nothing.

### Settings

`LOGFIRE_ENABLED`, `LOGFIRE_TOKEN`, `LOGFIRE_SERVICE_NAME` (default
`georag-fastapi`) and `LOGFIRE_ENVIRONMENT` (default `dev`) are the only
Logfire settings. With `LOGFIRE_TOKEN` set, spans go to Logfire's hosted
backend (requires outbound HTTPS to `*.pydantic.dev`). With no token and
`LOGFIRE_ENABLED=true` it runs in local-only mode (`send_to_logfire=False`):
spans are created in-process and shipped nowhere. There is no OTLP-endpoint
setting — `LOGFIRE_OTEL_ENDPOINT` does not exist.

### Enable for one-off triage (after the package is installed)

```bash
# In .env:
LOGFIRE_ENABLED=true
LOGFIRE_TOKEN=pylf_xxx       # omit for local-only mode
LOGFIRE_SERVICE_NAME=georag-fastapi
LOGFIRE_ENVIRONMENT=prod     # or dev / staging

# Restart fastapi only — Logfire init runs in lifespan section 0.
docker compose restart fastapi
docker compose logs -f fastapi | grep -i logfire
# Expect:  "Logfire configured (hosted backend, service=...)"
#   or, with no token: "Logfire configured in LOCAL-ONLY mode (LOGFIRE_TOKEN unset)"
```

### What you get

`app/main.py` instruments FastAPI requests, asyncpg, httpx and Pydantic AI.
Pydantic AI is vestigial (CLAUDE.md), so expect request, SQL and outbound-call
spans rather than a per-agent-run span tree. Per-tool latency is recorded by
the `georag_tool_duration_seconds` metric (`app/metrics.py`).

### Disable

```bash
LOGFIRE_ENABLED=false
docker compose restart fastapi
```

Init failures never block startup — they're `logger.exception()`-logged
and the app continues.

---

## LLM backend selection — Cohere / Bedrock / vLLM / Anthropic

**Context.** The FastAPI orchestrator calls exactly one chat backend, chosen by
`LLM_BACKEND`. The values and defaults live in `Settings`
(`src/fastapi/app/config.py`); `georag-architecture.html` §08 "Backend
selection" is the reference. There is no automatic failover from one backend to
another and no `LLM_BACKEND_FALLBACK` setting: an `.env.example` comment still
describes fallback modes, but nothing reads such a variable, and the
`georag_llm_failovers_total` counter in `app/metrics.py` is never incremented.

### Current state (verify with `docker compose exec fastapi env | grep LLM`)

| `LLM_BACKEND` | Reaches | Needs |
|---|---|---|
| `cohere` (default) | Cohere Command A+ (`COHERE_CHAT_MODEL`, `command-a-plus-05-2026`) on Cohere's own API — `app/agent/llm_cohere.py` | `COHERE_API_KEY`; `COHERE_BASE_URL` defaults to `https://api.cohere.com` |
| `bedrock` | The same model through a Bedrock Marketplace (SageMaker) endpoint you deploy yourself — `app/agent/llm_bedrock.py`. None exists in production (ADR-0023) | `BEDROCK_CHAT_MODEL_ID` (a model id or the endpoint ARN) |
| `vllm` | An OpenAI-compatible endpoint you operate yourself; the bundled `vllm` compose service was deleted 2026-07-30 | `VLLM_URL` — FastAPI refuses to start without it; `VLLM_MODEL` defaults to `Qwen/Qwen3-14B-AWQ` |
| `anthropic` | Anthropic's native SDK (prompt caching, adaptive thinking) | `ANTHROPIC_API_KEY`; `ANTHROPIC_MODEL` defaults to `claude-opus-4-8` |

`LLM_BACKEND=azure` is a hard startup error naming the replacement (Azure AI
Foundry was retired 2026-09-08, ADR-0022). With `GEORAG_ENV=production`, a
missing key or URL for the selected backend is logged CRITICAL at startup.

### Flip to Anthropic (cloud) primary

This activates the Anthropic-only code paths — prompt caching, adaptive thinking
and the multi-turn correction splice — which the other backends do not use.
These are the compose steps; on ECS `ANTHROPIC_API_KEY` is not provisioned
(`ops/runbooks/secret-rotation.md` §14).

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

Anthropic is outside the contracted provider set, so `_call_anthropic_llm` is
checked by the egress gate (`app/agent/egress_gate.py`): it is default-deny, and
for a workspace whose `allow_external_llm` setting is not true the call is
refused (`EGRESS_BLOCKED`) instead of answered. Cohere and Bedrock are not gated.

### Pointing at an operator-run vLLM endpoint

Set `LLM_BACKEND=vllm`, `VLLM_URL` (the endpoint's OpenAI-compatible base URL)
and `VLLM_MODEL` (the model name that endpoint serves), then recreate
`fastapi`. Nothing in this repo deploys or sizes that endpoint; see "vLLM prefix
caching" above.

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
# Expect: "_call_llm: attempt=1/8 label=agentic_retrieval model=<effective_llm_model> ..."
```

---

## Qdrant snapshots + re-embedding

The live collection is `georag_chunks` (1024-dim dense + SPLADE++ sparse
vectors; `RETRIEVAL_USE_DOCUMENT_PASSAGES` defaults to true, ADR-0010).
`georag_reports` is the legacy 384-dim collection: nothing writes it any more,
and `search_documents` reads it only if that flag is set false.

### Online snapshot (Community Edition)

Qdrant's snapshot API works while the collection is online. A 100 MB
collection snapshots in ~1 s on NVMe.

```bash
# Snapshot one collection
curl -X POST http://localhost:6333/collections/georag_chunks/snapshots

# Snapshot every collection
for c in $(curl -s http://localhost:6333/collections | \
           python3 -c "import sys,json; [print(c['name']) for c in json.load(sys.stdin)['result']['collections']]") ; do
  curl -X POST "http://localhost:6333/collections/$c/snapshots"
done

# List snapshots for a collection
curl http://localhost:6333/collections/georag_chunks/snapshots

# Download (snapshot file lives under /qdrant/storage/snapshots/<collection>/)
SNAP=$(curl -s http://localhost:6333/collections/georag_chunks/snapshots | \
       python3 -c "import sys,json; print(json.load(sys.stdin)['result'][-1]['name'])")
curl -o backups/georag_chunks.snapshot \
  "http://localhost:6333/collections/georag_chunks/snapshots/$SNAP"
```

### Restore from snapshot

```bash
# Upload + recover into a (possibly-wiped) collection
curl -X PUT "http://localhost:6333/collections/georag_chunks/snapshots/upload" \
  -F "snapshot=@./backups/georag_chunks.snapshot"
```

### Backup cadence (recommended)

| Environment | Frequency | Retention | Storage |
|---|---|---|---|
| Dev | On-demand | Latest 3 per collection | Host `backups/` dir |
| Staging | Nightly 02:00 UTC | 14 days | S3/MinIO |
| Prod | Nightly + before any reindex | 30 days daily + 12 monthly | S3/MinIO + offsite |

Nothing automates this today: the per-store `backup_*` workflows were deleted
2026-08-23, no `scripts/qdrant_snapshot.sh` exists, and ADR-0024 records that
Qdrant has no backup of its own beyond the snapshots
`deploy/aws/upgrade/upgrade-qdrant.sh` takes when it runs (recovery from a
corrupted collection is a re-embed). The ADR-0025 cutover took its snapshot with
the `snapshot` step of `.github/workflows/embed5-cutover.yml`.

---

## Re-embedding `georag_chunks` (there is no alias swap)

There is no Qdrant alias: `search_documents` and the embed sweep address the
concrete collection `georag_chunks`, so its name is fixed and the alias-swap
re-index this section used to describe does not apply. To re-encode — a new
embedding model, or enriched passage text — take a snapshot first (above), then
run `src/fastapi/scripts/reset_embeddings_for_reencode.py` (`--all` for a model
change; it asks first) and let `embed_pending_passages` (every 10 minutes, plus
its daily run) re-encode every passage from `silver.document_passages`.
Retrieval is degraded until that sweep finishes, and a collection holding two
embedding models returns meaningless cosines, so run the reset and the sweep in
one sitting. The ADR-0025 cutover
(`docs/adr/0025-embedding-moves-to-coheres-own-api-on-embed-5.md`, run from
`.github/workflows/embed5-cutover.yml`) is the worked example, including the
snapshot rollback.

---

## Qdrant access control

**Dev posture (current).** Qdrant listens on port 6333, published on the
host loopback only (`${GEORAG_BIND_ADDR:-127.0.0.1}` in `docker-compose.yml`),
so the `georag` Docker network and a shell on the host reach it, not other
machines. `QDRANT_API_KEY` in `.env` is empty. This is fine because
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

**Context.** One Redis instance (8.6.4 in compose, 8.10.0 on ECS) backs four concerns:

| db | Purpose | Loss-tolerance |
|----|---------|-----------------|
| db0 | Horizon supervisor state + Laravel sessions + queue jobs | **NOT tolerant** — restart without persistence = logged-out users + lost queued jobs |
| db1 | Laravel application cache | Tolerant (re-populates) |
| db2 | FastAPI chat response cache | Tolerant |
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
port = 6379 → 127.0.0.1:6380        # host loopback only (GEORAG_BIND_ADDR, REDIS_PORT=6380)
```

Before promoting to prod:

```bash
# 1. Strong random password
NEW_PASS=$(openssl rand -base64 24 | tr -d '/+' | cut -c1-32)
sed -i "s|^REDIS_PASSWORD=.*|REDIS_PASSWORD=$NEW_PASS|" .env

# 2. Close the host port — services reach Redis via the internal
#    Docker network hostname `redis:6379`. The host binding is only
#    useful for `redis-cli` during triage, and compose already limits it
#    to the host loopback (`${GEORAG_BIND_ADDR:-127.0.0.1}`); leave
#    GEORAG_BIND_ADDR unset, or delete the `ports:` block from the redis
#    service entirely.

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

The tile config is `docker/martin/martin.yaml`; its header comments document
the `cache_size_mb` arithmetic (Martin 1.x divides it across three caches: tile
`/2`, sprite `/8`, font `/8`, so `cache_size_mb: 512` gives a 256 MB tile
cache) and why a function source whose function does not exist is fatal to
Martin (it exits, the container never passes its health check, and the ECS
circuit breaker fails the deployment). Add a source to `martin.yaml` only
together with its function, its grant and the Laravel proxy allow-list
(`georag-architecture.html` §04d-tile and §07c-tile;
`docs/architecture/manual/09-martin-and-maplibre.md`). Martin 1.11 serves
`/metrics` (native since 1.7), but nothing scrapes it: no Prometheus is deployed
and the Martin alert rules were deleted with it.

---

## FastAPI → Laravel callback channel

**Context.** FastAPI pushes events INTO Laravel — the reverse of the normal
Laravel → FastAPI service-key call — so Laravel can fan them out over Reverb.
The same `FASTAPI_SERVICE_KEY` is reused symmetrically: Laravel-side
`App\Http\Middleware\VerifyServiceKey` mirrors FastAPI's `verify_service_key`
(constant-time compare via `hash_equals`), and the routes sit under
`/api/internal/*` (`routes/api.php`, "Internal — FastAPI → Laravel callback
bridge").

### Routes covered

Three routes exist:

- `POST /api/internal/v1/ingest-progress/broadcast` — `IngestionProgressBroadcast`
  on `project.{projectId}.ingestion`.
- `POST /api/internal/v1/workspace-data-updated` — `WorkspaceDataUpdated` on the
  same channel.
- `POST /api/internal/v1/admin-surface-updated` — `Admin\AdminSurfaceUpdated`,
  for the surfaces in the channel registry in `routes/channels.php`.

Commit 3c58f72 (#337, 2026-10-06) deleted the other three — `v1/workspace-activity`,
`v1/user-inbox-updated` and `admin/reports/{build_id}/progress` — together with
their controllers and the `User\UserInboxUpdated`, `Admin\ReportBuildProgress` and
`Admin\IngestionReviewDispositionChanged` events. FastAPI's `laravel_bridge.py`
still defines helpers (`post_report_build_progress`, `post_workspace_activity`,
`post_user_inbox_updated`) that post to those three URLs; they would get a 404,
which the helper logs as a warning and swallows, and no application code calls them.

### Rotating the shared key

The key must match between Laravel and FastAPI services. On the production
deployment follow `ops/runbooks/secret-rotation.md` §3 instead; for a dev or
on-prem install:

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
   best-effort and only the live progress strip is affected.
4. Verify with `curl -H "X-Service-Key: $NEW" ...` against both
   directions.

### Failure mode — broadcast bridge unreachable

When FastAPI calls Laravel and the call fails (Laravel down, key
mismatch, network), the `laravel_bridge` helper logs a `WARNING laravel_bridge:
... failed` line and continues. The workflow output is unaffected; only the
live update stops. If the warning is `Name or service not known`, see
"`LARAVEL_INTERNAL_URL`" above.

---

## Acknowledging an audit-ledger alert (break-glass)

**Context.** An alert is an `audit.audit_ledger` row whose `action_type` ends in
`.alert`. Acknowledging it writes an immutable `<original_action_type>.acknowledged`
counter row keyed on the same `target_id`; both rows are part of the audit hash
chain, so an acknowledgement cannot be retracted. The emitters today are
`cost.burn.alert` (the `cost_burn_watcher` workflow, which stays quiet for a
workspace while an unacknowledged alert is inside its window) and
`security.cross_workspace_access.alert` (`services/cross_workspace_audit.py`).
There is no alerts-inbox page or route and no FastAPI acknowledge endpoint any
more (the `admin.alerts-inbox` broadcast channel is still registered in
`routes/channels.php`, but no page subscribes to it), so the SQL below is the
way to acknowledge one.

### Acknowledging from psql

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

## Retired procedures

These sections described components that no longer exist and were removed on
2026-10-10 rather than kept as history. If an old link or checklist sends you
looking for one:

- **Query escalation tiers and the `GeoRAG — Signal Harvesting` Grafana
  dashboard** — the `AGENTIC_ESCALATION_ENABLED`, `AGENTIC_FULL_ESCALATION_ENABLED`
  and `AGENTIC_MAX_TOOL_CALLS` settings do not exist in `src/fastapi`, and compose
  has no Grafana or `dev-monitor` service. Agentic retrieval is described in
  `georag-architecture.html` §04j.
- **Cypher allowlist update procedure and Neo4j backup/restore** — Neo4j was
  removed on 2026-07-28 (CLAUDE.md hard rule 9). On AWS, Postgres relies on RDS
  point-in-time restore and object storage on S3 versioning (see "What is NOT
  covered here" in `ops/runbooks/aws-oncall.md`).
- **Ollama model-upgrade decision matrix** — there is no local model host in this
  repository; see "LLM backend selection".
- **The `/admin/alerts-inbox` listing and its partial indexes** — see "Acknowledging
  an audit-ledger alert".

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
