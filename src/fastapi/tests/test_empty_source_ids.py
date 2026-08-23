"""A zero-row lookup must not read as evidence, whatever shape its id takes.

WHY A TEST AND NOT JUST A LONGER LIST
    Two second-line filters decide whether an answer is grounded: the IND-6
    ungrounded-answer guard in the assembler, and
    `_count_independent_sources` in the confidence computer. Both see only
    the `source_chunk_id` STRING, so both need a string predicate.

    That predicate was a frozenset of three literals -- `no-tool-call`,
    `georag_reports:empty`, `pg_public_geoscience:empty` -- while
    `_extract_source_id` mints eleven distinct ids for zero-row results.
    A missed hole lookup (`silver.collars:miss`), an assay query that found
    nothing (`silver.samples:element=U3O8:count=0`), an empty graph
    traversal (`neo4j:count=0`): all read as real evidence.

    Adding eight more literals would fix today and rot tomorrow. What holds
    the line is below: the covered types are READ OUT OF
    `_is_empty_tool_result`'s own isinstance branches, an empty instance of
    each is built from its dataclass fields, and the string predicate is
    required to agree with the object-level one. Add a result type to
    `_is_empty_tool_result` and it is checked here the same day, without
    anyone remembering to.

THE ONE CASE THAT IS NOT A SUFFIX RULE
    `silver.lithology_logs:hole=X:collar=Y:intervals=0` is a hole that has
    a collar and no logged intervals. `_is_empty_tool_result` deliberately
    calls that NON-empty: collar metadata alone answers "tell me about
    hole X", and Wyoming-historical collars routinely carry nothing else.
    Only the collar-less `silver.lithology_logs:intervals=0` is empty.
"""
from __future__ import annotations

import ast
import dataclasses
import inspect
import typing
from typing import Any

import pytest

from app.agent import tool_result_helpers
from app.agent.response_assembler import _extract_source_id, is_empty_source_id
from app.agent.tool_result_helpers import _is_empty_tool_result


def _covered_type_names() -> list[str]:
    """Types `_is_empty_tool_result` tests with isinstance, in source order."""
    tree = ast.parse(inspect.getsource(_is_empty_tool_result))
    names: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Name) or node.func.id != "isinstance":
            continue
        if len(node.args) != 2 or not isinstance(node.args[1], ast.Name):
            continue
        names.append(node.args[1].id)

    return names


def _blank(annotation: Any) -> Any:
    """A value of the right shape carrying nothing.

    Deliberately dumb. The point is an instance that constructs, not a
    realistic one -- every emptiness decision is made on a count or a
    collection, and those come out zero/empty either way.
    """
    if annotation is None or annotation is type(None):
        return None

    origin = typing.get_origin(annotation)
    if origin is not None:
        args = typing.get_args(annotation)
        if type(None) in args:          # Optional[...] / X | None
            return None
        if origin in (list, set, tuple):
            return origin()
        if origin is dict:
            return {}
        # A non-optional union: take the first arm.
        return _blank(args[0]) if args else None

    if annotation is str:
        return ""
    if annotation is int:
        return 0
    if annotation is float:
        return 0.0
    if annotation is bool:
        return False
    if annotation in (list, set, tuple, dict):
        return annotation()
    return None


def _empty_instance(cls: type) -> Any:
    """Build an instance of a result dataclass with nothing in it."""
    hints = typing.get_type_hints(cls)
    kwargs = {
        field.name: _blank(hints.get(field.name))
        for field in dataclasses.fields(cls)
        if field.default is dataclasses.MISSING
        and field.default_factory is dataclasses.MISSING  # type: ignore[misc]
    }
    return cls(**kwargs)


COVERED = _covered_type_names()


def test_the_type_list_is_read_from_the_predicate_not_hardcoded() -> None:
    """Guards the guard. An empty list here would make every case below
    vanish silently, which is the same failure the finding is about."""
    assert len(COVERED) >= 6, (
        f"only {COVERED} found in _is_empty_tool_result — the AST walk is "
        "broken, or the predicate has been rewritten"
    )
    assert "CollarDetailsResult" in COVERED, (
        "the missed-hole case is the one that motivated this test"
    )


@pytest.mark.parametrize("type_name", COVERED)
def test_an_empty_result_mints_an_id_the_guards_recognise(
    type_name: str,
) -> None:
    """The anti-drift check.

    `_is_empty_tool_result` decides emptiness from the OBJECT, which it can
    always do correctly. `is_empty_source_id` has to decide it from the
    STRING, which is the only thing a Citation carries. This requires the
    two to agree, so a new result type cannot slip past the string side.
    """
    from app.agent import tools

    cls = getattr(tools, type_name, None) or getattr(
        tool_result_helpers, type_name, None
    )
    assert cls is not None, f"{type_name} is not importable from app.agent.tools"

    result = _empty_instance(cls)

    assert _is_empty_tool_result(result) is True, (
        f"{type_name}: a blank instance is not considered empty — either "
        "the predicate changed or the blank builder no longer produces "
        "zero counts; read nothing into the assertion below until this "
        "is resolved"
    )

    source_id = _extract_source_id("some_tool", result)

    assert is_empty_source_id(source_id) is True, (
        f"{type_name} mints {source_id!r} when it found nothing, and both "
        "the IND-6 guard and _count_independent_sources read that as real "
        "evidence"
    )


class TestTheNonEmptyCasesStillCount:
    """A predicate that says "empty" too often is the worse failure.

    It floors confidence on grounded answers and makes the platform look
    like it holds no data.
    """

    def test_a_hole_with_a_collar_but_no_intervals_is_real(self) -> None:
        """`_is_empty_tool_result` treats this as non-empty on purpose:
        collar metadata alone answers "tell me about hole X"."""
        source_id = (
            "silver.lithology_logs:hole=PLS-22-08:collar=c-1234:intervals=0"
        )

        assert is_empty_source_id(source_id) is False

    @pytest.mark.parametrize(
        "source_id",
        [
            "silver.samples:element=U3O8:count=14",
            "silver.collars:count=7:first=c-1",
            "neo4j:entities=3:first=e-9",
            "georag_reports:r-1:section=14:chunk=abc",
            "silver.collars:hole=PLS-22-08:collar=c-1:assays=4:litho=9",
            "silver.projects:slug=triple-r:company=Acme:curves=3:reports=2",
        ],
    )
    def test_a_populated_id_is_not_empty(self, source_id: str) -> None:
        assert is_empty_source_id(source_id) is False

    def test_a_count_of_ten_is_not_a_count_of_zero(self) -> None:
        """The rule matches `:count=0`, not a trailing zero. An
        `endswith("0")` would have swallowed both."""
        assert is_empty_source_id("silver.collars:count=10") is False
        assert is_empty_source_id("silver.collars:count=100") is False


class TestTheOriginalSentinels:
    @pytest.mark.parametrize(
        "sentinel",
        [
            "no-tool-call",
            "georag_reports:empty",
            "pg_public_geoscience:empty",
            "silver.collars:miss",
        ],
    )
    def test_the_literal_sentinels_still_match(self, sentinel: str) -> None:
        assert is_empty_source_id(sentinel) is True

    def test_an_absent_id_is_empty(self) -> None:
        assert is_empty_source_id("") is True


def test_both_filters_use_the_same_predicate() -> None:
    """The two consumers drifting apart IS the finding, one level up.

    The 2026-08-14 audit shared the sentinel set between them for exactly
    this reason; the set was simply too small to matter.
    """
    from app.agent import confidence_computer, response_assembler

    assembler = inspect.getsource(response_assembler.assemble_response)
    counter = inspect.getsource(confidence_computer._count_independent_sources)

    assert "is_empty_source_id" in assembler
    assert "is_empty_source_id" in counter
