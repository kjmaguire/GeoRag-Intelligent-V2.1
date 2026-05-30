"""Derive lithology / samples / interval visualisations from existing LAS curves.

Phase 2 of the Wyoming ingestion catch-up (2026-05-17). The Cameco Shirley
Basin LAS files give us GAMMA / GRADE / RES / SP / SANG / AZIMUTH curves
at 0.1 ft resolution per collar, but `silver.lithology_logs`,
`silver.samples`, and `gold.drillhole_intervals_visual` are empty —
nothing in the pipeline has produced them yet. The 3D / SECTION / STRIP
visualisations therefore render empty.

This module derives those rows from existing curve data using simple
threshold-based classification appropriate for sandstone-hosted
roll-front uranium (Wyoming Wind River / Wagon Bed Fm). Each derived
row carries an explicit `parser_used = 'derived-from-las-curves-v1'` +
`extraction_confidence = 0.55` provenance entry so it can be told
apart from operator-logged geology.

Classification rules (Wyoming roll-front, fine for Cameco Shirley):
    GRADE > 0.02 %eU3O8 AND GAMMA > 150 cps  → ORE   (mineralised sst)
    GAMMA > 80 cps AND RES < 30 Ω·m          → SHALE (mudstone / siltstone)
    GAMMA < 60 cps AND RES > 40 Ω·m          → SST   (clean sandstone)
    surface 0..7 m                           → SURF  (alluvium/overburden)
    otherwise                                → MIX   (transitional)

Run via:
    python -m app.services.ingest.derive_intervals --project-id <uuid>
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import os
import sys
import uuid
from dataclasses import dataclass

import asyncpg

log = logging.getLogger("georag.ingest.derive")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

# Lithology classification — keep terse strings to fit varchar(20) /
# varchar(40) constraints across silver/gold schemas.
LITHO_LABEL = {
    "ORE": "Mineralised sandstone (roll-front)",
    "SST": "Clean sandstone",
    "SHALE": "Mudstone / siltstone",
    "MIX": "Transitional / mixed lithology",
    "SURF": "Alluvium / overburden",
}
LITHO_COLOR = {
    "ORE": "#d4a017",     # mustard — flags ore zones strongly
    "SST": "#e8d59c",     # pale sand
    "SHALE": "#6b6360",   # mud-grey
    "MIX": "#b9a07a",     # blend
    "SURF": "#c9b78f",    # surficial tan
}

FT_TO_M = 0.3048
MIN_INTERVAL_M = 0.5          # collapse depth bands shorter than this
SAMPLE_COMPOSITE_M = 1.5      # ~5 ft composite for sample rows


def _parse_pg_double_array(raw: str | list[float] | None) -> list[float]:
    """Parse a Postgres `double precision[]` text literal `{1.2,3.4}` to
    a Python list. asyncpg with NumericCodec returns lists natively in
    some configurations, but the safe path is to handle both."""
    if raw is None:
        return []
    if isinstance(raw, list):
        return [float(v) for v in raw]
    s = str(raw).strip()
    if not s or s == "{}":
        return []
    if s.startswith("{") and s.endswith("}"):
        s = s[1:-1]
    return [float(v) for v in s.split(",") if v]


@dataclass
class CurvePack:
    depths_m: list[float]
    gamma: list[float] | None
    grade: list[float] | None
    res: list[float] | None
    sp: list[float] | None
    null_value: float


async def _fetch_curve_pack(conn: asyncpg.Connection, collar_id: str) -> CurvePack | None:
    """Pull GAMMA / GRADE / RES / SP for a single collar and align them on
    the GAMMA depth axis (always present in Cameco LAS exports). Returns
    None if no GAMMA curve exists."""
    rows = await conn.fetch(
        """
        SELECT curve_name, depths, "values", null_value
          FROM silver.well_log_curves
         WHERE collar_id = $1::uuid
           AND curve_name IN ('GAMMA','GRADE','RES','SP')
        """,
        collar_id,
    )
    by_name: dict[str, asyncpg.Record] = {r["curve_name"]: r for r in rows}
    if "GAMMA" not in by_name:
        return None
    g = by_name["GAMMA"]
    depths_ft = _parse_pg_double_array(g["depths"])
    if not depths_ft:
        return None
    null_v = float(g["null_value"])
    depths_m = [d * FT_TO_M for d in depths_ft]

    def _vals(name: str) -> list[float] | None:
        if name not in by_name:
            return None
        return _parse_pg_double_array(by_name[name]["values"])

    return CurvePack(
        depths_m=depths_m,
        gamma=_vals("GAMMA"),
        grade=_vals("GRADE"),
        res=_vals("RES"),
        sp=_vals("SP"),
        null_value=null_v,
    )


def _classify(depth_m: float, gamma: float | None, grade: float | None, res: float | None, null_v: float) -> str:
    """Apply threshold rules. Returns one of {ORE, SST, SHALE, MIX, SURF}."""
    if depth_m < 7.0:
        return "SURF"

    def _good(v: float | None) -> bool:
        return v is not None and abs(v - null_v) > 1e-6

    g = gamma if _good(gamma) else None
    gr = grade if _good(grade) else None
    r = res if _good(res) else None

    if gr is not None and g is not None and gr > 0.02 and g > 150:
        return "ORE"
    if g is not None and r is not None and g > 80 and r < 30:
        return "SHALE"
    if g is not None and r is not None and g < 60 and r > 40:
        return "SST"
    if g is not None and g > 200:
        return "ORE"
    return "MIX"


def _collapse_to_intervals(depths_m: list[float], labels: list[str]) -> list[tuple[float, float, str]]:
    """Walk depths + labels, return contiguous (from_m, to_m, label) bands.
    Bands shorter than MIN_INTERVAL_M are merged into their predecessor."""
    intervals: list[tuple[float, float, str]] = []
    if not depths_m or not labels:
        return intervals
    cur_label = labels[0]
    cur_start = depths_m[0]
    for i in range(1, len(depths_m)):
        if labels[i] != cur_label:
            intervals.append((cur_start, depths_m[i], cur_label))
            cur_label = labels[i]
            cur_start = depths_m[i]
    intervals.append((cur_start, depths_m[-1], cur_label))

    # Merge short bands forward into the previous band.
    merged: list[tuple[float, float, str]] = []
    for from_m, to_m, lab in intervals:
        if merged and (to_m - from_m) < MIN_INTERVAL_M:
            prev_from, _prev_to, prev_lab = merged[-1]
            merged[-1] = (prev_from, to_m, prev_lab)
        else:
            merged.append((from_m, to_m, lab))
    return merged


def _build_samples(curves: CurvePack, intervals: list[tuple[float, float, str]]) -> list[dict]:
    """Composite GRADE over each ORE interval into ~1.5 m sample rows."""
    if not curves.grade:
        return []
    samples: list[dict] = []
    for from_m, to_m, lab in intervals:
        if lab != "ORE":
            continue
        # Composite walker: emit a sample per SAMPLE_COMPOSITE_M depth slab.
        cur_from = from_m
        while cur_from < to_m:
            cur_to = min(cur_from + SAMPLE_COMPOSITE_M, to_m)
            grade_vals = [
                g for d, g in zip(curves.depths_m, curves.grade)
                if cur_from <= d < cur_to and abs(g - curves.null_value) > 1e-6
            ]
            if grade_vals:
                avg_grade = sum(grade_vals) / len(grade_vals)
                samples.append({
                    "from_depth": round(cur_from, 3),
                    "to_depth": round(cur_to, 3),
                    "u3o8_pct_e": round(avg_grade, 5),
                    "n_points": len(grade_vals),
                })
            cur_from = cur_to
    return samples


async def _emit_for_collar(
    conn: asyncpg.Connection,
    *,
    workspace_id: str,
    project_id: str,
    collar_id: str,
    hole_id: str,
) -> dict:
    """Derive + insert lithology_logs + samples + gold visual rows for one collar."""
    pack = await _fetch_curve_pack(conn, collar_id)
    if pack is None:
        return {"hole_id": hole_id, "skipped": True, "reason": "no_gamma_curve"}

    # Classify per-depth-point then collapse to intervals.
    labels: list[str] = []
    for i, d in enumerate(pack.depths_m):
        g = pack.gamma[i] if pack.gamma and i < len(pack.gamma) else None
        gr = pack.grade[i] if pack.grade and i < len(pack.grade) else None
        r = pack.res[i] if pack.res and i < len(pack.res) else None
        labels.append(_classify(d, g, gr, r, pack.null_value))
    intervals = _collapse_to_intervals(pack.depths_m, labels)

    # Wipe prior derived rows for this collar so the script is re-runnable.
    await conn.execute(
        "DELETE FROM silver.lithology_logs WHERE collar_id = $1::uuid AND lithology_code LIKE 'DERIVED-%'",
        collar_id,
    )
    await conn.execute(
        "DELETE FROM silver.samples WHERE collar_id = $1::uuid AND sample_type = 'derived_composite'",
        collar_id,
    )
    await conn.execute(
        "DELETE FROM gold.drillhole_intervals_visual WHERE collar_id = $1::uuid AND interval_kind = 'lithology'",
        collar_id,
    )

    litho_inserted = 0
    visual_inserted = 0
    for from_m, to_m, lab in intervals:
        if to_m - from_m < 0.05:  # guard against degenerate
            continue
        # silver.lithology_logs
        await conn.execute(
            """
            INSERT INTO silver.lithology_logs
                (log_id, collar_id, from_depth, to_depth,
                 lithology_code, lithology_description,
                 workspace_id, created_at, updated_at)
            VALUES (gen_random_uuid(), $1::uuid, $2, $3, $4, $5, $6::uuid, NOW(), NOW())
            """,
            collar_id, from_m, to_m, f"DERIVED-{lab}", LITHO_LABEL[lab], workspace_id,
        )
        litho_inserted += 1

        # gold.drillhole_intervals_visual
        await conn.execute(
            """
            INSERT INTO gold.drillhole_intervals_visual
                (visual_id, collar_id, workspace_id, project_id,
                 depth_from, depth_to, interval_kind,
                 lithology_code, lithology_label, color_hint,
                 visual_y_start, visual_y_end)
            VALUES (gen_random_uuid(), $1::uuid, $2::uuid, $3::uuid,
                    $4, $5, 'lithology', $6, $7, $8, $4, $5)
            """,
            collar_id, workspace_id, project_id, from_m, to_m,
            f"DERIVED-{lab}", LITHO_LABEL[lab], LITHO_COLOR[lab],
        )
        visual_inserted += 1

    # silver.samples — composite GRADE over the ORE bands
    sample_rows = _build_samples(pack, intervals)
    samples_inserted = 0
    for s in sample_rows:
        await conn.execute(
            """
            INSERT INTO silver.samples
                (sample_id, collar_id, from_depth, to_depth, sample_type,
                 commodity_assays, commodity_assay_flags,
                 workspace_id, created_at, updated_at)
            VALUES (gen_random_uuid(), $1::uuid, $2, $3, 'derived_composite',
                    $4::jsonb, $5::jsonb, $6::uuid, NOW(), NOW())
            """,
            collar_id, s["from_depth"], s["to_depth"],
            f'{{"U3O8_pct_e": {s["u3o8_pct_e"]}, "method": "gamma_log_grade", "confidence": 0.55, "n_points": {s["n_points"]}}}',
            '{"U3O8_pct_e": "derived"}',
            workspace_id,
        )
        samples_inserted += 1

    # Provenance — one row per derived target table per collar
    src_token = f"derived://{hole_id}@las-curves".encode()
    sha = hashlib.sha256(src_token).hexdigest()
    for target_table in ("lithology_logs", "samples", "drillhole_intervals_visual"):
        target_schema = "gold" if target_table == "drillhole_intervals_visual" else "silver"
        await conn.execute(
            """
            INSERT INTO bronze.provenance
                (provenance_id, target_schema, target_table, target_id,
                 source_file, source_file_sha256,
                 parser_name, parser_version, ingested_at)
            VALUES (gen_random_uuid(), $1, $2, $3::uuid, $4, $5,
                    'derived-from-las-curves', '1.0', NOW())
            """,
            target_schema, target_table, collar_id,
            f"derived://collar/{hole_id}", sha,
        )

    return {
        "hole_id": hole_id,
        "intervals": litho_inserted,
        "samples": samples_inserted,
        "ore_bands": sum(1 for _, _, l in intervals if l == "ORE"),
        "max_depth_m": round(max(pack.depths_m), 1) if pack.depths_m else 0,
    }


async def derive_project(project_id: str) -> dict:
    user = os.environ["POSTGRES_USER"]
    password = os.environ["POSTGRES_PASSWORD"]
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    conn = await asyncpg.connect(
        f"postgres://{user}:{password}@{host}:{port}/{db}",
        statement_cache_size=0,
    )
    try:
        workspace_id = await conn.fetchval(
            "SELECT workspace_id::text FROM silver.projects WHERE project_id = $1::uuid",
            project_id,
        )
        if not workspace_id:
            raise RuntimeError(f"project_id {project_id} not found")

        await conn.execute("SELECT set_config('app.workspace_id', $1, false)", workspace_id)
        await conn.execute("SELECT set_config('app.workspace_id', $1, false)", workspace_id)
        await conn.execute("SELECT set_config('app.project_id', $1, false)", project_id)

        collars = await conn.fetch(
            "SELECT collar_id::text AS collar_id, hole_id FROM silver.collars WHERE project_id = $1::uuid ORDER BY hole_id",
            project_id,
        )
        log.info("derive.project start project_id=%s collars=%d", project_id, len(collars))

        out: list[dict] = []
        for i, c in enumerate(collars):
            try:
                r = await _emit_for_collar(
                    conn,
                    workspace_id=workspace_id,
                    project_id=project_id,
                    collar_id=c["collar_id"],
                    hole_id=c["hole_id"],
                )
                out.append(r)
                if (i + 1) % 10 == 0:
                    log.info("derive.progress %d/%d", i + 1, len(collars))
            except Exception as e:
                log.warning("derive.collar_failed hole=%s err=%s", c["hole_id"], e)
                out.append({"hole_id": c["hole_id"], "skipped": True, "reason": str(e)[:120]})

        summary = {
            "project_id": project_id,
            "collars_total": len(collars),
            "collars_emitted": sum(1 for r in out if not r.get("skipped")),
            "collars_skipped": sum(1 for r in out if r.get("skipped")),
            "intervals_total": sum(r.get("intervals", 0) for r in out),
            "samples_total": sum(r.get("samples", 0) for r in out),
            "ore_bands_total": sum(r.get("ore_bands", 0) for r in out),
        }
        log.info("derive.project done %s", summary)
        return summary
    finally:
        await conn.close()


def _cli() -> int:
    p = argparse.ArgumentParser(description="Derive lithology/samples/intervals from LAS curves")
    p.add_argument("--project-id", required=True, help="silver.projects.project_id UUID")
    args = p.parse_args()
    # Validate UUID early.
    try:
        uuid.UUID(args.project_id)
    except ValueError:
        print(f"error: invalid project_id UUID: {args.project_id}", file=sys.stderr)
        return 2
    summary = asyncio.run(derive_project(args.project_id))
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
