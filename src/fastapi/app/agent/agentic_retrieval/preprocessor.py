"""Context pre-processor — Phase 3 / Step 3.1.

Translates a :class:`ContextEnvelope` into a :class:`RetrievalFilters`
struct the execute / assemble nodes can act on:

  * **CRS** — when ``crs_epsg`` is set, spatial tools may project /
    filter results against that EPSG. When unspecified the spatial path
    runs unfiltered (Phase 2.4 already surfaces the missing-CRS note).
  * **Depth reference** — selects the normalisation function the assay /
    downhole tools should apply (bgl / asl / rl / tvd / md). Stored as a
    string token — actual normalisation logic lives in the tools.
  * **Data sources** — restricts the set of tools the execute node may
    invoke. When unspecified, every primary tool in the retrieval profile
    is in play.
  * **Reporting code** — surfaces into the synthesis prompt so the LLM
    frames answers against the right framework (NI 43-101 / CIM / JORC /
    …). Defaulting (Step 2.4) flags the assumption.
  * **Specific objects** — recorded for the lineage but does not narrow
    retrieval directly (the tool layer is the right place for that).

Field / Office mode (Step 3.3) layers on top of the per-field translation:

  * **Field mode** caps ``max_chunks=3``, forces BM25-priority retrieval,
    restricts to project-corpus data sources, and emits the "max 300
    words" prompt cap.
  * **Office mode** keeps the profile's defaults.

The pre-processor is pure — no I/O, no settings reads. Easy to unit-test
without a running stack.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.agent.agentic_retrieval.context_envelope import (
    ContextEnvelope,
    DEFAULT_QUERY_MODE,
    DataSource,
    DepthReference,
    QueryMode,
    ReportingCode,
)

# Tool → data-source surface mapping. Used by the execute node to drop
# tools that the user explicitly excluded via `data_sources`. The mapping
# is conservative — a tool that touches multiple surfaces is associated
# with every surface that might lead a user to expect it.
TOOL_DATA_SOURCE_MAP: dict[str, set[DataSource]] = {
    "search_documents": {"technical_reports", "maps"},
    "search_documents_adversarial": {"technical_reports", "maps"},
    "query_spatial_collars": {"drill_logs", "maps"},
    "query_downhole_logs": {"drill_logs"},
    "query_assay_data": {"assays"},
    "traverse_knowledge_graph": {"technical_reports", "drill_logs"},
    "query_project_overview": {"drill_logs", "technical_reports"},
    # Geophysics surfaces aren't wired to a tool yet (Phase 4) — when they
    # land they should be added here so the data-source filter picks them up.
}


# Field mode floor — applied AFTER the profile's own max_chunks.
FIELD_MODE_MAX_CHUNKS = 3

# Word cap appended to the prompt when mode == "field".
FIELD_MODE_WORD_CAP_INSTRUCTION = (
    "\nFIELD MODE — keep the entire answer under 300 words. "
    "Prefer the Observations and the single highest-priority Recommended "
    "action; collapse Interpretations and Uncertainty to one sentence each "
    "unless they are the section the user asked about."
)


# Reporting-code reference appended to the prompt. The phrasing is
# deliberately compact — the geologist sees the full citation contract
# in the OIUR base rules; this just names the framework.
def _reporting_code_instruction(code: ReportingCode, was_defaulted: bool) -> str:
    if was_defaulted:
        return (
            f"\nREPORTING FRAME — defaulting to {code} (Canadian "
            "jurisdiction default; the user did not specify). Flag this "
            "assumption in the Uncertainty section."
        )
    return (
        f"\nREPORTING FRAME — frame the answer against {code} where the "
        "Evidence Set supports it."
    )


@dataclass(frozen=True)
class RetrievalFilters:
    """Pre-processed retrieval-side filter struct.

    All fields are read-only; callers should not mutate. The execute /
    assemble nodes inspect this struct to decide which tools to call and
    which prompt suffixes to append.
    """

    # CRS / spatial
    crs_epsg: int | None = None  # None = no spatial filtering (Step 2.4)

    # Depth reference token, normalised lower-case. None when unspecified.
    depth_reference: DepthReference | None = None

    # Allowed data-source surfaces. Empty set = no narrowing (every primary
    # tool may run). Non-empty set = the execute node skips tools whose
    # surfaces don't intersect this.
    allowed_data_sources: frozenset[DataSource] = field(default_factory=frozenset)

    # Reporting code + whether it was defaulted.
    reporting_code: ReportingCode = "NI 43-101"
    reporting_code_was_defaulted: bool = True

    # Mode-driven retrieval shape.
    mode: QueryMode = DEFAULT_QUERY_MODE
    max_chunks: int | None = None       # None = use profile's default
    force_bm25: bool = False            # True in Field mode
    project_corpus_only: bool = False   # True in Field mode

    # Prompt suffixes to append to the synthesis prompt.
    prompt_suffixes: tuple[str, ...] = ()

    # Specific objects (informational — passed through to lineage).
    specific_objects: tuple[str, ...] = ()

    def is_tool_allowed(self, tool_name: str) -> bool:
        """Return True if *tool_name* is permitted under this filter.

        Empty ``allowed_data_sources`` means no narrowing → every tool is
        allowed. When set, a tool is allowed only when at least one of its
        mapped data-source surfaces is in the allowed set. Tools the map
        doesn't recognise are allowed by default (don't break unknown tools).
        """
        if not self.allowed_data_sources:
            return True
        surfaces = TOOL_DATA_SOURCE_MAP.get(tool_name)
        if surfaces is None:
            return True
        return bool(surfaces & self.allowed_data_sources)


# ---------------------------------------------------------------------------
# Pre-processor
# ---------------------------------------------------------------------------


def preprocess_envelope(envelope: ContextEnvelope | None) -> RetrievalFilters:
    """Translate a :class:`ContextEnvelope` into :class:`RetrievalFilters`.

    Returns a default-filled :class:`RetrievalFilters` when ``envelope`` is
    None (legacy callers / fully-unspecified queries).
    """
    if envelope is None:
        return RetrievalFilters(
            prompt_suffixes=(
                _reporting_code_instruction("NI 43-101", was_defaulted=True),
            )
        )

    code, was_defaulted = envelope.effective_reporting_code()

    # Validate EPSG code range (EPSG officially issues 1024-32767).
    crs = envelope.crs_epsg
    if crs is not None and not (1024 <= crs <= 32767):
        crs = None

    allowed = frozenset(envelope.data_sources)

    mode = envelope.mode
    is_field_mode = mode == "field"
    if is_field_mode:
        # Field mode forces project corpus only — strip non-project surfaces
        # from the allowed set. Empty allowed_data_sources still means "all",
        # so we inject the project-only surfaces explicitly.
        project_corpus_sources: frozenset[DataSource] = frozenset(
            {"drill_logs", "assays", "technical_reports"}
        )
        if allowed:
            allowed = allowed & project_corpus_sources
        else:
            allowed = project_corpus_sources

    prompt_suffixes: list[str] = [_reporting_code_instruction(code, was_defaulted)]
    if is_field_mode:
        prompt_suffixes.append(FIELD_MODE_WORD_CAP_INSTRUCTION)

    return RetrievalFilters(
        crs_epsg=crs,
        depth_reference=envelope.depth_reference,
        allowed_data_sources=allowed,
        reporting_code=code,
        reporting_code_was_defaulted=was_defaulted,
        mode=mode,
        max_chunks=FIELD_MODE_MAX_CHUNKS if is_field_mode else None,
        force_bm25=is_field_mode,
        project_corpus_only=is_field_mode,
        prompt_suffixes=tuple(prompt_suffixes),
        specific_objects=tuple(envelope.specific_objects),
    )


__all__ = [
    "FIELD_MODE_MAX_CHUNKS",
    "FIELD_MODE_WORD_CAP_INSTRUCTION",
    "RetrievalFilters",
    "TOOL_DATA_SOURCE_MAP",
    "preprocess_envelope",
]
