"""L1546 — LLM spend must be a recorded fact, not an assumption.

What was measured on the live deployment, 2026-08-21:

  * fastapi-cc has `LLM_BACKEND=azure` and
    `AZURE_FOUNDRY_DEPLOYMENT=Cohere-command-a-plus-05-2026`, so
    `_call_openai_compatible_llm` is the production path. Cost accounting
    lived only in `_call_anthropic_llm`, which never runs there.
  * `select count(*) from usage.usage_events` returned **0**. Not "the
    chat path doesn't write" — nothing writes. `cost_burn_watcher` sums
    that table every five minutes to decide whether to alert and, at 2x
    the hourly ceiling, to suspend a workspace. Summing an empty table,
    neither branch could ever fire.
  * `silver.answer_runs` held 1 row with `count(input_tokens) = 0`. Those
    columns have existed since 2026-04-21 and `llm_calls.py:75-80` claims
    the orchestrator reads `get_run_token_usage()` "immediately before the
    answer_runs INSERT" — which named 15 columns, neither of them tokens.

Two mechanisms are load-bearing and each has its own tests below.

**Unpriced models must not produce priced rows.** `_PRICE_TABLE` has no
entry for the production model, so `estimate_cost_usd` returns the
STANDARD-tier Sonnet fallback for 100% of production traffic. A dashboard
can live with an approximation; a hard stop that suspends a customer
workspace cannot. Persisted cost is 0 when `has_pricing()` is False, and
`cost_burn_watcher`'s `HAVING SUM(projected_cost_usd) > 0` then skips the
workspace instead of acting on a number nobody computed.

**Token totals travel on the graph state, not on a contextvar.** LangGraph
gives each node its own `asyncio.Task`, and a Task receives a COPY of the
context — so a `ContextVar.set()` in assemble_node is invisible in
persist_node. `test_a_contextvar_set_in_one_node_is_invisible_in_the_next`
pins that behaviour, because it is the entire reason the accounting is
shaped the way it is. If a future LangGraph release changed it, the
simpler contextvar read would become correct and that test would say so.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from app.agent.pricing import estimate_cost_usd, has_pricing

FASTAPI_ROOT = Path(__file__).resolve().parents[1]
NODES_SRC = FASTAPI_ROOT / "app" / "agent" / "agentic_retrieval" / "nodes.py"
LLM_SRC = FASTAPI_ROOT / "app" / "agent" / "llm_calls.py"


# ---------------------------------------------------------------------------
# Pricing honesty
# ---------------------------------------------------------------------------


def test_the_production_model_is_reported_as_unpriced():
    # This is not a wish — it is the state of the price table. If someone
    # adds the Cohere rate, this test tells them to revisit the callers
    # that currently record 0.
    assert not has_pricing("Cohere-command-a-plus-05-2026"), (
        "Cohere-command-a-plus-05-2026 now has a rate. Good — but check "
        "that the 0-cost branches in _write_chat_usage_event and "
        "_call_openai_compatible_llm are still what you want."
    )


def test_known_models_are_still_priced():
    assert has_pricing("claude-sonnet-4-6")
    assert has_pricing("Qwen/Qwen3-14B-AWQ")


def test_estimate_still_falls_back_for_dashboards():
    # `estimate_cost_usd` keeps its approximating behaviour on purpose —
    # an approximate $/hour panel beats a blank one. `has_pricing` is the
    # guard for anything that PERSISTS or ACTS on the number.
    approximate = estimate_cost_usd(
        model="some-model-nobody-registered",
        input_tokens=1_000_000,
        output_tokens=0,
    )
    assert approximate == pytest.approx(3.00), "STANDARD-tier fallback rate"


def test_the_unpriced_warning_fires_once_per_model(caplog):
    import app.agent.pricing as pricing

    pricing._UNPRICED_SEEN.discard("brand-new-model-x")

    with caplog.at_level("WARNING", logger="app.agent.pricing"):
        estimate_cost_usd(model="brand-new-model-x", input_tokens=10, output_tokens=10)
        estimate_cost_usd(model="brand-new-model-x", input_tokens=10, output_tokens=10)

    hits = [r for r in caplog.records if "brand-new-model-x" in r.getMessage()]
    assert len(hits) == 1, (
        "A hot path must not log per call — but it must log at WARNING at "
        f"least once. Got {len(hits)} records."
    )
    assert hits[0].levelname == "WARNING", (
        "This was INFO, which is why nobody noticed that the model serving "
        "all production traffic has no published rate."
    )


# ---------------------------------------------------------------------------
# The production LLM path now costs its own calls
# ---------------------------------------------------------------------------


def test_the_openai_compatible_path_records_cost_and_output_tokens():
    source = LLM_SRC.read_text(encoding="utf-8")
    tree = ast.parse(source)

    fn = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "_call_openai_compatible_llm"
    )
    body = ast.get_source_segment(source, fn) or ""

    assert "LLM_TOKENS_OUTPUT" in body, (
        "The production path must count output tokens; only the dead "
        "Anthropic branch did."
    )
    assert "LLM_COST_USD" in body
    assert "has_pricing(" in body, (
        "Cost must be gated on a published rate — otherwise every "
        "production sample is Sonnet-priced fiction."
    )


def test_the_openai_compatible_path_takes_the_user_id():
    sig = inspect.signature(
        __import__("app.agent.llm_calls", fromlist=["x"])._call_openai_compatible_llm
    )
    assert "user_id" in sig.parameters, (
        "Without user_id every LLM_COST_USD sample collapses into the "
        '"unknown" bucket, which defeats per-user cost attribution.'
    )


def test_the_dispatcher_passes_the_user_id_through():
    source = LLM_SRC.read_text(encoding="utf-8")
    call_start = source.index("return await _call_openai_compatible_llm(")
    call_block = source[call_start:call_start + 700]
    assert "user_id=user_id" in call_block


# ---------------------------------------------------------------------------
# Why the totals ride on the state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_contextvar_set_in_one_node_is_invisible_in_the_next():
    """The measurement that dictated the design.

    If this ever fails — i.e. LangGraph starts running nodes in the
    caller's context — then `get_run_token_usage()` could simply be read
    in persist_node and `_fold_token_usage` becomes unnecessary machinery.
    Until then, reading the contextvar at persist time writes a confident,
    permanent zero.
    """
    import contextvars

    from langgraph.graph import END, StateGraph
    from typing_extensions import TypedDict

    probe: contextvars.ContextVar[int] = contextvars.ContextVar("probe", default=0)

    class S(TypedDict, total=False):
        n: int

    async def node_a(state):
        probe.set(41)
        return {"n": 1}

    async def node_b(state):
        return {"n": probe.get()}

    g = StateGraph(S)
    g.add_node("a", node_a)
    g.add_node("b", node_b)
    g.set_entry_point("a")
    g.add_edge("a", "b")
    g.add_edge("b", END)

    out = await g.compile().ainvoke({"n": 0})

    assert out["n"] == 0, (
        "LangGraph now propagates contextvars across nodes. _fold_token_usage "
        "can be replaced by a single get_run_token_usage() call in persist_node."
    )


def test_fold_token_usage_accumulates_across_nodes():
    from app.agent.agentic_retrieval.nodes import _fold_token_usage
    from app.agent.llm_calls import add_token_usage, reset_run_token_usage

    class _State:
        llm_input_tokens = 100
        llm_output_tokens = 20

    reset_run_token_usage()
    add_token_usage(7, 3)

    folded = _fold_token_usage(_State())

    assert folded == {"llm_input_tokens": 107, "llm_output_tokens": 23}

    reset_run_token_usage()


def test_fold_token_usage_is_applied_at_every_llm_capable_node():
    """A new LLM-calling node that forgets to fold silently under-bills."""
    source = NODES_SRC.read_text(encoding="utf-8")
    tree = ast.parse(source)

    calls_llm: set[str] = set()
    folds: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        body = ast.get_source_segment(source, node) or ""
        if "_fold_token_usage(state)" in body:
            folds.add(node.name)
        if "_call_llm(" in body or "classify_intent(" in body:
            calls_llm.add(node.name)

    missing = {
        name
        for name in calls_llm
        if name.endswith("_node") and name not in folds
    }
    assert not missing, (
        f"These nodes can spend tokens but never fold them onto the run "
        f"total, so their spend is invisible: {sorted(missing)}"
    )
    # repair_shadow_node reaches the LLM indirectly, via _run_repair_loop.
    assert "repair_shadow_node" in folds


# ---------------------------------------------------------------------------
# What persist_node writes
# ---------------------------------------------------------------------------


def test_answer_runs_insert_carries_the_token_columns():
    source = NODES_SRC.read_text(encoding="utf-8")
    insert_at = source.index("INSERT INTO silver.answer_runs")
    stmt = source[insert_at:source.index("RETURNING answer_run_id", insert_at)]

    assert "input_tokens" in stmt and "output_tokens" in stmt, (
        "These columns existed for four months and were never written."
    )
    # 16 bind parameters, one of which is the literal 0 for
    # workspace_data_version_at_query — check the highest placeholder so a
    # column/parameter mismatch fails here rather than at runtime.
    assert "$16" in stmt
    assert "$17" not in stmt


def test_answer_runs_records_the_answering_model_not_the_configured_one():
    source = NODES_SRC.read_text(encoding="utf-8")

    assert "_answering_model" in source
    assert 'getattr(state.response, "llm_model", None)' in source, (
        "model_name must come from the model that actually answered. The "
        "identical bug on audit.query_audit_log.llm_model was fixed on "
        "2026-08-21; answer_runs was its second home."
    )


def test_the_usage_event_is_written_outside_the_lineage_try():
    """Spend is real even when the lineage row is lost.

    `_insert_answer_run_with_retry` re-raises after three attempts. With
    the usage write inside that `try`, a PgBouncer flap would lose both
    the lineage row AND the cost record for the same query.
    """
    source = NODES_SRC.read_text(encoding="utf-8")
    tree = ast.parse(source)

    fn = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "persist_node"
    )

    call_lines = [
        n.lineno
        for n in ast.walk(fn)
        if isinstance(n, ast.Await)
        and isinstance(n.value, ast.Call)
        and getattr(n.value.func, "id", None) == "_write_chat_usage_event"
    ]
    assert len(call_lines) == 1, "expected exactly one usage write"

    try_ranges = [
        (n.lineno, max(getattr(c, "lineno", n.lineno) for c in ast.walk(n)))
        for n in ast.walk(fn)
        if isinstance(n, ast.Try)
        and any(
            isinstance(x, ast.Call)
            and getattr(x.func, "id", None) == "_insert_answer_run_with_retry"
            for x in ast.walk(n)
        )
    ]
    assert try_ranges, "could not locate the answer_runs try block"

    line = call_lines[0]
    for start, end in try_ranges:
        assert not (start <= line <= end), (
            "The usage write sits inside the answer_runs try block, so a "
            "failed lineage INSERT also loses the cost record."
        )


@pytest.mark.asyncio
async def test_usage_event_records_zero_cost_for_an_unpriced_model():
    from app.agent.agentic_retrieval.nodes import _write_chat_usage_event

    captured: dict[str, object] = {}

    class _Conn:
        async def execute(self, sql, *args):
            captured["sql"] = sql
            captured["args"] = args

    class _Acquire:
        async def __aenter__(self):
            return _Conn()

        async def __aexit__(self, *exc):
            return False

    class _Pool:
        def acquire(self):
            return _Acquire()

    await _write_chat_usage_event(
        _Pool(),
        workspace_id="11111111-1111-1111-1111-111111111111",
        model_id="Cohere-command-a-plus-05-2026",
        backend="azure",
        input_tokens=12_000,
        output_tokens=800,
        latency_ms=4200,
        trace_id="4bf92f3577b34da6a3ce929d0e0e4736",
        answer_run_id="22222222-2222-2222-2222-222222222222",
    )

    args = captured["args"]
    assert "INSERT INTO usage.usage_events" in str(captured["sql"])
    assert args[1] == "chat_rag"
    assert args[5] == 12_000, "tokens_prompt is a fact and must be recorded"
    assert args[6] == 800
    assert args[7] == 0.0, (
        "projected_cost_usd must stay 0 for an unpriced model. "
        "cost_burn_watcher suspends a workspace at 2x its ceiling; it must "
        "never act on a Sonnet-priced guess about a Cohere deployment."
    )
    assert args[10] == "4bf92f3577b34da6a3ce929d0e0e4736", "trace_id joins to Laravel"


@pytest.mark.asyncio
async def test_usage_event_records_real_cost_for_a_priced_model():
    from app.agent.agentic_retrieval.nodes import _write_chat_usage_event

    captured: dict[str, object] = {}

    class _Conn:
        async def execute(self, sql, *args):
            captured["args"] = args

    class _Acquire:
        async def __aenter__(self):
            return _Conn()

        async def __aexit__(self, *exc):
            return False

    class _Pool:
        def acquire(self):
            return _Acquire()

    await _write_chat_usage_event(
        _Pool(),
        workspace_id="11111111-1111-1111-1111-111111111111",
        model_id="claude-sonnet-4-6",
        backend="anthropic",
        input_tokens=1_000_000,
        output_tokens=0,
        latency_ms=100,
        trace_id=None,
        answer_run_id=None,
    )

    assert captured["args"][7] == pytest.approx(3.00)


@pytest.mark.asyncio
async def test_a_failed_usage_write_does_not_break_the_answer_path():
    from app.agent.agentic_retrieval.nodes import _write_chat_usage_event

    class _Pool:
        def acquire(self):
            raise RuntimeError("pool exhausted")

    # The answer has already been streamed to the user by this point.
    await _write_chat_usage_event(
        _Pool(),
        workspace_id=None,
        model_id="claude-sonnet-4-6",
        backend="anthropic",
        input_tokens=1,
        output_tokens=1,
        latency_ms=1,
        trace_id=None,
        answer_run_id=None,
    )


def test_cost_burn_watcher_skips_zero_cost_rows():
    """The safety property that makes recording 0 the right call.

    An unpriced model contributes token counts without ever pushing a
    workspace over a dollar threshold.
    """
    watcher = (
        FASTAPI_ROOT / "app" / "hatchet_workflows" / "cost_burn_watcher.py"
    ).read_text(encoding="utf-8")

    assert "HAVING SUM(projected_cost_usd) > 0" in watcher, (
        "Zero-cost rows must be excluded from the burn sum, otherwise "
        "recording 0 for unpriced models would still enter the alert and "
        "suspension logic."
    )
