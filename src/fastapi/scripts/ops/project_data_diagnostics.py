"""Why is this project's lithology / structure / logs / 3D missing? — a READ-ONLY diagnostics run.

Why this exists
---------------
Production RDS is in private subnets, ECS Exec is off (deploy/aws/terraform/rotation.tf)
and there is no bastion, so nobody can open a SQL prompt against it. This script is the
SQL prompt: it runs INSIDE AWS as a one-off ECS task started by
.github/workflows/project-diagnostics.yml (same shape as parse_vs_native.py /
parse-comparison.yml), prints a Markdown report plus JSON to stdout, and the workflow
copies both into the GitHub job summary.

What it reports (each check is independent — a missing table or column is reported for
that check and never stops the others)
-------------------------------------------------------------------------------------
0. project           the silver.projects row (ids, slug, name, commodity, CRS)
1. ingest_progress   the last 300 silver.ingest_progress rows for the project, the warning
                     CODES on them (never the warning text), rollups by status and by code
2. row_counts        rows per table, scoped to the project
3. attribute_tables  silver.attribute_tables per source file / layer: row count and the COLUMN
                     NAMES (keys of one sample row) — never values
4. collars           count, NULL geometry, georef_method, duplicate hole ids, Wyoming fallback box
5. coverage          holes with lithology but no surveys, with curves, with no trace, trace_quality
6. curves            silver.well_log_curves by curve_name
7. derived           DERIVED-% lithology (derive_intervals.py) vs logged, silver and gold
8. archive_runs      silver.archive_ingest_runs
9. documents         silver.reports per project with its silver.document_passages rolled up:
                     passages per report, text vs page-image, embedded vs not, which OCR
                     engine produced them (ocr_method), low-confidence OCR, and the legacy
                     silver.ingest_ocr_results count — i.e. "does every scanned file have
                     rows, and are they all in the vector index?"
10. placement        "Where the data lands": collar placement inputs (NULL / degree-looking
                     easting+northing, NULL elevation, no orientation and no surveys), easting/northing
                     vs geom_4326 re-projected into the project CRS, the lng/lat extent of every map
                     layer and its distance from the collars, drill-trace start vs collar, and survey
                     quality (NULL/out-of-range angles, up-holes, station counts, mixed source files)
11. visibility       "Will the UI show it": the Workspace caps (1000 collars, 200 holes / 80 bands in 3D,
                     5000 structures and samples), gold interval bands vs silver logs, the lithology
                     rows promotion drops, structures, samples, the gold tables with no writer, and
                     the holes the LOGS / SECTION picker cannot list

``--all-projects`` runs the same checks for EVERY silver.projects row and prefixes the
report with a one-table corpus overview; ``--only documents,row_counts`` limits the
checks (keys above) so a corpus-wide run stays readable.

Safety
------
* READ-ONLY. The session is ``default_transaction_read_only = on``; every statement is a
  SELECT. No object storage, Qdrant or Cohere client exists in this file.
* No customer free text. The report carries counts, file names, hole ids, column names,
  curve names and warning codes — plus ``error_text`` truncated to 300 characters, which
  Kyle asked for. Row VALUES are never selected: attribute_tables reports the keys of the
  JSONB, not what is in them; warnings are reduced to their ``code``.

Row-level security
------------------
silver.projects and silver.workspaces are bootstrap tables (readable with no
``app.workspace_id`` — see the 2026-08-21 fail-closed RLS migration), but the tables
below are RLS-protected. Like parse_vs_native.select_reports: the project is looked up by
slug unscoped first; if that shows nothing the script walks silver.workspaces, binding the
GUC to each. Once the project is found, the session is bound to ITS workspace and every
query is also filtered on the project (directly, or through silver.collars for the
collar-keyed tables), so a row from another tenant cannot be counted even if a policy is
permissive.

Exit codes: 0 report produced; 2 project not found / not visible; 1 unexpected error.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# /app/scripts/ops/x.py is run as `python3 /app/scripts/ops/x.py`, which puts the
# SCRIPT'S directory (not /app) on sys.path — so `import app` needs this.
_APP_ROOT = Path(__file__).resolve().parents[2]
if str(_APP_ROOT) not in sys.path:
    sys.path.insert(0, str(_APP_ROOT))

logger = logging.getLogger("project_data_diagnostics")

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

BEGIN_SUMMARY = "=====BEGIN PROJECT_DIAGNOSTICS_SUMMARY_MD====="
END_SUMMARY = "=====END PROJECT_DIAGNOSTICS_SUMMARY_MD====="
BEGIN_JSON = "=====BEGIN PROJECT_DIAGNOSTICS_JSON====="
END_JSON = "=====END PROJECT_DIAGNOSTICS_JSON====="

_SLUG_RE = re.compile(r"^[a-z0-9-]{1,64}$")

PROGRESS_ROWS_LIMIT = 300
ERROR_TEXT_MAX = 300
ID_LIST_LIMIT = 50
ATTRIBUTE_GROUP_LIMIT = 200
CURVE_NAME_LIMIT = 200
ARCHIVE_RUN_LIMIT = 50
#: Reports shown per project in check 9 (the rollups cover every report regardless).
DOCUMENT_LIMIT = 200
DUPLICATE_LIMIT = 50

#: Wyoming fallback box the collar writers fall back to when no CRS could be found
#: (lng -111..-104, lat 41..45). A project that is not in Wyoming and has collars here
#: was georeferenced by the fallback, not by its data.
WYOMING_BOX = (-111.0, 41.0, -104.0, 45.0)

#: Warning codes the diagnostics call out by name, whether or not any run produced them.
WATCH_CODES = (
    "orphaned_intervals",
    "rows_rejected",
    "optional_values_blanked",
    "nothing_classified",
    "archive_member_unhandled",
    "archive_member_skipped",
    "access_no_tables",
    "dbf_no_rows",
)
#: ...and every code with one of these prefixes (the LAS ingester's codes).
WATCH_PREFIXES = ("las_",)

#: Optional descriptive columns of silver.projects. Only the ones that exist are selected
#: (checked against information_schema), so a schema that drifts does not sink the run.
#: All verified against database/migrations: project_name / commodity / crs_datum
#: (2026_04_09_180000), slug / status (2026_04_13_300000), crs_epsg (2026_04_20_120000),
#: commodity_arr (2026_05_20_060100), lifecycle_state (2026_05_30_000001).
PROJECT_OPTIONAL_COLUMNS = (
    "project_name",
    "commodity",
    "commodity_arr",
    "crs_epsg",
    "crs_datum",
    "status",
    "lifecycle_state",
)

#: (table, keyed-by). ``project``: the table has its own project_id. ``collar``: it is keyed
#: by collar_id only (silver.surveys, lithology_logs, lithology, structure, samples,
#: well_log_curves) and is scoped through silver.collars.project_id.
ROW_COUNT_TABLES: tuple[tuple[str, str], ...] = (
    ("silver.collars", "project"),
    ("silver.surveys", "collar"),
    ("silver.lithology_logs", "collar"),
    ("silver.lithology", "collar"),
    ("silver.structure", "collar"),
    ("silver.well_log_curves", "collar"),
    ("silver.samples", "collar"),
    ("silver.drill_traces", "project"),
    ("gold.drillhole_intervals_visual", "project"),
    ("gold.structure_measurements_visual", "project"),
    ("silver.attribute_tables", "project"),
    ("silver.spatial_features", "project"),
)


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------

_CODE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_DSN_RE = re.compile(r"://[^/\s:@]+:[^@\s]+@")
_WS_RE = re.compile(r"\s+")


def clean_code(value: Any) -> str:
    """A warning code, or a placeholder. Codes are identifiers; anything else in that
    slot might be free text and is not printed."""
    if value is None or value == "":
        return "(no code)"
    text = str(value)
    return text if _CODE_RE.fullmatch(text) else "(non-code text)"


def warning_codes(raw: Any) -> list[str]:
    """The ``code`` of each entry of an ingest_progress.warnings JSONB value.

    asyncpg hands jsonb back as a string; tests pass the decoded value. The ``detail``
    text of a warning is never read into the report."""
    if raw is None:
        return []
    if isinstance(raw, bytes | str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return ["(unparseable warnings)"]
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    return [clean_code(item.get("code") if isinstance(item, dict) else None) for item in raw]


def scrub_error(text: Any, limit: int = ERROR_TEXT_MAX) -> str | None:
    """error_text, whitespace-collapsed, connection-string credentials removed, cut to ``limit``."""
    if text is None:
        return None
    out = _DSN_RE.sub("://[REDACTED]@", _WS_RE.sub(" ", str(text)).strip())
    return out if len(out) <= limit else out[:limit] + "...[truncated]"


def is_watched(code: str) -> bool:
    return code in WATCH_CODES or code.startswith(WATCH_PREFIXES)


def _md_cell(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")
    return text


def _ts(value: Any) -> str:
    if value is None:
        return ""
    return str(value)[:19]


def md_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join(_md_cell(c) for c in row) + " |" for row in rows]
    return lines


def _ids(values: Sequence[Any]) -> str:
    return ", ".join(f"`{v}`" for v in values) if values else "-"


# --------------------------------------------------------------------------
# Connection + project lookup (RLS handling as in parse_vs_native.select_reports)
# --------------------------------------------------------------------------


@dataclass
class Project:
    project_id: str
    workspace_id: str | None
    slug: str


#: The exact slug, or the slug the Projects page mints from the NAME: Project::makeSlug
#: appends ``-`` plus 8 random ``[a-z0-9]`` (LAR-14), so the project called "Red Star"
#: is ``red-star-k3j9x0qa`` and ``--project-slug=red-star`` finds it. $1 is validated
#: against ``^[a-z0-9-]+$`` before it gets here, so it carries no regex metacharacters.
_PROJECT_KEY_SQL = """
SELECT p.project_id::text AS project_id, p.workspace_id::text AS workspace_id, p.slug
  FROM silver.projects p
 WHERE p.slug = $1 OR p.slug ~ ('^' || $1 || '-[a-z0-9]{8}$')
 ORDER BY (p.slug = $1) DESC, p.slug
 LIMIT 10
"""


def _pick(rows: list[Any], slug: str) -> tuple[Any | None, list[str]]:
    """``(row, ambiguous_slugs)``: the exact match, else the one suffixed match; two or
    more suffixed matches (and no exact one) pick nothing and name them all."""
    if not rows:
        return None, []
    if rows[0]["slug"] == slug or len(rows) == 1:
        return rows[0], []
    return None, [r["slug"] for r in rows]


async def open_readonly_connection() -> Any:
    """A direct, session-read-only asyncpg connection built like every other Hatchet
    workflow's (app.db.dsn.build_dsn)."""
    import asyncpg  # noqa: PLC0415

    from app.db.dsn import build_dsn  # noqa: PLC0415

    conn = await asyncpg.connect(
        build_dsn(scheme="postgresql", include_sslmode=True), timeout=30, statement_cache_size=0
    )
    # Belt and braces for "read-only": the database refuses any write this session
    # attempts, whatever the code does.
    await conn.execute("SET default_transaction_read_only = on")
    await conn.execute("SET statement_timeout = '120s'")
    return conn


async def find_project(conn: Any, slug: str) -> tuple[Project | None, list[str], list[str]]:
    """Find the project by slug (or by the name part of one), then leave the session
    bound to its workspace.

    Returns ``(project, scopes_tried, ambiguous_slugs)``. Unscoped first (silver.projects
    is a bootstrap table); else walk silver.workspaces binding ``app.workspace_id`` to
    each. The first scope that sees any candidate decides; if it sees several suffixed
    slugs and no exact one, nothing is picked and they are all returned."""
    from app.db import bind_workspace_scope  # noqa: PLC0415

    tried = ["unscoped"]
    await conn.execute("SELECT set_config('app.workspace_id', '', false)")
    rows = list(await conn.fetch(_PROJECT_KEY_SQL, slug))
    if not rows:
        workspaces = [
            r["workspace_id"]
            for r in await conn.fetch(
                "SELECT workspace_id::text AS workspace_id FROM silver.workspaces ORDER BY workspace_id"
            )
        ]
        for ws in workspaces:
            await bind_workspace_scope(conn, workspace_id=ws, site="project_data_diagnostics", is_local=False)
            tried.append(f"workspace {ws}")
            rows = list(await conn.fetch(_PROJECT_KEY_SQL, slug))
            if rows:
                break
    row, ambiguous = _pick(rows, slug)
    if row is None:
        await conn.execute("SELECT set_config('app.workspace_id', '', false)")
        return None, tried, ambiguous

    project = Project(project_id=row["project_id"], workspace_id=row["workspace_id"], slug=row["slug"])
    if project.workspace_id:
        # Every check from here on runs inside the project's own workspace.
        await bind_workspace_scope(
            conn, workspace_id=project.workspace_id, site="project_data_diagnostics", is_local=False
        )
    return project, tried, []


async def project_details(conn: Any, project: Project) -> dict[str, Any]:
    """Check 0 body: whichever descriptive columns silver.projects actually has."""
    present = {
        r["column_name"]
        for r in await conn.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'silver' AND table_name = 'projects' AND column_name = ANY($1::text[])",
            list(PROJECT_OPTIONAL_COLUMNS),
        )
    }
    wanted = [c for c in PROJECT_OPTIONAL_COLUMNS if c in present]
    details: dict[str, Any] = {
        "project_id": project.project_id,
        "workspace_id": project.workspace_id,
        "slug": project.slug,
        "columns_missing": [c for c in PROJECT_OPTIONAL_COLUMNS if c not in present],
    }
    if wanted:
        cols = ", ".join(f'p."{c}"' for c in wanted)  # constants above, not input
        row = await conn.fetchrow(
            f"SELECT {cols} FROM silver.projects p WHERE p.project_id = $1::uuid", project.project_id
        )  # noqa: S608
        if row is not None:
            for c in wanted:
                details[c] = row[c]
    return details


# --------------------------------------------------------------------------
# Check runner: every check is independent and fails soft
# --------------------------------------------------------------------------


async def table_exists(conn: Any, qualified: str) -> bool:
    return bool(await conn.fetchval("SELECT to_regclass($1::text) IS NOT NULL", qualified))


def _error_text(exc: BaseException) -> str:
    return scrub_error(f"{type(exc).__name__}: {exc}", 300) or type(exc).__name__


async def guarded(
    conn: Any, requires: Sequence[str], fn: Callable[[], Awaitable[dict[str, Any]]], *, what: str
) -> dict[str, Any]:
    """Run ``fn`` if every table in ``requires`` exists.

    Returns ``{"status": "ok", **data}``, ``{"status": "table_absent", "missing_tables": [...]}``
    or ``{"status": "error", "error": "..."}``. Never raises: a broken check is a finding
    (and is logged to stderr), not a reason to lose the rest of the report."""
    try:
        missing = [t for t in requires if not await table_exists(conn, t)]
        if missing:
            return {"status": "table_absent", "missing_tables": missing}
        return {"status": "ok", **await fn()}
    except Exception as exc:  # noqa: BLE001 — recorded in the report; the other checks still run
        logger.warning("check %s failed: %s", what, _error_text(exc))
        return {"status": "error", "error": _error_text(exc)}


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------

_PROGRESS_ROWS_SQL = f"""
SELECT ip.filename, ip.status, ip.current_step, ip.rows_written, ip.attempt_number,
       ip.started_at, ip.completed_at, ip.failed_at, ip.error_text, ip.warnings
  FROM silver.ingest_progress ip
 WHERE ip.project_id = $1::uuid
 ORDER BY ip.started_at DESC
 LIMIT {PROGRESS_ROWS_LIMIT}
"""

_PROGRESS_STATUS_SQL = """
SELECT ip.status, count(*) AS n
  FROM silver.ingest_progress ip
 WHERE ip.project_id = $1::uuid
 GROUP BY ip.status
 ORDER BY n DESC, ip.status
"""

# Rollup over the WHOLE project, not just the last 300 rows. A non-object array element
# (a bare string) has no 'code' and lands under "(no code)".
_PROGRESS_CODES_SQL = """
SELECT w.elem ->> 'code' AS code, count(*) AS n
  FROM silver.ingest_progress ip
 CROSS JOIN LATERAL jsonb_array_elements(
        CASE WHEN jsonb_typeof(ip.warnings) = 'array' THEN ip.warnings ELSE '[]'::jsonb END
       ) AS w(elem)
 WHERE ip.project_id = $1::uuid
 GROUP BY 1
 ORDER BY n DESC, 1
"""


async def check_ingest_progress(conn: Any, project: Project) -> dict[str, Any]:
    rows = await conn.fetch(_PROGRESS_ROWS_SQL, project.project_id)
    runs: list[dict[str, Any]] = []
    for r in rows:
        runs.append(
            {
                "filename": r["filename"],
                "status": r["status"],
                "current_step": r["current_step"],
                "rows_written": r["rows_written"],
                "attempt_number": r["attempt_number"],
                "started_at": _ts(r["started_at"]),
                "completed_at": _ts(r["completed_at"]),
                "failed_at": _ts(r["failed_at"]),
                "error_text": scrub_error(r["error_text"]),
                "warning_codes": warning_codes(r["warnings"]),
            }
        )
    by_status = {r["status"]: int(r["n"]) for r in await conn.fetch(_PROGRESS_STATUS_SQL, project.project_id)}
    by_code: Counter[str] = Counter()
    for r in await conn.fetch(_PROGRESS_CODES_SQL, project.project_id):
        by_code[clean_code(r["code"])] += int(r["n"])
    watched = {c: n for c, n in sorted(by_code.items()) if is_watched(c)}
    return {
        "rows_shown": len(runs),
        "rows_limit": PROGRESS_ROWS_LIMIT,
        "runs": runs,
        "by_status": by_status,
        "runs_total": sum(by_status.values()),
        "by_warning_code": dict(by_code.most_common()),
        "watched_codes_seen": watched,
        "watched_codes_not_seen": [c for c in WATCH_CODES if c not in by_code],
        "runs_with_zero_rows_written": sum(1 for r in runs if r["rows_written"] == 0),
    }


def _count_sql(table: str, keyed_by: str) -> str:
    if keyed_by == "project":
        return f"SELECT count(*) FROM {table} t WHERE t.project_id = $1::uuid"  # noqa: S608 — constants
    return (  # noqa: S608 — constants
        f"SELECT count(*) FROM {table} t JOIN silver.collars c ON c.collar_id = t.collar_id WHERE c.project_id = $1::uuid"
    )


async def check_row_counts(conn: Any, project: Project) -> dict[str, Any]:
    tables: dict[str, Any] = {}
    for table, keyed_by in ROW_COUNT_TABLES:
        requires = [table] if keyed_by == "project" else [table, "silver.collars"]

        async def _count(table: str = table, keyed_by: str = keyed_by) -> dict[str, Any]:
            return {"rows": int(await conn.fetchval(_count_sql(table, keyed_by), project.project_id))}

        tables[table] = await guarded(conn, requires, _count, what=f"row_counts:{table}")
    return {"tables": tables}


_ATTR_GROUPS_SQL = f"""
SELECT a.source_file, a.source_file_sha256, a.source_layer,
       count(*) AS row_count, min(a.row_index) AS first_row_index,
       count(*) OVER () AS total_groups
  FROM silver.attribute_tables a
 WHERE a.project_id = $1::uuid
 GROUP BY a.source_file, a.source_file_sha256, a.source_layer
 ORDER BY a.source_file NULLS LAST, a.source_layer
 LIMIT {ATTRIBUTE_GROUP_LIMIT}
"""

# KEYS of one row, never its values.
_ATTR_KEYS_SQL = """
SELECT k AS key
  FROM silver.attribute_tables a, jsonb_object_keys(a.attributes) AS k
 WHERE a.project_id = $1::uuid
   AND a.source_file_sha256 = $2::text
   AND a.source_layer = $3::text
   AND a.row_index = $4::int
 ORDER BY k
"""


async def check_attribute_tables(conn: Any, project: Project) -> dict[str, Any]:
    groups: list[dict[str, Any]] = []
    total_groups = 0
    for g in await conn.fetch(_ATTR_GROUPS_SQL, project.project_id):
        total_groups = int(g["total_groups"])
        keys = [
            r["key"]
            for r in await conn.fetch(
                _ATTR_KEYS_SQL, project.project_id, g["source_file_sha256"], g["source_layer"], g["first_row_index"]
            )
        ]
        groups.append(
            {
                "source_file": g["source_file"],
                "source_layer": g["source_layer"],
                "sha256_prefix": (g["source_file_sha256"] or "")[:12],
                "row_count": int(g["row_count"]),
                "column_names": keys,
            }
        )
    return {
        "groups": groups,
        "groups_total": total_groups,
        "groups_limit": ATTRIBUTE_GROUP_LIMIT,
        "note": "column_names are the keys of the lowest-row_index row of each group; values are never read",
    }


_WYOMING_PRED = f"ST_Intersects(c.geom_4326, ST_MakeEnvelope({WYOMING_BOX[0]}, {WYOMING_BOX[1]}, {WYOMING_BOX[2]}, {WYOMING_BOX[3]}, 4326))"


async def check_collars(conn: Any, project: Project) -> dict[str, Any]:
    base = await conn.fetchrow(
        """
        SELECT count(*) AS total,
               count(*) FILTER (WHERE c.geom_4326 IS NULL) AS null_geom,
               count(*) FILTER (WHERE c.hole_id_canonical IS NULL) AS null_hole_id_canonical
          FROM silver.collars c
         WHERE c.project_id = $1::uuid
        """,
        project.project_id,
    )
    out: dict[str, Any] = {
        "total": int(base["total"]),
        "null_geometry": int(base["null_geom"]),
        "null_hole_id_canonical": int(base["null_hole_id_canonical"]),
    }

    async def _georef() -> dict[str, Any]:
        rows = await conn.fetch(
            """
            SELECT COALESCE(c.georef_method, '(null)') AS georef_method, count(*) AS n
              FROM silver.collars c
             WHERE c.project_id = $1::uuid
             GROUP BY 1
             ORDER BY n DESC, 1
            """,
            project.project_id,
        )
        return {"breakdown": {r["georef_method"]: int(r["n"]) for r in rows}}

    async def _duplicates() -> dict[str, Any]:
        rows = await conn.fetch(
            f"""
            SELECT c.hole_id_canonical AS hole_id, count(*) AS n, count(*) OVER () AS total
              FROM silver.collars c
             WHERE c.project_id = $1::uuid AND c.hole_id_canonical IS NOT NULL
             GROUP BY c.hole_id_canonical
            HAVING count(*) > 1
             ORDER BY n DESC, c.hole_id_canonical
             LIMIT {DUPLICATE_LIMIT}
            """,  # noqa: S608 — constant
            project.project_id,
        )
        # Rows whose canonical id is NULL are outside the unique index, so also compare a
        # punctuation-insensitive form of hole_id.
        near = await conn.fetch(
            f"""
            SELECT upper(regexp_replace(c.hole_id, '[^A-Za-z0-9]', '', 'g')) AS hole_id,
                   count(*) AS n, count(*) OVER () AS total
              FROM silver.collars c
             WHERE c.project_id = $1::uuid
             GROUP BY 1
            HAVING count(*) > 1
             ORDER BY n DESC, 1
             LIMIT {DUPLICATE_LIMIT}
            """,  # noqa: S608 — constant
            project.project_id,
        )
        return {
            "by_hole_id_canonical": {
                "groups": int(rows[0]["total"]) if rows else 0,
                "hole_ids": [{"hole_id": r["hole_id"], "rows": int(r["n"])} for r in rows],
            },
            "by_normalised_hole_id": {
                "groups": int(near[0]["total"]) if near else 0,
                "hole_ids": [{"hole_id": r["hole_id"], "rows": int(r["n"])} for r in near],
            },
        }

    async def _wyoming() -> dict[str, Any]:
        rows = await conn.fetch(
            f"""
            SELECT c.hole_id, count(*) OVER () AS total
              FROM silver.collars c
             WHERE c.project_id = $1::uuid AND {_WYOMING_PRED}
             ORDER BY c.hole_id
             LIMIT {ID_LIST_LIMIT}
            """,  # noqa: S608 — constants
            project.project_id,
        )
        return {
            "box_lng_lat": list(WYOMING_BOX),
            "count": int(rows[0]["total"]) if rows else 0,
            "hole_ids": [r["hole_id"] for r in rows],
        }

    out["georef_method"] = await guarded(conn, [], _georef, what="collars:georef_method")
    out["duplicates"] = await guarded(conn, [], _duplicates, what="collars:duplicates")
    out["wyoming_fallback_box"] = await guarded(conn, [], _wyoming, what="collars:wyoming")
    return out


def _id_list_sql(where: str) -> str:
    """SELECT hole ids of collars matching ``where`` (a fragment of constants), with the total."""
    return f"""
        SELECT c.hole_id, count(*) OVER () AS total
          FROM silver.collars c
         WHERE c.project_id = $1::uuid AND {where}
         ORDER BY c.hole_id
         LIMIT {ID_LIST_LIMIT}
    """  # noqa: S608 — constants


async def _hole_list(conn: Any, where: str, project: Project) -> dict[str, Any]:
    rows = await conn.fetch(_id_list_sql(where), project.project_id)
    return {
        "count": int(rows[0]["total"]) if rows else 0,
        "hole_ids": [r["hole_id"] for r in rows],
        "limit": ID_LIST_LIMIT,
    }


_HAS_LOGS = "EXISTS (SELECT 1 FROM silver.lithology_logs l WHERE l.collar_id = c.collar_id)"
_HAS_CANONICAL = "EXISTS (SELECT 1 FROM silver.lithology l2 WHERE l2.collar_id = c.collar_id)"
_HAS_SURVEYS = "EXISTS (SELECT 1 FROM silver.surveys s WHERE s.collar_id = c.collar_id)"
_HAS_CURVES = "EXISTS (SELECT 1 FROM silver.well_log_curves w WHERE w.collar_id = c.collar_id)"
_HAS_TRACE = "EXISTS (SELECT 1 FROM silver.drill_traces t WHERE t.collar_id = c.collar_id)"


async def check_coverage(conn: Any, project: Project) -> dict[str, Any]:
    out: dict[str, Any] = {}

    async def _total() -> dict[str, Any]:
        return {
            "count": int(
                await conn.fetchval(
                    "SELECT count(*) FROM silver.collars c WHERE c.project_id = $1::uuid", project.project_id
                )
            )
        }

    out["holes_total"] = await guarded(conn, [], _total, what="coverage:total")

    # "has lithology" is the legacy logs table OR the canonical table when it exists.
    has_canonical = await table_exists(conn, "silver.lithology")
    has_litho = f"({_HAS_LOGS} OR {_HAS_CANONICAL})" if has_canonical else _HAS_LOGS

    specs: tuple[tuple[str, list[str], str], ...] = (
        (
            "lithology_no_surveys",
            ["silver.lithology_logs", "silver.surveys"],
            f"{has_litho} AND NOT {_HAS_SURVEYS}",
        ),
        ("with_curves", ["silver.well_log_curves"], _HAS_CURVES),
        ("no_trace", ["silver.drill_traces"], f"NOT {_HAS_TRACE}"),
        ("surveys_no_trace", ["silver.surveys", "silver.drill_traces"], f"{_HAS_SURVEYS} AND NOT {_HAS_TRACE}"),
        ("lithology_no_trace", ["silver.lithology_logs", "silver.drill_traces"], f"{has_litho} AND NOT {_HAS_TRACE}"),
    )
    for name, requires, where in specs:

        async def _run(where: str = where) -> dict[str, Any]:
            return await _hole_list(conn, where, project)

        out[name] = await guarded(conn, ["silver.collars", *requires], _run, what=f"coverage:{name}")

    async def _quality() -> dict[str, Any]:
        rows = await conn.fetch(
            f"""
            SELECT t.trace_quality, count(*) AS n,
                   (array_agg(c.hole_id ORDER BY c.hole_id))[1:{ID_LIST_LIMIT}] AS hole_ids
              FROM silver.drill_traces t
              JOIN silver.collars c ON c.collar_id = t.collar_id
             WHERE t.project_id = $1::uuid
             GROUP BY t.trace_quality
             ORDER BY n DESC, t.trace_quality
            """,  # noqa: S608 — constant
            project.project_id,
        )
        return {
            "buckets": {
                str(r["trace_quality"]): {
                    "count": int(r["n"]),
                    "hole_ids": list(r["hole_ids"] or []),
                    "limit": ID_LIST_LIMIT,
                }
                for r in rows
            }
        }

    out["trace_quality"] = await guarded(
        conn, ["silver.drill_traces", "silver.collars"], _quality, what="coverage:trace_quality"
    )
    return out


async def check_curves(conn: Any, project: Project) -> dict[str, Any]:
    rows = await conn.fetch(
        f"""
        SELECT w.curve_name, count(*) AS curves, count(DISTINCT w.collar_id) AS holes,
               count(*) OVER () AS total_names
          FROM silver.well_log_curves w
          JOIN silver.collars c ON c.collar_id = w.collar_id
         WHERE c.project_id = $1::uuid
         GROUP BY w.curve_name
         ORDER BY curves DESC, w.curve_name
         LIMIT {CURVE_NAME_LIMIT}
        """,  # noqa: S608 — constant
        project.project_id,
    )
    return {
        "curve_names_total": int(rows[0]["total_names"]) if rows else 0,
        "curve_names_limit": CURVE_NAME_LIMIT,
        "curves": [{"curve_name": r["curve_name"], "curves": int(r["curves"]), "holes": int(r["holes"])} for r in rows],
        "curve_rows_total": sum(int(r["curves"]) for r in rows),
    }


def _split_derived(rows: Sequence[Any]) -> dict[str, Any]:
    by = {bool(r["derived"]): r for r in rows}

    def _side(flag: bool) -> dict[str, int]:
        r = by.get(flag)
        return {"intervals": int(r["intervals"]) if r else 0, "holes": int(r["holes"]) if r else 0}

    return {"derived": _side(True), "logged": _side(False)}


async def check_derived(conn: Any, project: Project) -> dict[str, Any]:
    out: dict[str, Any] = {}

    async def _silver() -> dict[str, Any]:
        rows = await conn.fetch(
            """
            SELECT COALESCE(l.lithology_code LIKE 'DERIVED-%', false) AS derived,
                   count(*) AS intervals, count(DISTINCT l.collar_id) AS holes
              FROM silver.lithology_logs l
              JOIN silver.collars c ON c.collar_id = l.collar_id
             WHERE c.project_id = $1::uuid
             GROUP BY 1
            """,
            project.project_id,
        )
        codes = await conn.fetch(
            """
            SELECT l.lithology_code AS code, count(*) AS n
              FROM silver.lithology_logs l
              JOIN silver.collars c ON c.collar_id = l.collar_id
             WHERE c.project_id = $1::uuid AND l.lithology_code LIKE 'DERIVED-%'
             GROUP BY 1
             ORDER BY n DESC, 1
            """,
            project.project_id,
        )
        return {**_split_derived(rows), "derived_codes": {str(r["code"]): int(r["n"]) for r in codes}}

    async def _gold() -> dict[str, Any]:
        rows = await conn.fetch(
            """
            SELECT COALESCE(g.lithology_code LIKE 'DERIVED-%', false) AS derived,
                   count(*) AS intervals, count(DISTINCT g.collar_id) AS holes
              FROM gold.drillhole_intervals_visual g
             WHERE g.project_id = $1::uuid AND g.interval_kind = 'lithology'
             GROUP BY 1
            """,
            project.project_id,
        )
        return _split_derived(rows)

    out["silver_lithology_logs"] = await guarded(
        conn, ["silver.lithology_logs", "silver.collars"], _silver, what="derived:silver"
    )
    out["gold_drillhole_intervals_visual"] = await guarded(
        conn, ["gold.drillhole_intervals_visual"], _gold, what="derived:gold"
    )
    return out


async def check_archive_runs(conn: Any, project: Project) -> dict[str, Any]:
    rows = await conn.fetch(
        f"""
        SELECT a.archive_run_id::text AS archive_run_id, a.filename, a.status, a.file_count,
               a.files_succeeded, a.files_failed, a.files_skipped,
               a.started_at, a.completed_at, a.failed_at, a.error_text
          FROM silver.archive_ingest_runs a
         WHERE a.project_id = $1::uuid
         ORDER BY a.started_at DESC
         LIMIT {ARCHIVE_RUN_LIMIT}
        """,  # noqa: S608 — constant
        project.project_id,
    )
    runs = [
        {
            "archive_run_id": r["archive_run_id"],
            "filename": r["filename"],
            "status": r["status"],
            "file_count": r["file_count"],
            "files_succeeded": r["files_succeeded"],
            "files_failed": r["files_failed"],
            "files_skipped": r["files_skipped"],
            "started_at": _ts(r["started_at"]),
            "completed_at": _ts(r["completed_at"]),
            "failed_at": _ts(r["failed_at"]),
            "error_text": scrub_error(r["error_text"]),
        }
        for r in rows
    ]
    return {"runs": runs, "runs_limit": ARCHIVE_RUN_LIMIT, "by_status": dict(Counter(r["status"] for r in runs))}


# One row per report with its passages rolled up. ``source_object_key`` is the bronze
# object path (a file name — allowed by the Safety note above); the report TITLE is text
# parsed out of the document and is deliberately not selected.
_DOCUMENT_ROWS_SQL = f"""
SELECT r.report_id::text AS report_id, r.source_object_key, r.page_count, r.is_scanned,
       r.parser_used, r.parse_quality_pct, r.text_page_coverage_pct, r.created_at,
       count(p.passage_id) AS passages,
       count(p.passage_id) FILTER (WHERE p.modality = 'image') AS image_passages,
       count(p.passage_id) FILTER (WHERE p.embedding_id IS NOT NULL) AS embedded,
       count(p.passage_id) FILTER (WHERE p.embedding_id IS NULL) AS unembedded,
       count(p.passage_id) FILTER (WHERE p.ocr_method = 'cohere_parse') AS cohere_parse_passages,
       count(p.passage_id) FILTER (WHERE p.ocr_method = 'tesseract') AS tesseract_passages,
       count(p.passage_id) FILTER (WHERE p.ocr_status = 'low_confidence') AS low_confidence,
       count(*) OVER () AS total_reports
  FROM silver.reports r
  LEFT JOIN silver.document_passages p ON p.document_id = r.report_id
 WHERE r.project_id = $1::uuid
 GROUP BY r.report_id
 ORDER BY r.created_at DESC NULLS LAST, r.report_id
 LIMIT {DOCUMENT_LIMIT}
"""

# Whole-project rollup, not limited to the rows above.
_PASSAGE_ROLLUP_SQL = """
SELECT p.modality, COALESCE(p.chunk_kind, '(none)') AS chunk_kind,
       COALESCE(p.ocr_method, '(none)') AS ocr_method,
       count(*) AS n,
       count(*) FILTER (WHERE p.embedding_id IS NOT NULL) AS embedded
  FROM silver.document_passages p
  JOIN silver.reports r ON r.report_id = p.document_id
 WHERE r.project_id = $1::uuid
 GROUP BY 1, 2, 3
 ORDER BY n DESC, 1, 2, 3
"""

_OCR_RESULTS_SQL = """
SELECT count(*) AS n, count(DISTINCT o.report_id) AS reports
  FROM silver.ingest_ocr_results o
  JOIN silver.reports r ON r.report_id = o.report_id
 WHERE r.project_id = $1::uuid
"""


def _basename(key: Any) -> Any:
    return key.rsplit("/", 1)[-1] if isinstance(key, str) else key


async def check_documents(conn: Any, project: Project) -> dict[str, Any]:
    reports: list[dict[str, Any]] = []
    total_reports = 0
    for r in await conn.fetch(_DOCUMENT_ROWS_SQL, project.project_id):
        total_reports = int(r["total_reports"])
        reports.append(
            {
                "report_id": r["report_id"],
                "source_file": _basename(r["source_object_key"]),
                "page_count": r["page_count"],
                "is_scanned": r["is_scanned"],
                "parser_used": r["parser_used"],
                "parse_quality_pct": r["parse_quality_pct"],
                "text_page_coverage_pct": r["text_page_coverage_pct"],
                "created_at": _ts(r["created_at"]),
                "passages": int(r["passages"]),
                "image_passages": int(r["image_passages"]),
                "embedded": int(r["embedded"]),
                "unembedded": int(r["unembedded"]),
                "cohere_parse_passages": int(r["cohere_parse_passages"]),
                "tesseract_passages": int(r["tesseract_passages"]),
                "low_confidence": int(r["low_confidence"]),
            }
        )
    rollup = [
        {
            "modality": r["modality"],
            "chunk_kind": r["chunk_kind"],
            "ocr_method": r["ocr_method"],
            "rows": int(r["n"]),
            "embedded": int(r["embedded"]),
        }
        for r in await conn.fetch(_PASSAGE_ROLLUP_SQL, project.project_id)
    ]
    passages_total = sum(x["rows"] for x in rollup)
    embedded_total = sum(x["embedded"] for x in rollup)
    scanned = [x for x in reports if x["is_scanned"]]

    async def _ocr_results() -> dict[str, Any]:
        row = await conn.fetchrow(_OCR_RESULTS_SQL, project.project_id)
        return {"rows": int(row["n"]), "reports": int(row["reports"])}

    return {
        "reports": reports,
        "reports_total": total_reports,
        "reports_limit": DOCUMENT_LIMIT,
        "reports_without_passages": sorted(x["source_file"] or x["report_id"] for x in reports if x["passages"] == 0),
        "scanned_reports": len(scanned),
        "scanned_reports_without_cohere_parse": sorted(
            x["source_file"] or x["report_id"] for x in scanned if x["cohere_parse_passages"] == 0
        ),
        "passages_total": passages_total,
        "embedded_total": embedded_total,
        "unembedded_total": passages_total - embedded_total,
        "image_passages_total": sum(x["rows"] for x in rollup if x["modality"] == "image"),
        "by_ocr_method": {
            k: sum(x["rows"] for x in rollup if x["ocr_method"] == k)
            for k in sorted({x["ocr_method"] for x in rollup})
        },
        "rollup": rollup,
        "legacy_ocr_results": await guarded(
            conn, ["silver.ingest_ocr_results"], _ocr_results, what="documents:ingest_ocr_results"
        ),
    }


# --------------------------------------------------------------------------
# Checks 10 and 11: does the data land in the right place, and will the UI show it
# --------------------------------------------------------------------------
#
# Every number below is a count, an extent, a distance or a hole id. No free text.
# Table and column names were checked against database/migrations (2026-10-06):
#   silver.collars        project_id, hole_id, easting, northing, elevation, azimuth, dip,
#                         geom_4326 (geometry(Point,4326); `geom` was dropped 2026-09-30)
#   silver.surveys        collar_id, depth, azimuth, dip, source_file (2026-10-04)
#   silver.drill_traces   project_id, collar_id, geom (LINESTRINGZ,4326), trace_quality
#   silver.spatial_features, project_boundaries, geological_formations, historic_workings,
#   silver.geochemistry   project_id + geom (geochemistry's project_id was added and
#                         back-filled by 2026_04_22_140000; surface samples written
#                         without one are invisible to a project-scoped query)
#   silver.seismic_surveys project_id (nullable) + bbox (POLYGON,4326)
#   silver.lithology_logs, lithology, alteration, mineralization, structure, samples,
#   gold.assay_composites, gold.significant_intersections: collar_id only -> through silver.collars
#   gold.drillhole_intervals_visual, gold.structure_measurements_visual: project_id and collar_id

#: Where the Workspace controller (WorkspaceController::buildThreeDPayload / show) cuts off.
MAX_WORKSPACE_COLLARS = 1000
MAX_INTERVAL_HOLES = 200
MAX_INTERVAL_BANDS_PER_HOLE = 80
MAX_SURVEY_STATIONS_PER_HOLE = 100
#: Strip-log band count per hole and kind past which the LOGS panel is a wall of ticks.
STRIP_BANDS_PER_KIND_LIMIT = 1500
#: LIMIT 5000 on the 3D structure and sample payloads.
THREE_D_ROW_CAP = 5000
#: gold.drillhole_intervals_visual kinds the LOGS hole picker lists a hole for.
PICKER_INTERVAL_KINDS = ("lithology", "alteration", "mineralization")

#: easting/northing (as metres) vs geom_4326 re-projected into the project CRS.
COLLAR_OFFSET_THRESHOLD_M = 25.0
#: trace start vs its collar.
TRACE_START_THRESHOLD_M = 5.0
#: a map layer whose centroid is this far from the collars is probably in the wrong place.
FAR_FROM_COLLARS_KM = 100.0

#: The sign convention for survey / collar dip, from the code that desurveys and from §04e.
DIP_CONVENTION = (
    "negative = below horizontal (-90 = straight down), positive = up-hole (0 < dip <= 90); "
    "valid range -90..90 (promote_silver_to_gold._clean_stations, georag_geoparsers._survey_interp, "
    "chk_dip_range 2026-09-29)"
)

#: (key, table, geometry column). Each is scoped by its own project_id.
EXTENT_TABLES: tuple[tuple[str, str, str], ...] = (
    ("collars", "silver.collars", "geom_4326"),
    ("drill_traces", "silver.drill_traces", "geom"),
    ("spatial_features", "silver.spatial_features", "geom"),
    ("geochemistry", "silver.geochemistry", "geom"),
    ("project_boundaries", "silver.project_boundaries", "geom"),
    ("geological_formations", "silver.geological_formations", "geom"),
    ("historic_workings", "silver.historic_workings", "geom"),
    ("seismic_surveys", "silver.seismic_surveys", "bbox"),
)


def haversine_km(lng1: float, lat1: float, lng2: float, lat2: float) -> float:
    """Great-circle distance in km between two lng/lat points (spherical earth, 6371.0088 km)."""
    from math import asin, cos, radians, sin, sqrt  # noqa: PLC0415

    p1, p2 = radians(lat1), radians(lat2)
    a = sin((p2 - p1) / 2) ** 2 + cos(p1) * cos(p2) * sin(radians(lng2 - lng1) / 2) ** 2
    return 2 * 6371.0088 * asin(min(1.0, sqrt(a)))


def _num(row: Any, key: str) -> float | None:
    value = row.get(key) if row is not None else None
    return None if value is None else float(value)


def _int(row: Any, key: str) -> int:
    value = row.get(key) if row is not None else None
    return 0 if value is None else int(value)


def _r(value: float | None, digits: int = 3) -> float | None:
    return None if value is None else round(value, digits)


async def _grouped_holes(
    conn: Any,
    project: Project,
    *,
    from_sql: str,
    having: str,
    kind_expr: str | None = None,
    n_expr: str = "count(*)",
) -> dict[str, Any]:
    """Holes (worst-first, capped) whose grouped rows satisfy ``having``; every fragment is a
    constant. ``kind_expr`` adds a column to the grouping (e.g. interval_kind)."""
    group = "c.collar_id, c.hole_id" + (f", {kind_expr}" if kind_expr else "")
    kind_col = f", {kind_expr} AS kind" if kind_expr else ""
    rows = await conn.fetch(
        f"""
        SELECT c.hole_id{kind_col}, {n_expr} AS n, count(*) OVER () AS total
          FROM {from_sql}
         WHERE c.project_id = $1::uuid
         GROUP BY {group}
        HAVING {having}
         ORDER BY n DESC, c.hole_id
         LIMIT {ID_LIST_LIMIT}
        """,  # noqa: S608 — constants
        project.project_id,
    )
    holes = []
    for r in rows:
        item: dict[str, Any] = {"hole_id": r["hole_id"], "n": int(r["n"])}
        if kind_expr:
            item["kind"] = r["kind"]
        holes.append(item)
    return {"count": int(rows[0]["total"]) if rows else 0, "holes": holes, "limit": ID_LIST_LIMIT}


# --- check 10: placement ----------------------------------------------------------------------

# Spatial reference of the project CRS, and whether it is projected / in metres. A
# geographic or unknown crs_epsg cannot be compared against easting/northing in metres.
_PROJECT_CRS_SQL = r"""
SELECT pr.crs_epsg AS crs_epsg,
       (SELECT srs.srtext ~* 'UNIT\["(metre|meter)"' FROM spatial_ref_sys srs WHERE srs.srid = pr.crs_epsg) AS metre_unit,
       (SELECT srs.srtext ~ '^PROJCS' FROM spatial_ref_sys srs WHERE srs.srid = pr.crs_epsg) AS projected
  FROM silver.projects pr
 WHERE pr.project_id = $1::uuid
"""

# $2 = project crs_epsg. What the 3D view does with easting/northing is "treat them as
# metres"; geom_4326 re-projected into the project CRS is where the map says the hole is.
# Written out in full, not assembled from shared fragments: the CI schema gate
# (scripts/ci/check_sql_against_schema.py) PREPAREs each literal with its f-string holes
# filled by placeholders, so a FROM clause held in a hole leaves `c.` unresolvable.
_OFFSET_AGG_SQL = f"""
SELECT count(*) AS compared,
       count(*) FILTER (WHERE d.dist_m > {COLLAR_OFFSET_THRESHOLD_M}) AS over_threshold,
       max(d.dist_m) AS max_m
  FROM (SELECT ST_Distance(ST_Transform(c.geom_4326, $2::int),
                           ST_SetSRID(ST_MakePoint(c.easting, c.northing), $2::int)) AS dist_m
          FROM silver.collars c
         WHERE c.project_id = $1::uuid AND c.geom_4326 IS NOT NULL
           AND c.easting IS NOT NULL AND c.northing IS NOT NULL) d
"""  # noqa: S608 — constants
_OFFSET_LIST_SQL = f"""
SELECT d.hole_id, d.dist_m
  FROM (SELECT c.hole_id,
               ST_Distance(ST_Transform(c.geom_4326, $2::int),
                           ST_SetSRID(ST_MakePoint(c.easting, c.northing), $2::int)) AS dist_m
          FROM silver.collars c
         WHERE c.project_id = $1::uuid AND c.geom_4326 IS NOT NULL
           AND c.easting IS NOT NULL AND c.northing IS NOT NULL) d
 WHERE d.dist_m > {COLLAR_OFFSET_THRESHOLD_M}
 ORDER BY d.dist_m DESC, d.hole_id
 LIMIT {ID_LIST_LIMIT}
"""  # noqa: S608 — constants


async def _placement_offset(conn: Any, project: Project) -> dict[str, Any]:
    pr = await conn.fetchrow(_PROJECT_CRS_SQL, project.project_id)
    crs = pr.get("crs_epsg") if pr is not None else None
    out: dict[str, Any] = {
        "project_crs_epsg": crs,
        "threshold_m": COLLAR_OFFSET_THRESHOLD_M,
        "limit": ID_LIST_LIMIT,
    }
    if crs is None:
        return {**out, "skipped": "silver.projects.crs_epsg is not set"}
    if pr.get("metre_unit") is None:
        return {**out, "skipped": f"EPSG:{crs} is not in spatial_ref_sys"}
    if not pr.get("projected"):
        return {**out, "skipped": f"EPSG:{crs} is a geographic CRS: easting/northing are not metres in it"}
    out["linear_unit_is_metre"] = bool(pr.get("metre_unit"))
    agg = await conn.fetchrow(_OFFSET_AGG_SQL, project.project_id, int(crs))
    rows = await conn.fetch(_OFFSET_LIST_SQL, project.project_id, int(crs))
    return {
        **out,
        "compared": _int(agg, "compared"),
        "over_threshold": _int(agg, "over_threshold"),
        "max_m": _r(_num(agg, "max_m")),
        "holes": [{"hole_id": r["hole_id"], "distance_m": _r(float(r["dist_m"]))} for r in rows],
    }


def _extent_sql(table: str, col: str) -> str:
    """One row: rows, NULL-or-empty geometries, rows outside valid WGS84, the lng/lat bbox and
    mean feature centroid of the in-range rows, and the SRIDs seen. A geometry whose SRID is
    neither 0 nor 4326 is transformed to 4326 first; SRID 0 is read as 4326 (and shows in
    ``srids``)."""
    return f"""
    SELECT count(*) AS n_rows,
           count(*) FILTER (WHERE x.g IS NULL) AS null_geom,
           count(*) FILTER (WHERE x.g IS NOT NULL AND NOT x.in_range) AS outside_wgs84,
           ST_XMin(ST_Extent(x.g) FILTER (WHERE x.in_range)) AS min_lng,
           ST_YMin(ST_Extent(x.g) FILTER (WHERE x.in_range)) AS min_lat,
           ST_XMax(ST_Extent(x.g) FILTER (WHERE x.in_range)) AS max_lng,
           ST_YMax(ST_Extent(x.g) FILTER (WHERE x.in_range)) AS max_lat,
           avg(ST_X(ST_Centroid(x.g))) FILTER (WHERE x.in_range) AS centroid_lng,
           avg(ST_Y(ST_Centroid(x.g))) FILTER (WHERE x.in_range) AS centroid_lat,
           array_agg(DISTINCT x.srid) AS srids
      FROM (
            SELECT y.g, y.srid,
                   COALESCE(ST_XMin(y.g) >= -180 AND ST_XMax(y.g) <= 180
                            AND ST_YMin(y.g) >= -90 AND ST_YMax(y.g) <= 90, false) AS in_range
              FROM (
                    SELECT CASE WHEN t.{col} IS NULL OR ST_IsEmpty(t.{col}) THEN NULL
                                WHEN ST_SRID(t.{col}) IN (0, 4326) THEN ST_SetSRID(t.{col}, 4326)
                                ELSE ST_Transform(t.{col}, 4326) END AS g,
                           ST_SRID(t.{col}) AS srid
                      FROM {table} t
                     WHERE t.project_id = $1::uuid
                   ) y
           ) x
    """  # noqa: S608 — constants


async def _placement_extents(conn: Any, project: Project) -> dict[str, Any]:
    tables: dict[str, Any] = {}
    for key, table, col in EXTENT_TABLES:

        async def _one(table: str = table, col: str = col) -> dict[str, Any]:
            row = await conn.fetchrow(_extent_sql(table, col), project.project_id)
            lng, lat = _num(row, "centroid_lng"), _num(row, "centroid_lat")
            bbox = [_num(row, k) for k in ("min_lng", "min_lat", "max_lng", "max_lat")]
            return {
                "table": table,
                "geometry_column": col,
                "rows": _int(row, "n_rows"),
                "null_or_empty_geometry": _int(row, "null_geom"),
                "outside_wgs84": _int(row, "outside_wgs84"),
                "srids": sorted({int(s) for s in (row.get("srids") if row is not None else None) or [] if s is not None}),
                "bbox_lng_lat": [_r(v, 5) for v in bbox] if all(v is not None for v in bbox) else None,
                "centroid_lng_lat": [_r(lng, 5), _r(lat, 5)] if lng is not None and lat is not None else None,
                "km_from_collar_centroid": None,
                "far_from_collars": False,
            }

        tables[key] = await guarded(conn, [table], _one, what=f"placement:extents:{key}")

    collars = tables.get("collars", {})
    anchor = collars.get("centroid_lng_lat") if collars.get("status") == "ok" else None
    if anchor:
        for key, res in tables.items():
            if key == "collars" or res.get("status") != "ok" or not res["centroid_lng_lat"]:
                continue
            km = haversine_km(anchor[0], anchor[1], res["centroid_lng_lat"][0], res["centroid_lng_lat"][1])
            res["km_from_collar_centroid"] = round(km, 1)
            res["far_from_collars"] = km > FAR_FROM_COLLARS_KM
    return {
        "tables": tables,
        "far_km_threshold": FAR_FROM_COLLARS_KM,
        "collar_centroid_lng_lat": anchor,
        "note": "centroid = mean of the per-feature centroids of the rows inside valid WGS84; scoped by each "
        "table's own project_id (rows with a NULL project_id are not counted)",
    }


# Start of each trace vs its collar, in metres on the spheroid. The promotion builds a trace
# by translating metre offsets onto the collar, so the first vertex IS the collar.
# Written out in full for the same reason as _OFFSET_AGG_SQL.
_TRACE_AGG_SQL = f"""
SELECT count(*) AS traces,
       count(*) FILTER (WHERE d.dist_m IS NULL) AS no_collar_position,
       count(*) FILTER (WHERE d.dist_m > {TRACE_START_THRESHOLD_M}) AS over_threshold,
       max(d.dist_m) AS max_m
  FROM (SELECT CASE WHEN c.geom_4326 IS NULL THEN NULL
                    ELSE ST_Distance(ST_StartPoint(t.geom)::geography, c.geom_4326::geography)
               END AS dist_m
          FROM silver.drill_traces t
          JOIN silver.collars c ON c.collar_id = t.collar_id
         WHERE t.project_id = $1::uuid AND c.project_id = $1::uuid) d
"""  # noqa: S608 — constants
_TRACE_LIST_SQL = f"""
SELECT d.hole_id, d.dist_m
  FROM (SELECT c.hole_id,
               CASE WHEN c.geom_4326 IS NULL THEN NULL
                    ELSE ST_Distance(ST_StartPoint(t.geom)::geography, c.geom_4326::geography)
               END AS dist_m
          FROM silver.drill_traces t
          JOIN silver.collars c ON c.collar_id = t.collar_id
         WHERE t.project_id = $1::uuid AND c.project_id = $1::uuid) d
 WHERE d.dist_m > {TRACE_START_THRESHOLD_M}
 ORDER BY d.dist_m DESC, d.hole_id
 LIMIT {ID_LIST_LIMIT}
"""  # noqa: S608 — constants


async def _placement_traces(conn: Any, project: Project) -> dict[str, Any]:
    agg = await conn.fetchrow(_TRACE_AGG_SQL, project.project_id)
    rows = await conn.fetch(_TRACE_LIST_SQL, project.project_id)
    return {
        "traces": _int(agg, "traces"),
        "no_collar_position": _int(agg, "no_collar_position"),
        "threshold_m": TRACE_START_THRESHOLD_M,
        "over_threshold": _int(agg, "over_threshold"),
        "max_m": _r(_num(agg, "max_m")),
        "holes": [{"hole_id": r["hole_id"], "distance_m": _r(float(r["dist_m"]))} for r in rows],
        "limit": ID_LIST_LIMIT,
        "note": "trace_quality buckets are reported under check 5 (coverage), not repeated here",
    }


_SURVEY_FROM = "silver.surveys s JOIN silver.collars c ON c.collar_id = s.collar_id"

_SURVEY_STATS_SQL = f"""
SELECT count(*) AS stations,
       count(DISTINCT s.collar_id) AS holes,
       count(*) FILTER (WHERE s.azimuth IS NULL) AS null_azimuth,
       count(*) FILTER (WHERE s.dip IS NULL) AS null_dip,
       count(*) FILTER (WHERE s.azimuth IS NULL OR s.dip IS NULL) AS dropped_by_desurvey,
       count(*) FILTER (WHERE s.dip < -90 OR s.dip > 90) AS dip_out_of_range,
       count(*) FILTER (WHERE s.azimuth < 0 OR s.azimuth > 360) AS azimuth_out_of_range,
       count(*) FILTER (WHERE s.dip > 0 AND s.dip <= 90) AS up_hole_stations,
       count(DISTINCT s.collar_id) FILTER (WHERE s.dip > 0 AND s.dip <= 90) AS up_hole_holes
  FROM {_SURVEY_FROM}
 WHERE c.project_id = $1::uuid
"""  # noqa: S608 — constants


async def _placement_surveys(conn: Any, project: Project) -> dict[str, Any]:
    out: dict[str, Any] = {"dip_convention": DIP_CONVENTION, "station_cap_in_3d": MAX_SURVEY_STATIONS_PER_HOLE}

    async def _stats() -> dict[str, Any]:
        row = await conn.fetchrow(_SURVEY_STATS_SQL, project.project_id)
        keys = (
            "stations",
            "holes",
            "null_azimuth",
            "null_dip",
            "dropped_by_desurvey",
            "dip_out_of_range",
            "azimuth_out_of_range",
            "up_hole_stations",
            "up_hole_holes",
        )
        return {k: _int(row, k) for k in keys}

    async def _over_cap() -> dict[str, Any]:
        return await _grouped_holes(
            conn, project, from_sql=_SURVEY_FROM, having=f"count(*) > {MAX_SURVEY_STATIONS_PER_HOLE}"
        )

    async def _mixed() -> dict[str, Any]:
        # A NULL source_file is a group of its own, as in the desurvey's per-file pick.
        return await _grouped_holes(
            conn,
            project,
            from_sql=_SURVEY_FROM,
            having="count(DISTINCT COALESCE(s.source_file, '(none)')) > 1",
            n_expr="count(DISTINCT COALESCE(s.source_file, '(none)'))",
        )

    async def _all_positive() -> dict[str, Any]:
        # Every dipped station of the hole is above horizontal: the usual signature of a
        # file that logs dip positive-down, which this platform would draw as an up-hole.
        return await _grouped_holes(
            conn, project, from_sql=_SURVEY_FROM, having="min(s.dip) > 0 AND max(s.dip) <= 90"
        )

    requires = ["silver.surveys", "silver.collars"]
    out["stations"] = await guarded(conn, requires, _stats, what="placement:surveys:stations")
    out["over_station_cap"] = await guarded(conn, requires, _over_cap, what="placement:surveys:over_cap")
    out["multiple_source_files"] = await guarded(conn, requires, _mixed, what="placement:surveys:mixed")
    out["all_dips_positive"] = await guarded(conn, requires, _all_positive, what="placement:surveys:positive")
    return out


async def check_placement(conn: Any, project: Project) -> dict[str, Any]:
    out: dict[str, Any] = {}

    where_by_key: tuple[tuple[str, str, list[str]], ...] = (
        ("null_easting_or_northing", "(c.easting IS NULL OR c.northing IS NULL)", []),
        # The 3D view reads easting/northing as metres: a longitude/latitude pair lands the
        # hole a few metres from the scene origin.
        ("degree_looking_easting_northing", "(abs(c.easting) <= 180 AND abs(c.northing) <= 90)", []),
        # No elevation from the file AND none from the terrain model
        # (promote_silver_to_gold; app/services/dem_elevation.py): z = 0 in 3D.
        ("null_elevation", "c.elevation IS NULL AND c.elevation_dem_m IS NULL", []),
        # Informational: drawn at the terrain model's ground height, not an RL.
        ("terrain_elevation", "c.elevation IS NULL AND c.elevation_dem_m IS NOT NULL", []),
        (
            "no_orientation_and_no_surveys",
            f"(c.azimuth IS NULL OR c.dip IS NULL) AND NOT {_HAS_SURVEYS}",
            ["silver.surveys"],
        ),
    )
    for key, where, extra in where_by_key:

        async def _run(where: str = where) -> dict[str, Any]:
            return await _hole_list(conn, where, project)

        out[key] = await guarded(conn, ["silver.collars", *extra], _run, what=f"placement:{key}")

    out["easting_northing_vs_geom_4326"] = await guarded(
        conn,
        ["silver.collars", "silver.projects", "spatial_ref_sys"],
        lambda: _placement_offset(conn, project),
        what="placement:offset",
    )
    out["extents"] = await _placement_extents(conn, project)
    out["trace_start_vs_collar"] = await guarded(
        conn,
        ["silver.drill_traces", "silver.collars"],
        lambda: _placement_traces(conn, project),
        what="placement:traces",
    )
    out["surveys"] = await _placement_surveys(conn, project)
    return out


# --- check 11: visibility ---------------------------------------------------------------------

_GOLD_VIS = "gold.drillhole_intervals_visual"
_HAS_LITHOLOGY_BANDS = (
    f"EXISTS (SELECT 1 FROM {_GOLD_VIS} gb WHERE gb.collar_id = c.collar_id AND gb.interval_kind = 'lithology')"
)
_PICKER_KINDS_SQL = ", ".join(f"'{k}'" for k in PICKER_INTERVAL_KINDS)
_HAS_PICKER_BANDS = (
    f"EXISTS (SELECT 1 FROM {_GOLD_VIS} gp WHERE gp.collar_id = c.collar_id AND gp.interval_kind IN ({_PICKER_KINDS_SQL}))"
)
_NO_CURVES_AT_ALL = "NOT EXISTS (SELECT 1 FROM silver.well_log_curves wc WHERE wc.collar_id = c.collar_id)"

_FIRST_HOLES_SQL = f"""
SELECT count(*) AS holes_considered,
       count(*) FILTER (WHERE EXISTS (
            SELECT 1 FROM {_GOLD_VIS} g1 WHERE g1.collar_id = f.collar_id AND g1.interval_kind = 'lithology'
       )) AS with_bands,
       (SELECT count(DISTINCT g2.collar_id)
          FROM {_GOLD_VIS} g2 JOIN silver.collars c2 ON c2.collar_id = g2.collar_id
         WHERE c2.project_id = $1::uuid AND g2.interval_kind = 'lithology') AS project_holes_with_bands
  FROM (SELECT c.collar_id FROM silver.collars c WHERE c.project_id = $1::uuid
         ORDER BY c.hole_id, c.collar_id LIMIT {MAX_INTERVAL_HOLES}) f
"""  # noqa: S608 — constants

_GOLD_KINDS_SQL = f"""
SELECT g.interval_kind AS kind, count(*) AS n_rows, count(DISTINCT g.collar_id) AS holes
  FROM {_GOLD_VIS} g
 WHERE g.project_id = $1::uuid
 GROUP BY g.interval_kind
 ORDER BY n_rows DESC, g.interval_kind
"""  # noqa: S608 — constant

_PICKER_HIDDEN_SQL = f"""
SELECT count(*) AS n
  FROM (SELECT c.collar_id, row_number() OVER (ORDER BY c.hole_id, c.collar_id) AS rn
          FROM silver.collars c WHERE c.project_id = $1::uuid) r
  JOIN silver.collars c ON c.collar_id = r.collar_id
 WHERE r.rn > {MAX_WORKSPACE_COLLARS} AND {_HAS_PICKER_BANDS} AND {_NO_CURVES_AT_ALL}
"""  # noqa: S608 — constants

#: (key, table, scope). Counted through silver.collars.
_SILVER_LOG_TABLES: tuple[tuple[str, str], ...] = (
    ("lithology_logs", "silver.lithology_logs"),
    ("lithology", "silver.lithology"),
    ("alteration", "silver.alteration"),
    ("mineralization", "silver.mineralization"),
)


def _dropped_rows_sql(table: str) -> str:
    # What _INTERVALS_LITHOLOGY (promote_silver_to_gold) filters out of silver.lithology:
    # NULL depth, to <= from, from < 0, to >= 10,000,000. silver.lithology_logs is checked
    # on the same terms because it is what the canonical table is derived from.
    return f"""
    SELECT count(*) AS n_rows,
           count(*) FILTER (WHERE t.from_depth IS NULL OR t.to_depth IS NULL) AS null_depth,
           count(*) FILTER (WHERE t.to_depth <= t.from_depth) AS to_not_after_from,
           count(*) FILTER (WHERE t.from_depth < 0) AS negative_from,
           count(*) FILTER (WHERE t.to_depth >= 10000000) AS to_depth_overflow,
           count(*) FILTER (WHERE t.from_depth IS NULL OR t.to_depth IS NULL OR t.to_depth <= t.from_depth
                              OR t.from_depth < 0 OR t.to_depth >= 10000000) AS dropped_any
      FROM {table} t JOIN silver.collars c ON c.collar_id = t.collar_id
     WHERE c.project_id = $1::uuid
    """  # noqa: S608 — constant


_STRUCTURE_SQL = """
SELECT count(*) AS n_rows,
       count(*) FILTER (WHERE t.true_dip IS NULL) AS null_true_dip,
       count(*) FILTER (WHERE t.true_dip_dir IS NULL) AS null_true_dip_dir,
       count(*) FILTER (WHERE t.true_dip IS NULL OR t.true_dip_dir IS NULL) AS unusable_in_3d,
       count(*) FILTER (WHERE t.depth IS NULL) AS null_depth
  FROM silver.structure t JOIN silver.collars c ON c.collar_id = t.collar_id
 WHERE c.project_id = $1::uuid
"""

_STRUCTURE_VISUAL_SQL = """
SELECT count(*) AS n_rows,
       count(*) FILTER (WHERE t.depth IS NULL) AS null_depth
  FROM gold.structure_measurements_visual t
 WHERE t.project_id = $1::uuid
"""

_SAMPLES_SQL = """
SELECT count(*) AS n_rows,
       count(*) FILTER (WHERE t.commodity_assays IS NULL) AS null_assays,
       count(*) FILTER (WHERE t.commodity_assays = '{}'::jsonb) AS empty_object,
       count(*) FILTER (WHERE t.commodity_assays IS NOT NULL AND t.commodity_assays <> '{}'::jsonb) AS non_empty
  FROM silver.samples t JOIN silver.collars c ON c.collar_id = t.collar_id
 WHERE c.project_id = $1::uuid
"""


def _gold_rowcount_sql(table: str) -> str:
    return (
        f"SELECT count(*) AS n_rows FROM {table} t JOIN silver.collars c ON c.collar_id = t.collar_id "  # noqa: S608
        "WHERE c.project_id = $1::uuid"
    )


async def check_visibility(conn: Any, project: Project) -> dict[str, Any]:
    out: dict[str, Any] = {}
    collars, vis = "silver.collars", _GOLD_VIS

    # 1. The caps -----------------------------------------------------------------------------
    async def _collar_cap() -> dict[str, Any]:
        total = int(await conn.fetchval("SELECT count(*) AS n FROM silver.collars c WHERE c.project_id = $1::uuid", project.project_id) or 0)
        return {"total": total, "cap": MAX_WORKSPACE_COLLARS, "beyond_cap": max(0, total - MAX_WORKSPACE_COLLARS)}

    async def _first_holes() -> dict[str, Any]:
        row = await conn.fetchrow(_FIRST_HOLES_SQL, project.project_id)
        first, project_wide = _int(row, "with_bands"), _int(row, "project_holes_with_bands")
        return {
            "holes_considered": _int(row, "holes_considered"),
            "first_n": MAX_INTERVAL_HOLES,
            "first_n_with_lithology_bands": first,
            "project_holes_with_lithology_bands": project_wide,
            "three_d_empty_trap": first == 0 and project_wide > 0,
            "order": "hole_id, collar_id (as WorkspaceController::show)",
        }

    async def _over_band_cap() -> dict[str, Any]:
        return await _grouped_holes(
            conn,
            project,
            from_sql=f"{vis} g JOIN silver.collars c ON c.collar_id = g.collar_id",
            having=f"count(*) FILTER (WHERE g.interval_kind = 'lithology') > {MAX_INTERVAL_BANDS_PER_HOLE}",
            n_expr="count(*) FILTER (WHERE g.interval_kind = 'lithology')",
        )

    async def _over_strip_cap() -> dict[str, Any]:
        return await _grouped_holes(
            conn,
            project,
            from_sql=f"{vis} g JOIN silver.collars c ON c.collar_id = g.collar_id",
            kind_expr="g.interval_kind",
            having=f"count(*) > {STRIP_BANDS_PER_KIND_LIMIT}",
        )

    out["collar_cap"] = await guarded(conn, [collars], _collar_cap, what="visibility:collar_cap")
    out["first_holes_lithology"] = await guarded(conn, [collars, vis], _first_holes, what="visibility:first_holes")
    out["holes_over_3d_band_cap"] = await guarded(
        conn, [collars, vis], _over_band_cap, what="visibility:over_band_cap"
    )
    out["holes_over_strip_band_limit"] = await guarded(
        conn, [collars, vis], _over_strip_cap, what="visibility:over_strip_cap"
    )
    out["band_caps"] = {
        "three_d_bands_per_hole": MAX_INTERVAL_BANDS_PER_HOLE,
        "strip_bands_per_hole_and_kind": STRIP_BANDS_PER_KIND_LIMIT,
    }

    # 2. Gold bands vs silver logs --------------------------------------------------------------
    async def _kinds() -> dict[str, Any]:
        rows = await conn.fetch(_GOLD_KINDS_SQL, project.project_id)
        return {"kinds": {str(r["kind"]): {"rows": int(r["n_rows"]), "holes": int(r["holes"])} for r in rows}}

    out["gold_intervals_by_kind"] = await guarded(conn, [vis], _kinds, what="visibility:gold_kinds")

    silver: dict[str, Any] = {}
    for key, table in _SILVER_LOG_TABLES:

        async def _count(table: str = table) -> dict[str, Any]:
            row = await conn.fetchrow(
                f"SELECT count(*) AS n_rows, count(DISTINCT t.collar_id) AS holes "  # noqa: S608
                f"FROM {table} t JOIN silver.collars c ON c.collar_id = t.collar_id WHERE c.project_id = $1::uuid",
                project.project_id,
            )
            return {"rows": _int(row, "n_rows"), "holes": _int(row, "holes")}

        silver[key] = await guarded(conn, [table, collars], _count, what=f"visibility:silver:{key}")
    out["silver_logs"] = silver

    for key, table, alias in (
        ("lithology_logs_without_gold_bands", "silver.lithology_logs", "lg"),
        ("canonical_lithology_without_gold_bands", "silver.lithology", "lc"),
    ):

        async def _missing(table: str = table, alias: str = alias) -> dict[str, Any]:
            where = (
                f"EXISTS (SELECT 1 FROM {table} {alias} WHERE {alias}.collar_id = c.collar_id) "  # noqa: S608
                f"AND NOT {_HAS_LITHOLOGY_BANDS}"
            )
            return await _hole_list(conn, where, project)

        out[key] = await guarded(conn, [table, collars, vis], _missing, what=f"visibility:{key}")

    dropped: dict[str, Any] = {}
    for key, table in _SILVER_LOG_TABLES[:2]:

        async def _drop(table: str = table) -> dict[str, Any]:
            row = await conn.fetchrow(_dropped_rows_sql(table), project.project_id)
            keys = ("n_rows", "null_depth", "to_not_after_from", "negative_from", "to_depth_overflow", "dropped_any")
            return {("rows" if k == "n_rows" else k): _int(row, k) for k in keys}

        dropped[key] = await guarded(conn, [table, collars], _drop, what=f"visibility:dropped:{key}")
    out["lithology_rows_promotion_drops"] = dropped

    # 3. Structures -----------------------------------------------------------------------------
    async def _structure() -> dict[str, Any]:
        row = await conn.fetchrow(_STRUCTURE_SQL, project.project_id)
        keys = ("n_rows", "null_true_dip", "null_true_dip_dir", "unusable_in_3d", "null_depth")
        res = {("rows" if k == "n_rows" else k): _int(row, k) for k in keys}
        return {**res, "over_3d_cap": res["rows"] > THREE_D_ROW_CAP, "cap": THREE_D_ROW_CAP}

    async def _structure_visual() -> dict[str, Any]:
        row = await conn.fetchrow(_STRUCTURE_VISUAL_SQL, project.project_id)
        n = _int(row, "n_rows")
        return {"rows": n, "null_depth": _int(row, "null_depth"), "over_3d_cap": n > THREE_D_ROW_CAP, "cap": THREE_D_ROW_CAP}

    out["structure"] = await guarded(conn, ["silver.structure", collars], _structure, what="visibility:structure")
    out["structure_measurements_visual"] = await guarded(
        conn, ["gold.structure_measurements_visual"], _structure_visual, what="visibility:structure_visual"
    )

    # 4. Samples and the gold tables with no writer --------------------------------------------
    async def _samples() -> dict[str, Any]:
        row = await conn.fetchrow(_SAMPLES_SQL, project.project_id)
        res = {
            "rows": _int(row, "n_rows"),
            "null_assays": _int(row, "null_assays"),
            "empty_object": _int(row, "empty_object"),
            "non_empty": _int(row, "non_empty"),
        }
        # The 3D payload takes WHERE commodity_assays IS NOT NULL ... LIMIT 5000.
        loaded = res["empty_object"] + res["non_empty"]
        return {**res, "non_null": loaded, "cap": THREE_D_ROW_CAP, "over_3d_cap": loaded > THREE_D_ROW_CAP}

    out["samples"] = await guarded(conn, ["silver.samples", collars], _samples, what="visibility:samples")

    writerless: dict[str, Any] = {}
    for key, table in (
        ("assay_composites", "gold.assay_composites"),
        ("significant_intersections", "gold.significant_intersections"),
    ):

        async def _rows(table: str = table) -> dict[str, Any]:
            n = _int(await conn.fetchrow(_gold_rowcount_sql(table), project.project_id), "n_rows")
            return {"rows": n, "note": "no writer in the codebase" if n == 0 else None}

        writerless[key] = await guarded(conn, [table, collars], _rows, what=f"visibility:{key}")
    out["gold_tables_without_writer"] = writerless

    # 5. The LOGS / SECTION hole picker ---------------------------------------------------------
    async def _absent_from_picker() -> dict[str, Any]:
        return await _hole_list(conn, f"{_NO_CURVES_AT_ALL} AND NOT {_HAS_PICKER_BANDS}", project)

    async def _hidden_by_cap() -> dict[str, Any]:
        return {
            "count": int(await conn.fetchval(_PICKER_HIDDEN_SQL, project.project_id) or 0),
            "cap": MAX_WORKSPACE_COLLARS,
        }

    picker = ["silver.well_log_curves", collars, vis]
    out["absent_from_logs_picker"] = await guarded(conn, picker, _absent_from_picker, what="visibility:picker")
    out["picker_hidden_by_collar_cap"] = await guarded(conn, picker, _hidden_by_cap, what="visibility:picker_cap")
    return out


# --- findings: facts only, shared by the headlines and the corpus overview ----------------------


def _count_of(res: Any) -> int:
    return int(res["count"]) if _sub_ok(res) else 0


def placement_findings(r: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for key, text in (
        ("null_easting_or_northing", "collar(s) have NULL easting or northing (the 3D view cannot place them)"),
        (
            "degree_looking_easting_northing",
            "collar(s) have degree-looking easting/northing (|easting|<=180, |northing|<=90): the 3D view reads "
            "them as metres and puts them at the scene origin",
        ),
        ("null_elevation", "collar(s) have no elevation from the file or the terrain model (3D plots them at z=0)"),
        ("no_orientation_and_no_surveys", "hole(s) have no surveys and no collar azimuth/dip (no trace, drawn vertical in 3D)"),
    ):
        if _count_of(r.get(key)) > 0:
            out.append(f"{r[key]['count']} {text}.")
    off = r.get("easting_northing_vs_geom_4326", {})
    if _sub_ok(off) and off.get("over_threshold"):
        out.append(
            f"{off['over_threshold']} of {off['compared']} collar(s) have easting/northing more than "
            f"{off['threshold_m']:g} m from geom_4326 in EPSG:{off['project_crs_epsg']} (max {off['max_m']} m): "
            "the 2D map and the 3D view disagree about where they are."
        )
    ext = r.get("extents", {}).get("tables", {})
    far = [k for k, v in ext.items() if _sub_ok(v) and v.get("far_from_collars")]
    if far:
        out.append(
            f"Map layer(s) more than {r['extents']['far_km_threshold']:g} km from the collar centroid: "
            + ", ".join(f"`{k}` ({ext[k]['km_from_collar_centroid']} km)" for k in far)
        )
    outside = [k for k, v in ext.items() if _sub_ok(v) and v.get("outside_wgs84")]
    if outside:
        out.append(
            "Geometry outside valid WGS84 (lng -180..180, lat -90..90): "
            + ", ".join(f"`{k}` x{ext[k]['outside_wgs84']}" for k in outside)
        )
    null_geom = [k for k, v in ext.items() if _sub_ok(v) and v.get("null_or_empty_geometry")]
    if null_geom:
        out.append(
            "NULL or empty geometry rows: " + ", ".join(f"`{k}` x{ext[k]['null_or_empty_geometry']}" for k in null_geom)
        )
    tr = r.get("trace_start_vs_collar", {})
    if _sub_ok(tr) and tr.get("over_threshold"):
        out.append(
            f"{tr['over_threshold']} of {tr['traces']} drill trace(s) start more than {tr['threshold_m']:g} m from "
            f"their collar (max {tr['max_m']} m)."
        )
    sv = r.get("surveys", {})
    st = sv.get("stations", {})
    if _sub_ok(st):
        if st["dropped_by_desurvey"]:
            out.append(f"{st['dropped_by_desurvey']} survey station(s) have a NULL azimuth or dip (skipped by the desurvey).")
        if st["dip_out_of_range"]:
            out.append(f"{st['dip_out_of_range']} survey station(s) have a dip outside -90..90 (skipped by the desurvey).")
    if _count_of(sv.get("all_dips_positive")) > 0:
        out.append(
            f"{sv['all_dips_positive']['count']} hole(s) have ONLY positive dips: drawn as up-holes "
            "(dip is negative-down here) — a sign-convention suspect."
        )
    if _count_of(sv.get("over_station_cap")) > 0:
        out.append(
            f"{sv['over_station_cap']['count']} hole(s) have more than {MAX_SURVEY_STATIONS_PER_HOLE} survey "
            "stations (thinned in 3D)."
        )
    if _count_of(sv.get("multiple_source_files")) > 0:
        out.append(
            f"{sv['multiple_source_files']['count']} hole(s) have survey stations from more than one source file "
            "(each trace uses the most recently written file only)."
        )
    return out


def visibility_findings(r: dict[str, Any]) -> list[str]:
    out: list[str] = []
    cap = r.get("collar_cap", {})
    if _sub_ok(cap) and cap["beyond_cap"]:
        out.append(
            f"{cap['total']} collars: {cap['beyond_cap']} are beyond the Workspace cap of {cap['cap']} and are not "
            "on the page."
        )
    first = r.get("first_holes_lithology", {})
    if _sub_ok(first) and first["three_d_empty_trap"]:
        out.append(
            f"3D lithology will be EMPTY: none of the first {first['first_n']} collars by hole_id has a gold "
            f"lithology band, but {first['project_holes_with_lithology_bands']} hole(s) in the project do."
        )
    if _count_of(r.get("holes_over_3d_band_cap")) > 0:
        out.append(
            f"{r['holes_over_3d_band_cap']['count']} hole(s) have more than {MAX_INTERVAL_BANDS_PER_HOLE} "
            "lithology bands (3D shows the first 80)."
        )
    if _count_of(r.get("holes_over_strip_band_limit")) > 0:
        out.append(
            f"{r['holes_over_strip_band_limit']['count']} hole/kind pair(s) have more than "
            f"{STRIP_BANDS_PER_KIND_LIMIT} bands."
        )
    for key in ("lithology_logs_without_gold_bands", "canonical_lithology_without_gold_bands"):
        if _count_of(r.get(key)) > 0:
            label = "silver.lithology_logs" if key.startswith("lithology_logs") else "silver.lithology"
            out.append(f"{r[key]['count']} hole(s) have `{label}` rows but no gold lithology band.")
    for key, res in r.get("lithology_rows_promotion_drops", {}).items():
        if _sub_ok(res) and res["dropped_any"]:
            out.append(
                f"{res['dropped_any']} of {res['rows']} `silver.{key}` row(s) are dropped by the promotion "
                "(NULL depth, to<=from, from<0 or to>=10,000,000)."
            )
    st = r.get("structure", {})
    if _sub_ok(st):
        if st["unusable_in_3d"]:
            out.append(f"{st['unusable_in_3d']} of {st['rows']} `silver.structure` row(s) lack true_dip or true_dip_dir (not in 3D).")
        if st["over_3d_cap"]:
            out.append(f"`silver.structure` has {st['rows']} rows: 3D loads {st['cap']}.")
    sv = r.get("structure_measurements_visual", {})
    if _sub_ok(sv) and sv["over_3d_cap"]:
        out.append(f"`gold.structure_measurements_visual` has {sv['rows']} rows: 3D loads {sv['cap']}.")
    sm = r.get("samples", {})
    if _sub_ok(sm) and sm["over_3d_cap"]:
        out.append(f"`silver.samples` has {sm['non_null']} rows with commodity_assays: 3D loads {sm['cap']}.")
    for key, res in r.get("gold_tables_without_writer", {}).items():
        if _sub_ok(res) and res["rows"] == 0:
            out.append(f"`gold.{key}` is empty for this project (no writer in the codebase).")
    if _count_of(r.get("absent_from_logs_picker")) > 0:
        out.append(
            f"{r['absent_from_logs_picker']['count']} hole(s) are absent from the LOGS picker (no curves and no "
            "gold lithology/alteration/mineralization bands)."
        )
    if _sub_ok(r.get("picker_hidden_by_collar_cap")) and r["picker_hidden_by_collar_cap"]["count"]:
        out.append(
            f"{r['picker_hidden_by_collar_cap']['count']} hole(s) with bands but no curves sit beyond the "
            f"{MAX_WORKSPACE_COLLARS}-collar cap, so the picker does not list them."
        )
    return out


@dataclass(frozen=True)
class CheckSpec:
    key: str
    title: str
    requires: tuple[str, ...]
    fn: Callable[[Any, Project], Awaitable[dict[str, Any]]]


CHECKS: tuple[CheckSpec, ...] = (
    CheckSpec(
        "ingest_progress", "Ingest runs (silver.ingest_progress)", ("silver.ingest_progress",), check_ingest_progress
    ),
    CheckSpec("row_counts", "Row counts", (), check_row_counts),
    CheckSpec(
        "attribute_tables", "Attribute tables (standalone .dbf)", ("silver.attribute_tables",), check_attribute_tables
    ),
    CheckSpec("collars", "Collars", ("silver.collars",), check_collars),
    CheckSpec("coverage", "Per-hole coverage", ("silver.collars",), check_coverage),
    CheckSpec("curves", "Well-log curves by name", ("silver.well_log_curves", "silver.collars"), check_curves),
    CheckSpec("derived", "Derived (DERIVED-%) vs logged lithology", (), check_derived),
    CheckSpec(
        "archive_runs", "Archive runs (silver.archive_ingest_runs)", ("silver.archive_ingest_runs",), check_archive_runs
    ),
    CheckSpec(
        "documents",
        "Documents, passages and embeddings (silver.reports / silver.document_passages)",
        ("silver.reports", "silver.document_passages"),
        check_documents,
    ),
    CheckSpec("placement", "Where the data lands (placement, extents, traces, surveys)", ("silver.collars",), check_placement),
    CheckSpec("visibility", "Will the UI show it (caps, gold bands, picker)", ("silver.collars",), check_visibility),
)
CHECK_KEYS: tuple[str, ...] = tuple(c.key for c in CHECKS)


async def run_checks(
    conn: Any, project: Project, only: Sequence[str] | None = None
) -> dict[str, dict[str, Any]]:
    """Every check, or only the keys in ``only`` (unknown keys are ignored here — the
    argument parser has already refused them)."""
    results: dict[str, dict[str, Any]] = {}
    for spec in CHECKS:
        if only and spec.key not in only:
            continue

        async def _go(spec: CheckSpec = spec) -> dict[str, Any]:
            return await spec.fn(conn, project)

        res = await guarded(conn, spec.requires, _go, what=spec.key)
        res["title"] = spec.title
        results[spec.key] = res
    return results


# --------------------------------------------------------------------------
# Headlines (facts only — no diagnosis)
# --------------------------------------------------------------------------


def _sub_ok(d: Any) -> bool:
    return isinstance(d, dict) and d.get("status") == "ok"


def errored_paths(node: Any, path: str = "") -> list[str]:
    """Dotted paths of every check or sub-check whose status is 'error' (nested sub-checks
    such as collars.wyoming_fallback_box count, not just the top level)."""
    found: list[str] = []
    if isinstance(node, dict):
        if node.get("status") == "error":
            found.append(path)
        for key, value in node.items():
            if isinstance(value, dict):
                found += errored_paths(value, f"{path}.{key}" if path else key)
    return found


def headlines(checks: dict[str, dict[str, Any]]) -> list[str]:
    out: list[str] = []
    counts = checks.get("row_counts", {})
    if counts.get("status") == "ok":
        zero = [t for t, r in counts["tables"].items() if r.get("status") == "ok" and r["rows"] == 0]
        absent = [t for t, r in counts["tables"].items() if r.get("status") == "table_absent"]
        if zero:
            out.append("Empty for this project: " + ", ".join(f"`{t}`" for t in zero))
        if absent:
            out.append("Table absent in this database: " + ", ".join(f"`{t}`" for t in absent))
    prog = checks.get("ingest_progress", {})
    if prog.get("status") == "ok":
        bad = {s: n for s, n in prog["by_status"].items() if s in ("failed", "partial", "timed_out")}
        if bad:
            out.append("Ingest runs needing attention: " + ", ".join(f"{s} x{n}" for s, n in bad.items()))
        if prog["watched_codes_seen"]:
            out.append(
                "Watched warning codes seen: " + ", ".join(f"`{c}` x{n}" for c, n in prog["watched_codes_seen"].items())
            )
        if prog["runs_total"] == 0:
            out.append(
                "No silver.ingest_progress rows for this project (uploads may not have been recorded against it)."
            )
    collars = checks.get("collars", {})
    if collars.get("status") == "ok":
        if collars["null_geometry"]:
            out.append(f"{collars['null_geometry']} collar(s) have NULL geometry.")
        wy = collars.get("wyoming_fallback_box", {})
        if _sub_ok(wy) and wy["count"]:
            out.append(f"{wy['count']} collar(s) sit inside the Wyoming fallback box.")
    cov = checks.get("coverage", {})
    if cov.get("status") == "ok":
        for key, label in (
            ("lithology_no_surveys", "hole(s) have lithology but no surveys"),
            ("no_trace", "hole(s) have no drill trace"),
        ):
            if _sub_ok(cov.get(key)) and cov[key]["count"]:
                out.append(f"{cov[key]['count']} {label}.")
    docs = checks.get("documents", {})
    if docs.get("status") == "ok":
        if docs["reports_total"] == 0:
            out.append("No silver.reports rows for this project: nothing was ingested as a document.")
        if docs["unembedded_total"]:
            out.append(
                f"{docs['unembedded_total']} of {docs['passages_total']} passage(s) have no embedding "
                "(embedding_id IS NULL): they are not in the vector index and cannot be retrieved."
            )
        if docs["reports_without_passages"]:
            out.append(
                f"{len(docs['reports_without_passages'])} report(s) have ZERO passages: "
                + ", ".join(f"`{f}`" for f in docs["reports_without_passages"][:10])
                + (" …" if len(docs["reports_without_passages"]) > 10 else "")
            )
        if docs["scanned_reports_without_cohere_parse"]:
            out.append(
                f"{len(docs['scanned_reports_without_cohere_parse'])} scanned report(s) have no "
                "cohere_parse passage (OCR ran on Tesseract or not at all): "
                + ", ".join(f"`{f}`" for f in docs["scanned_reports_without_cohere_parse"][:10])
                + (" …" if len(docs["scanned_reports_without_cohere_parse"]) > 10 else "")
            )
    if checks.get("placement", {}).get("status") == "ok":
        out += [f"[placement] {f}" for f in placement_findings(checks["placement"])]
    if checks.get("visibility", {}).get("status") == "ok":
        out += [f"[visibility] {f}" for f in visibility_findings(checks["visibility"])]
    errored = errored_paths(checks)
    if errored:
        out.append("Checks that ERRORED (see below): " + ", ".join(f"`{k}`" for k in errored))
    return out


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def _status_line(res: dict[str, Any]) -> str | None:
    if res.get("status") == "table_absent":
        return "**table absent:** " + ", ".join(f"`{t}`" for t in res.get("missing_tables", []))
    if res.get("status") == "error":
        return f"**check failed:** `{_md_cell(res.get('error'))}`"
    return None


def _render_sub(label: str, res: dict[str, Any]) -> list[str] | None:
    """A sub-result's failure line, or None when it is ok."""
    line = _status_line(res)
    return [f"- {label}: {line}"] if line else None


def _render_ingest_progress(r: dict[str, Any]) -> list[str]:
    lines = [
        f"{r['runs_total']} run row(s) for this project; last {r['rows_shown']} shown (limit {r['rows_limit']}); "
        f"{r['runs_with_zero_rows_written']} of the shown rows wrote 0 rows.",
        "",
        "**By status:** " + (", ".join(f"{s} x{n}" for s, n in r["by_status"].items()) or "none"),
        "",
        "**By warning code (whole project):** "
        + (", ".join(f"`{c}` x{n}" for c, n in r["by_warning_code"].items()) or "none"),
        "",
        "**Watched codes seen:** "
        + (", ".join(f"`{c}` x{n}" for c, n in r["watched_codes_seen"].items()) or "none")
        + "  |  **not seen:** "
        + (", ".join(f"`{c}`" for c in r["watched_codes_not_seen"]) or "none"),
        "",
    ]
    lines += md_table(
        ["file", "status", "step", "rows", "try", "started", "completed", "warning codes", "error (<=300 chars)"],
        [
            (
                x["filename"],
                x["status"],
                x["current_step"],
                x["rows_written"],
                x["attempt_number"],
                x["started_at"],
                x["completed_at"] or x["failed_at"],
                ", ".join(x["warning_codes"]),
                x["error_text"],
            )
            for x in r["runs"]
        ],
    )
    return lines


def _render_row_counts(r: dict[str, Any]) -> list[str]:
    rows = []
    for table, res in r["tables"].items():
        if res["status"] == "ok":
            rows.append((f"`{table}`", res["rows"], ""))
        elif res["status"] == "table_absent":
            rows.append((f"`{table}`", "", "table absent"))
        else:
            rows.append((f"`{table}`", "", "ERROR: " + str(res.get("error"))))
    return md_table(["table", "rows", "note"], rows)


def _render_attribute_tables(r: dict[str, Any]) -> list[str]:
    lines = [f"{r['groups_total']} source file / layer group(s); showing up to {r['groups_limit']}. {r['note']}.", ""]
    lines += md_table(
        ["source file", "layer", "sha256", "rows", "column names"],
        [
            (g["source_file"], g["source_layer"], g["sha256_prefix"], g["row_count"], ", ".join(g["column_names"]))
            for g in r["groups"]
        ],
    )
    return lines


def _render_collars(r: dict[str, Any]) -> list[str]:
    lines = [
        f"- collars: **{r['total']}**  |  NULL geometry: **{r['null_geometry']}**  |  NULL hole_id_canonical: {r['null_hole_id_canonical']}",
    ]
    geo = r["georef_method"]
    lines += _render_sub("georef_method", geo) or [
        "- georef_method: " + (", ".join(f"{k} x{v}" for k, v in geo["breakdown"].items()) or "none")
    ]
    dup = r["duplicates"]
    lines += _render_sub("duplicates", dup) or [
        f"- duplicate groups by hole_id_canonical: **{dup['by_hole_id_canonical']['groups']}** "
        f"(showing up to {DUPLICATE_LIMIT}): "
        + (", ".join(f"`{d['hole_id']}` x{d['rows']}" for d in dup["by_hole_id_canonical"]["hole_ids"]) or "-"),
        f"- duplicate groups by punctuation-insensitive hole_id: **{dup['by_normalised_hole_id']['groups']}**: "
        + (", ".join(f"`{d['hole_id']}` x{d['rows']}" for d in dup["by_normalised_hole_id"]["hole_ids"]) or "-"),
    ]
    wy = r["wyoming_fallback_box"]
    lines += _render_sub("Wyoming fallback box", wy) or [
        f"- inside the Wyoming fallback box (lng {WYOMING_BOX[0]}..{WYOMING_BOX[2]}, lat {WYOMING_BOX[1]}..{WYOMING_BOX[3]}): "
        f"**{wy['count']}** (showing up to {ID_LIST_LIMIT}): {_ids(wy['hole_ids'])}"
    ]
    return lines


def _render_coverage(r: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    labels = {
        "holes_total": "holes in silver.collars",
        "lithology_no_surveys": "holes with lithology but NO surveys",
        "with_curves": "holes with well-log curves",
        "no_trace": "holes with NO drill trace",
        "surveys_no_trace": "holes with surveys but NO drill trace",
        "lithology_no_trace": "holes with lithology but NO drill trace",
    }
    for key, label in labels.items():
        res = r[key]
        failure = _render_sub(label, res)
        if failure:
            lines += failure
        elif key == "holes_total":
            lines.append(f"- {label}: **{res['count']}**")
        else:
            lines.append(f"- {label}: **{res['count']}** (showing up to {res['limit']}): {_ids(res['hole_ids'])}")
    tq = r["trace_quality"]
    lines += _render_sub("trace_quality", tq) or [
        "- trace_quality: " + (", ".join(f"{k} x{v['count']}" for k, v in tq["buckets"].items()) or "no traces")
    ]
    if _sub_ok(tq):
        for bucket, v in tq["buckets"].items():
            lines.append(f"  - `{bucket}` ({v['count']}, up to {v['limit']} shown): {_ids(v['hole_ids'])}")
    return lines


def _render_curves(r: dict[str, Any]) -> list[str]:
    lines = [
        f"{r['curve_names_total']} distinct curve name(s); {r['curve_rows_total']} curve row(s) in the {len(r['curves'])} shown.",
        "",
    ]
    lines += md_table(
        ["curve_name", "curves", "holes"], [(c["curve_name"], c["curves"], c["holes"]) for c in r["curves"]]
    )
    return lines


def _render_derived(r: dict[str, Any]) -> list[str]:
    rows = []
    for label, res in (
        ("silver.lithology_logs", r["silver_lithology_logs"]),
        ("gold.drillhole_intervals_visual (lithology)", r["gold_drillhole_intervals_visual"]),
    ):
        line = _status_line(res)
        if line:
            rows.append((f"`{label}`", "", "", "", "", line))
        else:
            rows.append(
                (
                    f"`{label}`",
                    res["derived"]["intervals"],
                    res["derived"]["holes"],
                    res["logged"]["intervals"],
                    res["logged"]["holes"],
                    "",
                )
            )
    lines = md_table(["table", "DERIVED intervals", "DERIVED holes", "logged intervals", "logged holes", "note"], rows)
    silver = r["silver_lithology_logs"]
    if _sub_ok(silver) and silver["derived_codes"]:
        lines += ["", "DERIVED codes: " + ", ".join(f"`{c}` x{n}" for c, n in silver["derived_codes"].items())]
    return lines


def _render_archive_runs(r: dict[str, Any]) -> list[str]:
    lines = [
        f"{len(r['runs'])} run(s) shown (limit {r['runs_limit']}). By status: "
        + (", ".join(f"{s} x{n}" for s, n in r["by_status"].items()) or "none"),
        "",
    ]
    lines += md_table(
        ["archive", "status", "files", "ok", "failed", "skipped", "started", "finished", "error (<=300 chars)"],
        [
            (
                x["filename"],
                x["status"],
                x["file_count"],
                x["files_succeeded"],
                x["files_failed"],
                x["files_skipped"],
                x["started_at"],
                x["completed_at"] or x["failed_at"],
                x["error_text"],
            )
            for x in r["runs"]
        ],
    )
    return lines


def _render_documents(r: dict[str, Any]) -> list[str]:
    lines = [
        f"{r['reports_total']} report(s) ({len(r['reports'])} shown, limit {r['reports_limit']}), "
        f"{r['scanned_reports']} scanned. {r['passages_total']} passage(s): {r['embedded_total']} embedded, "
        f"{r['unembedded_total']} NOT embedded, {r['image_passages_total']} page image(s). "
        "By OCR method: "
        + (", ".join(f"{m} x{n}" for m, n in sorted(r["by_ocr_method"].items())) or "none"),
        "",
    ]
    legacy = r.get("legacy_ocr_results", {})
    if legacy.get("status") == "ok":
        lines.append(
            f"- legacy silver.ingest_ocr_results: {legacy['rows']} row(s) across {legacy['reports']} report(s)"
        )
    elif (sub := _render_sub("legacy silver.ingest_ocr_results", legacy)) is not None:
        lines += sub
    lines.append("")
    lines += md_table(
        [
            "file",
            "pages",
            "scanned",
            "parser",
            "quality %",
            "text cov. %",
            "passages",
            "images",
            "embedded",
            "not embedded",
            "cohere_parse",
            "tesseract",
            "low conf.",
            "created",
        ],
        [
            (
                x["source_file"] or x["report_id"],
                x["page_count"],
                x["is_scanned"],
                x["parser_used"],
                x["parse_quality_pct"],
                x["text_page_coverage_pct"],
                x["passages"],
                x["image_passages"],
                x["embedded"],
                x["unembedded"],
                x["cohere_parse_passages"],
                x["tesseract_passages"],
                x["low_confidence"],
                x["created_at"],
            )
            for x in r["reports"]
        ],
    )
    if r["rollup"]:
        lines += ["", "Passages by modality / chunk_kind / ocr_method (whole project):", ""]
        lines += md_table(
            ["modality", "chunk_kind", "ocr_method", "rows", "embedded"],
            [(x["modality"], x["chunk_kind"], x["ocr_method"], x["rows"], x["embedded"]) for x in r["rollup"]],
        )
    return lines


def _render_hole_list(label: str, res: dict[str, Any]) -> list[str]:
    failure = _render_sub(label, res)
    if failure:
        return failure
    return [f"- {label}: **{res['count']}** (showing up to {res['limit']}): {_ids(res['hole_ids'])}"]


def _render_grouped(label: str, res: dict[str, Any]) -> list[str]:
    failure = _render_sub(label, res)
    if failure:
        return failure
    shown = ", ".join(
        f"`{h['hole_id']}`" + (f" ({h['kind']})" if "kind" in h else "") + f" x{h['n']}" for h in res["holes"]
    )
    return [f"- {label}: **{res['count']}** (showing up to {res['limit']}): {shown or '-'}"]


def _render_distances(res: dict[str, Any]) -> list[str]:
    shown = ", ".join(f"`{h['hole_id']}` {h['distance_m']} m" for h in res["holes"])
    return [f"  - worst first (up to {res['limit']}): {shown or '-'}"]


def _render_placement(r: dict[str, Any]) -> list[str]:
    lines = ["**Collar placement inputs** (the 3D view uses raw easting/northing as metres; elevation falls back to the terrain model, then to z 0)", ""]
    for key, label in (
        ("null_easting_or_northing", "collars with NULL easting or northing"),
        ("degree_looking_easting_northing", "collars with degree-looking easting/northing (|e|<=180, |n|<=90)"),
        ("null_elevation", "collars with no elevation from the file or the terrain model"),
        ("terrain_elevation", "collars at the terrain model's ground height (no elevation in the file)"),
        ("no_orientation_and_no_surveys", "holes with NULL collar azimuth or dip AND no silver.surveys rows"),
    ):
        lines += _render_hole_list(label, r[key])
    off = r["easting_northing_vs_geom_4326"]
    failure = _render_sub("easting/northing vs geom_4326", off)
    if failure:
        lines += failure
    elif "skipped" in off:
        lines.append(f"- easting/northing vs geom_4326: not compared ({off['skipped']})")
    else:
        lines.append(
            f"- easting/northing vs geom_4326 re-projected to EPSG:{off['project_crs_epsg']}: "
            f"{off['compared']} collar(s) compared, **{off['over_threshold']}** more than {off['threshold_m']:g} m apart, "
            f"max {off['max_m']} m"
            + ("" if off.get("linear_unit_is_metre", True) else " (the CRS's linear unit is NOT the metre)")
        )
        if off["holes"]:
            lines += _render_distances(off)

    ext = r["extents"]
    lines += ["", "**Map extents** (WGS84; each table scoped by its own project_id)", ""]
    rows = []
    for key, res in ext["tables"].items():
        if res["status"] != "ok":
            rows.append((f"`{key}`", "", "", "", "", "", "", _status_line(res)))
            continue
        bbox = res["bbox_lng_lat"]
        rows.append(
            (
                f"`{key}`",
                res["rows"],
                res["null_or_empty_geometry"],
                res["outside_wgs84"],
                ",".join(str(s) for s in res["srids"]),
                "" if bbox is None else "{} {} .. {} {}".format(*bbox),
                "" if res["centroid_lng_lat"] is None else "{} {}".format(*res["centroid_lng_lat"]),
                "" if res["km_from_collar_centroid"] is None else f"{res['km_from_collar_centroid']} km"
                + (f" **> {ext['far_km_threshold']:g} km**" if res["far_from_collars"] else ""),
            )
        )
    lines += md_table(
        ["layer", "rows", "NULL/empty geom", "outside WGS84", "SRID", "bbox (min lng lat .. max lng lat)", "centroid lng lat", "from collar centroid"],
        rows,
    )
    lines += ["", f"_{ext['note']}_", "", "**Drill traces vs collars**", ""]

    tr = r["trace_start_vs_collar"]
    failure = _render_sub("trace start vs collar", tr)
    if failure:
        lines += failure
    else:
        lines.append(
            f"- {tr['traces']} trace(s); start more than {tr['threshold_m']:g} m from the collar's geom_4326: "
            f"**{tr['over_threshold']}**, max {tr['max_m']} m; {tr['no_collar_position']} with a collar that has no geom_4326"
        )
        if tr["holes"]:
            lines += _render_distances(tr)
        lines.append(f"- {tr['note']}")

    sv = r["surveys"]
    lines += ["", "**Surveys quality**", "", f"- dip convention used: {sv['dip_convention']}"]
    st = sv["stations"]
    lines += _render_sub("survey stations", st) or [
        f"- {st['stations']} station(s) on {st['holes']} hole(s): NULL azimuth {st['null_azimuth']}, NULL dip "
        f"{st['null_dip']} ({st['dropped_by_desurvey']} skipped by the desurvey), dip outside -90..90: "
        f"**{st['dip_out_of_range']}**, azimuth outside 0..360: {st['azimuth_out_of_range']}, "
        f"up-hole (0 < dip <= 90): {st['up_hole_stations']} station(s) on {st['up_hole_holes']} hole(s)"
    ]
    lines += _render_grouped(
        "holes whose every dip is positive (suspect positive-down file; stations)", sv["all_dips_positive"]
    )
    lines += _render_grouped(f"holes with more than {sv['station_cap_in_3d']} stations (stations)", sv["over_station_cap"])
    lines += _render_grouped("holes whose surveys come from more than one source_file (files)", sv["multiple_source_files"])
    return lines


def _render_visibility(r: dict[str, Any]) -> list[str]:
    lines: list[str] = ["**The caps**", ""]
    cap = r["collar_cap"]
    lines += _render_sub("collars", cap) or [
        f"- collars: **{cap['total']}**, Workspace cap {cap['cap']}, beyond the cap: **{cap['beyond_cap']}**"
    ]
    first = r["first_holes_lithology"]
    lines += _render_sub("first holes", first) or [
        f"- of the first {first['first_n']} collars by hole_id ({first['holes_considered']} found): "
        f"**{first['first_n_with_lithology_bands']}** have gold lithology bands; project-wide "
        f"**{first['project_holes_with_lithology_bands']}** hole(s) do"
        + (" — **the 3D lithology view will be EMPTY**" if first["three_d_empty_trap"] else "")
    ]
    bands = r["band_caps"]
    lines += _render_grouped(
        f"holes with more than {bands['three_d_bands_per_hole']} lithology bands (bands)", r["holes_over_3d_band_cap"]
    )
    lines += _render_grouped(
        f"holes with more than {bands['strip_bands_per_hole_and_kind']} bands of one kind (bands)",
        r["holes_over_strip_band_limit"],
    )

    lines += ["", "**gold.drillhole_intervals_visual by interval_kind**", ""]
    kinds = r["gold_intervals_by_kind"]
    lines += _render_sub("gold.drillhole_intervals_visual", kinds) or (
        md_table(["interval_kind", "rows", "holes"], [(k, v["rows"], v["holes"]) for k, v in kinds["kinds"].items()])
        if kinds["kinds"]
        else ["- no rows for this project"]
    )

    lines += ["", "**Silver logs** (rows / holes)", ""]
    silver_rows = []
    for key, res in r["silver_logs"].items():
        silver_rows.append(
            (f"`silver.{key}`", "", "", _status_line(res)) if res["status"] != "ok" else (f"`silver.{key}`", res["rows"], res["holes"], "")
        )
    lines += md_table(["table", "rows", "holes", "note"], silver_rows)
    lines.append("")
    lines += _render_hole_list("holes with silver.lithology_logs but ZERO gold lithology bands", r["lithology_logs_without_gold_bands"])
    lines += _render_hole_list("holes with silver.lithology but ZERO gold lithology bands", r["canonical_lithology_without_gold_bands"])
    for key, res in r["lithology_rows_promotion_drops"].items():
        failure = _render_sub(f"silver.{key} rows the promotion drops", res)
        lines += failure or [
            f"- `silver.{key}` rows the promotion drops: **{res['dropped_any']}** of {res['rows']} "
            f"(NULL depth {res['null_depth']}, to<=from {res['to_not_after_from']}, from<0 {res['negative_from']}, "
            f"to>=10,000,000 {res['to_depth_overflow']})"
        ]

    lines += ["", "**Structures**", ""]
    st = r["structure"]
    lines += _render_sub("silver.structure", st) or [
        f"- `silver.structure`: {st['rows']} row(s); NULL true_dip {st['null_true_dip']}, NULL true_dip_dir "
        f"{st['null_true_dip_dir']} ({st['unusable_in_3d']} not drawn in 3D), NULL depth {st['null_depth']}"
        + (f"; **over the 3D cap of {st['cap']}**" if st["over_3d_cap"] else "")
    ]
    sv = r["structure_measurements_visual"]
    lines += _render_sub("gold.structure_measurements_visual", sv) or [
        f"- `gold.structure_measurements_visual`: {sv['rows']} row(s), NULL depth {sv['null_depth']}"
        + (f"; **over the 3D cap of {sv['cap']}**" if sv["over_3d_cap"] else "")
    ]

    lines += ["", "**Samples and assay tables**", ""]
    sm = r["samples"]
    lines += _render_sub("silver.samples", sm) or [
        f"- `silver.samples`: {sm['rows']} row(s); commodity_assays NULL {sm['null_assays']}, '{{}}' "
        f"{sm['empty_object']}, non-empty {sm['non_empty']}"
        + (f"; **{sm['non_null']} non-NULL, over the 3D cap of {sm['cap']}**" if sm["over_3d_cap"] else "")
    ]
    for key, res in r["gold_tables_without_writer"].items():
        lines += _render_sub(f"gold.{key}", res) or [
            f"- `gold.{key}`: {res['rows']} row(s)" + (f" ({res['note']})" if res["note"] else "")
        ]

    lines += ["", "**LOGS / SECTION hole picker**", ""]
    lines += _render_hole_list(
        "holes absent from the picker (no well_log_curves and no gold lithology/alteration/mineralization bands)",
        r["absent_from_logs_picker"],
    )
    hid = r["picker_hidden_by_collar_cap"]
    lines += _render_sub("picker cap", hid) or [
        f"- holes with bands but no curves beyond the {hid['cap']}-collar cap (not listed): **{hid['count']}**"
    ]
    return lines


_RENDERERS: dict[str, Callable[[dict[str, Any]], list[str]]] = {
    "ingest_progress": _render_ingest_progress,
    "row_counts": _render_row_counts,
    "attribute_tables": _render_attribute_tables,
    "collars": _render_collars,
    "coverage": _render_coverage,
    "curves": _render_curves,
    "derived": _render_derived,
    "archive_runs": _render_archive_runs,
    "documents": _render_documents,
    "placement": _render_placement,
    "visibility": _render_visibility,
}


def render_markdown(result: dict[str, Any]) -> str:
    meta = result["meta"]
    lines = ["# Project data diagnostics", ""]
    lines.append(f"- requested slug: `{meta['project_slug']}`  |  read-only  |  generated {meta['generated_at']}")
    project = result.get("project")
    if project is None:
        ambiguous = meta.get("ambiguous_slugs") or []
        if ambiguous:
            lines += [
                "",
                f"**MORE THAN ONE PROJECT MATCHES** `{meta['project_slug']}`. Re-run with one of these full slugs:",
                "",
            ]
            lines += [f"- `{a}`" for a in ambiguous]
            return "\n".join(lines)
        lines += [
            "",
            f"**PROJECT NOT FOUND OR NOT VISIBLE.** No silver.projects row with slug `{meta['project_slug']}` "
            f"(or `{meta['project_slug']}-` plus the 8-character suffix a new project gets) was visible "
            f"(tried: {', '.join(meta['scopes_tried'])}). Check the slug (Projects page URL) and that RLS is not hiding it.",
        ]
        return "\n".join(lines)

    lines += ["", "## 0. Project", ""]
    detail_rows = [(k, v) for k, v in project.items() if k not in ("columns_missing",)]
    lines += md_table(["field", "value"], [(k, "" if v is None else v) for k, v in detail_rows])
    if project.get("columns_missing"):
        lines.append("")
        lines.append("_silver.projects has no column: " + ", ".join(project["columns_missing"]) + "_")

    heads = result["headlines"]
    lines += ["", "## Headlines", ""]
    lines += [f"- {h}" for h in heads] or ["- nothing stands out from the counts alone"]
    meta_line = f"{meta['checks_ok']} check(s) ok, {meta['checks_table_absent']} with a table absent, {meta['checks_errored']} errored"
    lines += ["", f"_{meta_line}._"]

    for n, spec in enumerate(CHECKS, start=1):
        res = result["checks"].get(spec.key)
        if res is None:  # not selected by --only
            continue
        lines += ["", f"## {n}. {res['title']}", ""]
        failure = _status_line(res)
        if failure:
            lines.append(failure)
            continue
        lines += _RENDERERS[spec.key](res)
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


def _count_statuses(checks: dict[str, dict[str, Any]]) -> Counter[str]:
    return Counter(str(c.get("status")) for c in checks.values())


def build_result(
    slug: str,
    project: dict[str, Any] | None,
    scopes_tried: list[str],
    checks: dict[str, dict[str, Any]],
    ambiguous_slugs: list[str] | None = None,
) -> dict[str, Any]:
    statuses = _count_statuses(checks)
    return {
        "meta": {
            "project_slug": slug,
            "ambiguous_slugs": list(ambiguous_slugs or []),
            "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "scopes_tried": scopes_tried,
            "checks_ok": statuses["ok"],
            "checks_table_absent": statuses["table_absent"],
            "checks_errored": len(errored_paths(checks)),
            "errored_paths": errored_paths(checks),
        },
        "project": project,
        "headlines": headlines(checks) if project else [],
        "checks": checks,
    }


def emit(result: dict[str, Any]) -> None:
    """Markdown summary, then the full JSON, each between markers."""
    print(BEGIN_SUMMARY)
    print(render_markdown(result))
    print(END_SUMMARY)
    print(BEGIN_JSON)
    # indent: keeps every log line short (CloudWatch splits very long events).
    print(json.dumps(result, indent=1, default=str))
    print(END_JSON)
    sys.stdout.flush()


async def run(args: argparse.Namespace, conn: Any | None = None) -> int:
    own = conn is None
    if conn is None:
        conn = await open_readonly_connection()
    try:
        if getattr(args, "all_projects", False):
            return await run_all(args, conn)
        project, tried, ambiguous = await find_project(conn, args.project_slug)
        if project is None:
            emit(build_result(args.project_slug, None, tried, {}, ambiguous))
            if ambiguous:
                print(
                    f"PROJECT AMBIGUOUS: {args.project_slug!r} matches {', '.join(ambiguous)}; re-run with one of them.",
                    file=sys.stderr,
                )
            else:
                print(
                    f"PROJECT NOT FOUND: no silver.projects row with slug {args.project_slug!r} was visible (tried: {', '.join(tried)}).",
                    file=sys.stderr,
                )
            return 2
        details = await project_details(conn, project)
        checks = await run_checks(conn, project, getattr(args, "only", None))
        emit(build_result(args.project_slug, details, tried, checks))
        return 0
    finally:
        if own:
            await conn.close()


# --------------------------------------------------------------------------
# --all-projects: the same report for every project, behind a corpus overview
# --------------------------------------------------------------------------

_ALL_PROJECTS_SQL = """
SELECT p.project_id::text AS project_id, p.workspace_id::text AS workspace_id, p.slug
  FROM silver.projects p
 ORDER BY p.slug
"""


async def list_projects(conn: Any) -> tuple[list[Project], list[str]]:
    """Every silver.projects row. Unscoped first (bootstrap table); if RLS hides them,
    walk silver.workspaces and union what each scope can see. Leaves the session
    unscoped."""
    from app.db import bind_workspace_scope  # noqa: PLC0415

    tried = ["unscoped"]
    await conn.execute("SELECT set_config('app.workspace_id', '', false)")
    rows = list(await conn.fetch(_ALL_PROJECTS_SQL))
    if not rows:
        seen: dict[str, Any] = {}
        for ws in await conn.fetch(
            "SELECT workspace_id::text AS workspace_id FROM silver.workspaces ORDER BY workspace_id"
        ):
            await bind_workspace_scope(
                conn, workspace_id=ws["workspace_id"], site="project_data_diagnostics", is_local=False
            )
            tried.append(f"workspace {ws['workspace_id']}")
            for r in await conn.fetch(_ALL_PROJECTS_SQL):
                seen.setdefault(r["project_id"], r)
        rows = sorted(seen.values(), key=lambda r: r["slug"])
        await conn.execute("SELECT set_config('app.workspace_id', '', false)")
    return [Project(project_id=r["project_id"], workspace_id=r["workspace_id"], slug=r["slug"]) for r in rows], tried


async def diagnose_project(conn: Any, project: Project, only: Sequence[str] | None) -> dict[str, Any]:
    """Details plus checks for one already-found project, inside its workspace."""
    from app.db import bind_workspace_scope  # noqa: PLC0415

    if project.workspace_id:
        await bind_workspace_scope(
            conn, workspace_id=project.workspace_id, site="project_data_diagnostics", is_local=False
        )
    else:
        await conn.execute("SELECT set_config('app.workspace_id', '', false)")
    details = await project_details(conn, project)
    checks = await run_checks(conn, project, only)
    return build_result(project.slug, details, [], checks)


def _overview_row(result: dict[str, Any]) -> tuple[Any, ...]:
    checks = result["checks"]
    docs = checks.get("documents", {})
    prog = checks.get("ingest_progress", {})
    counts = checks.get("row_counts", {}).get("tables", {})

    def _n(res: dict[str, Any], key: str) -> Any:
        return res.get(key, "") if res.get("status") == "ok" else "?"

    bad = (
        sum(n for s, n in prog["by_status"].items() if s in ("failed", "partial", "timed_out"))
        if prog.get("status") == "ok"
        else "?"
    )
    collars = counts.get("silver.collars", {})
    return (
        result["project"]["slug"],
        _n(docs, "reports_total"),
        _n(docs, "scanned_reports"),
        _n(docs, "passages_total"),
        _n(docs, "embedded_total"),
        _n(docs, "unembedded_total"),
        _n(docs, "image_passages_total"),
        collars.get("rows", "?") if collars.get("status") == "ok" else "?",
        _n(prog, "runs_total"),
        bad,
        len(result["headlines"]),
        len(placement_findings(checks["placement"])) if checks.get("placement", {}).get("status") == "ok" else "?",
        len(visibility_findings(checks["visibility"])) if checks.get("visibility", {}).get("status") == "ok" else "?",
    )


def render_corpus_markdown(corpus: dict[str, Any]) -> str:
    meta = corpus["meta"]
    lines = ["# Corpus diagnostics (all projects)", ""]
    lines.append(
        f"- {meta['projects_total']} project(s)  |  read-only  |  generated {meta['generated_at']}"
        f"  |  checks: {', '.join(meta['checks']) }"
    )
    if not corpus["projects"]:
        lines += ["", f"**NO PROJECTS VISIBLE** (tried: {', '.join(meta['scopes_tried'])})."]
        return "\n".join(lines)
    lines += ["", "## Overview", ""]
    lines += md_table(
        [
            "project",
            "reports",
            "scanned",
            "passages",
            "embedded",
            "not embedded",
            "page images",
            "collars",
            "ingest runs",
            "runs failed/partial",
            "headlines",
            "placement findings",
            "visibility findings",
        ],
        [_overview_row(r) for r in corpus["projects"]],
    )
    totals = corpus["totals"]
    lines += [
        "",
        f"Totals: {totals['reports']} report(s), {totals['passages']} passage(s), "
        f"{totals['embedded']} embedded, **{totals['unembedded']} not embedded**, "
        f"{totals['image_passages']} page image(s).",
    ]
    for r in corpus["projects"]:
        lines += ["", "---", ""]
        # Demote the per-project headings one level under the corpus report.
        lines += [re.sub(r"^(#+)", r"\1#", line) for line in render_markdown(r).splitlines()]
    return "\n".join(lines)


def build_corpus_result(
    projects: list[dict[str, Any]], scopes_tried: list[str], only: Sequence[str] | None, projects_total: int
) -> dict[str, Any]:
    def _sum(key: str) -> int:
        return sum(
            int(r["checks"]["documents"][key])
            for r in projects
            if r["checks"].get("documents", {}).get("status") == "ok"
        )

    return {
        "meta": {
            "mode": "all_projects",
            "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "scopes_tried": scopes_tried,
            "checks": list(only) if only else list(CHECK_KEYS),
            "projects_total": projects_total,
        },
        "totals": {
            "reports": _sum("reports_total"),
            "passages": _sum("passages_total"),
            "embedded": _sum("embedded_total"),
            "unembedded": _sum("unembedded_total"),
            "image_passages": _sum("image_passages_total"),
        },
        "projects": projects,
    }


def emit_corpus(corpus: dict[str, Any]) -> None:
    print(BEGIN_SUMMARY)
    print(render_corpus_markdown(corpus))
    print(END_SUMMARY)
    print(BEGIN_JSON)
    print(json.dumps(corpus, indent=1, default=str))
    print(END_JSON)
    sys.stdout.flush()


async def run_all(args: argparse.Namespace, conn: Any) -> int:
    projects, tried = await list_projects(conn)
    results: list[dict[str, Any]] = []
    for project in projects:
        try:
            results.append(await diagnose_project(conn, project, args.only))
        except Exception as exc:  # noqa: BLE001 — one broken project must not lose the others
            logger.warning("project %s failed: %s", project.slug, _error_text(exc))
            results.append(
                build_result(
                    project.slug,
                    {"project_id": project.project_id, "workspace_id": project.workspace_id, "slug": project.slug},
                    [],
                    {"documents": {"status": "error", "error": _error_text(exc), "title": "Project failed"}},
                )
            )
    emit_corpus(build_corpus_result(results, tried, args.only, len(projects)))
    if not projects:
        print(f"NO PROJECTS VISIBLE (tried: {', '.join(tried)}).", file=sys.stderr)
        return 2
    return 0


def _slug(raw: str) -> str:
    if not _SLUG_RE.fullmatch(raw):
        raise argparse.ArgumentTypeError("must match ^[a-z0-9-]{1,64}$")
    return raw


def _only(raw: str) -> list[str]:
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    unknown = [k for k in keys if k not in CHECK_KEYS]
    if unknown or not keys:
        raise argparse.ArgumentTypeError(f"unknown check(s) {unknown}; choose from {', '.join(CHECK_KEYS)}")
    return keys


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Read-only diagnostics for one project's drill data, or for every project.")
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("--project-slug", type=_slug, help="silver.projects.slug (^[a-z0-9-]{1,64}$)")
    target.add_argument(
        "--all-projects", action="store_true", help="every silver.projects row, behind a corpus overview"
    )
    p.add_argument(
        "--only",
        type=_only,
        default=None,
        help="comma-separated check keys to run (default: all): " + ", ".join(CHECK_KEYS),
    )
    return p


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.WARNING, stream=sys.stderr, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(run(args))
    except Exception as exc:  # noqa: BLE001 — last-resort report, exit 1
        print(f"PROJECT_DATA_DIAGNOSTICS FAILED: {_error_text(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
