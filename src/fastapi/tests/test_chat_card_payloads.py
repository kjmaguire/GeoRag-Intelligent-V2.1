"""Unit tests for ``_build_chat_card_payloads`` — ADR-0007 chat-card dispatcher.

Per the §6b audit (2026-05-29), the function was 0% covered. This file
pins the dispatch contract: one happy-path test per chart_type, one
falls-through test per branch, plus the precedence rules between
intent-agnostic and intent-specific cards.

The frontend `InlineViz` (resources/js/Components/InlineViz.tsx) reads
`chart_type` + specific `plotly_layout.meta.*` keys per branch. These
tests pin the contract Pythoneside so a refactor of the dispatcher
doesn't silently break the React render.

Precedence order (per the function source):
  1. DrillTrace3DResult (intent-agnostic) → returns immediately
  2. CollarDetailsResult count==1 + hole_id (intent-agnostic) → returns immediately
  3. StereonetResult count>0 (intent-agnostic) → returns immediately
  4. intent not in ('project_summary', 'coverage_gap') → (None, None)
  5. ProjectSummaryResult under intent='project_summary'
  6. CoverageGapResult under intent='coverage_gap'
"""

from __future__ import annotations

from app.agent.agentic_retrieval.nodes import _build_chat_card_payloads
from app.agent.tools import (
    AttributeCoverageRow,
    CollarDetailsResult,
    CoverageFindingRow,
    CoverageGapResult,
    DrillTrace3DResult,
    DrillTraceCollar,
    DrillTraceInterval,
    DrillTraceStructure,
    IngestGapStats,
    ProjectSummaryResult,
    StereonetPoint,
    StereonetResult,
    TechniqueBreakdownRow,
)


# ---------------------------------------------------------------------------
# Fixture builders — minimal-valid instances of each tool result type
# ---------------------------------------------------------------------------


def _collar_details(*, count: int = 1, hole_id: str | None = "ECK-22-001") -> CollarDetailsResult:
    return CollarDetailsResult(
        collar_id="coll-1",
        hole_id=hole_id,
        hole_id_canonical=hole_id,
        project_id="proj-1",
        workspace_id="ws-1",
        total_depth=350.0,
        drill_type="DDH",
        hole_type="exploration",
        drill_date="2022-06-15",
        easting=500000.0,
        northing=6500000.0,
        elevation=300.0,
        azimuth=180.0,
        dip=-60.0,
        geologist="J. Smith",
        assay_count=42,
        lithology_count=12,
        sample_count=42,
        structure_count=3,
        max_assay_value=None,
        lithology_summary=[],
        source_row_ids=["coll-1"],
        count=count,
    )


def _drill_trace_3d(*, count: int = 1, hole_id_filter: str | None = "ECK-22-001") -> DrillTrace3DResult:
    collars = [
        DrillTraceCollar(
            hole_id=hole_id_filter or f"H-{i}",
            collar_id=f"coll-{i}",
            longitude=-105.5,
            latitude=44.5,
            elevation=300.0 + i,
            total_depth=350.0,
            hole_type="exploration",
            status="completed",
            azimuth=180.0,
            dip=-60.0,
            trace_points=[],
        )
        for i in range(count)
    ]
    return DrillTrace3DResult(
        collars=collars,
        intervals=[
            DrillTraceInterval(
                collar_id="coll-0",
                depth_from=10.0,
                depth_to=20.0,
                interval_kind="assay",
                color_hint="#ff0000",
                label="Au 1.5 g/t",
                source_row_id="asy-1",
            ),
        ],
        structures=[
            DrillTraceStructure(
                collar_id="coll-0",
                depth=15.0,
                structure_type="fault",
                strike_deg=045.0,
                dip_deg=70.0,
                source_row_id="str-1",
            ),
        ],
        project_id="proj-1",
        workspace_id="ws-1",
        count=count,
        hole_id_filter=hole_id_filter,
        source_row_ids=["coll-0"],
    )


def _stereonet(*, count: int = 5) -> StereonetResult:
    return StereonetResult(
        points=[
            StereonetPoint(
                depth=10.0 + i,
                structure_type="fault",
                strike_deg=045.0,
                dip_deg=70.0,
                dip_direction_deg=135.0,
                plunge_deg=None,
                trend_deg=None,
                stereonet_x=0.1 * i,
                stereonet_y=0.2 * i,
                source_row_id=f"str-{i}",
            )
            for i in range(count)
        ],
        image_base64="iVBORw0KGgoAAAA-fake-base64-data",
        project_id="proj-1",
        workspace_id="ws-1",
        count=count,
    )


def _project_summary(*, has_breakdown: bool = True, year: int | None = 2022) -> ProjectSummaryResult:
    breakdown = []
    if has_breakdown:
        breakdown = [
            TechniqueBreakdownRow(
                technique="DDH",
                source_table="silver.collars",
                year=year,
                count=4,
                total_metres=1400.0,
                contractor="Major Drilling",
                geologist="J. Smith",
                source_row_ids=["coll-1", "coll-2"],
            ),
        ]
    return ProjectSummaryResult(
        technique_breakdown=breakdown,
        extraction_pending_fields=[],
        project_id="proj-1",
        workspace_id="ws-1",
        count=len(breakdown),
    )


def _coverage_geojson(*, feature_count: int = 3, all_have_data: bool = False) -> dict:
    """Fixture: a non-empty FeatureCollection matching the shape
    `_build_coverage_geojson` emits — one Point feature per collar with
    has_data + missing_attributes properties.

    When ``all_have_data=False`` the first collar is treated as a gap
    (no attributes) and the rest are populated — produces the realistic
    mixed-state map the §6b P4 work targets.
    """
    features = []
    for i in range(feature_count):
        is_gap = (i == 0 and not all_have_data)
        attrs_with = [] if is_gap else ["assays", "lithology_logs"]
        attrs_missing = ["assays", "lithology_logs", "structure", "alteration", "samples"] if is_gap \
            else ["structure", "alteration", "samples"]
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [-105.5 + i * 0.01, 44.5 + i * 0.01]},
            "properties": {
                "collar_id": f"coll-{i}",
                "hole_id": f"H-{i:03d}",
                "has_data": not is_gap,
                "attributes_with_data": attrs_with,
                "missing_attributes": attrs_missing,
            },
        })
    return {"type": "FeatureCollection", "features": features}


def _coverage_gap(
    *,
    has_attribute_coverage: bool = True,
    indexed: int = 100,
    findings: int = 0,
    gap_geojson: dict | None = None,
) -> CoverageGapResult:
    attribute_coverage = []
    if has_attribute_coverage:
        attribute_coverage = [
            AttributeCoverageRow(
                attribute="assays_v2",
                collars_with_data=8,
                collars_total=10,
                coverage_pct=80.0,
                source_row_ids=["c-1", "c-2"],
            ),
        ]
    return CoverageGapResult(
        ingest_gap=IngestGapStats(
            indexed=indexed,
            processed=indexed,
            gap_pct=0.0,
        ),
        attribute_coverage=attribute_coverage,
        findings=[
            CoverageFindingRow(
                kind="missing_assays",
                severity="WARNING",
                description="2 collars without assay data",
                source_row_ids=["c-9", "c-10"],
            )
            for _ in range(findings)
        ],
        project_id="proj-1",
        workspace_id="ws-1",
        count=len(attribute_coverage) + findings,
        gap_geojson=gap_geojson,
    )


# ---------------------------------------------------------------------------
# Branch 1 — DrillTrace3DResult (intent-agnostic, highest precedence)
# ---------------------------------------------------------------------------


def test_drill_trace_3d_emits_drill_trace_3d_chart():
    """DrillTrace3DResult with count>0 → chart_type='drill_trace_3d'."""
    _, viz = _build_chat_card_payloads(
        intent="synthesis",
        tool_results=[("query_drill_traces_3d", _drill_trace_3d(count=2))],
    )
    assert viz is not None
    assert viz.chart_type == "drill_trace_3d"


def test_drill_trace_3d_meta_carries_collars_intervals_structures():
    """Pin the meta shape InlineViz reads: collars + intervals + structures."""
    _, viz = _build_chat_card_payloads(
        intent="factual_lookup",
        tool_results=[("query_drill_traces_3d", _drill_trace_3d())],
    )
    meta = viz.plotly_layout["meta"]
    assert "collars" in meta and len(meta["collars"]) > 0
    assert "intervals" in meta
    assert "structures" in meta
    assert meta["project_id"] == "proj-1"


def test_drill_trace_3d_count_zero_falls_through():
    """count==0 means no collars found — must NOT emit the card."""
    _, viz = _build_chat_card_payloads(
        intent="synthesis",
        tool_results=[("query_drill_traces_3d", _drill_trace_3d(count=0))],
    )
    assert viz is None


def test_drill_trace_3d_title_uses_hole_id_when_filtered():
    """When hole_id_filter is set, the title names the specific hole."""
    _, viz = _build_chat_card_payloads(
        intent="factual_lookup",
        tool_results=[("query_drill_traces_3d", _drill_trace_3d(hole_id_filter="ECK-22-001"))],
    )
    assert "ECK-22-001" in viz.title


def test_drill_trace_3d_title_uses_count_when_unfiltered():
    """No hole_id_filter → title shows the count instead."""
    _, viz = _build_chat_card_payloads(
        intent="synthesis",
        tool_results=[("query_drill_traces_3d", _drill_trace_3d(count=5, hole_id_filter=None))],
    )
    assert "5" in viz.title or "hole(s)" in viz.title


# ---------------------------------------------------------------------------
# Branch 2 — CollarDetailsResult (intent-agnostic, downhole_strip)
# ---------------------------------------------------------------------------


def test_collar_details_single_hole_emits_downhole_strip():
    """CollarDetailsResult with count==1 + hole_id → chart_type='downhole_strip'."""
    _, viz = _build_chat_card_payloads(
        intent="factual_lookup",
        tool_results=[("query_collar_details", _collar_details(count=1, hole_id="ECK-22-001"))],
    )
    assert viz is not None
    assert viz.chart_type == "downhole_strip"
    assert viz.plotly_layout["meta"]["hole_id"] == "ECK-22-001"
    assert viz.plotly_layout["meta"]["collar_id"] == "coll-1"


def test_collar_details_count_zero_falls_through():
    """Hole not found (count=0) → no strip-log card."""
    _, viz = _build_chat_card_payloads(
        intent="factual_lookup",
        tool_results=[("query_collar_details", _collar_details(count=0))],
    )
    assert viz is None


def test_collar_details_missing_hole_id_falls_through():
    """count=1 but no hole_id (data integrity issue) → don't emit."""
    _, viz = _build_chat_card_payloads(
        intent="factual_lookup",
        tool_results=[("query_collar_details", _collar_details(count=1, hole_id=None))],
    )
    assert viz is None


# ---------------------------------------------------------------------------
# Branch 3 — StereonetResult (intent-agnostic)
# ---------------------------------------------------------------------------


def test_stereonet_emits_stereonet_chart():
    """StereonetResult with count>0 → chart_type='stereonet'."""
    _, viz = _build_chat_card_payloads(
        intent="synthesis",
        tool_results=[("query_stereonet", _stereonet(count=5))],
    )
    assert viz is not None
    assert viz.chart_type == "stereonet"


def test_stereonet_meta_carries_image_base64_and_points():
    """Pin the meta the StereonetCard reads: image_base64 + points."""
    _, viz = _build_chat_card_payloads(
        intent="synthesis",
        tool_results=[("query_stereonet", _stereonet(count=3))],
    )
    meta = viz.plotly_layout["meta"]
    assert meta["image_base64"].startswith("iVBORw")
    assert meta["structure_count"] == 3
    assert len(meta["points"]) == 3
    assert meta["projection"] == "Schmidt"


def test_stereonet_count_zero_falls_through():
    """No points → no card."""
    _, viz = _build_chat_card_payloads(
        intent="synthesis",
        tool_results=[("query_stereonet", _stereonet(count=0))],
    )
    assert viz is None


# ---------------------------------------------------------------------------
# Branch 4 — intent gate (project_summary / coverage_gap only)
# ---------------------------------------------------------------------------


def test_unsupported_intent_with_empty_tools_returns_none():
    """No intent-agnostic results + intent ∉ (project_summary, coverage_gap)
    → (None, None). This is the fast-exit path for chat-style queries."""
    m, v = _build_chat_card_payloads(intent="factual_lookup", tool_results=[])
    assert m is None and v is None

    m, v = _build_chat_card_payloads(intent="synthesis", tool_results=[])
    assert m is None and v is None


def test_none_intent_falls_through():
    """intent=None (classifier never ran) → (None, None)."""
    m, v = _build_chat_card_payloads(intent=None, tool_results=[])
    assert m is None and v is None


# ---------------------------------------------------------------------------
# Branch 5 — project_summary → technique_timeline
# ---------------------------------------------------------------------------


def test_project_summary_emits_technique_timeline():
    """ProjectSummaryResult under intent='project_summary' →
    chart_type='technique_timeline'."""
    _, viz = _build_chat_card_payloads(
        intent="project_summary",
        tool_results=[("get_project_summary", _project_summary())],
    )
    assert viz is not None
    assert viz.chart_type == "technique_timeline"


def test_project_summary_meta_carries_swimlanes_and_breakdown_table():
    """Pin the meta TimelineCard reads."""
    _, viz = _build_chat_card_payloads(
        intent="project_summary",
        tool_results=[("get_project_summary", _project_summary(year=2022))],
    )
    meta = viz.plotly_layout["meta"]
    assert "swimlanes" in meta and len(meta["swimlanes"]) == 1
    assert meta["swimlanes"][0]["technique"] == "DDH"
    assert meta["swimlanes"][0]["year_start"] == 2022
    assert "breakdown_table" in meta and len(meta["breakdown_table"]) == 1
    assert "extraction_pending_fields" in meta


def test_project_summary_year_none_excluded_from_swimlanes():
    """Rows with year=None go into breakdown_table but NOT swimlanes
    (timeline can't render a swimlane without a year)."""
    _, viz = _build_chat_card_payloads(
        intent="project_summary",
        tool_results=[("get_project_summary", _project_summary(year=None))],
    )
    meta = viz.plotly_layout["meta"]
    assert meta["swimlanes"] == []
    assert len(meta["breakdown_table"]) == 1


def test_project_summary_no_breakdown_falls_through():
    """Empty technique_breakdown → no card (no point rendering an empty chart)."""
    _, viz = _build_chat_card_payloads(
        intent="project_summary",
        tool_results=[("get_project_summary", _project_summary(has_breakdown=False))],
    )
    assert viz is None


def test_project_summary_wrong_intent_falls_through():
    """ProjectSummaryResult under intent='synthesis' → no card.
    The intent gate prevents emission when the user didn't ask for it."""
    _, viz = _build_chat_card_payloads(
        intent="synthesis",
        tool_results=[("get_project_summary", _project_summary())],
    )
    assert viz is None


# ---------------------------------------------------------------------------
# Branch 6 — coverage_gap → coverage_table + PR-2 placeholder MapPayload
# ---------------------------------------------------------------------------


def test_coverage_gap_emits_coverage_table():
    """CoverageGapResult under intent='coverage_gap' →
    chart_type='coverage_table'."""
    _, viz = _build_chat_card_payloads(
        intent="coverage_gap",
        tool_results=[("compute_coverage_gap", _coverage_gap())],
    )
    assert viz is not None
    assert viz.chart_type == "coverage_table"


def test_coverage_gap_meta_carries_rows_ingest_gap_findings():
    """Pin the meta CoverageTableCard reads."""
    _, viz = _build_chat_card_payloads(
        intent="coverage_gap",
        tool_results=[("compute_coverage_gap", _coverage_gap(findings=2))],
    )
    meta = viz.plotly_layout["meta"]
    assert "rows" in meta and len(meta["rows"]) == 1
    assert meta["rows"][0]["attribute"] == "assays_v2"
    assert meta["rows"][0]["coverage_pct"] == 80.0
    assert "ingest_gap" in meta
    assert meta["ingest_gap"]["indexed"] == 100
    assert "findings" in meta and len(meta["findings"]) == 2


def test_coverage_gap_fallback_when_gap_geojson_is_none():
    """When the tool couldn't produce a real geojson (no geom_4326 column,
    DB error, project with zero collars) the dispatcher emits the empty
    FeatureCollection fallback so the frontend renders the disabled-map
    hint. This locks the §6b P4 fallback path."""
    # gap_geojson=None is the default — simulates a tool failure
    map_payload, _ = _build_chat_card_payloads(
        intent="coverage_gap",
        tool_results=[("compute_coverage_gap", _coverage_gap(gap_geojson=None))],
    )
    assert map_payload is not None
    assert map_payload.layer_type == "collar"
    assert map_payload.geojson == {"type": "FeatureCollection", "features": []}
    assert "no collar geometries" in (map_payload.label or "")


def test_coverage_gap_emits_real_geojson_when_tool_provides_one():
    """§6b P4 — when the coverage tool produces a real per-collar
    FeatureCollection, the dispatcher ships it as the map_payload.geojson
    directly so InlineViz MapView can render the spatial-holes layer."""
    real_geojson = _coverage_geojson(feature_count=3)
    map_payload, _ = _build_chat_card_payloads(
        intent="coverage_gap",
        tool_results=[
            ("compute_coverage_gap", _coverage_gap(gap_geojson=real_geojson)),
        ],
    )
    assert map_payload is not None
    # Pydantic clones the dict during MapPayload construction so identity
    # isn't preserved — equality is the actual contract.
    assert map_payload.geojson == real_geojson
    assert len(map_payload.geojson["features"]) == 3
    assert "3 collar(s)" in (map_payload.label or "")


def test_coverage_gap_real_geojson_label_pluralises_count():
    """Label shows the collar count so users know how much coverage data
    they're looking at without inspecting the legend."""
    real_geojson = _coverage_geojson(feature_count=42)
    map_payload, _ = _build_chat_card_payloads(
        intent="coverage_gap",
        tool_results=[
            ("compute_coverage_gap", _coverage_gap(gap_geojson=real_geojson)),
        ],
    )
    assert "42 collar(s)" in (map_payload.label or "")


def test_coverage_gap_empty_features_falls_back_to_placeholder():
    """Edge case: tool returned a geojson but its features list is empty
    (e.g. all collars have NULL geom_4326). Treat as failure → empty
    FeatureCollection fallback, not 'real' geojson with 0 features."""
    empty_geojson = {"type": "FeatureCollection", "features": []}
    map_payload, _ = _build_chat_card_payloads(
        intent="coverage_gap",
        tool_results=[
            ("compute_coverage_gap", _coverage_gap(gap_geojson=empty_geojson)),
        ],
    )
    assert map_payload is not None
    # Empty real geojson → fall back to the "no geometries" path so the
    # frontend renders the hint rather than an actually-empty map.
    assert map_payload.geojson["features"] == []
    assert "no collar geometries" in (map_payload.label or "")


def test_coverage_gap_no_rows_no_findings_falls_through():
    """Empty coverage + no findings → no card. The condition is:
    has attribute_coverage OR ingest_gap.indexed>0 OR findings>0."""
    result = _coverage_gap(has_attribute_coverage=False, indexed=0, findings=0)
    m, v = _build_chat_card_payloads(
        intent="coverage_gap",
        tool_results=[("compute_coverage_gap", result)],
    )
    assert m is None and v is None


def test_coverage_gap_with_ingest_gap_only_emits():
    """Even with no attribute rows, a non-zero ingest_gap triggers the card."""
    _, viz = _build_chat_card_payloads(
        intent="coverage_gap",
        tool_results=[
            ("compute_coverage_gap",
             _coverage_gap(has_attribute_coverage=False, indexed=50, findings=0)),
        ],
    )
    assert viz is not None
    assert viz.chart_type == "coverage_table"


def test_coverage_gap_wrong_intent_falls_through():
    """CoverageGapResult under intent='synthesis' → no card."""
    _, viz = _build_chat_card_payloads(
        intent="synthesis",
        tool_results=[("compute_coverage_gap", _coverage_gap())],
    )
    assert viz is None


# ---------------------------------------------------------------------------
# Precedence — multiple results in the same response
# ---------------------------------------------------------------------------


def test_drill_trace_3d_wins_over_project_summary():
    """When the user asks for a 3D view in a project_summary context, the
    3D card takes precedence — the user's explicit ask wins over the
    intent-default card."""
    _, viz = _build_chat_card_payloads(
        intent="project_summary",
        tool_results=[
            ("query_drill_traces_3d", _drill_trace_3d()),
            ("get_project_summary", _project_summary()),
        ],
    )
    assert viz.chart_type == "drill_trace_3d"


def test_stereonet_wins_over_coverage_gap():
    """Stereonet (intent-agnostic, explicit user ask) beats coverage_gap
    intent default. Same precedence principle as drill_trace_3d."""
    _, viz = _build_chat_card_payloads(
        intent="coverage_gap",
        tool_results=[
            ("query_stereonet", _stereonet()),
            ("compute_coverage_gap", _coverage_gap()),
        ],
    )
    assert viz.chart_type == "stereonet"


def test_drill_trace_3d_wins_over_stereonet():
    """Within the intent-agnostic precedence tier, drill_trace_3d comes
    FIRST in source order — verify it wins when both are present."""
    _, viz = _build_chat_card_payloads(
        intent="synthesis",
        tool_results=[
            ("query_drill_traces_3d", _drill_trace_3d()),
            ("query_stereonet", _stereonet()),
        ],
    )
    assert viz.chart_type == "drill_trace_3d"


def test_collar_details_wins_over_stereonet_per_source_order():
    """CollarDetails appears before stereonet in the dispatch function —
    pin the order so a refactor doesn't silently reverse it."""
    _, viz = _build_chat_card_payloads(
        intent="factual_lookup",
        tool_results=[
            ("query_collar_details", _collar_details()),
            ("query_stereonet", _stereonet()),
        ],
    )
    assert viz.chart_type == "downhole_strip"


# ---------------------------------------------------------------------------
# Defensive: unknown result types ignored
# ---------------------------------------------------------------------------


def test_unknown_tool_result_types_are_ignored():
    """The dispatcher uses isinstance() checks; anything else passes
    through. Tool results that don't trigger a card should not crash."""
    m, v = _build_chat_card_payloads(
        intent="factual_lookup",
        tool_results=[
            ("search_documents", ["some", "list", "of", "strings"]),
            ("traverse_knowledge_graph", {"nodes": [], "edges": []}),
            ("some_future_tool", object()),
        ],
    )
    assert m is None and v is None


def test_empty_tool_results_with_supported_intent_returns_none():
    """intent=project_summary but no actual ProjectSummaryResult in
    tool_results → no card. Pin the safe-degrade behaviour."""
    m, v = _build_chat_card_payloads(intent="project_summary", tool_results=[])
    assert m is None and v is None

    m, v = _build_chat_card_payloads(intent="coverage_gap", tool_results=[])
    assert m is None and v is None
