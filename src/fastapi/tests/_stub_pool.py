"""A scriptable asyncpg pool/connection stand-in for router unit tests.

The routers under test build their SQL inline, so the stub routes each call
by the FIRST registered SQL substring that appears in the statement. Rows
are plain dicts: like an asyncpg ``Record``, a dict raises ``KeyError`` on a
key the query never selected — which is exactly the bug class these tests
exist to catch (trust-summary read ``lifecycle_state`` off a row that only
had ``state``).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any


class StubConn:
    """Answers fetch/fetchrow/fetchval by SQL substring; records everything."""

    def __init__(
        self,
        *,
        fetch: list[tuple[str, list[dict[str, Any]]]] | None = None,
        fetchrow: list[tuple[str, dict[str, Any] | None]] | None = None,
        fetchval: list[tuple[str, Any]] | None = None,
    ) -> None:
        self._fetch = fetch or []
        self._fetchrow = fetchrow or []
        self._fetchval = fetchval or []
        self.calls: list[tuple[str, str, tuple[Any, ...]]] = []
        self.executed: list[tuple[str, tuple[Any, ...]]] = []
        self.in_transaction = False

    @staticmethod
    def _route(table: list[tuple[str, Any]], sql: str, kind: str) -> Any:
        for needle, result in table:
            if needle in sql:
                return result
        raise AssertionError(f"StubConn: no {kind} route matches SQL:\n{sql}")

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.calls.append(("fetch", sql, args))
        return list(self._route(self._fetch, sql, "fetch"))

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        self.calls.append(("fetchrow", sql, args))
        return self._route(self._fetchrow, sql, "fetchrow")

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self.calls.append(("fetchval", sql, args))
        return self._route(self._fetchval, sql, "fetchval")

    async def execute(self, sql: str, *args: Any) -> str:
        self.executed.append((sql, args))
        return "OK"

    def is_in_transaction(self) -> bool:
        return self.in_transaction

    @asynccontextmanager
    async def _tx(self):  # type: ignore[no-untyped-def]
        self.in_transaction = True
        try:
            yield self
        finally:
            self.in_transaction = False

    def transaction(self):  # type: ignore[no-untyped-def]
        return self._tx()

    def sql_for(self, needle: str) -> list[str]:
        return [sql for _kind, sql, _args in self.calls if needle in sql]

    def workspace_guc_values(self) -> list[Any]:
        """Values bound to ``app.workspace_id`` via set_config on this conn."""
        return [
            args[0]
            for sql, args in self.executed
            if "set_config('app.workspace_id'" in sql and args
        ]


class StubPool:
    """``pool.acquire()`` yields the one StubConn."""

    def __init__(self, conn: StubConn) -> None:
        self.conn = conn

    @asynccontextmanager
    async def _acquire(self):  # type: ignore[no-untyped-def]
        yield self.conn

    def acquire(self):  # type: ignore[no-untyped-def]
        return self._acquire()


class FailingPool:
    """``pool.acquire()`` raises — a Postgres outage."""

    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def acquire(self):  # type: ignore[no-untyped-def]
        raise self.exc
