"""Stub bronze→silver assets for tables without a defined bronze source yet.

Each stub defines the asset with its description + expected upstream
shape but raises ``NotImplementedError`` on materialization. This keeps
the Dagster catalog complete (so the asset graph in the UI shows
every drillhole pipeline node) while making the gap visible.

Wiring path when a real bronze source lands:
  1. Add the corresponding bronze.raw_<X> table (or CSV upload path).
  2. Implement the body of the function below.
  3. Drop the NotImplementedError.
"""
from dagster import AssetExecutionContext, Config, MaterializeResult, asset


class _DrillholeStubConfig(Config):
    workspace_id: str


def _stub_body(name: str, target_table: str, expected_source: str) -> str:
    return (
        f"{name} bronze→silver transform not yet wired. "
        f"Target: {target_table}. "
        f"Expected source: {expected_source}. "
        "Implement when ingest path lands."
    )


@asset(
    group_name="drillhole_silver",
    description="Core recovery + RQD per run → silver.recovery.",
)
def silver_recovery(
    context: AssetExecutionContext, config: _DrillholeStubConfig,
) -> MaterializeResult:
    raise NotImplementedError(_stub_body(
        "silver_recovery", "silver.recovery",
        "bronze.raw_recovery_logs (TBD) or CSV upload via S3",
    ))


@asset(
    group_name="drillhole_silver",
    description="Density measurements → silver.specific_gravity.",
)
def silver_specific_gravity(
    context: AssetExecutionContext, config: _DrillholeStubConfig,
) -> MaterializeResult:
    raise NotImplementedError(_stub_body(
        "silver_specific_gravity", "silver.specific_gravity",
        "bronze.raw_sg_measurements (TBD) or CSV upload via S3",
    ))


@asset(
    group_name="drillhole_silver",
    description="Structural measurements → silver.structure.",
)
def silver_structure(
    context: AssetExecutionContext, config: _DrillholeStubConfig,
) -> MaterializeResult:
    raise NotImplementedError(_stub_body(
        "silver_structure", "silver.structure",
        "bronze.raw_structural_logs (TBD) or CSV upload via S3",
    ))


@asset(
    group_name="drillhole_silver",
    description="Alteration zones → silver.alteration.",
)
def silver_alteration(
    context: AssetExecutionContext, config: _DrillholeStubConfig,
) -> MaterializeResult:
    raise NotImplementedError(_stub_body(
        "silver_alteration", "silver.alteration",
        "bronze.raw_alteration_logs (TBD) or CSV upload via S3",
    ))


@asset(
    group_name="drillhole_silver",
    description="Visible mineralisation → silver.mineralization.",
)
def silver_mineralization(
    context: AssetExecutionContext, config: _DrillholeStubConfig,
) -> MaterializeResult:
    raise NotImplementedError(_stub_body(
        "silver_mineralization", "silver.mineralization",
        "bronze.raw_mineralization_logs (TBD) or CSV upload via S3",
    ))


@asset(
    group_name="drillhole_silver",
    description="Engineering data (UCS, RMR, Q) → silver.geotechnical.",
)
def silver_geotechnical(
    context: AssetExecutionContext, config: _DrillholeStubConfig,
) -> MaterializeResult:
    raise NotImplementedError(_stub_body(
        "silver_geotechnical", "silver.geotechnical",
        "bronze.raw_geotech_logs (TBD) or CSV upload via S3",
    ))
