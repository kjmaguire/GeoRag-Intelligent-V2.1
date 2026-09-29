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


_PROJECT_KEY_SQL = """
SELECT p.project_id::text AS project_id, p.workspace_id::text AS workspace_id, p.slug
  FROM silver.projects p
 WHERE p.slug = $1
 LIMIT 1
"""


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


async def find_project(conn: Any, slug: str) -> tuple[Project | None, list[str]]:
    """Find the project by slug, then leave the session bound to its workspace.

    Returns ``(project, scopes_tried)``. Unscoped first (silver.projects is a bootstrap
    table); else walk silver.workspaces binding ``app.workspace_id`` to each."""
    from app.db import bind_workspace_scope  # noqa: PLC0415

    tried = ["unscoped"]
    await conn.execute("SELECT set_config('app.workspace_id', '', false)")
    row = await conn.fetchrow(_PROJECT_KEY_SQL, slug)
    if row is None:
        workspaces = [
            r["workspace_id"]
            for r in await conn.fetch(
                "SELECT workspace_id::text AS workspace_id FROM silver.workspaces ORDER BY workspace_id"
            )
        ]
        for ws in workspaces:
            await bind_workspace_scope(conn, workspace_id=ws, site="project_data_diagnostics", is_local=False)
            tried.append(f"workspace {ws}")
            row = await conn.fetchrow(_PROJECT_KEY_SQL, slug)
            if row is not None:
                break
    if row is None:
        await conn.execute("SELECT set_config('app.workspace_id', '', false)")
        return None, tried

    project = Project(project_id=row["project_id"], workspace_id=row["workspace_id"], slug=row["slug"])
    if project.workspace_id:
        # Every check from here on runs inside the project's own workspace.
        await bind_workspace_scope(
            conn, workspace_id=project.workspace_id, site="project_data_diagnostics", is_local=False
        )
    return project, tried


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
               count(*) FILTER (WHERE c.geom IS NULL) AS null_geom,
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
)


async def run_checks(conn: Any, project: Project) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    for spec in CHECKS:

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


_RENDERERS: dict[str, Callable[[dict[str, Any]], list[str]]] = {
    "ingest_progress": _render_ingest_progress,
    "row_counts": _render_row_counts,
    "attribute_tables": _render_attribute_tables,
    "collars": _render_collars,
    "coverage": _render_coverage,
    "curves": _render_curves,
    "derived": _render_derived,
    "archive_runs": _render_archive_runs,
}


def render_markdown(result: dict[str, Any]) -> str:
    meta = result["meta"]
    lines = ["# Project data diagnostics", ""]
    lines.append(f"- requested slug: `{meta['project_slug']}`  |  read-only  |  generated {meta['generated_at']}")
    project = result.get("project")
    if project is None:
        lines += [
            "",
            f"**PROJECT NOT FOUND OR NOT VISIBLE.** No silver.projects row with slug `{meta['project_slug']}` was visible "
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
        res = result["checks"][spec.key]
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
    slug: str, project: dict[str, Any] | None, scopes_tried: list[str], checks: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    statuses = _count_statuses(checks)
    return {
        "meta": {
            "project_slug": slug,
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
        project, tried = await find_project(conn, args.project_slug)
        if project is None:
            emit(build_result(args.project_slug, None, tried, {}))
            print(
                f"PROJECT NOT FOUND: no silver.projects row with slug {args.project_slug!r} was visible (tried: {', '.join(tried)}).",
                file=sys.stderr,
            )
            return 2
        details = await project_details(conn, project)
        checks = await run_checks(conn, project)
        emit(build_result(args.project_slug, details, tried, checks))
        return 0
    finally:
        if own:
            await conn.close()


def _slug(raw: str) -> str:
    if not _SLUG_RE.fullmatch(raw):
        raise argparse.ArgumentTypeError("must match ^[a-z0-9-]{1,64}$")
    return raw


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Read-only diagnostics for one project's drill data.")
    p.add_argument("--project-slug", type=_slug, required=True, help="silver.projects.slug (^[a-z0-9-]{1,64}$)")
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
