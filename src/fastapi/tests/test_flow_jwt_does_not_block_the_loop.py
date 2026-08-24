"""An auth check must not stop the event loop, and must not fail open.

WHAT IT USED TO DO
    `verify_flow_jwt_token` is called from an `async def` route. Its key
    lookup was a sync function that detected the running loop and, rather
    than refusing, spawned a single-worker ThreadPoolExecutor per call and
    blocked on `future.result(timeout=10)`.

    CLAUDE.md hard rule 2 calls that a blocker. The 10-second cap was not
    even a bound: the executor is used as a context manager, so on the
    timeout path `__exit__` runs `shutdown(wait=True)` and the loop keeps
    blocking until the worker's connect / query / close finishes. Every
    cache miss — once per flow per worker per 60s TTL, and always after a
    restart — froze every concurrent SSE stream on that worker.

    The bridge existed only because the function signature was sync. The
    fetch underneath was always an asyncpg coroutine.

AND THE PART THAT MATTERED MORE
    `except Exception: return []` collapsed "the database is unreachable",
    "AUDIT_ENCRYPTION_KEY is missing" and "this flow has no per-flow key"
    into one value. The caller then verified against the global env-var
    key with nothing logged — an auth downgrade triggered by an outage,
    indistinguishable from normal operation.
"""
from __future__ import annotations

import ast
import asyncio
import inspect

import pytest

from app.services import flow_jwt


def _code_only(module) -> str:
    """A module's source with comments and docstrings removed.

    Every fix in this file leaves a comment quoting the construct it
    replaced, so a plain source grep matches its own explanation and the
    test passes for the wrong reason. It has already happened three times
    in this audit; `ast.unparse` drops comments outright, and the walk
    strips the docstrings that survive as string expressions.
    """
    tree = ast.parse(inspect.getsource(module))

    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = getattr(node, "body", [])
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            node.body = body[1:] or [ast.Pass()]

    return ast.unparse(ast.fix_missing_locations(tree))


class TestTheSyncWrapperRefusesToBridge:
    def test_it_raises_inside_a_running_loop(self) -> None:
        """Refusing is the whole point.

        Bridging is what blocked the loop. A caller that reaches this
        from async code has an async path available and should be told
        so, loudly, rather than quietly costing a DB round trip of
        stalled service.
        """

        async def _call_from_async() -> None:
            flow_jwt._load_per_flow_keys_sync("some-flow")

        with pytest.raises(RuntimeError, match="running event loop"):
            asyncio.run(_call_from_async())

    def test_the_error_names_the_alternative(self) -> None:
        """An error that does not say what to do instead gets worked
        around, usually by restoring the bridge."""

        async def _call_from_async() -> None:
            flow_jwt._load_per_flow_keys_sync("some-flow")

        with pytest.raises(RuntimeError) as excinfo:
            asyncio.run(_call_from_async())

        assert "_aget_per_flow_keys" in str(excinfo.value)

    def test_no_thread_pool_bridge_remains(self) -> None:
        code = _code_only(flow_jwt)

        assert "ThreadPoolExecutor" not in code, (
            "the thread bridge is back; this is what blocked the event loop"
        )
        assert "future.result(timeout=" not in code


class TestLookupFailureIsNotAnEmptyList:
    def test_a_connect_failure_raises_rather_than_returning_empty(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`[]` means "this flow has no per-flow keys", which is a normal
        state that falls back to the env-var key. An outage must not be
        able to produce it."""
        monkeypatch.setenv("AUDIT_ENCRYPTION_KEY", "x" * 32)

        async def _boom(*_args, **_kwargs):
            raise OSError("connection refused")

        monkeypatch.setattr(flow_jwt.asyncpg, "connect", _boom)

        with pytest.raises(flow_jwt.FlowKeyLookupError):
            asyncio.run(flow_jwt._fetch_per_flow_keys("some-flow"))

    def test_a_missing_encryption_key_is_still_an_empty_list(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Distinct from a failure. A deployment with no per-flow keys
        provisioned never sets AUDIT_ENCRYPTION_KEY, and the env-var
        fallback is the intended behaviour there — so this one must NOT
        become a 503."""
        monkeypatch.delenv("AUDIT_ENCRYPTION_KEY", raising=False)

        assert asyncio.run(flow_jwt._fetch_per_flow_keys("some-flow")) == []

    def test_the_blanket_swallow_is_gone(self) -> None:
        """A bare `except Exception: return []` anywhere in this module
        recreates the silent auth downgrade."""
        tree = ast.parse(inspect.getsource(flow_jwt))

        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            broad = node.type is None or (
                isinstance(node.type, ast.Name) and node.type.id == "Exception"
            )
            if not broad:
                continue
            returns_empty = any(
                isinstance(stmt, ast.Return)
                and isinstance(stmt.value, ast.List)
                and not stmt.value.elts
                for stmt in node.body
            )
            if returns_empty:
                offenders.append(node.lineno)

        assert offenders == [], (
            f"lines {offenders}: a broad handler returning [] makes an "
            "unreachable database indistinguishable from 'this flow has no "
            "per-flow keys', and the caller then verifies against the global "
            "env-var key with nothing logged"
        )


class TestTheAsyncPathIsTheOneTheRouteTakes:
    def test_the_verifier_the_router_imports_is_a_coroutine(self) -> None:
        assert inspect.iscoroutinefunction(flow_jwt.averify_flow_jwt_token)

    def test_the_router_awaits_it(self) -> None:
        from app.routers import integrations_trigger

        source = inspect.getsource(integrations_trigger)

        assert "await averify_flow_jwt_token(" in source
        assert "async def _check_trigger_auth" in source
        assert "await _check_trigger_auth(" in source

    def test_the_sync_verifier_still_exists_for_cli_callers(self) -> None:
        """The rotation CLI and the smoke scripts are genuinely sync.
        Removing their path would have been a different bug."""
        assert callable(flow_jwt.verify_flow_jwt_token)
        assert not inspect.iscoroutinefunction(flow_jwt.verify_flow_jwt_token)

    def test_both_verifiers_share_one_implementation(self) -> None:
        """Two copies of a JWT check drift, and the drift is a security
        difference rather than a behavioural one."""
        code = _code_only(flow_jwt)

        assert code.count("def _verify_with_keys(") == 1
        assert code.count("_verify_with_keys(token, expected_flow_name") == 2


class TestTheCacheStillWorks:
    def test_a_fresh_entry_is_served_without_a_fetch(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls = []

        async def _counting_fetch(flow_name: str):
            calls.append(flow_name)
            return [("kid-1", "secret-1")]

        monkeypatch.setattr(flow_jwt, "_fetch_per_flow_keys", _counting_fetch)
        flow_jwt._per_flow_cache.clear()

        async def _twice():
            first = await flow_jwt._aget_per_flow_keys("f")
            second = await flow_jwt._aget_per_flow_keys("f")
            return first, second

        first, second = asyncio.run(_twice())

        assert first == second == [("kid-1", "secret-1")]
        assert len(calls) == 1, "the second call should have hit the cache"

        flow_jwt._per_flow_cache.clear()

    def test_an_expired_entry_is_refetched(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls = []

        async def _counting_fetch(flow_name: str):
            calls.append(flow_name)
            return [("kid-1", "secret-1")]

        monkeypatch.setattr(flow_jwt, "_fetch_per_flow_keys", _counting_fetch)
        monkeypatch.setattr(flow_jwt, "_PER_FLOW_KEY_TTL_SECONDS", 0)
        flow_jwt._per_flow_cache.clear()

        async def _twice():
            await flow_jwt._aget_per_flow_keys("f")
            await flow_jwt._aget_per_flow_keys("f")

        asyncio.run(_twice())

        assert len(calls) == 2

        flow_jwt._per_flow_cache.clear()
