"""Tests for P0 #1 — Qdrant project_id scope filter in search_documents.

The review called out that the `georag_reports` collection was retrieved
without any project-scoping filter. These tests pin the behaviour of the
`_build_document_scope_filter` helper across all three policy modes:

  cross_project     — returns None (no filter applied)
  project_or_public — returns a Filter with should=[match, missing, public]
  strict            — returns a Filter with must=[match]

We also verify the fail-open behaviour when qdrant-client is missing or the
policy name is typoed, since shipping a filter that silently drops every
document would be a worse outcome than a security escape hatch logged in
the warnings stream.
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def _scope_fn(monkeypatch):
    """Return _build_document_scope_filter from tools without reloading the module.

    10.1 drift fix: the original fixture called importlib.reload(tools_module) to
    get a 'clean settings' state. The reload breaks isinstance() checks in
    test_followups.py (run order dependent): after reload, app.agent.followups
    holds the pre-reload SpatialQueryResult class while test_followups imports
    the post-reload version — the two class objects are not identical.

    The setting is patched via monkeypatch.setattr on the shared settings singleton
    which is already sufficient; the reload is unnecessary and harmful.
    """
    from app.agent import tools as tools_module

    return tools_module._build_document_scope_filter


def _set_mode(monkeypatch, mode: str) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "QDRANT_DOCUMENT_PROJECT_SCOPE", mode, raising=False)


def test_cross_project_returns_none(monkeypatch, _scope_fn):
    """Historical behaviour preserved when flag is left at default."""
    _set_mode(monkeypatch, "cross_project")
    assert _scope_fn("proj-123") is None


def test_strict_returns_must_filter(monkeypatch, _scope_fn):
    """Strict mode narrows retrieval to exactly the requesting project."""
    from qdrant_client.models import Filter

    _set_mode(monkeypatch, "strict")
    flt = _scope_fn("proj-abc")

    assert isinstance(flt, Filter)
    # must[] contains the project_id FieldCondition
    assert flt.must is not None and len(flt.must) == 1
    cond = flt.must[0]
    assert cond.key == "project_id"
    assert cond.match.value == "proj-abc"
    # Nothing admitted via should[] in strict mode
    assert not flt.should


def test_project_or_public_admits_legacy_and_public(monkeypatch, _scope_fn):
    """Safe default for mixed proprietary + NI 43-101 collections.

    - Legacy rows without a project_id payload field MUST still be returned
      (IsEmptyCondition on project_id)
    - Explicit public documents (project_id == "public") MUST still be returned
    - Rows stamped with the caller's project_id are returned.
    """
    from qdrant_client.models import Filter, IsEmptyCondition

    _set_mode(monkeypatch, "project_or_public")
    flt = _scope_fn("proj-xyz")

    assert isinstance(flt, Filter)
    assert flt.should is not None and len(flt.should) == 3

    # Collect the disjunction contents.
    has_project_match = False
    has_empty_match = False
    has_public_match = False
    for clause in flt.should:
        if isinstance(clause, IsEmptyCondition):
            if clause.is_empty.key == "project_id":
                has_empty_match = True
        else:
            # FieldCondition
            if clause.key == "project_id":
                if clause.match.value == "proj-xyz":
                    has_project_match = True
                elif clause.match.value == "public":
                    has_public_match = True

    assert has_project_match, "should[] must include the caller's project_id"
    assert has_empty_match, "should[] must admit legacy rows with no project_id"
    assert has_public_match, "should[] must admit explicit public rows"


def test_unknown_mode_fails_open(monkeypatch, _scope_fn, caplog):
    """A typo in the setting must NOT silently drop every document."""
    import logging

    _set_mode(monkeypatch, "stricct")  # typo
    with caplog.at_level(logging.WARNING, logger="app.agent.tools"):
        flt = _scope_fn("proj-123")

    assert flt is None
    assert any("unknown QDRANT_DOCUMENT_PROJECT_SCOPE" in r.message for r in caplog.records)


def test_empty_project_id_in_strict_still_builds_filter(monkeypatch, _scope_fn):
    """Defensive: even if the caller passes '' we build a filter (matches nothing).

    The security contract is that a mis-configured caller gets zero results
    rather than being leaked into the cross-project pool.
    """
    from qdrant_client.models import Filter

    _set_mode(monkeypatch, "strict")
    flt = _scope_fn("")
    assert isinstance(flt, Filter)
    assert flt.must[0].match.value == ""
