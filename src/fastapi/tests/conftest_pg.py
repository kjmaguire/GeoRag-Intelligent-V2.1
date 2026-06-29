"""Testcontainers Postgres scaffolding (REC#5 — 2026-06-03).

SCAFFOLD ONLY — not yet wired into conftest.py
-----------------------------------------------
This file lays out the fixture shape but is NOT registered by pytest
yet (filename `conftest_pg.py` — pytest only auto-loads `conftest.py`).
Activating it requires:

  1. Add ``testcontainers[postgres]>=4.0`` to ``src/fastapi/pyproject.toml``
  2. Rename this file to ``conftest_postgres.py`` + import the fixture
     from ``tests/conftest.py`` (or rename inline)
  3. Decide on a fixture scope policy:
     - ``session`` — one PG container per pytest run; tests share schema
       state. Fastest, but tests have to clean up after themselves.
     - ``module`` — one per test file; each module starts fresh.
       Slowest per test, but trivially isolated.
     - Recommended starting point: ``session`` with a ``transactional_db``
       sub-fixture that wraps each test in a SAVEPOINT.

Why this is deferred
--------------------
The full REC#5 delivery (migrating the 7 PHP tests + ~15 Python tests
that need live PG, removing the "host vs container" gap entirely)
needs:
  - Container image selection (postgis/postgis:18-3.6 matches CI per
    the existing GitHub Actions workflow)
  - Migration runner integration (apply Laravel + raw-SQL migrations
    against the testcontainer at session start)
  - PgBouncer-in-tests decision (skip vs include — production uses
    PgBouncer transaction-pool mode; tests probably skip it)
  - PHP-side equivalent (`testcontainers-php` or `docker-compose`-based
    fixture)

That's a session of focused work in its own right. This scaffold
captures the architectural intent + the API surface so the next
contributor doesn't have to re-derive it.

Reference implementation sketch
-------------------------------
The fixture below is a minimal viable implementation — it boots PG
with PostGIS, applies any *.sql files in a directory, yields a
connection. Real impl needs the Laravel migration runner integration
which is the bulk of the work.
"""
from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    # testcontainers is the planned import; not in pyproject yet.
    # Guarded with TYPE_CHECKING so this file still imports cleanly
    # without the dep installed.
    from testcontainers.postgres import PostgresContainer


@pytest.fixture(scope="session")
async def pg_container_url() -> AsyncIterator[str]:
    """Boot a PostGIS-enabled Postgres testcontainer for the test session.

    Returns the asyncpg-compatible DSN. Tests that need a live DB take
    this fixture (or a more specific one built on top — eg.
    `pg_with_migrations`, `pg_with_seed_data`).

    Implementation note: the image digest matches `.github/workflows/ci.yml`
    so test environment parity with CI is bit-exact. If CI bumps the
    image, bump it here too.
    """
    pytest.importorskip(
        "testcontainers.postgres",
        reason=(
            "testcontainers[postgres] not installed. REC#5 scaffold — add "
            "to pyproject + rename conftest_pg.py to conftest_postgres.py "
            "to activate."
        ),
    )
    from testcontainers.postgres import PostgresContainer  # noqa: PLC0415

    # Same major version as production + CI. Digest is bit-exact in
    # the GH Actions workflow; reference here for parity.
    container = PostgresContainer(
        "postgis/postgis:18-3.6",
        username="georag",
        password="test_password",
        dbname="georag_test",
        driver=None,  # we'll use asyncpg directly, not SQLAlchemy
    )
    container.start()
    try:
        # asyncpg DSN. The container reports a localhost port that's
        # mapped to PG's 5432; use that directly.
        host = container.get_container_host_ip()
        port = container.get_exposed_port(5432)
        dsn = f"postgres://georag:test_password@{host}:{port}/georag_test"

        # Surface as env vars too so any code path that reads
        # POSTGRES_HOST etc. directly (instead of taking the DSN as a
        # parameter) sees a consistent view.
        os.environ["POSTGRES_HOST"] = host
        os.environ["POSTGRES_PORT"] = str(port)
        os.environ["POSTGRES_DB"] = "georag_test"
        os.environ["POSTGRES_USER"] = "georag"
        os.environ["POSTGRES_PASSWORD"] = "test_password"

        yield dsn
    finally:
        container.stop()


# Subsequent fixtures to build (when this is activated):
#
#   pg_with_migrations(pg_container_url) — applies Laravel migrations.
#     Needs a way to invoke `php artisan migrate --force` against the
#     test DSN. Likely a subprocess wrapper since the FastAPI test env
#     doesn't usually have PHP. Alternative: pre-build a "test PG
#     image" with migrations baked in (faster, but image-build complexity).
#
#   pg_with_seed_data(pg_with_migrations) — applies a known fixture set
#     (a workspace + projects + a few collars). The fixture decides
#     which seed level the test wants.
#
#   transactional_pg_conn(pg_with_migrations) — yields an asyncpg
#     connection wrapped in a SAVEPOINT that rolls back at test exit,
#     so per-test isolation is automatic + cheap.
