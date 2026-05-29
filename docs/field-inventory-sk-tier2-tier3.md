# Saskatchewan ArcGIS Field Inventory — Tier 2 + Tier 3

**Probed:** 2026-04-15
**Base URL:** `https://gis.saskatchewan.ca/arcgis/rest/services/Economy/`
**All services:** WKID=2957 (NAD83/UTM 13N), MapServer (not FeatureServer)

This file is the authoritative record of field-level metadata captured during Phase 1 of the Tier 2+3 expansion. Each entry informs the canonical schema + `FieldMapping` registry entries downstream.

---

## 1. Mining/MapServer (maxRecordCount=1000)

**16 layers**, all `esriGeometryPolygon`. **Two field schemas** — legacy cryptic names (layers 0–4) and modern clean names (layers 5–8). Layers 9–15 are CR Preclude variants (deferred unless needed).

### Layer 0 — Mineral Dispositions (active)
Display field: `DISPOSIT_1`

| Name | Alias | Type | Length |
|---|---|---|---|
| OBJECTID | OBJECTID | OID | — |
| DISPOSITIO | DISPOSITIO | Integer | — |
| DISPOSIT_1 | DISPOSIT_1 | String | 50 |
| OWNERS | OWNERS | String | 254 |
| EFFECTIVED | EFFECTIVED | Date | 8 |
| GOODSTANDI | GOODSTANDI | Date | 8 |
| WORKWAITIN | WORKWAITIN | String | 50 |
| ISDELETED | ISDELETED | String | 50 |
| DISPOSIT_2 | DISPOSIT_2 | Integer | — |
| DISPOSIT_3 | DISPOSIT_3 | String | 50 |

**Canonical mapping (legacy schema):**
- `disposition_number` ← `DISPOSIT_1` (string; e.g., `CBS-123456`)
- `holder_name` ← `OWNERS`
- `issue_date` ← `EFFECTIVED`
- `expiry_date` ← `GOODSTANDI`
- `work_status` ← `WORKWAITIN`
- `internal_id` ← `DISPOSITIO` (integer)

Layers 1–4 use the same legacy schema, distinguished by layer position:
- Layer 1 → status='legacy'
- Layer 2 → status='pending'
- Layer 3 → status='reopening'
- Layer 4 → status='lapsed'

### Layer 5 — Potash Dispositions (clean schema)
Display field: `DISPOSITION`

| Name | Alias | Type | Length |
|---|---|---|---|
| OBJECTID | OBJECTID | OID | — |
| DISPOSITION | DISPOSITION | String | 255 |
| DISPOSITIONREVISION | DISPOSITIONREVISION | String | 255 |
| MAPCOLOR | MAPCOLOR | String | 255 |
| STATUS | STATUS | String | 255 |
| HOLDER | HOLDER | String | 255 |
| ANNIVERSARYDATE | ANNIVERSARYDATE | Date | 8 |
| HECTARES | HECTARES | Double | — |

**Canonical mapping (clean schema):**
- `disposition_number` ← `DISPOSITION`
- `disposition_revision` ← `DISPOSITIONREVISION`
- `status` ← `STATUS`
- `holder_name` ← `HOLDER`
- `anniversary_date` ← `ANNIVERSARYDATE`
- `area_ha` ← `HECTARES`

Layers 6 (Alkali), 7 (Coal), 8 (Quarry) use the same clean schema, distinguished by layer position → `commodity_type`.

---

## 2. Mineral_Tenure_Crown_Dispositions/MapServer (maxRecordCount=2000)

**9 layers**. Layers 0–7 duplicate Mining service data (confirmed by identical fields on layer 0). **Only layer 8 (Oil and Gas Dispositions) has unique data worth ingesting.**

### Layer 8 — Oil and Gas Dispositions
Display field: `DISPID`

| Name | Alias | Type | Length |
|---|---|---|---|
| OBJECTID | OBJECTID | OID | — |
| DISPID | DISPID | String | 200 |
| DISPTYPE | DISPTYPE | String | 200 |
| DISPSTATUS | DISPSTATUS | String | 200 |
| ISSUEDATE | ISSUEDATE | Date | 8 |
| LESSEES | LESSEES | String | 200 |
| GEOAREA | GEOAREA | String | 50 |
| BONUSBID | BONUSBID | Double | — |
| DSTRATRGHT | DSTRATRGHT | String | 254 |
| PARCELHECT | PARCELHECT | Double | — |

**Canonical mapping (routes to `pg_mineral_disposition` with `disposition_type='oil_gas'`):**
- `disposition_number` ← `DISPID`
- `disposition_subtype` ← `DISPTYPE`
- `status` ← `DISPSTATUS`
- `issue_date` ← `ISSUEDATE`
- `holder_name` ← `LESSEES`
- `geographic_area` ← `GEOAREA`
- `bonus_bid_amount` ← `BONUSBID`
- `strata_rights` ← `DSTRATRGHT`
- `area_ha` ← `PARCELHECT`

**Decision:** ingest only this single layer from the Crown service. Skip layers 0–7 as duplicates.

---

## 3. Geology/MapServer (maxRecordCount=2000)

**9 feature layers** (rest are group headers).

### Layer 10 — Bedrock Geology 250K (polygon)
Field list partially truncated. Key renderer field: `ROCK_CODE` (400+ coded values). **Needs sample-feature query during implementation to capture full schema** — expected fields include `ROCK_CODE`, `ROCK_NAME`, `UNIT`, `FORMATION`, `AGE`, `ERA`.

**Action at implementation time:** query `?where=1=1&outFields=*&resultRecordCount=1&f=json` to capture sample feature properties.

### Layer 11 — Bedrock Geology 1M (polygon)
Same probe approach at implementation time.

### Layer 5 — Surficial Geology 250K (polygon)
Display field: `COLOUR_CODE`

| Name | Alias | Type | Length |
|---|---|---|---|
| OBJECTID | OBJECTID | OID | — |
| CODE | CODE | String | 50 |
| GLOBALID | GLOBALID | GlobalID | 38 |
| COLOUR_CODE | COLOUR_CODE | String | 15 |
| MAIN_ENVIRONMENT | MAIN_ENVIRONMENT | String | 50 |

**Coded values** (COLOUR_CODE): A, Ap, C, E, Eh, Ep, Er, GF, GFe, GFh, GFp, GFt, GL, GLd, GLp, L, M, Md, Me, Mh, Mp, Mr, Mu, Mv, O, Op, R (27 classes).

### Layer 8 — Surficial Geology 1M (polygon)
Expected similar structure; probe at implementation.

### Layer 1 — Faults 250K (polyline)
Display field: `FEAT_TYPE`

| Name | Alias | Type | Length |
|---|---|---|---|
| OBJECTID_1 | OBJECTID_1 | OID | — |
| OBJECTID | OBJECTID | Integer | — |
| DIRECTION | DIRECTION | String | 10 |
| TYPE | TYPE | String | 7 |
| SHAPE_LENG | SHAPE_LENG | Double | — |
| FEAT_TYPE | FEAT_TYPE | String | 254 |
| FEAT_NAME | FEAT_NAME | String | 254 |
| TITLE | TITLE | String | 254 |
| SOURCE1 | SOURCE1 | String | 250 |
| SOURCE2 | SOURCE2 | String | 250 |
| YEAR | YEAR | Integer | — |
| CRIT_MAIN | CRIT_MAIN | String | 254 |
| CRIT_OTHER | CRIT_OTHER | String | 254 |
| COMPILERS | COMPILERS | String | 50 |

**Note:** `OBJECTID_1` is the real primary key; `OBJECTID` is degenerate (existing `_derive_source_feature_id()` already handles this).

**Canonical mapping (`pg_geological_fault`):**
- `fault_name` ← `FEAT_NAME`
- `fault_type` ← `FEAT_TYPE`
- `direction` ← `DIRECTION`
- `kinematic_type` ← `TYPE` (thrust/normal/strike-slip abbreviation)
- `reference_title` ← `TITLE`
- `source_reference` ← COALESCE(`SOURCE1`, `SOURCE2`)
- `compilation_year` ← `YEAR`
- `compilers` ← `COMPILERS`
- `criteria_main` ← `CRIT_MAIN`
- `criteria_other` ← `CRIT_OTHER`
- `scale` ← '250K' (fixed per layer source)

### Layer 2 — Major Faults and Shear Zones 1M (polyline)
Probe at implementation time; expected similar schema with `scale`='1M'.

### Layer 4 — Surficial Geology Linear Landforms 250K (polyline)
Probe at implementation time. **Decision:** include in scope if schema is substantive; otherwise defer.

### Layer 6 — Surficial Geology Point Landforms 1M (point)
Probe at implementation time. Small value; may defer.

### Layer 7 — Surficial Geology Linear Landforms 1M (polyline)
Probe at implementation time; pair with layer 4 via `scale`.

### Layer 12 — Mackenzie Dyke 1M (polyline)
Single structural feature. Probe at implementation; can route to `pg_geological_fault` with `fault_type='dyke'` if schema compatible, else standalone.

---

## 4. Petroleum/MapServer (maxRecordCount=2000)

**22 layers.** Scope narrowed to high-value subset for mineral-exploration geologists.

### Layer 0 — Vertical Wells (point)
Field list truncated; renderer keys: `WELLSTATUS`, `WELLBORECOMP_CURRENTCOMPTYPE`.

**Action at implementation time:** sample-feature query to capture full schema — expected: UWI, operator, spud_date, completion_date, total_depth, well_name, formation, pool.

**WELLSTATUS coded values:** Abandoned (Junked), Abandoned (Reentry), Abandoned, Active, Cased, Completed, Downhole Abandoned, Drilling, Planned (Cancelled).

**WELLBORECOMP_CURRENTCOMPTYPE coded values:** Oil Well (10), Gas Well (20), Water Source Well (30), Disposal Well (34), Relief Well (02), Stratigraphic Test Well (00), plus injection types (40-72).

### Layer 1 — Non Vertical Wells (polyline)
Probe at implementation time. Likely same field schema as layer 0 with trajectory geometry. Routes to `pg_petroleum_well_trajectory` (MULTILINESTRING).

### Layer 6 — Pool Land (polygon)
Display field: `POOLNAME`

| Name | Alias | Type | Length |
|---|---|---|---|
| OBJECTID | OBJECTID | OID | — |
| POOLCODE | POOLCODE | String | 20 |
| POOLNAME | POOLNAME | String | 25 |
| GEOAREA | GEOAREA | String | 20 |
| POOLTYPE | POOLTYPE | String | 20 |
| ZONETYPE | ZONETYPE | String | 20 |
| HECTARES | HECTARES | Double | — |

**Canonical mapping (`pg_petroleum_pool`):**
- `pool_code` ← `POOLCODE`
- `pool_name` ← `POOLNAME`
- `geographic_area` ← `GEOAREA`
- `fluid_type` ← `POOLTYPE` (Oil/Gas/Oil&Gas)
- `zone_type` ← `ZONETYPE`
- `area_ha` ← `HECTARES`

### Layer 18 — Orphan Wells (point)
Probe at implementation. Expected: same well schema + orphan-specific status fields. Route to `pg_petroleum_well` with `status='orphan'`.

### Deferred layers
- Layer 2 Drill Core, 3 Drill Cuttings, 4 Thin Sections (point) — highly specialized, defer to future session.
- Layer 5 Unit Boundaries, 7 Disposition, 8 Landsale Posting, 9-12 Restricted areas, 13-17 admin, 15 Spacing, 16 Trust, 20 Project Data, 21 Incidents — administrative/regulatory layers. Defer.
- Layer 19 Orphan Facilities — defer unless SME requests.

---

## 5. Geophysical_Data_and_Indicies/MapServer (maxRecordCount=2000)

**3 feature layers** (0–2) + 5 raster layers (3–7, confirmed non-ingestible via current Bronze).

### Layer 0 — Lithoprobe Lines (point)
Probe at implementation time.

### Layer 1 — Aeromagnetic Survey Index (polygon)
Probe at implementation time. Coverage footprints with likely fields: survey_id, year, line_spacing, flight_height, contractor.

### Layer 2 — NATGAM Spectrometer Survey Index (polygon)
Probe at implementation time. Radiometric coverage footprints with similar schema to layer 1.

---

## 6. Geophysical_Interpretation/MapServer (maxRecordCount=2000)

**4 layers**, mixed polyline + polygon.

### Layer 0 — Structure Form Lines (polyline)
| Name | Alias | Type | Length |
|---|---|---|---|
| OBJECTID | OBJECTID | OID | — |
| ID | ID | Integer | — |
| GLOBALID | GLOBALID | GlobalID | 38 |

**No attributive metadata.** Just geometry with incrementing ID. **Decision: SKIP** — no semantic value for RAG/chat tool.

### Layer 1 — EM Conductors (polyline)
Probe at implementation time. Expected: conductor_id, survey_source, conductivity_class. If schema is substantive, ingest to `pg_geophysical_interpretation_line`.

### Layer 2 — North American Central Plains Anomaly (polygon)
Probe at implementation time. Single named polygon; low volume.

### Layer 3 — Magnetic Domains (polygon)
Probe at implementation time. Expected: domain_name, dominant_signature.

---

## 7. Geological_Domains/MapServer (maxRecordCount=2000)

### Layer 0 — Geological Domains (polygon)
Display field: `DOMAIN`

| Name | Alias | Type | Length |
|---|---|---|---|
| OBJECTID | OBJECTID | OID | — |
| DOMAIN | DOMAIN | String | 20 |
| PREC_PHAN | Prec_Phan | String | 50 |
| CRATON_PROVINCE | Craton_Province | String | 50 |

34 polygons.

**Canonical mapping (`pg_geological_domain`, `domain_type='tectonic'`):**
- `domain_name` ← `DOMAIN`
- `prec_phan_classification` ← `PREC_PHAN` (Precambrian vs Phanerozoic)
- `parent_craton_province` ← `CRATON_PROVINCE`

---

## 8. Metamorphic_Facies/MapServer (maxRecordCount=2000)

### Layer 0 — Metamorphic Facies (polygon)
Display field: `DOMINANT_F`

| Name | Alias | Type | Length |
|---|---|---|---|
| OBJECTID | OBJECTID | OID | — |
| CONTEXT | CONTEXT | String | 50 |
| TIMING | TIMING | String | 150 |
| DOMINANT_F | DOMINANT_F | String | 50 |
| GLOBALID | GLOBALID | GlobalID | 38 |

16 polygons.

**Canonical mapping (`pg_geological_domain`, `domain_type='metamorphic_facies'`):**
- `domain_name` ← `DOMINANT_F`
- `context` ← `CONTEXT`
- `timing` ← `TIMING`

---

## 9. Cratonic/MapServer (maxRecordCount=2000)

### Layer 0 — Cratonic Elements (polygon)
Display field: `NAME`

| Name | Alias | Type | Length |
|---|---|---|---|
| OBJECTID | OBJECTID | OID | — |
| MAPKEY | MAPKEY | String | 128 |
| NAME | NAME | String | 128 |

5 polygons: Hearne Craton, Medicine Hat Block, Rae Craton, Reindeer Zone, Sask Craton.

**Canonical mapping (`pg_geological_domain`, `domain_type='cratonic'`):**
- `domain_name` ← `NAME`
- `mapkey` ← `MAPKEY`

---

## 10. Chronology/MapServer (maxRecordCount=2000)

### Layer 0 — Chronology (polygon)
Display field: `LITHOSTRAT_LEVEL1_MAJ`

| Name | Alias | Type | Length |
|---|---|---|---|
| OBJECTID | OBJECTID | OID | — |
| LITHOSTRAT_LEVEL1_MAJ | STRAT_LEV1 | String | 100 |
| CHRONOLOGY | CHRONOLOGY | String | 20 |

**This is NOT geochronology samples.** It's lithostratigraphic time-period polygons (Precambrian/Paleozoic/Mesozoic/Cenozoic surface exposure zones).

**Canonical mapping (`pg_geological_domain`, `domain_type='chronostratigraphic'`):**
- `domain_name` ← `LITHOSTRAT_LEVEL1_MAJ`
- `chronology_level` ← `CHRONOLOGY`

---

## 11. Regional_Datasets_and_Compilations/MapServer (maxRecordCount=2000)

**7 feature layers** (all point/polygon, no raster).

Each layer is a distinct scientific feature type. **Decision:** use a **unified `pg_regional_compilation` table** with `feature_type` discriminator (pattern mirrors `pg_resource_potential_zone` with `commodity_type`).

### Layer 3 — Kimberlite Occurrences (point)
Display field: `BODY_ID`

| Name | Alias | Type | Length |
|---|---|---|---|
| OBJECTID | OBJECTID | OID | — |
| BODY_ID | BODY_ID | String | 74 |
| STATUS | STATUS | String | 62 |
| FILE_NUM | FILE_NUM | String | 92 |
| DIAMND_REP | DIAMND_REP | String | 23 |
| UTM_E | UTM_E | Double | — |
| UTM_N | UTM_N | Double | — |

**High value for minerals exploration** (diamond targeting).

### Layers 1, 2, 4, 5, 6, 7 — Astroblemes, Ice-Flow, Paleocurrent, Radioactive Boulders, Outcrops, Uranium Deposit Footprints
Probe at implementation time. Expected: each has a name/ID field, description, reference.

---

## 12. GroundWater/MapServer (maxRecordCount=2000)

### Layer 0 — Groundwater Sensitivity Area (polygon)
Display field: `SURFACE_TYPE`

| Name | Alias | Type | Length |
|---|---|---|---|
| OBJECTID | OBJECTID | OID | — |
| SURFACE_TYPE | Type | String | 55 |

Single polygon attribute. **Low value. DEFER to Tier 3 or skip.** Does not include wells — service name is misleading.

---

## 13. Geoscience_Maps_and_Publications/MapServer (maxRecordCount=2000)

**318 layers total.** Layers 0 and 1 are the map sheet indexes (valuable). Layers 2–317 are stratigraphic isopach/structure contours (316 individual polylines per formation — **out of scope** as separate canonical entity).

### Layer 0 — Geoscience Publications (polygon)
Display field: `TITLE`

| Name | Alias | Type | Length |
|---|---|---|---|
| OBJECTID | OBJECTID | OID | — |
| REPORT_NUMBER | REPORT_NUM | String | 10 |
| MAP_NUMBER | MAP_NUM | String | 13 |
| SCALE | SCALE | Double | — |
| YEAR_PUBLISHED | YEAR_PUBL | Double | — |
| AUTHORS | AUTHORS | String | 58 |
| TITLE | TITLE | String | 256 |
| WEBLINK | WEBLINK | String | 200 |
| TYPE | TYPE | String | 20 |
| CONTENT | CONTENT | String | 256 |
| REPORT_PDF | REPORT_PDF | String | 3 |
| MAPS | MAPS | String | 3 |
| GIS_DATA | GIS_DATA | String | 3 |
| ORIG_DATA | ORIG_DATA | String | 3 |

**High value for RAG citations.** WEBLINK directly usable for source attribution.

**Canonical mapping (`pg_geoscience_publication`, `publication_type='publication'`):**
- `report_number` ← `REPORT_NUMBER`
- `map_number` ← `MAP_NUMBER`
- `title` ← `TITLE`
- `authors` ← `AUTHORS`
- `scale` ← `SCALE`
- `year_published` ← `YEAR_PUBLISHED`
- `publication_type_raw` ← `TYPE`
- `content_summary` ← `CONTENT`
- `weblink` ← `WEBLINK`
- `has_pdf` ← (`REPORT_PDF` = 'Yes')
- `has_maps` ← (`MAPS` = 'Yes')
- `has_gis_data` ← (`GIS_DATA` = 'Yes')
- `has_orig_data` ← (`ORIG_DATA` = 'Yes')

### Layer 1 — Geoscience Theses (polygon)
Probe at implementation time. Expected: similar structure with `author`, `university`, `degree`, `year`, `title`, `weblink`.

---

## 14. Analytical_and_Rock_Property_Data/MapServer (maxRecordCount=2000)

**7 feature point layers.** Layer 7 (Geochronology) has a very rich 37-field schema.

### Layer 7 — Geochronology (point)
Display field: `SAMPLE_NAME`

**37 fields** including:
- Identity: SAMPLE_NAME, LAB_NO, OBJECTID
- Age: AGE_MA, ERROR_PLUS, ERROR_MINUS, MSWD, TH_U_RATIO, DISCORDANCE_PERCENT
- Method: ISOTOPIC_SYSTEM, INSTRUMENTATION, CALCULATION_METHOD, ISOTOPIC_RATIO_USED_FOR_CALCUA, NUMBER_OF_FRACTIONS_OR_GRAINS
- Sample: MINERAL, LITHO_TYPE, SAMPLE_MATERIAL, ROCK_QUALIFIER, ROCK_COMMENTS, GEOLOGICAL_ENTITY, STATION_TYPE, UNIT_NAME
- Drillhole context: DRILL_HOLE_NAME, DRILL_HOLE_SAMPLE_DEPTH_FROM_M, DRILL_HOLE_SAMPLE_DEPTH_TO_MET
- Meta: GEOLOGIST, LABORATORY, SAMPLE_COLLECTION_YEAR, LAST_UPDATED, AGE_SIGNIFICANCE, GEOLOGICAL_INTERPRETATION, ADDITIONAL_INFORMATION, COMMENTS_REGARDING_AGE, FULL_REFERENCE, EPMA_POP_DESCRIPT
- Location: UTM_E, UTM_N

**Canonical mapping (`pg_geochronology_sample`):**
- `sample_id` ← `SAMPLE_NAME`
- `lab_number` ← `LAB_NO`
- `age_ma` ← `AGE_MA`
- `uncertainty_plus_ma` ← `ERROR_PLUS`
- `uncertainty_minus_ma` ← `ERROR_MINUS`
- `mswd` ← `MSWD`
- `th_u_ratio` ← `TH_U_RATIO`
- `discordance_percent` ← `DISCORDANCE_PERCENT`
- `isotopic_system` ← `ISOTOPIC_SYSTEM`
- `instrumentation` ← `INSTRUMENTATION`
- `mineral_dated` ← `MINERAL`
- `lithology_type` ← `LITHO_TYPE`
- `sample_material` ← `SAMPLE_MATERIAL`
- `rock_qualifier` ← `ROCK_QUALIFIER`
- `geological_entity` ← `GEOLOGICAL_ENTITY`
- `unit_name` ← `UNIT_NAME`
- `drillhole_name` ← `DRILL_HOLE_NAME`
- `drillhole_depth_from_m` ← `DRILL_HOLE_SAMPLE_DEPTH_FROM_M`
- `drillhole_depth_to_m` ← `DRILL_HOLE_SAMPLE_DEPTH_TO_MET`
- `geologist` ← `GEOLOGIST`
- `laboratory` ← `LABORATORY`
- `collection_year` ← `SAMPLE_COLLECTION_YEAR`
- `age_significance` ← `AGE_SIGNIFICANCE`
- `geological_interpretation` ← `GEOLOGICAL_INTERPRETATION`
- `reference_full` ← `FULL_REFERENCE`
- `rock_comments` ← `ROCK_COMMENTS`
- `age_comments` ← `COMMENTS_REGARDING_AGE`
- `additional_info` ← `ADDITIONAL_INFORMATION`

### Layers 2-6 and 8 — Geochemistry / Radioisotopic Tracers
Probe at implementation time. All are point layers. **Decision:** use unified `pg_geochemistry_sample` table with `sample_type` discriminator:
- Layer 2 Lake Water → `sample_type='lake_water'`
- Layer 3 Lake Sediment → `sample_type='lake_sediment'`
- Layer 4 GSC Lake Sediment → `sample_type='gsc_lake_sediment'`
- Layer 5 Lithogeochemistry → `sample_type='lithogeochem'`
- Layer 6 Surficial Geochemistry → `sample_type='surficial_geochem'`
- Layer 8 Radioisotopic Tracers → `sample_type='radioisotopic'`

Analytical results (element concentrations) likely carried as wide columns or JSONB. Decide at probe time.

---

## Plan revisions based on probe results

**Accept:**
- ✅ Mineral_Tenure_Crown_Dispositions layer 8 (Oil and Gas) — ingest alone; skip layers 0–7 as duplicates.
- ✅ Geochronology samples live in `Analytical_and_Rock_Property_Data/7`, NOT `Chronology/0`.
- ✅ Plan's `pg_geological_domain` unified table concept holds — use it for Cratonic, Geological_Domains, Metamorphic_Facies, AND Chronology (4 sources → 1 table with `domain_type` discriminator).
- ✅ Plan's `pg_regional_compilation` unified table concept holds — use it for all 7 Regional_Datasets_and_Compilations layers with `feature_type` discriminator.
- ✅ Plan's `pg_geoscience_publication` scoped to layers 0 (Publications) + 1 (Theses) only of Geoscience_Maps_and_Publications.

**Revise:**
- ⚠ Mining layers 0–4 and 5–8 have DIFFERENT field schemas → need TWO field mappings (`MineralDispositionLegacyFieldMapping` + `MineralDispositionModernFieldMapping`) or branch inside extractor.
- ⚠ Structure Form Lines (Geophysical_Interpretation/0) has no metadata → SKIP.
- ⚠ GroundWater/0 has single attribute → DEFER / low-priority.
- ⚠ Chronology/0 is a time-polygon domain, NOT geochronology samples. Fold into `pg_geological_domain` with `domain_type='chronostratigraphic'`.

**Defer / out of scope:**
- Petroleum layers 2, 3, 4 (core/cuttings/thin sections) — specialized, out of scope.
- Petroleum admin layers (5, 7-17, 19-21) — out of scope for minerals-exploration audience.
- Geoscience_Maps_and_Publications layers 2-317 (stratigraphic contours) — 316 formation-specific polyline layers; too specialized for v1.
- Geophysical_Data_and_Indicies raster layers 3-7 — confirmed non-ingestible via current Bronze; future Raster Bronze architecture is out of scope.

**Still-to-probe at implementation time (field-level):**
- Bedrock Geology 250K + 1M (full field list)
- Surficial Geology 1M, Surficial Linear Landforms 250K+1M, Surficial Point Landforms 1M, Mackenzie Dyke 1M
- Petroleum Vertical Wells (full fields), Non-Vertical Wells, Orphan Wells
- Geophysical_Data_and_Indicies/0, /1, /2 (full fields)
- Geophysical_Interpretation/1, /2, /3 (full fields)
- Regional_Datasets_and_Compilations/1, /2, /4, /5, /6, /7 (full fields)
- Analytical_and_Rock_Property_Data/2, /3, /4, /5, /6, /8 (full fields)
- Geoscience_Maps_and_Publications/1 (Theses — full fields)

These are per-layer `?f=json` calls plus a sample-feature query `?where=1=1&outFields=*&resultRecordCount=1&f=json` captured inline during Silver extractor development.
