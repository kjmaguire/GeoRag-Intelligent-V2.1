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

When this runs -- and when it does not
    The thresholds above are Wyoming roll-front uranium numbers and the depth
    axis is assumed to be feet. Applied to a gold or copper hole they
    manufacture a lithology log that reads like geology. So, per project:

      * the project's commodity must be uranium (``silver.projects.commodity``
        is free text, ``commodity_arr`` a text[]; see ``is_uranium_commodity``)
        -- anything else, including NULL ("not stated"), is skipped whole;
      * a hole that already has LOGGED lithology (any silver.lithology_logs
        row whose code is not ``DERIVED-%``) is never derived over. Its stale
        derived rows, if an earlier run left any, are removed instead.

    Every delete stays scoped to derived rows (``DERIVED-%`` codes,
    ``derived_composite`` samples). The depth-unit assumption (feet) is
    unchanged, and is reported in the summary as ``depth_unit_assumed``.

Run via:
    python -m app.services.ingest.derive_intervals --project-id <uuid>
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import re
import sys
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import asyncpg

from app.db import bind_workspace_scope
from app.db.dsn import build_dsn

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

#: Reported in the summary so an operator can see what was assumed.
DEPTH_UNIT_ASSUMED = "ft"

_URANIUM_WORDS = frozenset({"uranium", "u3o8", "u308"})
#: A bare "u" only counts as a whole list item ("Au, U"), never as a word
#: inside prose ("U.S. porphyry").
_COMMODITY_SPLIT = re.compile(r"[,;/&|+]|\band\b|-")


def is_uranium_commodity(*values: str | Iterable[str] | None) -> bool:
    """Whether any commodity value names uranium.

    ``silver.projects.commodity`` is a free-text varchar(50) typed by the
    project's owner ("Uranium", "U3O8", "U", "Au, U", "Uranium (ISR)") and
    ``commodity_arr`` a text[] that may hold the same. NULL / empty means
    "not stated", which is NOT uranium: the answer is False.
    """
    items: list[str] = []
    for value in values:
        if value is None:
            continue
        if isinstance(value, str):
            items.append(value)
        else:
            items.extend(str(v) for v in value if v is not None)
    for item in items:
        for part in _COMMODITY_SPLIT.split(item.lower()):
            part = part.strip()
            if part == "u":
                return True
            if _URANIUM_WORDS & set(re.findall(r"[a-z0-9]+", part)):
                return True
    return False


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
                g for d, g in zip(curves.depths_m, curves.grade, strict=False)
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


async def _clear_derived(conn: asyncpg.Connection, collar_id: str) -> None:
    """Delete this collar's DERIVED rows and nothing else.

    The gold delete used to be scoped by interval_kind alone. Only this
    module writes gold.drillhole_intervals_visual today, but a scope of "every
    lithology row" is one future writer away from deleting logged geology, so
    it carries the same DERIVED-% guard as the silver one.
    """
    await conn.execute(
        "DELETE FROM silver.lithology_logs WHERE collar_id = $1::uuid AND lithology_code LIKE 'DERIVED-%'",
        collar_id,
    )
    await conn.execute(
        "DELETE FROM silver.samples WHERE collar_id = $1::uuid AND sample_type = 'derived_composite'",
        collar_id,
    )
    await conn.execute(
        "DELETE FROM gold.drillhole_intervals_visual "
        "WHERE collar_id = $1::uuid AND interval_kind = 'lithology' "
        "AND lithology_code LIKE 'DERIVED-%'",
        collar_id,
    )


async def _collars_with_logged_lithology(
    conn: asyncpg.Connection, project_id: str,
) -> set[str]:
    """Collar ids in the project that carry lithology somebody LOGGED.

    A row counts as logged unless its code is ``DERIVED-%`` (NULL code with a
    description is a logged row too). silver.lithology, the canonical table,
    is a per-project projection of silver.lithology_logs rebuilt by
    promote_silver_to_gold (its ids ARE the log_ids), so lithology_logs is
    the source of truth and the only table consulted; the projection can lag
    behind a re-derive and would misread stale derived rows as logged.
    """
    rows = await conn.fetch(
        """
        SELECT DISTINCT l.collar_id::text AS collar_id
          FROM silver.lithology_logs l
          JOIN silver.collars c ON c.collar_id = l.collar_id
         WHERE c.project_id = $1::uuid
           AND (l.lithology_code IS NULL OR l.lithology_code NOT LIKE 'DERIVED-%')
        """,
        project_id,
    )
    return {r["collar_id"] for r in rows}


async def _project_commodities(
    conn: asyncpg.Connection, project_id: str,
) -> tuple[str | None, list[str]]:
    """(commodity, commodity_arr) for the project.

    Read through to_jsonb so a database without the commodity_arr column
    (added 2026-05-20, PostgreSQL only) still answers on `commodity`.
    """
    raw = await conn.fetchval(
        "SELECT to_jsonb(p) FROM silver.projects p WHERE project_id = $1::uuid",
        project_id,
    )
    row: dict[str, Any] = json.loads(raw) if isinstance(raw, str) else (raw or {})
    arr = row.get("commodity_arr") or []
    return row.get("commodity"), [str(v) for v in arr]


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
    await _clear_derived(conn, collar_id)

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
        "ore_bands": sum(1 for _, _, l in intervals if l == "ORE"),  # noqa: E741
        "max_depth_m": round(max(pack.depths_m), 1) if pack.depths_m else 0,
    }


def _empty_summary(project_id: str) -> dict[str, Any]:
    return {
        "project_id": project_id,
        "skipped": False,
        "skipped_reason": None,
        "commodity": None,
        "depth_unit_assumed": DEPTH_UNIT_ASSUMED,
        "collars_total": 0,
        "collars_emitted": 0,
        "collars_skipped": 0,
        "collars_skipped_logged_lithology": 0,
        "collars_skipped_no_gamma": 0,
        "intervals_total": 0,
        "samples_total": 0,
        "ore_bands_total": 0,
    }


async def derive_project(project_id: str) -> dict:
    """Derive strip logs for one project -- uranium projects only.

    Returns a summary dict. ``skipped=True`` with ``skipped_reason=
    'commodity_not_uranium'`` means nothing at all was touched. Holes with
    logged lithology are counted in ``collars_skipped_logged_lithology``.
    """
    conn = await asyncpg.connect(
        build_dsn(),
        statement_cache_size=0,
    )
    try:
        workspace_id = await conn.fetchval(
            "SELECT workspace_id::text FROM silver.projects WHERE project_id = $1::uuid",
            project_id,
        )
        if not workspace_id:
            raise RuntimeError(f"project_id {project_id} not found")

        # is_local=False, and called ONCE. It was called twice,
        # transaction-scoped, on a dedicated asyncpg.connect() with no
        # transaction -- while the very next line binds app.project_id
        # SESSION-scoped on the same connection. Session scope was already
        # understood to be right here; workspace_id never got the same
        # treatment, so it evaporated.
        await bind_workspace_scope(
            conn, workspace_id=workspace_id, site="ingest.derive_intervals",
            is_local=False,
        )
        await conn.execute("SELECT set_config('app.project_id', $1, false)", project_id)

        commodity, commodity_arr = await _project_commodities(conn, project_id)
        if not is_uranium_commodity(commodity, commodity_arr):
            # Not uranium (or not stated): the thresholds below mean nothing
            # here. Logged once, reported once by the caller -- never per hole.
            summary = _empty_summary(project_id)
            summary.update({
                "skipped": True,
                "skipped_reason": "commodity_not_uranium",
                "commodity": commodity or (", ".join(commodity_arr) or None),
            })
            log.info(
                "derive.project skipped project_id=%s reason=commodity_not_uranium "
                "commodity=%r", project_id, summary["commodity"],
            )
            return summary

        collars = await conn.fetch(
            "SELECT collar_id::text AS collar_id, hole_id FROM silver.collars WHERE project_id = $1::uuid ORDER BY hole_id",
            project_id,
        )
        logged = await _collars_with_logged_lithology(conn, project_id)
        log.info(
            "derive.project start project_id=%s collars=%d with_logged_lithology=%d",
            project_id, len(collars), len(logged),
        )

        out: list[dict] = []
        for i, c in enumerate(collars):
            try:
                if c["collar_id"] in logged:
                    # A geologist logged this hole; derived strips must not
                    # sit beside (or over) it. Clear any an earlier run left.
                    await _clear_derived(conn, c["collar_id"])
                    out.append({
                        "hole_id": c["hole_id"], "skipped": True,
                        "reason": "has_logged_lithology",
                    })
                    continue
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

        summary = _empty_summary(project_id)
        summary.update({
            "commodity": commodity or (", ".join(commodity_arr) or None),
            "collars_total": len(collars),
            "collars_emitted": sum(1 for r in out if not r.get("skipped")),
            "collars_skipped": sum(1 for r in out if r.get("skipped")),
            "collars_skipped_logged_lithology": sum(
                1 for r in out if r.get("reason") == "has_logged_lithology"
            ),
            "collars_skipped_no_gamma": sum(
                1 for r in out if r.get("reason") == "no_gamma_curve"
            ),
            "intervals_total": sum(r.get("intervals", 0) for r in out),
            "samples_total": sum(r.get("samples", 0) for r in out),
            "ore_bands_total": sum(r.get("ore_bands", 0) for r in out),
        })
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
