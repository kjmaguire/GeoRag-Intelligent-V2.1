# Phase A — Uranium_Logs_ALL.zip walk complete

**Date:** 2026-05-14
**Run ID:** `67929603-8b5b-484d-bbc2-807c4a0f8d7f`
**Duration:** 9 minutes 2 seconds (after 35-min one-time staging copy)
**Source:** `C:\Users\GeoRAG\Desktop\Uranium_Logs_ALL.zip` (199.6GB) →
            staged to `georag-phase-a-stage` Docker volume → walked

## What's in the archive

### Provenance (from `00_ReadMe.txt` at outer level)

> *"These logs were scanned in 2005 or 2006 by Energy Metals Corp and Bob
> Gregory, former WSGS U geologist. As of spring 2024, the paper files are
> backed up at the WY State Archives. This is a copy of the data that is
> on the WSGS website, viewed through the Minerals Map of WY."*

**Data lineage:** Wyoming State Geological Survey (WSGS) historical
uranium drill logs. Open public data via the WSGS Minerals Map of
Wyoming. NOT Athabasca Basin — **Wyoming sandstone-hosted roll-front
uranium** (Shirley Basin, Powder River Basin, Wind River Basin, Gas
Hills, Crook County, Carbon County).

### Headline numbers

| Metric | Value |
|---|---|
| Total files indexed | **36,232** |
| Total bytes | **204 GB** (218,910,536,626) |
| Distinct clusters (one per inner zip) | **1,012** |
| Walk duration | 9m 2s (66.84 files/sec) |
| Header-parse anomalies | 33,751 (99% of TIFFs — known: 64KB head slice too small for Wyoming TIFF flavor; cosmetic for Phase A) |

### File type breakdown

| Type | Count | Notes |
|---|---|---|
| **TIFF** | **34,119** (94.2%) | Scanned paper logs, 7MB avg, multi-page |
| **PDF** | **634** (1.7%) | Reports, abandonment filings, development docs — **machine-readable text** |
| **LAS** | **188** (0.5%) | Standard well-log format, `lasio` parses natively |
| **JPEG** | 32 | Supplementary imagery |
| **XLSX** | 11 | Structured tabular data |
| **Inner zip** | 4 | Triple-nested archives (rare) |
| Unknown (.db, .log, .bmp, .txt) | 1,244 | 662 Thumbs.db + **487 .log files** (proprietary Cameco binary) + 94 BMP + 1 readme |

**Hidden value:** the **487 `.log` files are 2012 Cameco gamma-tool
logs from the Shirley Basin operation.** Binary format with embedded
text headers carrying:
- Tool ID (`9057C`)
- Service company (`CAMECO RESOURCES SVCS`)
- Hole ID, drill date, depth range
- PLSS Township-Range-Section coords + state-plane northing/easting
- Lithology + drilling mud + basin name

Sample header from `36-1051_08-14-12_16-13_9057C_.10_0.40_404.90_ORIG.log`:

```
ORIGINAL 9057C 3.60K   1   F.597923 .10  0.40  404.90UE  240008/14/1216:13: 0
CAMECO RESOURCES SVCS  36-1051 5
SHIRLEY BASIN  CARBON  WY  E 36 E 28 79
HOLMGREN 1 LG
E=793606 N=615386
```

LAS counterparts (`PROC.LAS` files) are the processed equivalents in
standard LAS format — these are immediately ingestible via `lasio`.

### Cluster organization

**Township-Range-Section grid.** Each inner zip is one section
bundle. Top 20 by file count:

| Cluster | Files |
|---|---|
| `027N078W15.zip` | 2,228 |
| `024N093WXX.zip` | 1,545 |
| `027N078W14.zip` | 1,530 |
| `028N079W36.zip` | 1,523 (Cameco 2012 Shirley Basin operation) |
| `027N078W09.zip` | 1,290 |
| `036N074W36.zip` | 1,107 |
| `027N078W10.zip` | 994 |
| `027N078W04.zip` | 987 |
| `033N089W28.zip` | 976 |
| `024N093W16.zip` | 943 |
| ...20 more... | |

Township translation (Wyoming PLSS):
- **027N078W** = Carbon County / Sweetwater County (Shirley Basin)
- **024N093W** = Carbon County / Sweetwater County (Great Divide Basin
  / Red Desert area)
- **028N079W** = Carbon County (Shirley Basin — Cameco operations)
- **036N074W** = Albany / Carbon County (Shirley Basin north)
- **045N081W** = Natrona County (Wind River Basin north)
- **055N065W** = Sheridan / Crook County (Powder River Basin)

Some clusters lack TRS coords and use county names instead:
- `Albany_No_TRS.zip` (19MB)
- `Carbon_No_TRS.zip` (9.6MB)
- `Fremont_No_TRS.zip` (7.6MB)
- `Johnson_No_TRS.zip` (777MB — substantial!)

### Operator codes observed in filenames

| Code | Likely operator | Examples |
|---|---|---|
| `KM` | Kerr-McGee | `WY_KM_BS_024N091W11_*` |
| `MX` | Mexican Hat / Mountain West | `WY_MX_RD_024N093WXX_*` |
| `UN` | Union Carbide / Uranium One | `WY_UN_SPRB_036N073W36_*` |
| `CX_CR` | Crow Butte | `WY_CX_CR_006N002W06_*` |
| `TE_LI` | Tetra Tech / Liberty | `WY_TE_LI_O18N110W18_*` |
| Cameco (named) | Cameco Resources | `2012 Cameco/2012 E-Log Data/...` |
| IC-N (Albany Co) | Industrial Constructors? | `IC-003 Abandonment Report` |

## Phase B recommendations (post-review)

### Tier 1 — fast wins (no OCR needed; <1 day of compute)

1. **Ingest 188 LAS files via `lasio`** — these are standard well-log
   format. Schema maps directly to `silver.lithology_logs` +
   `silver.geophysics_logs`. Each LAS file has 0.1ft sampling
   resolution. **Total: ~200 ingested drillholes with real gamma data.**

2. **Parse 634 PDFs via §04p CPU-OCR pipeline.** Many are native PDFs
   with extractable text; OCR only fires on scanned PDFs. Each PDF
   becomes a `document` + N `document_passages`.

3. **Parse 11 XLSX via Polars.** Structured tabular data, likely
   collar/assay/lithology tables. Map via `column_mapping_wizard`.

4. **Parse 487 Cameco `.log` files via custom binary header reader.**
   The embedded header text carries hole-ID + location + depth range
   without needing the full binary decode. **A Python regex pass on
   the first 64KB of each .log file extracts the metadata.** Land in
   `silver.collars` (hole locations) + `silver.geophysics_logs` (depth
   ranges).

### Tier 2 — heavy OCR (days of compute)

5. **OCR 34,119 TIFF scans.** At ~5 sec/page CPU OCR (typical for
   §04p pipeline), 34k pages = ~47 hours of compute on a single
   worker. Distribute across the 2 Hatchet pools to halve to ~24h.
   Stratified sample first to validate quality.

### Tier 3 — derived intelligence

6. **PLSS Township-Range-Section grid join.** Use the manifest's
   `guessed_project` (which is the township-section name) to link
   every ingested drillhole back to its USGS quadrangle. The
   `public_geoscience.pg_*` adapters should add a Wyoming MINFILE
   equivalent (USGS Mineral Resources Data System) for cross-ref.

7. **Cameco 2012 Shirley Basin focus.** The `028N079W36.zip` cluster
   (1,523 files) is the single richest modern dataset in the archive.
   Prioritize this cluster for Phase B end-to-end demonstration —
   Cameco's 2012 hole-by-hole gamma logs are the closest the archive
   comes to "fresh production data."

## What this unlocks

| Unlock | Status now | After Tier 1 ingest |
|---|---|---|
| `silver.document_passages` populated | 0 rows | ~10k+ rows (from PDFs) |
| `silver.lithology_logs` populated | 0 rows | ~200 drillholes |
| `silver.collars` populated | 0 rows | ~700 unique drillholes |
| Real RAG eval non-vacuous on Layers 1-5 | refusal-only | Wyoming-uranium grounded |
| Cross-encoder Layer 5 exercised | inert (no chunks) | active with real scoring |
| Numeric grounding (Layer 3) | structural-only | live against gamma counts |
| Reports buildable | template only | real Cameco 2012 reconstruction possible |

## Ingestion plan for Kyle's review

When you wake up, the manifest is ready and queryable. Recommended
next sequence (each is one tick):

1. **SQL spot-check** the manifest (queries in `scripts/phase_a_post_smoke.sh`)
2. **Approve Tier 1 ingest order** — LAS → PDFs → XLSX → Cameco .log
3. **Pick one cluster for end-to-end demo** (recommend `028N079W36` for
   Cameco Shirley Basin density)
4. **Phase B kickoff** — wire the existing `ingest_pdf` Hatchet
   workflow + add `ingest_las` + `ingest_xlsx` siblings.

## Validation queries

```sql
-- Run summary
SELECT * FROM bronze.ingest_runs WHERE status = 'completed'
 ORDER BY started_at DESC LIMIT 1;

-- File type rollup
SELECT file_type, count(*), pg_size_pretty(sum(file_size_bytes)::bigint)
  FROM bronze.ingest_manifest
 WHERE run_id = '67929603-8b5b-484d-bbc2-807c4a0f8d7f'::uuid
 GROUP BY file_type ORDER BY count(*) DESC;

-- Top clusters
SELECT cluster_key, count(*), pg_size_pretty(sum(file_size_bytes)::bigint)
  FROM bronze.ingest_manifest
 WHERE run_id = '67929603-8b5b-484d-bbc2-807c4a0f8d7f'::uuid
 GROUP BY cluster_key ORDER BY count(*) DESC LIMIT 25;
```

## Caveats

- 33,751 TIFFs (99%) have `anomalies=["header_parse_error:..."]`.
  This is the 64KB head-slice limit; the TIFF IFD entries for many of
  these scans extend past 64KB. **Cosmetic for Phase A** (we still
  have file name, size, cluster). Phase B will read full files for
  actual decode.
- The Phase A walk did NOT extract any pixels. No OCR happened. The
  manifest is metadata-only.
- The staged copy at `georag-phase-a-stage` Docker volume is **204 GB**.
  Delete after Phase B completes to reclaim disk.
- 487 Cameco `.log` files are proprietary binary format with text
  headers. Without the Cameco specification, only the header
  metadata is reliably extractable; the binary tail (gamma counts
  per depth) requires reverse-engineering OR is duplicated in the
  paired `PROC.LAS` files (188 of them) which ARE standard.
