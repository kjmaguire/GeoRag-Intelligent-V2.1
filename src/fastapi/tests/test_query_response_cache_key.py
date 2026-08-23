"""The response cache must not outlive the policy that produced its answers.

WHY THIS EXISTS
    `_query_response_cache_key` decides which cached answer a query is
    allowed to read back. An answer is a function of the documents
    retrieval was permitted to see, so anything that changes that
    permission set has to change the key too -- otherwise a policy flip
    keeps serving answers computed under the old policy until the TTL
    expires.

    This is not hypothetical. config.py used to promise a
    DOCUMENT_SCOPE_VERSION setting "folded into the response cache key so
    a policy flip cleanly invalidates stale cross-project answers".
    Nothing read it; the key hardcoded `v1`. On 2026-08-21 the scope was
    flipped from `cross_project` to `project_or_public` -- a change made
    precisely because cross-project answers were wrong -- and every wrong
    answer already in the cache stayed servable.

    The lesson is not "remember to bump the version". It is that a manual
    lever nobody pulls is indistinguishable from no lever, so the key is
    derived from the policy value itself.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.agent.orchestrator import _query_response_cache_key
from app.config import settings


class _Deps(SimpleNamespace):
    """Enough of AgentDeps for the key builder."""


@pytest.fixture
def deps() -> _Deps:
    return _Deps(
        workspace_id="11111111-1111-1111-1111-111111111111",
        project_id="22222222-2222-2222-2222-222222222222",
    )


def test_the_scope_policy_is_part_of_the_key(deps: _Deps, monkeypatch) -> None:
    monkeypatch.setattr(
        settings, "QDRANT_DOCUMENT_PROJECT_SCOPE", "cross_project",
    )
    cross = _query_response_cache_key(deps, "what is the grade")

    monkeypatch.setattr(
        settings, "QDRANT_DOCUMENT_PROJECT_SCOPE", "project_or_public",
    )
    scoped = _query_response_cache_key(deps, "what is the grade")

    assert cross is not None and scoped is not None
    assert cross != scoped, (
        "the same question cached under two different document-scope "
        "policies resolves to one key, so flipping the policy keeps "
        "serving answers built from the wrong document set"
    )


def test_strict_is_distinct_from_the_other_two(deps: _Deps, monkeypatch) -> None:
    """Three modes, three key spaces -- not just "default vs not"."""
    keys = set()
    for mode in ("cross_project", "project_or_public", "strict"):
        monkeypatch.setattr(settings, "QDRANT_DOCUMENT_PROJECT_SCOPE", mode)
        keys.add(_query_response_cache_key(deps, "same question"))

    assert len(keys) == 3


def test_the_same_policy_and_question_still_hits(deps: _Deps) -> None:
    """The point of a cache. Two identical requests must collide."""
    first = _query_response_cache_key(deps, "what is the grade")
    second = _query_response_cache_key(deps, "what is the grade")

    assert first == second


def test_workspace_and_project_still_separate_the_key(
    deps: _Deps, monkeypatch,
) -> None:
    """Adding the scope segment must not have displaced the tenancy ones.

    Reading another workspace's cached answer would be a tenancy leak, not
    a staleness bug -- so this is checked alongside, not assumed.
    """
    base = _query_response_cache_key(deps, "q")

    other_ws = _Deps(
        workspace_id="99999999-9999-9999-9999-999999999999",
        project_id=deps.project_id,
    )
    other_proj = _Deps(
        workspace_id=deps.workspace_id,
        project_id="88888888-8888-8888-8888-888888888888",
    )

    assert _query_response_cache_key(other_ws, "q") != base
    assert _query_response_cache_key(other_proj, "q") != base


def test_the_raw_query_text_is_not_in_the_key(deps: _Deps) -> None:
    """Redis keys land in slow-log and MONITOR output; customer question
    text does not belong there. The key carries a hash for that reason."""
    key = _query_response_cache_key(deps, "grade at the Fox Lake deposit")

    assert key is not None
    assert "Fox Lake" not in key
    assert "grade at" not in key


def test_an_unresolvable_workspace_does_not_cache(monkeypatch) -> None:
    """Degrading to "run the graph for real" is correct; falling back to a
    workspace-less key would let two tenants share one entry."""
    broken = _Deps(workspace_id=None, project_id=None)

    def _raise(*_args, **_kwargs):
        raise RuntimeError("cannot resolve")

    monkeypatch.setattr(
        "app.agent.workspace_context.WorkspaceContext.from_state", _raise,
    )

    assert _query_response_cache_key(broken, "q") is None
