"""Index Public Geoscience — populate Qdrant collections.

Phase 3.2 of the Public Geoscience feature. One Dagster asset that reads the
four canonical `public_geo.pg_*` tables and writes into four dedicated
Qdrant collections per plan §05d:

    Collection                   ← Canonical table
    pg_mine                      ← public_geo.pg_mine
    pg_mineral_occurrence        ← public_geo.pg_mineral_occurrence
    pg_drillhole_collar          ← public_geo.pg_drillhole_collar
    pg_resource_potential_zone   ← public_geo.pg_resource_potential_zone

Embedding model: BAAI/bge-small-en-v1.5 (384-dim, cosine) — same model as the
internal `georag_chunks` collection so a single cross-corpus retrieval path
can union results without re-embedding the query.

Each vector's text is a *structured natural-language summary* built from the
canonical fields (not raw JSON); the payload carries the fields the chat
tool needs for post-retrieval filtering + citation resolution:

    payload = {
        jurisdiction_code,        # e.g. "CA-SK"
        source_id,                # e.g. "CA-SK-SMDI"
        source_feature_id,        # upstream OBJECTID
        canonical_type,           # "mine" | "mineral_occurrence" | ...
        pg_id,                    # UUID on the canonical PG table
        commodities,              # str[]
        commodity_grouping,       # str | None
        status,                   # enum str | None (only on mine / occurrence)
        geom_bbox,                # [minLon, minLat, maxLon, maxLat]
        source_url,               # deep link back to upstream
        summary_text,             # human-readable — same as the embedded text
    }

Idempotency: points use a deterministic UUID derived from `pg_id` so re-
running the asset updates existing points in place rather than creating
duplicates. This means a full re-run after a Silver refresh is safe and
cheap. Embedding is CPU-bound so we short-circuit unchanged rows (same
`checksum` seen on a previous upsert) — in practice this means the second run
after fresh data skips most embedding work.

NOTE: Do NOT add `from __future__ import annotations` to this file. Dagster
1.13 Config classes use Pydantic for type introspection and that import
breaks runtime annotation evaluation.
"""

import os
import uuid
from typing import Any

import psycopg2.extras
from dagster import (
    AssetExecutionContext,
    Config,
    MaterializeResult,
    MetadataValue,
    asset,
)

from georag_dagster.assets.sparse_encoder import (
    SPARSE_MODEL_VERSION,
    encode_sparse_batch,
)
from georag_dagster.assets.silver_public_geoscience import (
    silver_pg_ca_sk_assessment_airborne,
    silver_pg_ca_sk_assessment_ground,
    silver_pg_ca_sk_assessment_underground,
    silver_pg_ca_sk_drillhole,
    silver_pg_ca_sk_mine_loc,
    silver_pg_ca_sk_resource_potential,
    silver_pg_ca_sk_rock_samples,
    silver_pg_ca_sk_smdi,
)
from georag_dagster.resources import PostgresResource, QdrantResource


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Audit 2026-06-27 (C1): the public-geoscience pg_* collections are a SEPARATE
# 384-dim bge-small vector space — NOT migrated to Qwen3 (only georag_chunks was,
# on 2026-06-03). Do NOT wire this to the shared EMBEDDING_MODEL_NAME /
# EMBEDDING_DIMENSION env (those now resolve to Qwen3/1024 on FastAPI) or it
# would collide 1024-dim vectors into these 384-dim collections. Pin bge here.
EMBED_MODEL_NAME = os.environ.get("PUBLIC_GEO_EMBED_MODEL_NAME", "BAAI/bge-small-en-v1.5")
EMBED_DIMENSIONS = 384
EMBED_BATCH_SIZE = 32
UPSERT_BATCH_SIZE = 100

# Default workspace UUID for multi-tenant isolation (GI-9).
# All public geoscience points are stamped with this so hybrid_query()
# workspace_id filter finds them under the default workspace.
# Module 9 will parameterize this when workspace-scoped ingestion ships.
DEFAULT_WORKSPACE_UUID = "a0000000-0000-0000-0000-000000000001"

# Qdrant expects UUIDs (or integers) for point ids. We derive one from the
# canonical pg_id so re-embedding the same row replaces the previous point.
_POINT_ID_NS = uuid.UUID("f7d6e3a1-6a82-4b4b-9b5a-b38e0c3f1a01")


def _deterministic_point_id(pg_id: str) -> str:
    return str(uuid.uuid5(_POINT_ID_NS, pg_id))


# Per-canonical-type collection metadata. Keeping them in a single registry
# so every function in this file reads from one place.
COLLECTIONS: dict[str, dict[str, Any]] = {
    "mine": {
        "name": "pg_mine",
        "table": "public_geo.pg_mine",
    },
    "mineral_occurrence": {
        "name": "pg_mineral_occurrence",
        "table": "public_geo.pg_mineral_occurrence",
    },
    "drillhole_collar": {
        "name": "pg_drillhole_collar",
        "table": "public_geo.pg_drillhole_collar",
    },
    "resource_potential_zone": {
        "name": "pg_resource_potential_zone",
        "table": "public_geo.pg_resource_potential_zone",
    },
    "rock_sample": {
        "name": "pg_rock_sample",
        "table": "public_geo.pg_rock_sample",
    },
    "assessment_survey": {
        "name": "pg_assessment_survey",
        "table": "public_geo.pg_assessment_survey",
    },
}


# ---------------------------------------------------------------------------
# Embedding model — cached at module scope (load is ~500 ms)
# ---------------------------------------------------------------------------

_MODEL = None


def _get_model():
    global _MODEL  # noqa: PLW0603
    if _MODEL is None:
        from sentence_transformers import SentenceTransformer  # noqa: PLC0415
        _MODEL = SentenceTransformer(EMBED_MODEL_NAME)
    return _MODEL


# ---------------------------------------------------------------------------
# Collection provisioning — CREATE IF NOT EXISTS with payload indices.
# Plan §05d lists the indexable payload fields. Qdrant needs these declared
# up-front so filter queries hit an index rather than scanning.
# ---------------------------------------------------------------------------

PAYLOAD_KEYWORD_FIELDS = [
    "jurisdiction_code",
    "source_id",
    "canonical_type",
    "commodity_grouping",
    "status",
    # Tier 1 expansion — filterable keywords for rock samples + surveys.
    # Having these as indexed payload keys makes "airborne surveys in SK"
    # and "rock samples in NTS 74H" O(index-lookup) instead of O(scan).
    "survey_type",
    "nts_250k",
]

PAYLOAD_KEYWORD_ARRAY_FIELDS = [
    "commodities",  # TEXT[] in Postgres → keyword list in Qdrant
]


def _ensure_collection(client, collection_name: str, context: AssetExecutionContext) -> None:
    """Create the collection if missing, patch its settings, ensure payload indices.

    Qdrant review:
      #1 — `default_segment_number=2` + `indexing_threshold=5000` force HNSW
           index to build on segments the moment they cross 5k points.
           Previous defaults (8 segments at 10k threshold) meant 33k-point
           collections were NEVER indexed and every search was brute-force.
      #3 — Explicit int8 scalar quantization at collection creation. The
           cluster-level env-var defaults in docker-compose don't apply
           retroactively; they must be specified here or left null.
      #4 — `m=32` (double the Qdrant default of 16). For chat RAG where a
           missed retrieval = wrong answer, the ~2× graph-RAM cost pays
           back in +5% recall@10 on ms-marco benchmarks.
    """
    from qdrant_client.models import (  # noqa: PLC0415
        Distance,
        HnswConfigDiff,
        OptimizersConfigDiff,
        ScalarQuantization,
        ScalarQuantizationConfig,
        ScalarType,
        SparseIndexParams,
        SparseVectorParams,
        VectorParams,
    )

    existing = {c.name for c in client.get_collections().collections}
    # Audit 2026-06-27 (C1) dim-parity guard: refuse to embed into a collection
    # whose dense size disagrees with EMBED_DIMENSIONS (here, 384/bge). Backstop
    # against a model/dim swap silently corrupting these public-geo collections.
    if collection_name in existing:
        _info = client.get_collection(collection_name)
        _vp = _info.config.params.vectors
        _dim = _vp[""].size if isinstance(_vp, dict) else getattr(_vp, "size", None)
        if _dim is not None and _dim != EMBED_DIMENSIONS:
            raise RuntimeError(
                f"{collection_name} dense dim={_dim} != EMBED_DIMENSIONS="
                f"{EMBED_DIMENSIONS} (model {EMBED_MODEL_NAME}); refusing to embed "
                "at a mismatched dimension."
            )
    if collection_name not in existing:
        # Module 4 Chunk 2 cleanup: create with named dense "" + sparse "text"
        # slots so hybrid_query() Prefetch branches can address each slot by name.
        client.create_collection(
            collection_name=collection_name,
            vectors_config={
                "": VectorParams(
                    size=EMBED_DIMENSIONS,
                    distance=Distance.COSINE,
                    # Keep full-precision vectors in RAM until Tier-3 scale-out
                    # pushes us past ~5M points per collection. Then flip to
                    # on_disk=True with quantized in-RAM rescoring.
                    on_disk=False,
                ),
            },
            sparse_vectors_config={
                "text": SparseVectorParams(
                    index=SparseIndexParams(on_disk=False),
                )
            },
            hnsw_config=HnswConfigDiff(
                m=32,              # Qdrant review #4
                ef_construct=256,  # Qdrant review #4
            ),
            optimizers_config=OptimizersConfigDiff(
                indexing_threshold=5000,     # Qdrant review #1
                default_segment_number=2,    # Qdrant review #1
            ),
            quantization_config=ScalarQuantization(  # Qdrant review #3
                scalar=ScalarQuantizationConfig(
                    type=ScalarType.INT8,
                    always_ram=True,   # quantized vectors stay in RAM
                    quantile=0.99,     # tighter buckets → better recall
                ),
            ),
        )
        context.log.info(
            "Qdrant: created collection '%s' with dense+sparse slots", collection_name,
        )
    else:
        # For existing collections, PATCH the dynamic settings. HNSW `m` is
        # IMMUTABLE after creation — a live change requires a re-index via
        # the collection-aliasing pattern (see docs/RUNBOOK.md). What we
        # CAN change in-place is the optimizer config that unblocks
        # indexing of existing segments.
        # Fix: was `optimizer_config=` (wrong kwarg); correct is `optimizers_config=`.
        client.update_collection(
            collection_name=collection_name,
            optimizers_config=OptimizersConfigDiff(
                indexing_threshold=5000,
                default_segment_number=2,
            ),
        )
        context.log.info(
            "Qdrant: patched '%s' with indexing_threshold=5000 + default_segment_number=2",
            collection_name,
        )

    # Payload indices — safe to call repeatedly; Qdrant returns OK if the
    # index already exists.
    for field in PAYLOAD_KEYWORD_FIELDS:
        client.create_payload_index(
            collection_name=collection_name,
            field_name=field,
            field_schema="keyword",
        )
    for field in PAYLOAD_KEYWORD_ARRAY_FIELDS:
        client.create_payload_index(
            collection_name=collection_name,
            field_name=field,
            field_schema="keyword",
        )


# ---------------------------------------------------------------------------
# SQL — one query per canonical type. Jurisdictions must be `active` so
# we don't eagerly index half-seeded coming-soon entries.
# ---------------------------------------------------------------------------

MINE_SQL = """
SELECT
    m.id::text            AS pg_id,
    m.jurisdiction_code,
    m.source_id,
    m.source_feature_id,
    m.name,
    m.status,
    m.commodities,
    m.commodity_grouping,
    m.operator,
    m.source_url,
    m.checksum,
    ST_X(m.geom::geometry) AS lon,
    ST_Y(m.geom::geometry) AS lat,
    j.display_name          AS jurisdiction_name,
    j.primary_authority
  FROM public_geo.pg_mine m
  JOIN public_geo.jurisdictions j
       ON j.jurisdiction_code = m.jurisdiction_code
 WHERE j.status = 'active'
"""

OCCURRENCE_SQL = """
SELECT
    o.id::text            AS pg_id,
    o.jurisdiction_code,
    o.source_id,
    o.source_feature_id,
    o.external_id,
    o.name,
    o.historic_names,
    o.status,
    o.primary_commodities,
    o.associated_commodities,
    o.commodity_grouping,
    o.discovery_type,
    o.production_flag,
    o.reserves_resources,
    o.source_url,
    o.checksum,
    ST_X(o.geom::geometry) AS lon,
    ST_Y(o.geom::geometry) AS lat,
    j.display_name         AS jurisdiction_name
  FROM public_geo.pg_mineral_occurrence o
  JOIN public_geo.jurisdictions j
       ON j.jurisdiction_code = o.jurisdiction_code
 WHERE j.status = 'active'
"""

DRILLHOLE_SQL = """
SELECT
    d.id::text            AS pg_id,
    d.jurisdiction_code,
    d.source_id,
    d.source_feature_id,
    d.drillhole_id,
    d.drillhole_name,
    d.company,
    d.project_name,
    d.date_drilled::text  AS date_drilled,
    d.drill_type,
    d.commodity_of_interest,
    d.total_length_m,
    d.collar_elevation_m,
    d.stratigraphic_depths,
    d.core_availability,
    d.disposition,
    d.source_url,
    d.checksum,
    ST_X(d.geom::geometry) AS lon,
    ST_Y(d.geom::geometry) AS lat,
    j.display_name         AS jurisdiction_name
  FROM public_geo.pg_drillhole_collar d
  JOIN public_geo.jurisdictions j
       ON j.jurisdiction_code = d.jurisdiction_code
 WHERE j.status = 'active'
"""

ROCK_SAMPLE_SQL = """
SELECT
    rs.id::text            AS pg_id,
    rs.jurisdiction_code,
    rs.source_id,
    rs.source_feature_id,
    rs.station,
    rs.sample_number,
    rs.geologist,
    rs.geographic_area,
    rs.report_number,
    rs.map_number,
    rs.nts_250k,
    rs.nts_50k,
    rs.date_collected::text AS date_collected,
    rs.source_url,
    rs.checksum,
    ST_X(rs.geom::geometry) AS lon,
    ST_Y(rs.geom::geometry) AS lat,
    j.display_name          AS jurisdiction_name
  FROM public_geo.pg_rock_sample rs
  JOIN public_geo.jurisdictions j
       ON j.jurisdiction_code = rs.jurisdiction_code
 WHERE j.status = 'active'
"""

ASSESSMENT_SURVEY_SQL = """
SELECT
    a.id::text             AS pg_id,
    a.jurisdiction_code,
    a.source_id,
    a.source_feature_id,
    a.survey_type,
    a.source_url,
    a.checksum,
    ST_XMin(a.geom::geometry) AS min_lon,
    ST_YMin(a.geom::geometry) AS min_lat,
    ST_XMax(a.geom::geometry) AS max_lon,
    ST_YMax(a.geom::geometry) AS max_lat,
    j.display_name         AS jurisdiction_name
  FROM public_geo.pg_assessment_survey a
  JOIN public_geo.jurisdictions j
       ON j.jurisdiction_code = a.jurisdiction_code
 WHERE j.status = 'active'
"""

RESOURCE_POTENTIAL_SQL = """
SELECT
    r.id::text            AS pg_id,
    r.jurisdiction_code,
    r.source_id,
    r.source_feature_id,
    r.commodity,
    r.commodity_grouping,
    r.potential_rank,
    r.methodology_ref,
    r.checksum,
    ST_XMin(r.geom::geometry) AS min_lon,
    ST_YMin(r.geom::geometry) AS min_lat,
    ST_XMax(r.geom::geometry) AS max_lon,
    ST_YMax(r.geom::geometry) AS max_lat,
    j.display_name            AS jurisdiction_name
  FROM public_geo.pg_resource_potential_zone r
  JOIN public_geo.jurisdictions j
       ON j.jurisdiction_code = r.jurisdiction_code
 WHERE j.status = 'active'
"""


# ---------------------------------------------------------------------------
# Summary builders — structured natural-language text per canonical type
# ---------------------------------------------------------------------------

def _fmt_list(values: Any, fallback: str = "unknown") -> str:
    if not values:
        return fallback
    if isinstance(values, (list, tuple)):
        clean = [str(v) for v in values if v is not None and str(v).strip()]
        return ", ".join(clean) if clean else fallback
    return str(values) or fallback


def _fmt(value: Any, fallback: str = "unknown") -> str:
    if value is None or value == "":
        return fallback
    return str(value)


def _grouping_label(grouping: str | None) -> str:
    if not grouping:
        return "unclassified commodity"
    return grouping.replace("_", " ")


def _mine_summary(row: dict) -> str:
    return (
        f"{_fmt(row.get('status'), 'status-unknown').title()} mine "
        f"'{_fmt(row.get('name'), 'Unnamed')}' "
        f"in {_fmt(row.get('jurisdiction_name'), 'unknown jurisdiction')} "
        f"({_grouping_label(row.get('commodity_grouping'))}). "
        f"Commodities: {_fmt_list(row.get('commodities'))}. "
        f"Operator: {_fmt(row.get('operator'), 'unspecified')}. "
        f"Government record: {_fmt(row.get('source_id'))} "
        f"#{_fmt(row.get('source_feature_id'))}."
    )


def _occurrence_summary(row: dict) -> str:
    status = _fmt(row.get("status"), "status-unknown")
    # `external_id` is the V1.2-renamed jurisdiction-native identifier
    # (was `smdi_id` in V1.0–V1.1). Label it generically; the per-source
    # convention (SMDI for SK, MINFILE for BC, etc.) is encoded in
    # `source_id` which the chat tool already surfaces.
    ext_id = _fmt(row.get("external_id"), None) if row.get("external_id") else None
    ext_id_str = f"#{ext_id}" if ext_id else "no external identifier"
    return (
        f"{status.title()} mineral occurrence "
        f"'{_fmt(row.get('name'), 'Unnamed')}' ({ext_id_str}) "
        f"in {_fmt(row.get('jurisdiction_name'))}, "
        f"{_grouping_label(row.get('commodity_grouping'))}. "
        f"Primary commodities: {_fmt_list(row.get('primary_commodities'))}. "
        f"Associated: {_fmt_list(row.get('associated_commodities'), 'none listed')}. "
        f"Discovery type: {_fmt(row.get('discovery_type'), 'not specified')}. "
        f"Historical production: {'yes' if row.get('production_flag') else 'none recorded'}. "
        f"Government record: {_fmt(row.get('source_id'))} "
        f"#{_fmt(row.get('source_feature_id'))}."
    )


def _drillhole_summary(row: dict) -> str:
    name = _fmt(row.get("drillhole_name"), _fmt(row.get("drillhole_id"), "Unnamed"))
    strat = row.get("stratigraphic_depths") or {}
    strat_parts: list[str] = []
    if isinstance(strat, dict):
        for key, label in (
            ("base_quaternary_m", "base of Quaternary"),
            ("base_phanerozoic_m", "base of Phanerozoic"),
            ("base_athabasca_m", "base of Athabasca Group"),
            ("top_basement_m", "top of crystalline basement"),
        ):
            v = strat.get(key)
            if v is not None:
                strat_parts.append(f"{label} at {v}m")
    strat_str = "; ".join(strat_parts) if strat_parts else "no stratigraphic markers"

    return (
        f"Drillhole '{name}' (public ID {_fmt(row.get('drillhole_id'))}) "
        f"in {_fmt(row.get('jurisdiction_name'))}. "
        f"Operator {_fmt(row.get('company'), 'unknown operator')} at project "
        f"'{_fmt(row.get('project_name'), 'unspecified project')}'. "
        f"Drilled {_fmt(row.get('date_drilled'), 'date unknown')} as "
        f"{_fmt(row.get('drill_type'), 'drill type unknown')}, "
        f"total depth {_fmt(row.get('total_length_m'))}m, "
        f"collar elevation {_fmt(row.get('collar_elevation_m'))}m. "
        f"Target commodities: {_fmt_list(row.get('commodity_of_interest'), 'none listed')}. "
        f"Stratigraphic depths: {strat_str}. "
        f"Core availability: {_fmt(row.get('core_availability'))}. "
        f"Disposition: {_fmt(row.get('disposition'), 'not specified')}. "
        f"Government record: {_fmt(row.get('source_id'))} "
        f"#{_fmt(row.get('source_feature_id'))}."
    )


def _rock_sample_summary(row: dict) -> str:
    sample = _fmt(row.get("sample_number"), _fmt(row.get("station"), "unlabelled"))
    return (
        f"Government rock sample '{sample}' collected in "
        f"{_fmt(row.get('geographic_area'), 'unspecified area')} "
        f"of {_fmt(row.get('jurisdiction_name'))} "
        f"(NTS 1:250K {_fmt(row.get('nts_250k'), 'unknown tile')}). "
        f"Geologist: {_fmt(row.get('geologist'), 'unrecorded')}. "
        f"Collection date: {_fmt(row.get('date_collected'), 'not recorded')}. "
        f"Referenced in report {_fmt(row.get('report_number'), 'no report linked')}. "
        f"Government record: {_fmt(row.get('source_id'))} "
        f"#{_fmt(row.get('source_feature_id'))}."
    )


def _assessment_survey_summary(row: dict) -> str:
    stype = _fmt(row.get("survey_type"), "assessment")
    label = {
        "airborne": "Airborne geophysical",
        "ground": "Ground geophysical",
        "underground": "Underground",
    }.get(stype, stype.title())
    return (
        f"{label} survey footprint in {_fmt(row.get('jurisdiction_name'))}. "
        f"Covers the polygon delimited by approximately "
        f"({_fmt(row.get('min_lon'))}, {_fmt(row.get('min_lat'))}) — "
        f"({_fmt(row.get('max_lon'))}, {_fmt(row.get('max_lat'))}). "
        f"Detailed survey content lives in the linked SMAD assessment "
        f"filing. Government record: {_fmt(row.get('source_id'))} "
        f"#{_fmt(row.get('source_feature_id'))}."
    )


def _resource_potential_summary(row: dict) -> str:
    rank = row.get("potential_rank")
    rank_str = f"rank {rank}/6" if rank is not None else "rank unspecified"
    return (
        f"{_fmt(row.get('commodity'), 'Unknown commodity').title()} "
        f"resource potential zone ({rank_str}) "
        f"in {_fmt(row.get('jurisdiction_name'))}, "
        f"{_grouping_label(row.get('commodity_grouping'))}. "
        f"Methodology: {_fmt(row.get('methodology_ref'), 'not referenced')}. "
        f"Government record: {_fmt(row.get('source_id'))} "
        f"#{_fmt(row.get('source_feature_id'))}."
    )


# ---------------------------------------------------------------------------
# Payload builders
# ---------------------------------------------------------------------------

def _bbox_for_point(row: dict) -> list[float] | None:
    lon, lat = row.get("lon"), row.get("lat")
    if lon is None or lat is None:
        return None
    return [float(lon), float(lat), float(lon), float(lat)]


def _bbox_for_polygon(row: dict) -> list[float] | None:
    try:
        return [
            float(row["min_lon"]),
            float(row["min_lat"]),
            float(row["max_lon"]),
            float(row["max_lat"]),
        ]
    except (KeyError, TypeError, ValueError):
        return None


def _clean_list(values: Any) -> list[str]:
    if not values:
        return []
    if isinstance(values, (list, tuple)):
        return [str(v) for v in values if v is not None and str(v).strip()]
    return [str(values)]


def _mine_payload(row: dict, summary: str) -> dict[str, Any]:
    return {
        "jurisdiction_code":  row["jurisdiction_code"],
        "source_id":          row["source_id"],
        "source_feature_id":  row["source_feature_id"],
        "canonical_type":     "mine",
        "pg_id":              row["pg_id"],
        "commodities":        _clean_list(row.get("commodities")),
        "commodity_grouping": row.get("commodity_grouping"),
        "status":             row.get("status"),
        "geom_bbox":          _bbox_for_point(row),
        "source_url":         row.get("source_url"),
        "summary_text":       summary,
        "workspace_id":       DEFAULT_WORKSPACE_UUID,   # GI-9 multi-tenant isolation
        "parser_version":     SPARSE_MODEL_VERSION,     # SPLADE model tag for cache invalidation
    }


def _occurrence_payload(row: dict, summary: str) -> dict[str, Any]:
    # Union primary + associated for the filter-friendly `commodities` slot
    # so chat-tool filters like "commodity == Au" hit either class.
    commodities = _clean_list(row.get("primary_commodities")) + _clean_list(
        row.get("associated_commodities")
    )
    return {
        "jurisdiction_code":  row["jurisdiction_code"],
        "source_id":          row["source_id"],
        "source_feature_id":  row["source_feature_id"],
        "canonical_type":     "mineral_occurrence",
        "pg_id":              row["pg_id"],
        "external_id":        row.get("external_id"),
        "commodities":        commodities,
        "commodity_grouping": row.get("commodity_grouping"),
        "status":             row.get("status"),
        "production_flag":    bool(row.get("production_flag")),
        "geom_bbox":          _bbox_for_point(row),
        "source_url":         row.get("source_url"),
        "summary_text":       summary,
        "workspace_id":       DEFAULT_WORKSPACE_UUID,   # GI-9 multi-tenant isolation
        "parser_version":     SPARSE_MODEL_VERSION,     # SPLADE model tag for cache invalidation
    }


def _drillhole_payload(row: dict, summary: str) -> dict[str, Any]:
    return {
        "jurisdiction_code":  row["jurisdiction_code"],
        "source_id":          row["source_id"],
        "source_feature_id":  row["source_feature_id"],
        "canonical_type":     "drillhole_collar",
        "pg_id":              row["pg_id"],
        "drillhole_id":       row.get("drillhole_id"),
        "commodities":        _clean_list(row.get("commodity_of_interest")),
        "commodity_grouping": None,  # drillholes are not grouping-classified
        "status":             row.get("core_availability"),
        "geom_bbox":          _bbox_for_point(row),
        "source_url":         row.get("source_url"),
        "summary_text":       summary,
        "workspace_id":       DEFAULT_WORKSPACE_UUID,   # GI-9 multi-tenant isolation
        "parser_version":     SPARSE_MODEL_VERSION,     # SPLADE model tag for cache invalidation
    }


def _rock_sample_payload(row: dict, summary: str) -> dict[str, Any]:
    return {
        "jurisdiction_code":  row["jurisdiction_code"],
        "source_id":          row["source_id"],
        "source_feature_id":  row["source_feature_id"],
        "canonical_type":     "rock_sample",
        "pg_id":              row["pg_id"],
        "sample_number":      row.get("sample_number"),
        "station":            row.get("station"),
        "nts_250k":           row.get("nts_250k"),
        "report_number":      row.get("report_number"),
        "commodities":        [],  # rock samples aren't commodity-tagged
        "commodity_grouping": None,
        "status":             None,
        "geom_bbox":          _bbox_for_point(row),
        "source_url":         row.get("source_url"),
        "summary_text":       summary,
        "workspace_id":       DEFAULT_WORKSPACE_UUID,   # GI-9 multi-tenant isolation
        "parser_version":     SPARSE_MODEL_VERSION,     # SPLADE model tag for cache invalidation
    }


def _assessment_survey_payload(row: dict, summary: str) -> dict[str, Any]:
    return {
        "jurisdiction_code":  row["jurisdiction_code"],
        "source_id":          row["source_id"],
        "source_feature_id":  row["source_feature_id"],
        "canonical_type":     "assessment_survey",
        "pg_id":              row["pg_id"],
        "survey_type":        row.get("survey_type"),
        "commodities":        [],
        "commodity_grouping": None,
        "status":             row.get("survey_type"),  # filterable as "airborne" etc.
        "geom_bbox":          _bbox_for_polygon(row),
        "source_url":         row.get("source_url"),
        "summary_text":       summary,
        "workspace_id":       DEFAULT_WORKSPACE_UUID,   # GI-9 multi-tenant isolation
        "parser_version":     SPARSE_MODEL_VERSION,     # SPLADE model tag for cache invalidation
    }


def _resource_potential_payload(row: dict, summary: str) -> dict[str, Any]:
    return {
        "jurisdiction_code":  row["jurisdiction_code"],
        "source_id":          row["source_id"],
        "source_feature_id":  row["source_feature_id"],
        "canonical_type":     "resource_potential_zone",
        "pg_id":              row["pg_id"],
        "commodities":        [row["commodity"]] if row.get("commodity") else [],
        "commodity_grouping": row.get("commodity_grouping"),
        "status":             None,
        "potential_rank":     row.get("potential_rank"),
        "geom_bbox":          _bbox_for_polygon(row),
        "source_url":         None,
        "summary_text":       summary,
        "workspace_id":       DEFAULT_WORKSPACE_UUID,   # GI-9 multi-tenant isolation
        "parser_version":     SPARSE_MODEL_VERSION,     # SPLADE model tag for cache invalidation
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fetch_dicts(postgres: PostgresResource, sql: str) -> list[dict]:
    with postgres.get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql)
            return [dict(r) for r in cur.fetchall()]


def _embed_batched(texts: list[str], context: AssetExecutionContext) -> list:
    model = _get_model()
    out = []
    total = len(texts)
    for start in range(0, total, EMBED_BATCH_SIZE):
        batch = texts[start : start + EMBED_BATCH_SIZE]
        out.extend(model.encode(batch, batch_size=EMBED_BATCH_SIZE))
        context.log.info(
            "Embedded %d / %d chunks", min(start + EMBED_BATCH_SIZE, total), total,
        )
    return out


def _upsert_batched(client, collection: str, points: list, context: AssetExecutionContext) -> None:
    total = len(points)
    for start in range(0, total, UPSERT_BATCH_SIZE):
        batch = points[start : start + UPSERT_BATCH_SIZE]
        client.upsert(collection_name=collection, points=batch)
        context.log.info(
            "Upserted %d / %d points into '%s'",
            min(start + UPSERT_BATCH_SIZE, total), total, collection,
        )


def _run_canonical_type(
    *,
    context: AssetExecutionContext,
    postgres: PostgresResource,
    qdrant_client,
    sql: str,
    collection: str,
    summary_fn,
    payload_fn,
) -> tuple[int, int]:
    """Returns (rows_read, points_upserted)."""
    rows = _fetch_dicts(postgres, sql)
    context.log.info("Loaded %d rows for collection '%s'", len(rows), collection)
    if not rows:
        return 0, 0

    _ensure_collection(qdrant_client, collection, context)

    texts: list[str] = []
    payloads: list[dict[str, Any]] = []
    ids: list[str] = []

    for row in rows:
        summary = summary_fn(row)
        texts.append(summary)
        payloads.append(payload_fn(row, summary))
        ids.append(_deterministic_point_id(str(row["pg_id"])))

    # Dense embedding (sentence-transformer).
    embeddings = _embed_batched(texts, context)

    # Sparse embedding (SPLADE++ — Module 4 Chunk 2 cleanup, C3-02).
    context.log.info(
        "index_public_geoscience: encoding %d texts (sparse SPLADE++ %s) for '%s'...",
        len(texts), SPARSE_MODEL_VERSION, collection,
    )
    sparse_vecs = encode_sparse_batch(texts, batch_size=16)
    context.log.info(
        "index_public_geoscience: sparse encoding complete for '%s' -- avg non-zero=%.0f",
        collection,
        sum(len(sv) for sv in sparse_vecs) / max(len(sparse_vecs), 1),
    )

    from qdrant_client.models import PointStruct, SparseVector  # noqa: PLC0415

    points = []
    for i in range(len(rows)):
        # Build named-vector dict: dense "" slot + sparse "text" slot.
        # Dense slot uses float list; sparse slot uses SparseVector.
        # Only include sparse slot when SPLADE produced non-zero terms
        # (empty SparseVector causes Qdrant to error on upsert).
        vector_payload: dict = {"": embeddings[i].tolist()}
        sparse_vec = sparse_vecs[i]
        if sparse_vec:
            sorted_indices = sorted(sparse_vec.keys())
            vector_payload["text"] = SparseVector(
                indices=sorted_indices,
                values=[sparse_vec[k] for k in sorted_indices],
            )
        points.append(
            PointStruct(
                id=ids[i],
                vector=vector_payload,
                payload=payloads[i],
            )
        )

    _upsert_batched(qdrant_client, collection, points, context)
    return len(rows), len(points)


# ---------------------------------------------------------------------------
# Asset config
# ---------------------------------------------------------------------------

class IndexPublicGeoscienceConfig(Config):
    """Runtime knobs for the Qdrant index asset. Defaults are production-safe;
    override in the Dagster UI for dev runs against a subset.
    """

    # Per-canonical-type toggles. Useful for partial re-runs after a single
    # Silver asset refreshes.
    include_mines: bool = True
    include_mineral_occurrences: bool = True
    include_drillholes: bool = True
    include_resource_potential_zones: bool = True
    include_rock_samples: bool = True
    include_assessment_surveys: bool = True


# ---------------------------------------------------------------------------
# Asset
# ---------------------------------------------------------------------------

@asset(
    group_name="index",
    deps=[
        silver_pg_ca_sk_mine_loc,
        silver_pg_ca_sk_smdi,
        silver_pg_ca_sk_drillhole,
        silver_pg_ca_sk_resource_potential,
        silver_pg_ca_sk_rock_samples,
        silver_pg_ca_sk_assessment_underground,
        silver_pg_ca_sk_assessment_ground,
        silver_pg_ca_sk_assessment_airborne,
    ],
    description=(
        "Embed structured summaries of the six Public Geoscience canonical "
        "tables into six Qdrant collections (plan §05d + Tier 1 expansion). "
        "Uses BAAI/bge-small-en-v1.5 (384-dim cosine), deterministic point "
        "IDs keyed on pg_id so re-runs UPSERT in place. Payload carries the "
        "fields the chat tool needs for filtering + citation resolution."
    ),
)
def index_public_geoscience_qdrant(
    context: AssetExecutionContext,
    config: IndexPublicGeoscienceConfig,
    postgres: PostgresResource,
    qdrant: QdrantResource,
) -> MaterializeResult:
    client = qdrant.get_client()

    tasks = []
    if config.include_mines:
        tasks.append(("mine", MINE_SQL, _mine_summary, _mine_payload))
    if config.include_mineral_occurrences:
        tasks.append(("mineral_occurrence", OCCURRENCE_SQL, _occurrence_summary, _occurrence_payload))
    if config.include_drillholes:
        tasks.append(("drillhole_collar", DRILLHOLE_SQL, _drillhole_summary, _drillhole_payload))
    if config.include_resource_potential_zones:
        tasks.append((
            "resource_potential_zone",
            RESOURCE_POTENTIAL_SQL,
            _resource_potential_summary,
            _resource_potential_payload,
        ))
    if config.include_rock_samples:
        tasks.append((
            "rock_sample",
            ROCK_SAMPLE_SQL,
            _rock_sample_summary,
            _rock_sample_payload,
        ))
    if config.include_assessment_surveys:
        tasks.append((
            "assessment_survey",
            ASSESSMENT_SURVEY_SQL,
            _assessment_survey_summary,
            _assessment_survey_payload,
        ))

    totals: dict[str, int] = {}
    grand_rows = 0
    grand_points = 0
    for canonical_type, sql, summary_fn, payload_fn in tasks:
        collection = COLLECTIONS[canonical_type]["name"]
        rows, points = _run_canonical_type(
            context=context,
            postgres=postgres,
            qdrant_client=client,
            sql=sql,
            collection=collection,
            summary_fn=summary_fn,
            payload_fn=payload_fn,
        )
        totals[collection] = points
        grand_rows += rows
        grand_points += points

    return MaterializeResult(
        metadata={
            "embedding_model":       MetadataValue.text(EMBED_MODEL_NAME),
            "embedding_dimensions":  MetadataValue.int(EMBED_DIMENSIONS),
            "sparse_model":          MetadataValue.text(SPARSE_MODEL_VERSION),
            "collections":           MetadataValue.json(totals),
            "total_rows_read":       MetadataValue.int(grand_rows),
            "total_points_upserted": MetadataValue.int(grand_points),
        }
    )
