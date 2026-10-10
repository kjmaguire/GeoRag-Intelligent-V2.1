"""Tests for the context pre-processor — Phase 3 / Steps 3.1 + 3.3."""

from __future__ import annotations

import pytest

from app.agent.agentic_retrieval import (
    DEFAULT_QUERY_MODE,
    FIELD_MODE_MAX_CHUNKS,
    TOOL_DATA_SOURCE_MAP,
    AgenticRetrievalState,
    ContextEnvelope,
    RetrievalFilters,
    preprocess_envelope,
    profile_for_intent,
)
from app.agent.agentic_retrieval.nodes import route_node
from app.agent.agentic_retrieval.preprocessor import (
    FIELD_MODE_WORD_CAP_INSTRUCTION,
)

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def test_none_envelope_yields_office_mode_defaults() -> None:
    f = preprocess_envelope(None)
    assert f.mode == DEFAULT_QUERY_MODE == "office"
    assert f.max_chunks is None
    assert f.force_bm25 is False
    assert f.project_corpus_only is False
    assert f.allowed_data_sources == frozenset()
    # Reporting code defaulted with explicit assumption flag.
    assert f.reporting_code == "NI 43-101"
    assert f.reporting_code_was_defaulted is True
    # Default suffix carries the assumption-flag instruction.
    assert any("defaulting to NI 43-101" in s for s in f.prompt_suffixes)


def test_empty_envelope_matches_none_envelope_defaults() -> None:
    f_empty = preprocess_envelope(ContextEnvelope())
    f_none = preprocess_envelope(None)
    # Same shape: empty envelope is the same as no envelope.
    assert f_empty.mode == f_none.mode
    assert f_empty.max_chunks == f_none.max_chunks
    assert f_empty.allowed_data_sources == f_none.allowed_data_sources
    assert f_empty.reporting_code == f_none.reporting_code


# ---------------------------------------------------------------------------
# CRS translation
# ---------------------------------------------------------------------------


def test_valid_epsg_passes_through() -> None:
    f = preprocess_envelope(ContextEnvelope(crs_epsg=26913))
    assert f.crs_epsg == 26913


def test_out_of_range_epsg_dropped() -> None:
    """EPSG codes outside the official 1024-32767 range are rejected."""
    f = preprocess_envelope(ContextEnvelope(crs_epsg=999))
    assert f.crs_epsg is None
    f = preprocess_envelope(ContextEnvelope(crs_epsg=99999))
    assert f.crs_epsg is None


# ---------------------------------------------------------------------------
# Depth reference
# ---------------------------------------------------------------------------


def test_depth_reference_passes_through() -> None:
    f = preprocess_envelope(ContextEnvelope(depth_reference="bgl"))
    assert f.depth_reference == "bgl"


# ---------------------------------------------------------------------------
# Data-source filter + is_tool_allowed
# ---------------------------------------------------------------------------


def test_empty_data_sources_means_all_tools_allowed() -> None:
    f = preprocess_envelope(ContextEnvelope())
    for tool in TOOL_DATA_SOURCE_MAP:
        assert f.is_tool_allowed(tool)


def test_data_source_filter_blocks_unmatched_tools() -> None:
    f = preprocess_envelope(ContextEnvelope(data_sources=["technical_reports"]))
    # search_documents touches technical_reports → allowed
    assert f.is_tool_allowed("search_documents")
    # query_assay_data touches only assays → blocked
    assert not f.is_tool_allowed("query_assay_data")
    # Unknown tools allowed by default (don't break new tools).
    assert f.is_tool_allowed("brand_new_tool_2027")


def test_multiple_data_sources_union() -> None:
    f = preprocess_envelope(
        ContextEnvelope(data_sources=["drill_logs", "assays"])
    )
    assert f.is_tool_allowed("query_spatial_collars")  # drill_logs
    assert f.is_tool_allowed("query_assay_data")  # assays
    assert not f.is_tool_allowed("search_documents")  # technical_reports + maps


# ---------------------------------------------------------------------------
# Reporting code translation
# ---------------------------------------------------------------------------


def test_explicit_reporting_code_no_default_flag() -> None:
    f = preprocess_envelope(ContextEnvelope(reporting_code="JORC"))
    assert f.reporting_code == "JORC"
    assert f.reporting_code_was_defaulted is False
    # The prompt suffix references the actual code, no "defaulting" language.
    joined = "".join(f.prompt_suffixes)
    assert "JORC" in joined
    assert "defaulting" not in joined.lower()


def test_unspecified_reporting_code_appends_assumption_flag() -> None:
    f = preprocess_envelope(ContextEnvelope())
    joined = "".join(f.prompt_suffixes)
    assert "defaulting to NI 43-101" in joined
    assert "Flag this assumption" in joined


# ---------------------------------------------------------------------------
# Field-mode behaviour (Step 3.3)
# ---------------------------------------------------------------------------


def test_field_mode_caps_max_chunks() -> None:
    f = preprocess_envelope(ContextEnvelope(mode="field"))
    assert f.mode == "field"
    assert f.max_chunks == FIELD_MODE_MAX_CHUNKS == 3


def test_field_mode_forces_bm25_and_project_corpus() -> None:
    f = preprocess_envelope(ContextEnvelope(mode="field"))
    assert f.force_bm25 is True
    assert f.project_corpus_only is True
    # Project-corpus sources are forced on.
    assert "drill_logs" in f.allowed_data_sources
    assert "assays" in f.allowed_data_sources
    assert "technical_reports" in f.allowed_data_sources


def test_field_mode_appends_word_cap_to_prompt() -> None:
    f = preprocess_envelope(ContextEnvelope(mode="field"))
    joined = "".join(f.prompt_suffixes)
    assert "FIELD MODE" in joined
    assert "under 300 words" in joined


def test_field_mode_intersects_user_data_sources() -> None:
    """When the user asks for geophysics in field mode, field mode wins
    (project-corpus only — geophysics is not a project-corpus surface).
    """
    f = preprocess_envelope(
        ContextEnvelope(mode="field", data_sources=["geophysics"])
    )
    # geophysics was intersected away — only project-corpus sources remain.
    assert "geophysics" not in f.allowed_data_sources
    # The intersection of ["geophysics"] and the project corpus is empty. An
    # empty allowed set used to read as "no narrowing", so the narrowest
    # request ran EVERY tool (2026-10-10 audit, finding 13). It is now an
    # explicit "narrowed to nothing" state that denies: the user's sources
    # and Field mode contradict, and nothing satisfies both.
    assert f.allowed_data_sources == frozenset()
    assert f.no_data_source_allowed is True
    for tool in TOOL_DATA_SOURCE_MAP:
        assert f.is_tool_allowed(tool) is False, tool
    assert f.is_tool_allowed("a_tool_nobody_mapped") is False


@pytest.mark.parametrize(
    "envelope",
    [
        ContextEnvelope(),
        ContextEnvelope(mode="office"),
        ContextEnvelope(mode="office", data_sources=[]),
    ],
)
def test_no_narrowing_still_allows_every_tool(envelope: ContextEnvelope) -> None:
    """The other half of the distinction: nothing was restricted."""
    f = preprocess_envelope(envelope)
    assert f.allowed_data_sources == frozenset()
    assert f.no_data_source_allowed is False
    assert all(f.is_tool_allowed(tool) for tool in TOOL_DATA_SOURCE_MAP)
    assert f.is_tool_allowed("a_tool_nobody_mapped") is True


def test_a_narrowing_that_leaves_something_is_unchanged() -> None:
    f = preprocess_envelope(ContextEnvelope(mode="field", data_sources=["assays", "geophysics"]))
    assert f.allowed_data_sources == frozenset({"assays"})
    assert f.no_data_source_allowed is False
    assert f.is_tool_allowed("query_assay_data") is True
    assert f.is_tool_allowed("search_documents") is False


def test_office_mode_narrowed_to_one_source_is_not_a_denial() -> None:
    f = preprocess_envelope(ContextEnvelope(data_sources=["public_geoscience"]))
    assert f.no_data_source_allowed is False
    assert f.is_tool_allowed("search_public_geoscience") is True
    assert f.is_tool_allowed("query_assay_data") is False


def test_loosening_a_repair_drops_the_denial_too() -> None:
    """repair_apply's LOOSEN_FILTERS empties allowed_data_sources; with the
    denial flag left set that would keep refusing every tool."""
    from app.agent.repair_apply import apply_retrieval_strategy
    from app.agent.repair_strategy import RepairStrategy

    snapshot = {
        "retrieval_filters": {
            "allowed_data_sources": [],
            "no_data_source_allowed": True,
            "mode": "field",
        }
    }
    patch = apply_retrieval_strategy(RepairStrategy.LOOSEN_FILTERS, snapshot)
    assert patch["retrieval_filters"]["no_data_source_allowed"] is False
    assert patch["retrieval_filters"]["allowed_data_sources"] == []
    # a snapshot that never carried the flag is not given one
    plain = apply_retrieval_strategy(
        RepairStrategy.LOOSEN_FILTERS, {"retrieval_filters": {"allowed_data_sources": ["assays"]}}
    )
    assert "no_data_source_allowed" not in plain["retrieval_filters"]


def test_office_mode_does_not_cap_chunks() -> None:
    f = preprocess_envelope(ContextEnvelope(mode="office"))
    assert f.max_chunks is None
    assert f.force_bm25 is False
    assert f.project_corpus_only is False
    joined = "".join(f.prompt_suffixes)
    assert "FIELD MODE" not in joined
    assert FIELD_MODE_WORD_CAP_INSTRUCTION.strip() not in joined.strip()


# ---------------------------------------------------------------------------
# Mode is NOT counted as one of the 12 context fields
# ---------------------------------------------------------------------------


def test_mode_excluded_from_unspecified_fields_count() -> None:
    """The plan tracks 12 fields. ``mode`` is a setting, not a context
    field — it must not appear in populated/unspecified sets.
    """
    env = ContextEnvelope()  # all unspecified, mode = default office
    assert "mode" not in env.populated_fields()
    assert "mode" not in env.unspecified_fields()
    # 12 fields total.
    assert len(env.unspecified_fields()) == 12


def test_mode_change_does_not_affect_unspecified_count() -> None:
    env = ContextEnvelope(mode="field")
    assert "mode" not in env.populated_fields()
    assert "mode" not in env.unspecified_fields()
    assert len(env.unspecified_fields()) == 12


# ---------------------------------------------------------------------------
# Specific objects pass-through
# ---------------------------------------------------------------------------


def test_specific_objects_recorded_as_tuple() -> None:
    f = preprocess_envelope(
        ContextEnvelope(specific_objects=["DDH-07", "DDH-12"])
    )
    assert f.specific_objects == ("DDH-07", "DDH-12")


# ---------------------------------------------------------------------------
# Route node populates retrieval_filters
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_route_node_populates_retrieval_filters() -> None:
    state = AgenticRetrievalState(
        query="Integrate the assays across DDH-07 to DDH-12.",
        deps=object(),
        context_envelope=ContextEnvelope(
            crs_epsg=26913,
            data_sources=["assays", "drill_logs"],
            reporting_code="NI 43-101",
        ),
    )
    state = state.model_copy(
        update={"intent": "synthesis", "intent_result": None}
    )
    update = await route_node(state)
    filters: RetrievalFilters = update["retrieval_filters"]
    assert filters.crs_epsg == 26913
    assert filters.reporting_code == "NI 43-101"
    assert filters.reporting_code_was_defaulted is False
    assert filters.is_tool_allowed("query_assay_data")
    assert not filters.is_tool_allowed("search_documents")  # tech reports not selected


@pytest.mark.asyncio
async def test_route_node_field_mode_filter_carries_word_cap() -> None:
    state = AgenticRetrievalState(
        query="Recommend infill spacing.",
        deps=object(),
        context_envelope=ContextEnvelope(
            mode="field",
            decision_to_support="Choose infill spacing for the next program.",
        ),
    )
    state = state.model_copy(
        update={"intent": "decision_support", "intent_result": None}
    )
    update = await route_node(state)
    filters: RetrievalFilters = update["retrieval_filters"]
    assert filters.mode == "field"
    assert filters.max_chunks == FIELD_MODE_MAX_CHUNKS
    joined = "".join(filters.prompt_suffixes)
    assert "under 300 words" in joined


# ---------------------------------------------------------------------------
# Execute-node respects the data-source filter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_node_skips_filtered_out_tools(monkeypatch) -> None:
    from app.agent.agentic_retrieval.nodes import execute_node

    calls: list[str] = []

    async def fake_search_documents(ctx, query_text: str, project_id: str):
        calls.append("search_documents")
        return {"chunks": [], "count": 0}

    async def fake_project_id_only(ctx, project_id: str, **_kwargs):
        return {"chunks": [], "count": 0}

    import app.agent.tools as _tools_mod

    monkeypatch.setattr(_tools_mod, "search_documents", fake_search_documents, raising=False)
    for t in ("query_spatial_collars", "query_assay_data", "query_project_overview"):
        monkeypatch.setattr(_tools_mod, t, fake_project_id_only, raising=False)

    # Filter to assays only — synthesis profile's broad primary set
    # should be reduced to just query_assay_data. deps must carry a
    # project_id for the new dispatcher to invoke tools.
    class _Deps:
        project_id = "00000000-0000-0000-0000-000000000001"

    envelope = ContextEnvelope(data_sources=["assays"])
    state = AgenticRetrievalState(
        query="x", deps=_Deps(), context_envelope=envelope
    )
    state = state.model_copy(
        update={
            "intent": "synthesis",
            "retrieval_profile": profile_for_intent("synthesis"),
            "retrieval_filters": preprocess_envelope(envelope),
        }
    )
    update = await execute_node(state)
    tool_names = [name for name, _ in update["tool_results"]]
    # Only assays-touching tools allowed.
    assert "query_assay_data" in tool_names
    assert "search_documents" not in tool_names
    assert "query_spatial_collars" not in tool_names


@pytest.mark.asyncio
async def test_execute_node_runs_nothing_when_the_narrowing_left_nothing(monkeypatch) -> None:
    """2026-10-10 audit, finding 13. Field mode plus ``data_sources=["geophysics"]``
    used to leave an empty allowed set, read as "no narrowing", and the
    synthesis profile ran its whole primary tool set: the narrowest request
    ran the widest search."""
    from app.agent.agentic_retrieval.nodes import execute_node

    calls: list[str] = []

    async def fake_search_documents(ctx, query_text: str, project_id: str):
        calls.append("search_documents")
        return {"chunks": [{"text": "x"}], "count": 1}

    async def fake_project_id_only(ctx, project_id: str, **_kwargs):
        calls.append("structured")
        return {"chunks": [{"text": "x"}], "count": 1}

    import app.agent.tools as _tools_mod

    monkeypatch.setattr(_tools_mod, "search_documents", fake_search_documents, raising=False)
    for t in ("query_spatial_collars", "query_assay_data", "query_project_overview"):
        monkeypatch.setattr(_tools_mod, t, fake_project_id_only, raising=False)

    class _Deps:
        project_id = "00000000-0000-0000-0000-000000000001"

    envelope = ContextEnvelope(mode="field", data_sources=["geophysics"])
    state = AgenticRetrievalState(query="x", deps=_Deps(), context_envelope=envelope)
    state = state.model_copy(
        update={
            "intent": "synthesis",
            "retrieval_profile": profile_for_intent("synthesis"),
            "retrieval_filters": preprocess_envelope(envelope),
        }
    )
    update = await execute_node(state)
    assert calls == []
    assert update["tool_results"] == []
