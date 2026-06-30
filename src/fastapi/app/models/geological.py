"""Geological domain Pydantic models for the GeoRAG FastAPI service.

These models mirror the 9 PostGIS schemas defined in Section 04e of the
architecture. They are used for:
  - Request/response validation in API endpoints
  - Ingestion pipeline output (Dagster → FastAPI hand-off)
  - RAG tool return types (agent grounding via PostGIS query results)

Create variants (e.g. CollarCreate) omit server-assigned fields such as
primary-key UUIDs and created_at timestamps. Read variants include all
persisted fields and are the canonical shape returned from the database.

Validation rules match the architecture schema exactly. Do not change
enumeration values or constraint bounds without SME approval (CLAUDE.md §6).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Project
# ---------------------------------------------------------------------------


class ProjectCreate(BaseModel):
    """Payload to create a new exploration project."""

    project_name: str = Field(..., min_length=1, max_length=255)
    crs_datum: str = Field(default="EPSG:32613", max_length=64)
    company: str = Field(..., min_length=1, max_length=255)
    magnetic_declination: float = Field(default=0.0, ge=-180.0, le=180.0)
    orientation_reference: Literal["BOH", "TOH"] = "BOH"
    commodity: str = Field(..., min_length=1, max_length=128)
    region: str = Field(..., min_length=1, max_length=255)


class ProjectRead(ProjectCreate):
    """Full project record as returned from the database.

    Includes the ``status`` and ``slug`` columns added by the
    2026_04_13_300000_add_dashboard_fields_to_projects migration.
    ``updated_at`` mirrors the Laravel timestamps() column.
    """

    project_id: UUID
    status: Literal["active", "indexing", "degraded", "archived"] = "active"
    slug: str
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Collar
# ---------------------------------------------------------------------------


class CollarCreate(BaseModel):
    """Payload to create a new drill-hole collar."""

    hole_id: str = Field(..., min_length=1, max_length=128)
    project_id: UUID
    easting: float
    northing: float
    elevation: float
    total_depth: float = Field(..., gt=0.0)
    hole_type: Literal["Diamond", "RC", "RAB", "Rotary", "Percussion"]
    azimuth: float = Field(..., ge=0.0, le=360.0)
    dip: float = Field(..., ge=-90.0, le=0.0)
    drill_date: date | None = None
    status: Literal["Active", "Completed", "Abandoned"] = "Active"


class CollarRead(CollarCreate):
    """Full collar record as returned from the database."""

    collar_id: UUID


# ---------------------------------------------------------------------------
# Survey
# ---------------------------------------------------------------------------


class SurveyCreate(BaseModel):
    """Downhole survey measurement at a given depth."""

    collar_id: UUID
    depth: float = Field(..., ge=0.0)
    azimuth: float = Field(..., ge=0.0, le=360.0)
    dip: float = Field(..., ge=-90.0, le=0.0)
    survey_method: Literal["Reflex", "Gyro", "Magnetic", "Acid Test"]


class SurveyRead(SurveyCreate):
    """Full survey record as returned from the database."""

    survey_id: UUID


# ---------------------------------------------------------------------------
# LithologyLog
# ---------------------------------------------------------------------------


class LithologyLogCreate(BaseModel):
    """Single interval of a downhole lithology log."""

    collar_id: UUID
    from_depth: float = Field(..., ge=0.0)
    to_depth: float = Field(..., gt=0.0)
    lithology_code: str = Field(..., min_length=1, max_length=64)
    lithology_description: str = Field(..., min_length=1)
    grain_size: Literal["Fine", "Medium", "Coarse", "Very Coarse"] | None = None
    color: str | None = Field(default=None, max_length=128)
    hardness: Literal["Soft", "Medium", "Hard", "Very Hard"] | None = None
    rqd: float | None = Field(default=None, ge=0.0, le=100.0)
    recovery: float | None = Field(default=None, ge=0.0, le=100.0)
    weathering: Literal["Fresh", "Slight", "Moderate", "High", "Complete"] | None = None

    def model_post_init(self, __context: Any) -> None:  # noqa: ANN401
        """Enforce to_depth > from_depth across the interval."""
        if self.to_depth <= self.from_depth:
            raise ValueError(f"to_depth ({self.to_depth}) must be greater than from_depth ({self.from_depth})")


class LithologyLogRead(LithologyLogCreate):
    """Full lithology log record as returned from the database."""

    log_id: UUID


# ---------------------------------------------------------------------------
# Alteration
# ---------------------------------------------------------------------------


class AlterationCreate(BaseModel):
    """Hydrothermal alteration interval logged against a drill hole."""

    collar_id: UUID
    from_depth: float = Field(..., ge=0.0)
    to_depth: float = Field(..., gt=0.0)
    alteration_type: Literal[
        "Chlorite",
        "Sericite",
        "Silicification",
        "Clay",
        "Hematite",
        "Potassic",
        "Propylitic",
        "Argillic",
        "Phyllic",
    ]
    intensity: Literal["Weak", "Moderate", "Strong", "Intense", "Pervasive"]
    minerals: str | None = Field(default=None, description="Free-text mineral assemblage description")

    def model_post_init(self, __context: Any) -> None:  # noqa: ANN401
        """Enforce to_depth > from_depth across the interval."""
        if self.to_depth <= self.from_depth:
            raise ValueError(f"to_depth ({self.to_depth}) must be greater than from_depth ({self.from_depth})")


class AlterationRead(AlterationCreate):
    """Full alteration record as returned from the database."""

    alteration_id: UUID


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------


class StructureCreate(BaseModel):
    """Structural measurement (orientation data) at a point in a drill hole."""

    collar_id: UUID
    depth: float = Field(..., ge=0.0)
    structure_type: Literal[
        "Bedding",
        "Fault",
        "Foliation",
        "Fracture",
        "Joint",
        "Lineation",
        "Shear",
        "Vein",
    ]
    alpha_angle: float = Field(..., ge=0.0, le=90.0)
    beta_angle: float = Field(..., ge=0.0, le=360.0)
    true_dip: float = Field(..., ge=0.0, le=90.0)
    dip_direction: float = Field(..., ge=0.0, le=360.0)
    description: str | None = None


class StructureRead(StructureCreate):
    """Full structure record as returned from the database."""

    structure_id: UUID


# ---------------------------------------------------------------------------
# Sample
# ---------------------------------------------------------------------------


class SampleCreate(BaseModel):
    """Geochemical sample interval with assay results.

    commodity_assays stores analyte-value pairs keyed by a string such as
    "U3O8_ppm", "Au_ppm", "Cu_pct". Values are floating-point concentrations.
    The dict is intentionally open-ended because commodity suites vary by
    project; specific analyte validation is handled downstream by the
    ingestion pipeline and geological constraint rules (Layer 6).
    """

    collar_id: UUID
    from_depth: float = Field(..., ge=0.0)
    to_depth: float = Field(..., gt=0.0)
    sample_type: Literal["Core", "Chip", "Grab", "Channel", "Soil"]
    lab_id: str | None = Field(default=None, max_length=128)
    commodity_assays: dict[str, float] = Field(default_factory=dict)
    qaqc_type: Literal["Primary", "Duplicate", "Blank", "Standard"] | None = None

    def model_post_init(self, __context: Any) -> None:  # noqa: ANN401
        """Enforce to_depth > from_depth across the sample interval."""
        if self.to_depth <= self.from_depth:
            raise ValueError(f"to_depth ({self.to_depth}) must be greater than from_depth ({self.from_depth})")


class SampleRead(SampleCreate):
    """Full sample record as returned from the database."""

    sample_id: UUID


# ---------------------------------------------------------------------------
# Geochemistry
# ---------------------------------------------------------------------------


class GeochemistryCreate(BaseModel):
    """Whole-rock geochemical analysis for a downhole interval.

    Major oxides (SiO2 through K2O) are reported as weight percent and
    constrained to 0–100. ree_json stores rare-earth element concentrations
    as an open dict (e.g. {"La_ppm": 12.3, "Ce_ppm": 24.1}).
    """

    collar_id: UUID
    from_depth: float = Field(..., ge=0.0)
    to_depth: float = Field(..., gt=0.0)

    # Major oxides (wt%)
    sio2_wt_pct: float | None = Field(default=None, ge=0.0, le=100.0)
    tio2_wt_pct: float | None = Field(default=None, ge=0.0, le=100.0)
    al2o3_wt_pct: float | None = Field(default=None, ge=0.0, le=100.0)
    fe2o3_wt_pct: float | None = Field(default=None, ge=0.0, le=100.0)
    mno_wt_pct: float | None = Field(default=None, ge=0.0, le=100.0)
    mgo_wt_pct: float | None = Field(default=None, ge=0.0, le=100.0)
    cao_wt_pct: float | None = Field(default=None, ge=0.0, le=100.0)
    na2o_wt_pct: float | None = Field(default=None, ge=0.0, le=100.0)
    k2o_wt_pct: float | None = Field(default=None, ge=0.0, le=100.0)

    # Derived values
    ree_json: dict[str, float] = Field(default_factory=dict)
    mg_number: float | None = Field(default=None, ge=0.0, le=100.0)
    cia: float | None = Field(default=None, description="Chemical Index of Alteration")
    eu_anomaly: float | None = Field(default=None, description="Europium anomaly (Eu/Eu*)")

    def model_post_init(self, __context: Any) -> None:  # noqa: ANN401
        """Enforce to_depth > from_depth across the geochemistry interval."""
        if self.to_depth <= self.from_depth:
            raise ValueError(f"to_depth ({self.to_depth}) must be greater than from_depth ({self.from_depth})")


class GeochemistryRead(GeochemistryCreate):
    """Full geochemistry record as returned from the database."""

    geochem_id: UUID


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


class ReportCreate(BaseModel):
    """Technical report record (NI 43-101, JORC, company reports, publications).

    sections_text maps section headings to their extracted text content.
    resource_estimate stores the structured resource table extracted from the
    report (tonnage, grade, classification, commodity).
    embedding_ids lists the Qdrant/RAGFlow chunk IDs generated from this report
    so provenance can be traced from Citation → chunk → Report.
    """

    title: str = Field(..., min_length=1, max_length=512)
    authors: list[str] = Field(default_factory=list)
    company: str = Field(..., min_length=1, max_length=255)
    filing_date: date | None = None
    commodity: str = Field(..., min_length=1, max_length=128)
    project_name: str = Field(..., min_length=1, max_length=255)
    region: str = Field(..., min_length=1, max_length=255)
    resource_estimate: dict[str, Any] = Field(default_factory=dict)
    sections_text: dict[str, str] = Field(default_factory=dict)
    embedding_ids: list[str] = Field(default_factory=list)


class ReportRead(ReportCreate):
    """Full report record as returned from the database."""

    report_id: UUID
