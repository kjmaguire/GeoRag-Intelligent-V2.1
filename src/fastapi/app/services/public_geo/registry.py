"""Public-geoscience source registry — the ArcGIS feeds we query LIVE.

This lives in CODE, not in the database, and that is the whole point.

Public geoscience is a look-through onto what provincial and federal surveys
already publish. We do not copy it, index it, or embed it — a query hits the
upstream ArcGIS REST service and returns what is there right now. That means
there is no `public_geo.*` schema to keep in sync, no Qdrant collection to
re-embed when the embedding model changes, and no staleness: the data is
whatever the survey is serving at the moment you ask.

The previous design stored all of it — ~514k rows in Postgres and 182,826
embedded points across six Qdrant collections, built by a Dagster pipeline
that has been dormant since 2026-07-28. That corpus never reached Azure at
all, and by 2026-08-19 it had drifted three weeks stale while its embeddings
sat at 384 dimensions against a 1024-dim reader, so every search returned
`HTTP 400: expected dim: 384, got 1024`. All of it is removed.

What is lost by not indexing: semantic search. You cannot rank by meaning
over data you never embedded. What replaces it is the query model these
services were built for — spatial (bounding box) and attribute (commodity,
status, name) filtering, executed by the survey's own database. For
structured government feature services that is the more faithful interface,
not a downgrade.

Registry entries are transcribed from the source table that used to hold
them, minus every runtime-state column (last_refreshed_at,
last_service_edit_ms) — nothing here is state, only addressing.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Jurisdiction:
    """A publishing authority, used for attribution on every returned record."""

    code: str
    display_name: str | None
    license_summary: str | None
    license_url: str | None


@dataclass(frozen=True)
class PublicGeoSource:
    """One live ArcGIS REST layer.

    ``service_url`` already includes the layer index for most feeds; the
    separate ``layer_index`` is retained because a few registry rows point at
    a MapServer root and identify the layer separately.
    """

    source_id: str
    jurisdiction_code: str
    name: str
    canonical_type: str
    service_url: str
    layer_index: int | None
    source_crs: int | None
    license_summary: str | None
    license_url: str | None

    @property
    def is_queryable(self) -> bool:
        """Whether this row addresses a LAYER rather than a service root.

        Three registry rows — CA-SK-RESOURCE-POTENTIAL,
        CA-SK-MINERAL-DISPOSITION-CROWN and CA-SK-MINERAL-DISPOSITION-MINING —
        point at a MapServer root and exist only as parents of the per-layer
        children beside them (…-URANIUM, …-MINING-0, and so on). They carry no
        layer index, so `/query` against them is a 400:

            GET .../Economy/Resource_Map/MapServer/query -> 400 Bad Request

        Filtering them here rather than at each call site means a caller
        asking for every resource_potential_zone feed gets the eleven that
        actually answer, instead of ten answers and one error.
        """
        tail = self.service_url.rstrip("/").rsplit("/", 1)[-1]
        return tail.isdigit() or self.layer_index is not None


JURISDICTIONS: dict[str, Jurisdiction] = {
    "CA-AB": Jurisdiction(code="CA-AB", display_name="Alberta", license_summary="Open Government Licence — Alberta", license_url="https://open.alberta.ca/licence"),
    "CA-BC": Jurisdiction(code="CA-BC", display_name="British Columbia", license_summary="Open Government Licence – British Columbia (v2.0)", license_url="https://www2.gov.bc.ca/gov/content/data/open-data/open-government-licence-bc"),
    "CA-FED": Jurisdiction(code="CA-FED", display_name="Canada (federal)", license_summary=None, license_url=None),
    "CA-FEDERAL": Jurisdiction(code="CA-FEDERAL", display_name="Canada (Federal)", license_summary="Open Government Licence — Canada (v2.0)", license_url="https://open.canada.ca/en/open-government-licence-canada"),
    "CA-MB": Jurisdiction(code="CA-MB", display_name="Manitoba", license_summary="Open Government Licence — Canada (v2.0)", license_url="https://open.canada.ca/en/open-government-licence-canada"),
    "CA-NB": Jurisdiction(code="CA-NB", display_name="New Brunswick", license_summary=None, license_url=None),
    "CA-NL": Jurisdiction(code="CA-NL", display_name="Newfoundland & Labrador", license_summary=None, license_url=None),
    "CA-NS": Jurisdiction(code="CA-NS", display_name="Nova Scotia", license_summary=None, license_url=None),
    "CA-NT": Jurisdiction(code="CA-NT", display_name="Northwest Territories", license_summary=None, license_url=None),
    "CA-NU": Jurisdiction(code="CA-NU", display_name="Nunavut", license_summary=None, license_url=None),
    "CA-ON": Jurisdiction(code="CA-ON", display_name="Ontario", license_summary=None, license_url=None),
    "CA-PE": Jurisdiction(code="CA-PE", display_name="Prince Edward Island", license_summary=None, license_url=None),
    "CA-QC": Jurisdiction(code="CA-QC", display_name="Québec", license_summary=None, license_url=None),
    "CA-SK": Jurisdiction(code="CA-SK", display_name="Saskatchewan", license_summary="Government of Saskatchewan Standard Unrestricted Use Data License v2.0", license_url="https://pubsaskdev.blob.core.windows.net/pubsask-prod/107346/107346-Standard_Unrestricted_Use_Data_Licence.pdf"),
    "CA-YT": Jurisdiction(code="CA-YT", display_name="Yukon", license_summary=None, license_url=None),}

SOURCES: list[PublicGeoSource] = [
    PublicGeoSource(
        source_id="CA-BC-MINFILE",
        jurisdiction_code="CA-BC",
        name="BC MINFILE — Mineral Occurrences",
        canonical_type="mineral_occurrence",
        service_url="https://delivery.maps.gov.bc.ca/arcgis/rest/services/mpcm/bcgwpub/MapServer/137",
        layer_index=137,
        source_crs=3005,
        license_summary="Open Government Licence – British Columbia (v2.0)",
        license_url="https://www2.gov.bc.ca/gov/content/data/open-data/open-government-licence-bc",
    ),
    PublicGeoSource(
        source_id="CA-SK-ASSESSMENT-AIRBORNE",
        jurisdiction_code="CA-SK",
        name="Saskatchewan Mineral Assessment — Airborne Surveys",
        canonical_type="assessment_survey",
        service_url="https://gis.saskatchewan.ca/arcgis/rest/services/Economy/P_Mineral_Assessment_File_Information/MapServer/3",
        layer_index=3,
        source_crs=2957,
        license_summary="Government of Saskatchewan Standard Unrestricted Use Data License v2.0",
        license_url="https://pubsaskdev.blob.core.windows.net/pubsask-prod/107346/107346-Standard_Unrestricted_Use_Data_Licence.pdf",
    ),
    PublicGeoSource(
        source_id="CA-SK-ASSESSMENT-GROUND",
        jurisdiction_code="CA-SK",
        name="Saskatchewan Mineral Assessment — Ground Surveys",
        canonical_type="assessment_survey",
        service_url="https://gis.saskatchewan.ca/arcgis/rest/services/Economy/P_Mineral_Assessment_File_Information/MapServer/2",
        layer_index=2,
        source_crs=2957,
        license_summary="Government of Saskatchewan Standard Unrestricted Use Data License v2.0",
        license_url="https://pubsaskdev.blob.core.windows.net/pubsask-prod/107346/107346-Standard_Unrestricted_Use_Data_Licence.pdf",
    ),
    PublicGeoSource(
        source_id="CA-SK-ASSESSMENT-UNDERGROUND",
        jurisdiction_code="CA-SK",
        name="Saskatchewan Mineral Assessment — Underground Surveys",
        canonical_type="assessment_survey",
        service_url="https://gis.saskatchewan.ca/arcgis/rest/services/Economy/P_Mineral_Assessment_File_Information/MapServer/1",
        layer_index=1,
        source_crs=2957,
        license_summary="Government of Saskatchewan Standard Unrestricted Use Data License v2.0",
        license_url="https://pubsaskdev.blob.core.windows.net/pubsask-prod/107346/107346-Standard_Unrestricted_Use_Data_Licence.pdf",
    ),
    PublicGeoSource(
        source_id="CA-SK-DRILLHOLE",
        jurisdiction_code="CA-SK",
        name="Saskatchewan Minerals & Quaternary Drillhole Compilation",
        canonical_type="drillhole_collar",
        service_url="https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Mineral_Exploration/MapServer/3",
        layer_index=3,
        source_crs=2957,
        license_summary="Government of Saskatchewan Standard Unrestricted Use Data License v2.0",
        license_url="https://pubsaskdev.blob.core.windows.net/pubsask-prod/107346/107346-Standard_Unrestricted_Use_Data_Licence.pdf",
    ),
    PublicGeoSource(
        source_id="CA-SK-GEOLOGY-BEDROCK-250K",
        jurisdiction_code="CA-SK",
        name="SK Bedrock Geology 250K",
        canonical_type="bedrock_geology",
        service_url="https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Geology/MapServer/10",
        layer_index=10,
        source_crs=2957,
        license_summary="Government of Saskatchewan Standard Unrestricted Use Data License v2.0",
        license_url="https://pubsaskdev.blob.core.windows.net/pubsask-prod/107346/107346-Standard_Unrestricted_Use_Data_Licence.pdf",
    ),
    PublicGeoSource(
        source_id="CA-SK-MINE-LOC",
        jurisdiction_code="CA-SK",
        name="Saskatchewan Mine Locations",
        canonical_type="mine",
        service_url="https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Mineral_Exploration/MapServer/1",
        layer_index=1,
        source_crs=2957,
        license_summary="Government of Saskatchewan Standard Unrestricted Use Data License v2.0",
        license_url="https://pubsaskdev.blob.core.windows.net/pubsask-prod/107346/107346-Standard_Unrestricted_Use_Data_Licence.pdf",
    ),
    PublicGeoSource(
        source_id="CA-SK-MINERAL-DISPOSITION-CROWN",
        jurisdiction_code="CA-SK",
        name="Saskatchewan Crown Dispositions — Oil and Gas (parent)",
        canonical_type="mineral_disposition",
        service_url="https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Mineral_Tenure_Crown_Dispositions/MapServer",
        layer_index=None,
        source_crs=2957,
        license_summary="Government of Saskatchewan Standard Unrestricted Use Data License v2.0",
        license_url="https://pubsaskdev.blob.core.windows.net/pubsask-prod/107346/107346-Standard_Unrestricted_Use_Data_Licence.pdf",
    ),
    PublicGeoSource(
        source_id="CA-SK-MINERAL-DISPOSITION-CROWN-OIL-GAS",
        jurisdiction_code="CA-SK",
        name="Saskatchewan Mineral Tenure - CROWN-OIL-GAS",
        canonical_type="mineral_disposition",
        service_url="https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Mineral_Tenure_Crown_Dispositions/MapServer/8",
        layer_index=8,
        source_crs=2957,
        license_summary="Government of Saskatchewan Standard Unrestricted Use Data License v2.0",
        license_url="https://pubsaskdev.blob.core.windows.net/pubsask-prod/107346/107346-Standard_Unrestricted_Use_Data_Licence.pdf",
    ),
    PublicGeoSource(
        source_id="CA-SK-MINERAL-DISPOSITION-MINING",
        jurisdiction_code="CA-SK",
        name="Saskatchewan Mineral Tenure — Mining Service (parent)",
        canonical_type="mineral_disposition",
        service_url="https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Mining/MapServer",
        layer_index=None,
        source_crs=2957,
        license_summary="Government of Saskatchewan Standard Unrestricted Use Data License v2.0",
        license_url="https://pubsaskdev.blob.core.windows.net/pubsask-prod/107346/107346-Standard_Unrestricted_Use_Data_Licence.pdf",
    ),
    PublicGeoSource(
        source_id="CA-SK-MINERAL-DISPOSITION-MINING-0",
        jurisdiction_code="CA-SK",
        name="Saskatchewan Mineral Tenure - MINING-0",
        canonical_type="mineral_disposition",
        service_url="https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Mining/MapServer/0",
        layer_index=0,
        source_crs=2957,
        license_summary="Government of Saskatchewan Standard Unrestricted Use Data License v2.0",
        license_url="https://pubsaskdev.blob.core.windows.net/pubsask-prod/107346/107346-Standard_Unrestricted_Use_Data_Licence.pdf",
    ),
    PublicGeoSource(
        source_id="CA-SK-MINERAL-DISPOSITION-MINING-1",
        jurisdiction_code="CA-SK",
        name="Saskatchewan Mineral Tenure - MINING-1",
        canonical_type="mineral_disposition",
        service_url="https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Mining/MapServer/1",
        layer_index=1,
        source_crs=2957,
        license_summary="Government of Saskatchewan Standard Unrestricted Use Data License v2.0",
        license_url="https://pubsaskdev.blob.core.windows.net/pubsask-prod/107346/107346-Standard_Unrestricted_Use_Data_Licence.pdf",
    ),
    PublicGeoSource(
        source_id="CA-SK-MINERAL-DISPOSITION-MINING-2",
        jurisdiction_code="CA-SK",
        name="Saskatchewan Mineral Tenure - MINING-2",
        canonical_type="mineral_disposition",
        service_url="https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Mining/MapServer/2",
        layer_index=2,
        source_crs=2957,
        license_summary="Government of Saskatchewan Standard Unrestricted Use Data License v2.0",
        license_url="https://pubsaskdev.blob.core.windows.net/pubsask-prod/107346/107346-Standard_Unrestricted_Use_Data_Licence.pdf",
    ),
    PublicGeoSource(
        source_id="CA-SK-MINERAL-DISPOSITION-MINING-3",
        jurisdiction_code="CA-SK",
        name="Saskatchewan Mineral Tenure - MINING-3",
        canonical_type="mineral_disposition",
        service_url="https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Mining/MapServer/3",
        layer_index=3,
        source_crs=2957,
        license_summary="Government of Saskatchewan Standard Unrestricted Use Data License v2.0",
        license_url="https://pubsaskdev.blob.core.windows.net/pubsask-prod/107346/107346-Standard_Unrestricted_Use_Data_Licence.pdf",
    ),
    PublicGeoSource(
        source_id="CA-SK-MINERAL-DISPOSITION-MINING-4",
        jurisdiction_code="CA-SK",
        name="Saskatchewan Mineral Tenure - MINING-4",
        canonical_type="mineral_disposition",
        service_url="https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Mining/MapServer/4",
        layer_index=4,
        source_crs=2957,
        license_summary="Government of Saskatchewan Standard Unrestricted Use Data License v2.0",
        license_url="https://pubsaskdev.blob.core.windows.net/pubsask-prod/107346/107346-Standard_Unrestricted_Use_Data_Licence.pdf",
    ),
    PublicGeoSource(
        source_id="CA-SK-MINERAL-DISPOSITION-MINING-5",
        jurisdiction_code="CA-SK",
        name="Saskatchewan Mineral Tenure - MINING-5",
        canonical_type="mineral_disposition",
        service_url="https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Mining/MapServer/5",
        layer_index=5,
        source_crs=2957,
        license_summary="Government of Saskatchewan Standard Unrestricted Use Data License v2.0",
        license_url="https://pubsaskdev.blob.core.windows.net/pubsask-prod/107346/107346-Standard_Unrestricted_Use_Data_Licence.pdf",
    ),
    PublicGeoSource(
        source_id="CA-SK-MINERAL-DISPOSITION-MINING-6",
        jurisdiction_code="CA-SK",
        name="Saskatchewan Mineral Tenure - MINING-6",
        canonical_type="mineral_disposition",
        service_url="https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Mining/MapServer/6",
        layer_index=6,
        source_crs=2957,
        license_summary="Government of Saskatchewan Standard Unrestricted Use Data License v2.0",
        license_url="https://pubsaskdev.blob.core.windows.net/pubsask-prod/107346/107346-Standard_Unrestricted_Use_Data_Licence.pdf",
    ),
    PublicGeoSource(
        source_id="CA-SK-MINERAL-DISPOSITION-MINING-7",
        jurisdiction_code="CA-SK",
        name="Saskatchewan Mineral Tenure - MINING-7",
        canonical_type="mineral_disposition",
        service_url="https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Mining/MapServer/7",
        layer_index=7,
        source_crs=2957,
        license_summary="Government of Saskatchewan Standard Unrestricted Use Data License v2.0",
        license_url="https://pubsaskdev.blob.core.windows.net/pubsask-prod/107346/107346-Standard_Unrestricted_Use_Data_Licence.pdf",
    ),
    PublicGeoSource(
        source_id="CA-SK-MINERAL-DISPOSITION-MINING-8",
        jurisdiction_code="CA-SK",
        name="Saskatchewan Mineral Tenure - MINING-8",
        canonical_type="mineral_disposition",
        service_url="https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Mining/MapServer/8",
        layer_index=8,
        source_crs=2957,
        license_summary="Government of Saskatchewan Standard Unrestricted Use Data License v2.0",
        license_url="https://pubsaskdev.blob.core.windows.net/pubsask-prod/107346/107346-Standard_Unrestricted_Use_Data_Licence.pdf",
    ),
    PublicGeoSource(
        source_id="CA-SK-RESOURCE-POTENTIAL",
        jurisdiction_code="CA-SK",
        name="Saskatchewan Resource Potential (all commodities)",
        canonical_type="resource_potential_zone",
        service_url="https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Resource_Map/MapServer",
        layer_index=None,
        source_crs=2957,
        license_summary="Government of Saskatchewan Standard Unrestricted Use Data License v2.0",
        license_url="https://pubsaskdev.blob.core.windows.net/pubsask-prod/107346/107346-Standard_Unrestricted_Use_Data_Licence.pdf",
    ),
    PublicGeoSource(
        source_id="CA-SK-RESOURCE-POTENTIAL-BASE",
        jurisdiction_code="CA-SK",
        name="Saskatchewan Resource Potential — Base Metals Potential",
        canonical_type="resource_potential_zone",
        service_url="https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Resource_Map/MapServer/13",
        layer_index=13,
        source_crs=2957,
        license_summary="Government of Saskatchewan Standard Unrestricted Use Data License v2.0",
        license_url="https://pubsaskdev.blob.core.windows.net/pubsask-prod/107346/107346-Standard_Unrestricted_Use_Data_Licence.pdf",
    ),
    PublicGeoSource(
        source_id="CA-SK-RESOURCE-POTENTIAL-BITUMEN",
        jurisdiction_code="CA-SK",
        name="Saskatchewan Resource Potential — Bitumen (Oil Sands) Potential",
        canonical_type="resource_potential_zone",
        service_url="https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Resource_Map/MapServer/16",
        layer_index=16,
        source_crs=2957,
        license_summary="Government of Saskatchewan Standard Unrestricted Use Data License v2.0",
        license_url="https://pubsaskdev.blob.core.windows.net/pubsask-prod/107346/107346-Standard_Unrestricted_Use_Data_Licence.pdf",
    ),
    PublicGeoSource(
        source_id="CA-SK-RESOURCE-POTENTIAL-COAL",
        jurisdiction_code="CA-SK",
        name="Saskatchewan Resource Potential — Coal Field",
        canonical_type="resource_potential_zone",
        service_url="https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Resource_Map/MapServer/3",
        layer_index=3,
        source_crs=2957,
        license_summary="Government of Saskatchewan Standard Unrestricted Use Data License v2.0",
        license_url="https://pubsaskdev.blob.core.windows.net/pubsask-prod/107346/107346-Standard_Unrestricted_Use_Data_Licence.pdf",
    ),
    PublicGeoSource(
        source_id="CA-SK-RESOURCE-POTENTIAL-GOLD",
        jurisdiction_code="CA-SK",
        name="Saskatchewan Resource Potential — Gold Potential",
        canonical_type="resource_potential_zone",
        service_url="https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Resource_Map/MapServer/14",
        layer_index=14,
        source_crs=2957,
        license_summary="Government of Saskatchewan Standard Unrestricted Use Data License v2.0",
        license_url="https://pubsaskdev.blob.core.windows.net/pubsask-prod/107346/107346-Standard_Unrestricted_Use_Data_Licence.pdf",
    ),
    PublicGeoSource(
        source_id="CA-SK-RESOURCE-POTENTIAL-HELIUM",
        jurisdiction_code="CA-SK",
        name="Saskatchewan Resource Potential — Helium Potential",
        canonical_type="resource_potential_zone",
        service_url="https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Resource_Map/MapServer/4",
        layer_index=4,
        source_crs=2957,
        license_summary="Government of Saskatchewan Standard Unrestricted Use Data License v2.0",
        license_url="https://pubsaskdev.blob.core.windows.net/pubsask-prod/107346/107346-Standard_Unrestricted_Use_Data_Licence.pdf",
    ),
    PublicGeoSource(
        source_id="CA-SK-RESOURCE-POTENTIAL-LITHIUM",
        jurisdiction_code="CA-SK",
        name="Saskatchewan Resource Potential — Lithium Potential",
        canonical_type="resource_potential_zone",
        service_url="https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Resource_Map/MapServer/15",
        layer_index=15,
        source_crs=2957,
        license_summary="Government of Saskatchewan Standard Unrestricted Use Data License v2.0",
        license_url="https://pubsaskdev.blob.core.windows.net/pubsask-prod/107346/107346-Standard_Unrestricted_Use_Data_Licence.pdf",
    ),
    PublicGeoSource(
        source_id="CA-SK-RESOURCE-POTENTIAL-OIL",
        jurisdiction_code="CA-SK",
        name="Saskatchewan Resource Potential — Oil and Gas Pools ",
        canonical_type="resource_potential_zone",
        service_url="https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Resource_Map/MapServer/5",
        layer_index=5,
        source_crs=2957,
        license_summary="Government of Saskatchewan Standard Unrestricted Use Data License v2.0",
        license_url="https://pubsaskdev.blob.core.windows.net/pubsask-prod/107346/107346-Standard_Unrestricted_Use_Data_Licence.pdf",
    ),
    PublicGeoSource(
        source_id="CA-SK-RESOURCE-POTENTIAL-POTASH",
        jurisdiction_code="CA-SK",
        name="Saskatchewan Resource Potential — Potash and Salt Resource Area",
        canonical_type="resource_potential_zone",
        service_url="https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Resource_Map/MapServer/9",
        layer_index=9,
        source_crs=2957,
        license_summary="Government of Saskatchewan Standard Unrestricted Use Data License v2.0",
        license_url="https://pubsaskdev.blob.core.windows.net/pubsask-prod/107346/107346-Standard_Unrestricted_Use_Data_Licence.pdf",
    ),
    PublicGeoSource(
        source_id="CA-SK-RESOURCE-POTENTIAL-RARE",
        jurisdiction_code="CA-SK",
        name="Saskatchewan Resource Potential — Rare Earths Potential",
        canonical_type="resource_potential_zone",
        service_url="https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Resource_Map/MapServer/17",
        layer_index=17,
        source_crs=2957,
        license_summary="Government of Saskatchewan Standard Unrestricted Use Data License v2.0",
        license_url="https://pubsaskdev.blob.core.windows.net/pubsask-prod/107346/107346-Standard_Unrestricted_Use_Data_Licence.pdf",
    ),
    PublicGeoSource(
        source_id="CA-SK-RESOURCE-POTENTIAL-URANIUM",
        jurisdiction_code="CA-SK",
        name="Saskatchewan Resource Potential — Uranium Potential",
        canonical_type="resource_potential_zone",
        service_url="https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Resource_Map/MapServer/11",
        layer_index=11,
        source_crs=2957,
        license_summary="Government of Saskatchewan Standard Unrestricted Use Data License v2.0",
        license_url="https://pubsaskdev.blob.core.windows.net/pubsask-prod/107346/107346-Standard_Unrestricted_Use_Data_Licence.pdf",
    ),
    PublicGeoSource(
        source_id="CA-SK-ROCK-SAMPLES",
        jurisdiction_code="CA-SK",
        name="Saskatchewan Government Rock Samples",
        canonical_type="rock_sample",
        service_url="https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Mineral_Exploration/MapServer/4",
        layer_index=4,
        source_crs=2957,
        license_summary="Government of Saskatchewan Standard Unrestricted Use Data License v2.0",
        license_url="https://pubsaskdev.blob.core.windows.net/pubsask-prod/107346/107346-Standard_Unrestricted_Use_Data_Licence.pdf",
    ),
    PublicGeoSource(
        source_id="CA-SK-SMDI",
        jurisdiction_code="CA-SK",
        name="Saskatchewan Mineral Deposits Index (SMDI)",
        canonical_type="mineral_occurrence",
        service_url="https://gis.saskatchewan.ca/arcgis/rest/services/Economy/Mineral_Exploration/MapServer/5",
        layer_index=5,
        source_crs=2957,
        license_summary="Government of Saskatchewan Standard Unrestricted Use Data License v2.0",
        license_url="https://pubsaskdev.blob.core.windows.net/pubsask-prod/107346/107346-Standard_Unrestricted_Use_Data_Licence.pdf",
    ),]

# Canonical types the chat tool and UI expose. Anything in SOURCES with a type
# outside this set is still addressable by source_id but is not offered as a
# browsable category.
CANONICAL_TYPES: tuple[str, ...] = (
    "mine",
    "mineral_occurrence",
    "drillhole_collar",
    "resource_potential_zone",
    "rock_sample",
    "assessment_survey",
    "mineral_disposition",
)


def sources_for(
    *,
    canonical_types: list[str] | None = None,
    jurisdiction_codes: list[str] | None = None,
) -> list[PublicGeoSource]:
    """Select feeds to query, narrowing by type and/or jurisdiction.

    Empty/None filters mean "no narrowing" rather than "match nothing" — a
    caller that supplies no hints gets every browsable feed, which is the
    behaviour the chat tool relies on when the classifier extracted nothing.
    """
    wanted_types = set(canonical_types or CANONICAL_TYPES)
    wanted_juris = set(jurisdiction_codes or ())

    out = []
    for s in SOURCES:
        if not s.is_queryable:
            continue
        if s.canonical_type not in wanted_types:
            continue
        if wanted_juris and s.jurisdiction_code not in wanted_juris:
            continue
        out.append(s)
    return out


def source_by_id(source_id: str) -> PublicGeoSource | None:
    """Look up one feed — the entry point citation resolution needs.

    A citation carries `source_id` plus the upstream OBJECTID, so resolving it
    means finding the feed here and re-fetching that single feature.
    """
    for s in SOURCES:
        if s.source_id == source_id:
            return s
    return None


def jurisdiction_for(source: PublicGeoSource) -> Jurisdiction | None:
    return JURISDICTIONS.get(source.jurisdiction_code)
