"""GeoRAG asset-check definitions — Module 3 Phase B1.

Each function decorated with @asset_check corresponds to a concrete data-quality
assertion on a Silver, Gold, or Index asset.  Checks marked blocking=True cause
Dagster to refuse downstream execution (including commit_ingestion_run) if they
fail.  Non-blocking checks with severity='WARN' still emit metadata for Phase C
measurement but do not block the commit gate.

Convention
----------
- Every check function name encodes the asset it targets and the assertion:
    <asset_key>_check_<assertion_name>
- Every AssetCheckResult carries:
    - passed: bool
    - metadata: dict with at minimum parse_total, parse_ok, parse_failed,
      parse_ratio, parser_name, parser_version where applicable.

NOTE: Do NOT add `from __future__ import annotations` to this file.
Dagster 1.13 Config/ConfigurableResource classes use Pydantic for type
introspection and that import breaks runtime annotation evaluation.
"""

from georag_dagster.checks.silver_checks import (
    silver_collars_check_collar_count_positive,
    silver_collars_check_schema_conformance,
    silver_collars_check_crs_srid_populated,
    silver_surveys_check_parse_total_positive,
    silver_lithology_check_parse_total_positive,
    silver_samples_check_parse_total_positive,
    silver_well_logs_check_parse_total_positive,
    silver_spatial_check_geom_not_null,
    silver_spatial_check_srid_populated,
    silver_reports_check_parse_total_positive,
    silver_reports_check_schema_conformance,
    silver_xlsx_check_parse_total_positive,
    silver_seismic_check_parse_total_positive,
    silver_seismic_check_schema_conformance,
    silver_xyz_check_parse_total_positive,
    silver_xyz_check_schema_conformance,
)
from georag_dagster.checks.evidence_checks import (
    document_passages_check_no_duplicate_passage_ids,
    document_passages_check_text_hash_sha256_valid,
    document_revisions_check_document_id_not_null,
    document_revisions_check_sha256_format,
    evidence_items_check_exactly_one_ref,
)
from georag_dagster.checks.index_checks import (
    index_reports_check_embedding_id_present,
    index_reports_check_parser_quality_floor,
)
from georag_dagster.checks.drill_traces_checks import (
    desurvey_trace_count_matches_collar_count_with_surveys,
)
from georag_dagster.checks.interval_overlap_checks import (
    silver_lithology_interval_overlap,
    silver_samples_assays_v2_interval_overlap,
)

__all__ = [
    # Silver — collars
    "silver_collars_check_collar_count_positive",
    "silver_collars_check_schema_conformance",
    "silver_collars_check_crs_srid_populated",
    # Silver — tabular
    "silver_surveys_check_parse_total_positive",
    "silver_lithology_check_parse_total_positive",
    "silver_samples_check_parse_total_positive",
    "silver_well_logs_check_parse_total_positive",
    # Silver — spatial
    "silver_spatial_check_geom_not_null",
    "silver_spatial_check_srid_populated",
    # Silver — reports/xlsx
    "silver_reports_check_parse_total_positive",
    "silver_reports_check_schema_conformance",
    "silver_xlsx_check_parse_total_positive",
    # Silver — seismic (Silver-trapped, DAG-05)
    "silver_seismic_check_parse_total_positive",
    "silver_seismic_check_schema_conformance",
    # Silver — xyz (Silver-trapped, DAG-05)
    "silver_xyz_check_parse_total_positive",
    "silver_xyz_check_schema_conformance",
    # Evidence model
    "document_passages_check_no_duplicate_passage_ids",
    "document_passages_check_text_hash_sha256_valid",
    "document_revisions_check_document_id_not_null",
    "document_revisions_check_sha256_format",
    "evidence_items_check_exactly_one_ref",
    # Index
    "index_reports_check_embedding_id_present",
    "index_reports_check_parser_quality_floor",
    # Drill traces
    "desurvey_trace_count_matches_collar_count_with_surveys",
    # CC-01 Item 1 Slice 3 — interval overlap
    "silver_lithology_interval_overlap",
    "silver_samples_assays_v2_interval_overlap",
]
