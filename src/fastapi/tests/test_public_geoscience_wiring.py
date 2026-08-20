"""Pin the public-geoscience tool's reachability chain.

Background — 2026-08-19
-----------------------
182,826 points were indexed across six pg_* Qdrant collections and the tool
that reads them, ``search_public_geoscience``, had never been invoked once.
Two independent faults:

  1. The agentic dispatcher resolves tools with
     ``getattr(app.agent.tools, tool_name)``. The tool lives in its own
     module and was never re-exported onto that namespace, so every lookup
     returned None and logged "unknown tool — skipped". No profile listed it
     either, so nothing ever asked for it in the first place.

  2. The collections are 384-dim (bge-small, deliberately pinned) while the
     runtime embedder is 1024-dim, so even a reachable tool would have
     returned HTTP 400 on every query.

These tests pin fault 1's fix — pure import/inspection, no live DB or Qdrant
— plus the suffix indirection that fault 2's cutover depends on.
"""
from __future__ import annotations


def test_tool_is_reachable_via_the_dispatcher_namespace() -> None:
    """The dispatcher does getattr(app.agent.tools, name); it must resolve.

    This is the exact lookup in agentic_retrieval/nodes.py::_call_tool_safely.
    Without the re-export it returns None and the tool is silently skipped —
    the state this corpus sat in since it was indexed.
    """
    from app.agent import tools as _t

    fn = getattr(_t, "search_public_geoscience", None)
    assert fn is not None, (
        "app.agent.tools.search_public_geoscience is missing. The agentic "
        "dispatcher resolves tools by getattr against that module, so a tool "
        "defined elsewhere is invisible to the planner unless re-exported "
        "there. See the deferred import at the bottom of tools.py — it sits "
        "at the bottom on purpose, because public_geoscience_tool imports "
        "_metered back out of tools.py."
    )
    assert callable(fn)


def test_tool_is_listed_in_at_least_one_retrieval_profile() -> None:
    """A re-export alone is inert — some profile has to request the tool."""
    from app.agent.agentic_retrieval.retrieval_profile import _PROFILES

    listing = {
        intent: p
        for intent, p in _PROFILES.items()
        if "search_public_geoscience" in (*p.primary_tools, *p.secondary_tools)
    }
    assert listing, (
        "No retrieval profile references search_public_geoscience, so the "
        "execute node will never dispatch it no matter what the classifier "
        "decides."
    )
    # Secondary, not primary: it should only fire when internal coverage is
    # thin (nodes.py's _SECONDARY_COVERAGE_THRESHOLD), never on every query.
    for intent, profile in listing.items():
        assert "search_public_geoscience" not in profile.primary_tools, (
            f"{intent} lists search_public_geoscience as a PRIMARY tool. It "
            "belongs in secondary_tools so a well-covered query does not pay "
            "its latency — it exists for the 'little in-house on this, check "
            "the government record' case."
        )


def test_dispatcher_has_a_call_branch_for_the_tool() -> None:
    """The tool's signature is keyword-only and takes no project_id.

    Every other tool in the dispatch chain takes positional (ctx, project_id)
    or (deps, workspace_id, project_id). Falling through to one of those
    shapes would raise TypeError, so the branch must exist explicitly.
    """
    import inspect
    import pathlib

    from app.agent.public_geoscience_tool import search_public_geoscience

    sig = inspect.signature(search_public_geoscience)
    params = list(sig.parameters.values())
    assert params[0].name == "ctx"
    assert "project_id" not in sig.parameters, (
        "search_public_geoscience gained a project_id parameter. The public "
        "geoscience corpus is government-published data that is not scoped "
        "to a workspace's projects; if this changed, the dispatch branch in "
        "nodes.py must change with it."
    )
    assert sig.parameters["text_query"].kind is inspect.Parameter.KEYWORD_ONLY

    src = (
        pathlib.Path(__file__).parents[1]
        / "app" / "agent" / "agentic_retrieval" / "nodes.py"
    ).read_text(encoding="utf-8")
    assert 'real_name == "search_public_geoscience"' in src, (
        "nodes.py has no dispatch branch for search_public_geoscience — it "
        "would fall through to a positional call shape and raise TypeError."
    )
    assert "text_query=query" in src, (
        "The dispatch branch must pass the query as the keyword-only "
        "text_query argument."
    )


def test_every_canonical_type_has_a_table_and_a_sync_mapper() -> None:
    """Replaces the old `_resolve_collection` suffix test.

    That test guarded the 384→1024 Qdrant cutover for the six `pg_*`
    collections. There is no embedded copy any more — the tool reads
    `public_geo.*`, refreshed by the public_geo_sync workflow — so the
    invariant worth guarding moved: every canonical type the tool will query
    must have both a table it can SELECT from and a mapper that fills it.

    Without this, adding a type to CANONICAL_TYPES yields a tool that silently
    returns nothing for it (no table branch) or a table that silently never
    fills (no mapper), and both look like "no data in that jurisdiction".
    """
    from app.agent.public_geoscience_tool import _TABLES
    from app.services.public_geo.registry import CANONICAL_TYPES
    from app.services.public_geo.sync import MAPPERS, SPECS

    for canonical_type in CANONICAL_TYPES:
        assert canonical_type in _TABLES, (
            f"{canonical_type} is offered by the registry but the tool has no "
            "table branch for it — searches would silently skip the type."
        )
        assert canonical_type in SPECS, f"{canonical_type} has no TableSpec"
        assert canonical_type in MAPPERS, (
            f"{canonical_type} has no sync mapper — its table would never fill."
        )
        assert _TABLES[canonical_type] == SPECS[canonical_type].table, (
            f"{canonical_type}: the tool reads {_TABLES[canonical_type]} but "
            f"the sync writes {SPECS[canonical_type].table}."
        )


def test_tool_is_mapped_to_the_public_geoscience_data_source() -> None:
    """Envelope narrowing must actually reach this tool.

    'public_geoscience' has been a declared DataSource since the envelope
    landed but was mapped to no tool, so search_public_geoscience fell
    through is_tool_allowed's `surfaces is None` default and was permitted
    under every narrowing — including ones that deliberately excluded it.
    """
    from app.agent.agentic_retrieval.preprocessor import TOOL_DATA_SOURCE_MAP

    assert TOOL_DATA_SOURCE_MAP.get("search_public_geoscience") == {
        "public_geoscience",
    }
