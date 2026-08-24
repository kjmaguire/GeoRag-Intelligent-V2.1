"""`_fire_fallback_metric` used to raise instead of counting.

THE BUG, AND HOW IT WAS FOUND
    mypy reported `app/services/qdrant_fallback.py:223: "None" has no
    attribute "labels"`. It was one line among 850 under `strict = true`,
    where 207 were `type-arg` and 146 `import-untyped` -- a wall nobody
    reads. Under the reachable baseline the same run reports 286, and this
    line is legible.

    The function guarded its lazy singleton with a NameError check while
    the module also assigned `_QDRANT_FALLBACK_TOTAL = None` at import, so
    the name always existed, NameError never fired, the Counter was never
    built, and `.labels` was called on None. Verified against a faithful
    reproduction of the old code: it raises AttributeError, which the
    enclosing `except ImportError` does not catch, so it reaches the
    caller.

    Latent, not live: the function has no callers today, the same as
    `safe_hybrid_query` in the same module. It would have become live on
    the first Qdrant outage after someone wired the fallback up -- turning
    a degraded-but-working query path into a crash, in the code whose only
    job is to keep working when Qdrant is down.

WHY THE TESTS BELOW LOOK LIKE THIS
    A metrics helper has exactly two obligations: count, and never be the
    reason a request fails. So there is a test for each, and the second
    one matters more.
"""
from __future__ import annotations

import pytest

from app.services import qdrant_fallback

# NO autouse reset fixture, deliberately, and this cost a debugging round.
#
# The obvious fixture -- capture _QDRANT_FALLBACK_TOTAL, restore it after
# each test -- puts the sentinel back to None, so the NEXT test rebuilds a
# Counter with a name already in prometheus_client's process-global
# REGISTRY and gets "Duplicated timeseries in CollectorRegistry". The
# singleton persisting across tests IS the production behaviour, so the
# assertions below are deltas rather than absolutes.


def _value(counter, collection: str) -> float:
    for metric in counter.collect():
        for sample in metric.samples:
            if (sample.name.endswith("_total")
                    and sample.labels.get("collection") == collection):
                return sample.value
    return 0.0


def test_it_increments_instead_of_raising() -> None:
    """The whole bug in one line: this call used to be an AttributeError."""
    qdrant_fallback._fire_fallback_metric("georag_chunks")

    counter = qdrant_fallback._QDRANT_FALLBACK_TOTAL
    assert counter is not None, "the Counter was never constructed"
    before = _value(counter, "georag_chunks")

    qdrant_fallback._fire_fallback_metric("georag_chunks")

    assert _value(counter, "georag_chunks") == before + 1


def test_repeated_calls_do_not_re_register() -> None:
    """The singleton is the point. Constructing the Counter twice raises
    Duplicated timeseries out of the global registry, which would turn a
    working metric into an exception on the second Qdrant outage."""
    qdrant_fallback._fire_fallback_metric("repeat_check")
    before = _value(qdrant_fallback._QDRANT_FALLBACK_TOTAL, "repeat_check")

    qdrant_fallback._fire_fallback_metric("repeat_check")
    qdrant_fallback._fire_fallback_metric("repeat_check")

    after = _value(qdrant_fallback._QDRANT_FALLBACK_TOTAL, "repeat_check")
    assert after == before + 2


def test_each_collection_counts_separately() -> None:
    qdrant_fallback._fire_fallback_metric("collection_a")
    counter = qdrant_fallback._QDRANT_FALLBACK_TOTAL
    a_before = _value(counter, "collection_a")
    b_before = _value(counter, "collection_b")

    qdrant_fallback._fire_fallback_metric("collection_b")
    qdrant_fallback._fire_fallback_metric("collection_b")

    assert _value(counter, "collection_a") == a_before
    assert _value(counter, "collection_b") == b_before + 2


def test_a_missing_prometheus_client_is_survivable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """prometheus_client is optional, and the ImportError branch is the
    reason the try/except exists at all."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "prometheus_client":
            raise ImportError("no prometheus_client")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.setattr(qdrant_fallback, "_QDRANT_FALLBACK_TOTAL", None)

    qdrant_fallback._fire_fallback_metric("georag_chunks")  # must not raise


def test_the_name_guard_did_not_come_back() -> None:
    """`try: NAME except NameError` cannot work for a name the module
    assigns at import. If it reappears, so does the AttributeError.

    Asserted on the source rather than by behaviour because the behaviour
    only diverges on the very first call in a fresh process, which is
    exactly the case a test suite does not get to observe.
    """
    import ast
    import inspect
    import textwrap

    # The docstring QUOTES the old broken guard to explain it, so a raw
    # substring search over the source matches its own explanation. Strip
    # the docstring and look at the code.
    tree = ast.parse(textwrap.dedent(
        inspect.getsource(qdrant_fallback._fire_fallback_metric)))
    function = tree.body[0]
    assert isinstance(function, ast.FunctionDef)
    if (function.body and isinstance(function.body[0], ast.Expr)
            and isinstance(function.body[0].value, ast.Constant)):
        function.body = function.body[1:]
    source = ast.unparse(function)

    assert "except NameError" not in source, (
        "the NameError guard is back -- the module assigns the sentinel at "
        "import, so NameError can never fire and the Counter is never built"
    )
    assert "is None" in source, (
        "the guard must test the VALUE; that is what the sentinel encodes"
    )


def test_the_sentinel_is_declared_before_the_function() -> None:
    """Ordering was load-bearing for the bug's invisibility: the sentinel
    sat thirty lines BELOW the code whose correctness depended on it."""
    import inspect

    module_source = inspect.getsource(qdrant_fallback)
    sentinel = module_source.index("_QDRANT_FALLBACK_TOTAL: Any = None")
    function = module_source.index("def _fire_fallback_metric")

    assert sentinel < function, (
        "the sentinel moved back below the function that depends on it"
    )
