"""Locks the concurrency contract on the cron workflows that declare one.

The workflows here are per-workspace singletons: same-workspace runs queue
(GROUP_ROUND_ROBIN), different-workspace runs proceed in parallel. This
prevents the every-10-min safety-net cron from racing the daily bulk
sync — and prevents a Hatchet retry from clobbering an in-flight run.

2026-08-21 — this file used to assert the expression as an exact string,
which is precisely why the bug below survived a year of green tests: the
string was stable, and wrong.

A declarative `on_crons` trigger sends NO input. The Python SDK hardcodes
`cron_input=None` (hatchet_sdk/runnables/workflow.py:257), so the engine
evaluates the CEL concurrency expression against `{}`. An expression that
dereferences `input.workspace_id` therefore hits an ABSENT key, cel-go
raises `no such key: workspace_id`, and the engine writes the run straight
to FAILED before any worker is involved (pkg/repository/task.go:2103).
No worker log line, no retry, nothing to notice. `embed_pending_passages`
logged 93 runs against ~2,610 expected over 18 days; every one of the 93
was an inline dispatch from `ingest_pdf.persist`, never a cron tick.

The old `input.workspace_id != ''` guard tested for EMPTY, not MISSING, so
it never fired. `has(input.workspace_id)` is the absence-safe form; the
engine's own suite covers this exact shape (internal/cel/cel_test.go:40).

So these tests assert the *property* — absence-safety — rather than a
literal, and the source-level guard at the bottom extends it to any cron
workflow added later.
"""
from __future__ import annotations

import ast
import pathlib
import re

import pytest
from hatchet_sdk import ConcurrencyLimitStrategy

# (module, attribute) for every workflow that declares a concurrency
# expression AND is cron-triggered.
CONCURRENT_CRON_WORKFLOWS = [
    ("app.hatchet_workflows.embed_pending_passages", "embed_pending_passages_wf"),
    ("app.hatchet_workflows.enrich_passage_context", "enrich_passage_context_wf"),
    ("app.hatchet_workflows.verbalize_page_images", "verbalize_page_images_wf"),
]


def _load(module_path: str, attr: str):
    import importlib

    return getattr(importlib.import_module(module_path), attr)


@pytest.mark.parametrize(("module_path", "attr"), CONCURRENT_CRON_WORKFLOWS)
def test_cron_workflow_concurrency_is_singleton_and_queues(module_path, attr):
    cfg = _load(module_path, attr).config

    assert cfg.concurrency is not None, (
        f"{attr} must declare concurrency — without it the cron can race a "
        f"long-running run."
    )
    assert cfg.concurrency.max_runs == 1, (
        f"Expected max_runs=1 (singleton per workspace), "
        f"got {cfg.concurrency.max_runs}"
    )
    assert cfg.concurrency.limit_strategy == ConcurrencyLimitStrategy.GROUP_ROUND_ROBIN, (
        f"Expected GROUP_ROUND_ROBIN (queue, do not cancel), "
        f"got {cfg.concurrency.limit_strategy}"
    )


@pytest.mark.parametrize(("module_path", "attr"), CONCURRENT_CRON_WORKFLOWS)
def test_cron_workflow_concurrency_expression_is_absence_safe(module_path, attr):
    """Every `input.X` in the expression must sit behind a `has(input.X)`.

    Without that the run is failed by the engine on every cron tick, and
    the failure is invisible from the worker side.
    """
    wf = _load(module_path, attr)
    expression = wf.config.concurrency.expression

    referenced = set(re.findall(r"input\.([A-Za-z_][A-Za-z0-9_]*)", expression))
    guarded = set(re.findall(r"has\(\s*input\.([A-Za-z_][A-Za-z0-9_]*)\s*\)", expression))

    assert referenced, (
        f"{attr}: expected the expression to key on some input field, "
        f"got {expression!r}"
    )
    assert referenced <= guarded, (
        f"{attr}: concurrency expression dereferences {sorted(referenced - guarded)} "
        f"without a has() guard, so every cron tick will be recorded FAILED by "
        f"the engine (no such key). Expression: {expression!r}"
    )


@pytest.mark.parametrize(("module_path", "attr"), CONCURRENT_CRON_WORKFLOWS)
def test_cron_workflow_input_validates_against_empty_cron_payload(module_path, attr):
    """A cron sends `{}`. If the input model rejects that, the run dies on
    the worker instead — a different symptom of the same broken contract.
    """
    wf = _load(module_path, attr)
    validator = wf.config.input_validator

    # Must not raise. Previously embed_pending_passages and
    # enrich_passage_context both declared workspace_id as required, so
    # every cron tick would have failed validation even once the CEL
    # expression was fixed.
    validated = validator.validate_python({})
    assert validated.project_id == "*", (
        f"{attr}: a cron payload should fan out over every project, "
        f"got project_id={validated.project_id!r}"
    )
    assert validated.workspace_id == "", (
        f"{attr}: the cron payload must leave workspace_id empty so the "
        f'project_id="*" path resolves it per project — a default tenant '
        f"UUID here would silently mis-scope every sweep (REC#1)."
    )


def test_no_new_cron_workflow_ships_an_absence_unsafe_expression():
    """Source-level guard, so a workflow added later cannot reintroduce this.

    Deliberately reads the files rather than importing them: importing every
    workflow module pulls in the whole app, and this needs to stay a cheap
    check that runs everywhere.
    """
    workflow_dir = pathlib.Path(__file__).resolve().parents[1] / "app" / "hatchet_workflows"
    offenders: list[str] = []

    for path in sorted(workflow_dir.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg}
            if "on_crons" not in kwargs or "concurrency" not in kwargs:
                continue

            # Pull the literal expression= out of the ConcurrencyExpression(...)
            concurrency = kwargs["concurrency"]
            if not isinstance(concurrency, ast.Call):
                continue
            expr_node = next(
                (kw.value for kw in concurrency.keywords if kw.arg == "expression"),
                None,
            )
            expression = _literal_str(expr_node)
            if expression is None:
                continue

            referenced = set(re.findall(r"input\.([A-Za-z_][A-Za-z0-9_]*)", expression))
            guarded = set(
                re.findall(r"has\(\s*input\.([A-Za-z_][A-Za-z0-9_]*)\s*\)", expression)
            )
            if referenced - guarded:
                offenders.append(f"{path.name}: {expression!r}")

    assert not offenders, (
        "Cron-triggered workflows whose concurrency expression dereferences an "
        "input field without a has() guard. A cron sends no input, so the engine "
        "records every tick as FAILED before dispatch and nothing appears in the "
        "worker log:\n  " + "\n  ".join(offenders)
    )


def _literal_str(node: ast.expr | None) -> str | None:
    """Resolve a str literal or an implicit concatenation of str literals."""
    if node is None:
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):  # f-string — not statically resolvable
        return None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _literal_str(node.left)
        right = _literal_str(node.right)
        return None if left is None or right is None else left + right
    return None
