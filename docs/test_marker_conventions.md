# Test Marker Conventions

Quick reference for the pytest marker pattern used in `src/fastapi/tests/`.
The full opinion lives in the `[tool.pytest.ini_options]` block of
`src/fastapi/pyproject.toml`; this file is the operator-facing summary
new contributors should read first.

## The four markers

| Marker          | When to use                                                        | Runs by default? |
|-----------------|--------------------------------------------------------------------|------------------|
| (unmarked)      | Pure-Python unit test — no network, no DB, no LLM                  | yes              |
| `integration`   | Needs the live Docker stack (PG + FastAPI process at minimum)      | no               |
| `golden`        | Golden RAG query — uses the corpus + LLM, gate-blocking            | no               |
| `hallucination` | Adversarial query that must be refused                             | no               |
| `live`          | Needs a loaded vLLM/Ollama model (superset of integration)         | no               |
| `chaos`         | Chaos / resilience tests — weekly CI cron, not per-PR              | no               |

The default `pytest tests/` invocation runs unmarked tests only.

## How to opt in

```bash
# Run only integration tests
docker compose exec fastapi python -m pytest -q -m integration tests/

# Run everything except chaos and live
docker compose exec fastapi python -m pytest -q -m "not chaos and not live" tests/

# Single integration file (typical local debug loop)
docker compose exec fastapi python -m pytest -q -m integration \
    tests/test_alerts_inbox_integration.py
```

## How to author a new integration test

1. Add the module-level marker so every test in the file is gated:

    ```python
    pytestmark = pytest.mark.integration
    ```

2. Read DB connection info from env vars with sensible defaults:

    ```python
    PG_DSN = os.environ.get(
        "PG_DSN",
        "postgresql://georag:OMljaORhiA7RGQN3ilfemNWpezF9waU@localhost:5432/georag",
    )
    ```

   Inside the FastAPI container the host should be `postgresql`. The
   acceptance harness (`scripts/phase_h4_acceptance.sh`) sets this for you.

3. **Always SET ROLE** to `georag_app` when testing RLS — the default
   `georag` superuser bypasses every policy:

    ```python
    @pytest.fixture
    async def app_conn():
        conn = await asyncpg.connect(PG_DSN)
        try:
            await conn.execute("SET ROLE georag_app")
            yield conn
        finally:
            await conn.execute("RESET ROLE")
            await conn.close()
    ```

4. **Always clean up** in `finally:` — integration tests share the
   live DB with other tests and the dev stack. Use a per-test UUID
   tag for `target_id` / `name` so deletes are precise.

5. **Don't assume seed data**: skip when the prerequisite isn't
   present, never fail:

    ```python
    @pytest.fixture
    async def workspace_id(pg_conn):
        row = await pg_conn.fetchrow(
            "SELECT workspace_id::text AS w FROM silver.workspaces LIMIT 1",
        )
        if row is None:
            pytest.skip("silver.workspaces is empty — seed required")
        return row["w"]
    ```

## CI alignment

Per-PR CI runs unmarked + (eventually) `integration` against a service
container. `golden` and `hallucination` run pre-milestone-gate.
`chaos` runs on a weekly cron. `live` is local-only until self-hosted
runners come online (see Module 10 Chunk 10.2 in the architecture doc).

## Phase H4 integration suite — current inventory

| File                                                | Cases | Subject                                  |
|-----------------------------------------------------|-------|------------------------------------------|
| `test_alerts_inbox_integration.py`                  | 5     | Alerts inbox list + paginate + ack       |
| `test_report_section_drafts_integration.py`         | 8     | Section editor PUT + history             |
| `test_trg_geojson_integration.py`                   | 3     | TRG cockpit geojson endpoint             |
| `test_audit_chain_verify.py`                        | 5     | Hash-chain integrity verifier            |
| `test_workspace_settings_rls_integration.py`        | 4     | silver.workspace_settings RLS isolation  |
| **Total**                                           | **25**|                                          |

All 25 pass against the live Docker stack as of Phase H4 acceptance.
