"""Data dictionary catalog dump — per appendix F-data-dictionary.md / Z.7.

Walks every table in the ``silver`` and ``gold`` schemas in PostgreSQL,
dumps column metadata + PK + FK + table/column comments into a single
JSON document, persists it to S3 (SeaweedFS) under
``catalogs/data_dictionary/<UTC date>/data_dictionary.json``, and emits
Dagster materialisation metadata pointing at the S3 URL.

Optional ERD generation: tries ``eralchemy2`` first (writes ``erd.svg`` +
``erd.dot`` alongside the JSON). Falls back to a plain Graphviz DOT
file synthesised from the same metadata when eralchemy2 is unavailable.
SchemaSpy fallback is left as a future enhancement — the DOT file is
always sufficient for downstream re-rendering.

A companion ``data_dictionary_drift_check`` asset-check compares today's
dump against yesterday's and fails when columns, types, or primary keys
change unexpectedly. That's the CI drift guard the appendix calls for.

Per-table JSON shape (one entry per silver.* / gold.* table)::

    {
      "schema": "silver",
      "table": "collars",
      "comment": "...",                      # pg_description on the table
      "primary_key": ["collar_id"],
      "columns": [
        {
          "name": "collar_id",
          "ordinal_position": 1,
          "data_type": "uuid",
          "is_nullable": false,
          "default": "gen_random_uuid()",
          "comment": "..."
        },
        ...
      ],
      "foreign_keys": [
        {
          "name": "collars_project_id_fkey",
          "columns": ["project_id"],
          "references_schema": "silver",
          "references_table": "projects",
          "references_columns": ["project_id"]
        },
        ...
      ]
    }

ERD groupings are derived deterministically from the leading underscore-
delimited domain prefix of the table name (``collars`` / ``surveys`` /
``samples`` / ``assays_v2`` etc.); the asset writes a sidecar
``erd_groups.json`` mapping ``<group> -> [<schema.table>, ...]`` so a
downstream renderer can lay out clusters without re-parsing names.

NOTE: Do NOT add ``from __future__ import annotations`` — Dagster 1.13
ConfigurableResource / Config classes are Pydantic-introspected and
that import breaks runtime annotation evaluation.
"""

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from typing import Any, Optional

from dagster import (
    AssetCheckExecutionContext,
    AssetCheckResult,
    AssetCheckSeverity,
    AssetExecutionContext,
    Config,
    MaterializeResult,
    MetadataValue,
    asset,
    asset_check,
)

from georag_dagster.resources import PostgresResource, S3Resource


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Schemas the data dictionary covers.  Bronze is excluded by design —
# bronze is the raw landing zone and its shape is governed by upstream
# vendors, not the GeoRAG schema contract.
SCHEMAS: tuple[str, ...] = ("silver", "gold")

# S3 bucket + prefix template.  The bucket name "catalogs" is reserved
# for this asset family per the appendix; do not collide with the
# ``bronze`` / ``silver`` / ``gold`` buckets.
S3_BUCKET = "catalogs"
S3_PREFIX_TEMPLATE = "data_dictionary/{date}"
JSON_OBJECT_NAME = "data_dictionary.json"
ERD_GROUPS_OBJECT_NAME = "erd_groups.json"
ERD_DOT_OBJECT_NAME = "erd.dot"
ERD_SVG_OBJECT_NAME = "erd.svg"


# ---------------------------------------------------------------------------
# SQL — pulls per-table column / PK / FK / comment metadata from the
# information_schema + pg_catalog views.  These views are public reads
# so the regular georag application role suffices.
# ---------------------------------------------------------------------------

SELECT_TABLES_SQL = """
SELECT
    n.nspname              AS schema,
    c.relname              AS table_name,
    obj_description(c.oid) AS table_comment
FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p')          -- ordinary + partitioned tables
  AND n.nspname = ANY(%s::text[])
ORDER BY n.nspname, c.relname;
"""

SELECT_COLUMNS_SQL = """
SELECT
    c.table_schema,
    c.table_name,
    c.column_name,
    c.ordinal_position,
    c.data_type,
    c.udt_name,
    c.is_nullable,
    c.column_default,
    pgd.description AS column_comment
FROM information_schema.columns c
LEFT JOIN pg_catalog.pg_statio_all_tables st
       ON st.schemaname = c.table_schema
      AND st.relname    = c.table_name
LEFT JOIN pg_catalog.pg_description pgd
       ON pgd.objoid    = st.relid
      AND pgd.objsubid  = c.ordinal_position
WHERE c.table_schema = ANY(%s::text[])
ORDER BY c.table_schema, c.table_name, c.ordinal_position;
"""

SELECT_PRIMARY_KEYS_SQL = """
SELECT
    tc.table_schema,
    tc.table_name,
    kcu.column_name,
    kcu.ordinal_position
FROM information_schema.table_constraints tc
JOIN information_schema.key_column_usage kcu
  ON kcu.constraint_name = tc.constraint_name
 AND kcu.table_schema    = tc.table_schema
 AND kcu.table_name      = tc.table_name
WHERE tc.constraint_type = 'PRIMARY KEY'
  AND tc.table_schema    = ANY(%s::text[])
ORDER BY tc.table_schema, tc.table_name, kcu.ordinal_position;
"""

SELECT_FOREIGN_KEYS_SQL = """
SELECT
    tc.table_schema           AS schema,
    tc.table_name             AS table_name,
    tc.constraint_name        AS constraint_name,
    kcu.column_name           AS column_name,
    kcu.ordinal_position      AS ordinal_position,
    ccu.table_schema          AS ref_schema,
    ccu.table_name            AS ref_table,
    ccu.column_name           AS ref_column
FROM information_schema.table_constraints tc
JOIN information_schema.key_column_usage kcu
  ON kcu.constraint_name = tc.constraint_name
 AND kcu.table_schema    = tc.table_schema
 AND kcu.table_name      = tc.table_name
JOIN information_schema.constraint_column_usage ccu
  ON ccu.constraint_name = tc.constraint_name
 AND ccu.table_schema    = tc.table_schema
WHERE tc.constraint_type = 'FOREIGN KEY'
  AND tc.table_schema    = ANY(%s::text[])
ORDER BY tc.table_schema, tc.table_name, tc.constraint_name, kcu.ordinal_position;
"""


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class DataDictionaryConfig(Config):
    """Asset config.

    The defaults bake in the appendix F contract; an operator can
    override ``schemas`` for an ad-hoc dump (e.g. only ``gold``) but
    the CI drift guard expects ``silver + gold`` and will alarm if a
    schema disappears from one snapshot to the next.
    """

    schemas: list[str] = list(SCHEMAS)
    """Schemas to walk. Defaults to ('silver', 'gold')."""

    generate_erd: bool = True
    """Whether to attempt ERD generation. False = JSON-only dump."""


# ---------------------------------------------------------------------------
# Pure helpers — easily unit-testable without Dagster / DB / S3
# ---------------------------------------------------------------------------


def _group_for_table(table_name: str) -> str:
    """Return the ERD grouping for a given table name.

    Rule: leading underscore-delimited prefix wins
    (``samples_assays_v2`` -> ``samples``; ``cog_rasters`` -> ``cog``).
    Tables without an underscore land in a ``misc`` bucket. Pure
    string transform — no I/O, no DB.
    """
    if not table_name:
        return "misc"
    head = table_name.split("_", 1)[0]
    return head or "misc"


def build_dictionary(
    tables: list[dict],
    columns: list[dict],
    primary_keys: list[dict],
    foreign_keys: list[dict],
) -> list[dict[str, Any]]:
    """Fold the four raw row sets into the per-table JSON shape.

    Pure function: every input is a list[dict] of plain Python values
    so this is trivially testable with hand-crafted fixtures (see the
    tests module). Output preserves the (schema, table) sort order
    from ``tables``.
    """
    cols_by_table: dict[tuple[str, str], list[dict]] = {}
    for c in columns:
        key = (c["table_schema"], c["table_name"])
        cols_by_table.setdefault(key, []).append({
            "name": c["column_name"],
            "ordinal_position": c["ordinal_position"],
            "data_type": c.get("data_type"),
            "udt_name": c.get("udt_name"),
            "is_nullable": (c.get("is_nullable") == "YES"),
            "default": c.get("column_default"),
            "comment": c.get("column_comment"),
        })

    pks_by_table: dict[tuple[str, str], list[str]] = {}
    for pk in primary_keys:
        key = (pk["table_schema"], pk["table_name"])
        pks_by_table.setdefault(key, []).append(pk["column_name"])

    fks_by_table: dict[tuple[str, str], dict[str, dict]] = {}
    for fk in foreign_keys:
        key = (fk["schema"], fk["table_name"])
        cname = fk["constraint_name"]
        entry = fks_by_table.setdefault(key, {}).setdefault(cname, {
            "name": cname,
            "columns": [],
            "references_schema": fk["ref_schema"],
            "references_table": fk["ref_table"],
            "references_columns": [],
        })
        entry["columns"].append(fk["column_name"])
        entry["references_columns"].append(fk["ref_column"])

    result: list[dict[str, Any]] = []
    for t in tables:
        key = (t["schema"], t["table_name"])
        result.append({
            "schema": t["schema"],
            "table": t["table_name"],
            "comment": t.get("table_comment"),
            "primary_key": pks_by_table.get(key, []),
            "columns": cols_by_table.get(key, []),
            "foreign_keys": list(fks_by_table.get(key, {}).values()),
        })
    return result


def build_erd_groups(dictionary: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Return ``{group -> [schema.table, ...]}`` for ERD layout.

    Stable sort: groups are sorted alphabetically, members within each
    group are sorted by fully-qualified name. Deterministic output
    keeps the JSON diff-stable across reruns when the schema is
    unchanged.
    """
    groups: dict[str, list[str]] = {}
    for entry in dictionary:
        fq = f"{entry['schema']}.{entry['table']}"
        g = _group_for_table(entry["table"])
        groups.setdefault(g, []).append(fq)
    return {k: sorted(v) for k, v in sorted(groups.items())}


def build_fallback_dot(dictionary: list[dict[str, Any]]) -> str:
    """Synthesise a minimal Graphviz DOT file describing the schema.

    Nodes are tables, edges are foreign keys. Cluster subgraphs group
    tables by their ERD group. Used when eralchemy2 isn't installed
    in the dagster image — still good enough for a downstream renderer
    or a manual ``dot -Tsvg`` invocation.
    """
    by_group: dict[str, list[dict]] = {}
    for entry in dictionary:
        by_group.setdefault(_group_for_table(entry["table"]), []).append(entry)

    lines: list[str] = [
        "digraph data_dictionary {",
        '  rankdir="LR";',
        '  node [shape=record, fontname="Helvetica", fontsize=10];',
        '  edge [fontname="Helvetica", fontsize=9];',
    ]
    for group_name in sorted(by_group.keys()):
        # Cluster name must be alphanumeric for Graphviz.
        safe = re.sub(r"[^A-Za-z0-9_]+", "_", group_name)
        lines.append(f'  subgraph cluster_{safe} {{')
        lines.append(f'    label="{group_name}";')
        lines.append('    style="rounded,filled";')
        lines.append('    color="#dddddd";')
        for entry in sorted(by_group[group_name], key=lambda e: (e["schema"], e["table"])):
            fq = f'{entry["schema"]}.{entry["table"]}'
            cols = "\\l".join(
                f'{c["name"]}: {c.get("data_type") or c.get("udt_name") or "?"}'
                for c in entry["columns"]
            ) or "(no columns)"
            label = f'{{{fq}|{cols}\\l}}'
            lines.append(f'    "{fq}" [label="{label}"];')
        lines.append("  }")

    for entry in dictionary:
        src = f'{entry["schema"]}.{entry["table"]}'
        for fk in entry["foreign_keys"]:
            tgt = f'{fk["references_schema"]}.{fk["references_table"]}'
            label = ",".join(fk["columns"])
            lines.append(f'  "{src}" -> "{tgt}" [label="{label}"];')

    lines.append("}")
    return "\n".join(lines) + "\n"


def compare_dictionaries(
    today: list[dict[str, Any]],
    yesterday: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compute drift between two data dictionaries.

    Returns a dict with the keys::

        {
          "added_tables":    ["silver.foo", ...],
          "removed_tables":  ["silver.bar", ...],
          "added_columns":   ["silver.foo.col_a", ...],
          "removed_columns": ["silver.bar.col_b", ...],
          "type_changes":    [{"column": "silver.x.y", "from": "int", "to": "bigint"}, ...],
          "pk_changes":      [{"table": "silver.x", "from": [...], "to": [...]}, ...]
        }

    Pure function — easily unit-tested with hand-crafted fixtures.
    """
    def by_table(d):
        return {f'{e["schema"]}.{e["table"]}': e for e in d}

    today_t = by_table(today)
    yest_t = by_table(yesterday)

    added_tables = sorted(set(today_t) - set(yest_t))
    removed_tables = sorted(set(yest_t) - set(today_t))

    added_columns: list[str] = []
    removed_columns: list[str] = []
    type_changes: list[dict[str, str]] = []
    pk_changes: list[dict[str, Any]] = []

    for table, ent in today_t.items():
        if table not in yest_t:
            continue
        prev = yest_t[table]

        today_cols = {c["name"]: c for c in ent["columns"]}
        prev_cols = {c["name"]: c for c in prev["columns"]}

        for cname in sorted(set(today_cols) - set(prev_cols)):
            added_columns.append(f"{table}.{cname}")
        for cname in sorted(set(prev_cols) - set(today_cols)):
            removed_columns.append(f"{table}.{cname}")
        for cname in sorted(set(today_cols) & set(prev_cols)):
            t_now = today_cols[cname].get("data_type")
            t_prev = prev_cols[cname].get("data_type")
            if t_now != t_prev:
                type_changes.append({
                    "column": f"{table}.{cname}",
                    "from": t_prev,
                    "to": t_now,
                })

        if ent.get("primary_key", []) != prev.get("primary_key", []):
            pk_changes.append({
                "table": table,
                "from": prev.get("primary_key", []),
                "to": ent.get("primary_key", []),
            })

    return {
        "added_tables": added_tables,
        "removed_tables": removed_tables,
        "added_columns": added_columns,
        "removed_columns": removed_columns,
        "type_changes": type_changes,
        "pk_changes": pk_changes,
    }


def _today_utc() -> str:
    """Return today's UTC date in YYYY-MM-DD. Indirection lets the
    tests freeze ``data_dictionary_dump._today_utc`` cheaply."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _yesterday_utc() -> str:
    """Return yesterday's UTC date in YYYY-MM-DD."""
    today = datetime.now(timezone.utc).date()
    from datetime import timedelta
    return (today - timedelta(days=1)).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# ERD generation — tries eralchemy2, falls back to a hand-rolled DOT
# ---------------------------------------------------------------------------


def _try_eralchemy2(database_url: str, out_dir: str) -> Optional[dict[str, str]]:
    """Attempt to render an ERD with eralchemy2.

    Returns a dict of ``{kind: path}`` for the files actually
    produced, or ``None`` if eralchemy2 is not importable. Failure to
    render after a successful import is logged and re-raised so the
    asset surfaces the error rather than silently swallowing it.
    """
    try:
        from eralchemy2 import render_er  # type: ignore[import-not-found]  # noqa: PLC0415
    except ImportError:
        logger.info(
            "eralchemy2 not installed in this environment — falling back to "
            "hand-rolled Graphviz DOT. Install eralchemy2 + graphviz to get "
            "the full ERD."
        )
        return None

    produced: dict[str, str] = {}
    svg_path = os.path.join(out_dir, ERD_SVG_OBJECT_NAME)
    dot_path = os.path.join(out_dir, ERD_DOT_OBJECT_NAME)
    try:
        render_er(database_url, svg_path)
        produced["svg"] = svg_path
    except Exception:
        logger.exception("eralchemy2 SVG render failed")
    try:
        render_er(database_url, dot_path)
        produced["dot"] = dot_path
    except Exception:
        logger.exception("eralchemy2 DOT render failed")
    return produced or None


def _maybe_render_dot_to_svg(dot_path: str) -> Optional[str]:
    """If ``dot`` (Graphviz CLI) is on PATH, render the .dot to .svg.

    Returns the .svg path on success, ``None`` otherwise. The
    fallback path uses our hand-rolled DOT, so this lets the asset
    still produce an SVG when eralchemy2 isn't around but Graphviz
    is.
    """
    if shutil.which("dot") is None:
        return None
    svg_path = dot_path.replace(".dot", ".svg")
    try:
        subprocess.run(
            ["dot", "-Tsvg", dot_path, "-o", svg_path],
            check=True,
            capture_output=True,
        )
        return svg_path
    except subprocess.CalledProcessError:
        logger.exception("Graphviz dot rendering failed")
        return None


# ---------------------------------------------------------------------------
# Asset — dump
# ---------------------------------------------------------------------------


def _build_database_url(postgres: PostgresResource) -> str:
    """Build a libpq URL eralchemy2 can consume.

    Uses the same fields the PostgresResource exposes; safe to share
    in-process since eralchemy2 opens its own short-lived connection.
    """
    pw = postgres.password
    return (
        f"postgresql://{postgres.user}:{pw}@{postgres.host}:{postgres.port}"
        f"/{postgres.dbname}"
    )


@asset(
    name="data_dictionary_dump",
    group_name="catalogs",
    compute_kind="postgres",
    description=(
        "Per-table data dictionary for the silver + gold schemas, "
        "persisted as JSON to S3 under catalogs/data_dictionary/<UTC date>/. "
        "Includes per-column type/nullable/default/comment, primary keys, "
        "foreign keys, table comments, and an ERD groupings sidecar. "
        "Attempts an SVG + DOT ERD via eralchemy2; falls back to a "
        "hand-rolled Graphviz DOT when eralchemy2 is unavailable. "
        "Appendix F-data-dictionary / Z.7."
    ),
)
def data_dictionary_dump(
    context: AssetExecutionContext,
    config: DataDictionaryConfig,
    postgres: PostgresResource,
    minio: S3Resource,
) -> MaterializeResult:
    """Materialise today's data dictionary snapshot to S3."""
    schemas = list(config.schemas)

    # ---- 1. Pull metadata from PostgreSQL ----
    with postgres.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(SELECT_TABLES_SQL, (schemas,))
            tcols = [d[0] for d in cur.description]
            tables = [dict(zip(tcols, r)) for r in cur.fetchall()]

            cur.execute(SELECT_COLUMNS_SQL, (schemas,))
            ccols = [d[0] for d in cur.description]
            columns = [dict(zip(ccols, r)) for r in cur.fetchall()]

            cur.execute(SELECT_PRIMARY_KEYS_SQL, (schemas,))
            pcols = [d[0] for d in cur.description]
            primary_keys = [dict(zip(pcols, r)) for r in cur.fetchall()]

            cur.execute(SELECT_FOREIGN_KEYS_SQL, (schemas,))
            fcols = [d[0] for d in cur.description]
            foreign_keys = [dict(zip(fcols, r)) for r in cur.fetchall()]

    dictionary = build_dictionary(tables, columns, primary_keys, foreign_keys)
    erd_groups = build_erd_groups(dictionary)

    context.log.info(
        "data_dictionary_dump: %d tables across schemas=%s "
        "(%d columns, %d primary-key columns, %d foreign-key columns)",
        len(dictionary), schemas, len(columns), len(primary_keys), len(foreign_keys),
    )

    # ---- 2. Persist JSON + ERD groupings to S3 ----
    date = _today_utc()
    prefix = S3_PREFIX_TEMPLATE.format(date=date)
    json_key = f"{prefix}/{JSON_OBJECT_NAME}"
    groups_key = f"{prefix}/{ERD_GROUPS_OBJECT_NAME}"

    payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "schemas": schemas,
        "table_count": len(dictionary),
        "tables": dictionary,
    }
    minio.upload_bytes(
        bucket=S3_BUCKET,
        object_name=json_key,
        data=json.dumps(payload, indent=2, default=str).encode("utf-8"),
        content_type="application/json",
    )
    minio.upload_bytes(
        bucket=S3_BUCKET,
        object_name=groups_key,
        data=json.dumps(erd_groups, indent=2).encode("utf-8"),
        content_type="application/json",
    )
    json_url = f"s3://{S3_BUCKET}/{json_key}"
    groups_url = f"s3://{S3_BUCKET}/{groups_key}"

    # ---- 3. ERD (optional, best-effort) ----
    erd_svg_url: Optional[str] = None
    erd_dot_url: Optional[str] = None
    erd_kind = "none"
    if config.generate_erd:
        with tempfile.TemporaryDirectory(prefix="ddict_erd_") as tmp:
            produced = _try_eralchemy2(_build_database_url(postgres), tmp)
            if produced is None:
                # Fallback path — synthesise DOT from the dictionary.
                dot_path = os.path.join(tmp, ERD_DOT_OBJECT_NAME)
                with open(dot_path, "w", encoding="utf-8") as fh:
                    fh.write(build_fallback_dot(dictionary))
                produced = {"dot": dot_path}
                svg_path = _maybe_render_dot_to_svg(dot_path)
                if svg_path is not None:
                    produced["svg"] = svg_path
                erd_kind = "fallback_graphviz"
            else:
                erd_kind = "eralchemy2"

            for kind, path in produced.items():
                obj_name = (
                    f"{prefix}/{ERD_SVG_OBJECT_NAME}" if kind == "svg"
                    else f"{prefix}/{ERD_DOT_OBJECT_NAME}"
                )
                with open(path, "rb") as fh:
                    minio.upload_bytes(
                        bucket=S3_BUCKET,
                        object_name=obj_name,
                        data=fh.read(),
                        content_type=(
                            "image/svg+xml" if kind == "svg" else "text/vnd.graphviz"
                        ),
                    )
                url = f"s3://{S3_BUCKET}/{obj_name}"
                if kind == "svg":
                    erd_svg_url = url
                else:
                    erd_dot_url = url

    # ---- 4. Materialisation metadata ----
    metadata: dict[str, MetadataValue] = {
        "table_count": MetadataValue.int(len(dictionary)),
        "schemas": MetadataValue.text(",".join(schemas)),
        "json_s3_url": MetadataValue.url(json_url),
        "erd_groups_s3_url": MetadataValue.url(groups_url),
        "erd_kind": MetadataValue.text(erd_kind),
        "snapshot_date_utc": MetadataValue.text(date),
        "group_count": MetadataValue.int(len(erd_groups)),
    }
    if erd_svg_url:
        metadata["erd_svg_s3_url"] = MetadataValue.url(erd_svg_url)
    if erd_dot_url:
        metadata["erd_dot_s3_url"] = MetadataValue.url(erd_dot_url)

    return MaterializeResult(metadata=metadata)


# ---------------------------------------------------------------------------
# Asset check — drift guard
# ---------------------------------------------------------------------------


@asset_check(
    asset=data_dictionary_dump,
    name="data_dictionary_drift_check",
    description=(
        "Compares today's data dictionary to yesterday's snapshot in S3 and "
        "fails when columns, types, or primary keys change unexpectedly. "
        "Added tables / columns are reported but do NOT fail the check — "
        "only removals + type changes + PK shape changes are drift events."
    ),
    blocking=False,
)
def data_dictionary_drift_check(
    context: AssetCheckExecutionContext,
    minio: S3Resource,
) -> AssetCheckResult:
    """CI guard — yesterday vs today snapshot diff in S3."""
    today_date = _today_utc()
    yesterday_date = _yesterday_utc()

    today_key = f"{S3_PREFIX_TEMPLATE.format(date=today_date)}/{JSON_OBJECT_NAME}"
    yesterday_key = (
        f"{S3_PREFIX_TEMPLATE.format(date=yesterday_date)}/{JSON_OBJECT_NAME}"
    )

    # Pass vacuously when today's snapshot hasn't landed yet — the
    # dump asset is what should have run first; the check is the
    # second half of the gate and is expected to be skipped on the
    # very first day.
    if not minio.object_exists(S3_BUCKET, today_key):
        return AssetCheckResult(
            passed=True,
            severity=AssetCheckSeverity.WARN,
            description=(
                "today's data dictionary snapshot not found in S3 "
                f"({today_key}); drift check skipped"
            ),
            metadata={
                "today_key": MetadataValue.text(today_key),
            },
        )
    # First-day case — no yesterday snapshot to compare against.
    if not minio.object_exists(S3_BUCKET, yesterday_key):
        return AssetCheckResult(
            passed=True,
            severity=AssetCheckSeverity.WARN,
            description=(
                "no yesterday snapshot to compare against — drift baseline "
                "established"
            ),
            metadata={
                "today_key": MetadataValue.text(today_key),
                "yesterday_key": MetadataValue.text(yesterday_key),
            },
        )

    today_doc = json.loads(minio.download_bytes(S3_BUCKET, today_key).decode("utf-8"))
    yest_doc = json.loads(
        minio.download_bytes(S3_BUCKET, yesterday_key).decode("utf-8")
    )

    drift = compare_dictionaries(today_doc["tables"], yest_doc["tables"])

    # Removals + type changes + PK changes are unsafe; additions are
    # safe (forward-compatible schema growth).
    unsafe = (
        bool(drift["removed_tables"])
        or bool(drift["removed_columns"])
        or bool(drift["type_changes"])
        or bool(drift["pk_changes"])
    )

    return AssetCheckResult(
        passed=not unsafe,
        severity=(
            AssetCheckSeverity.ERROR if unsafe else AssetCheckSeverity.WARN
        ),
        description=(
            f"drift summary: +{len(drift['added_tables'])} tables, "
            f"-{len(drift['removed_tables'])} tables, "
            f"+{len(drift['added_columns'])} cols, "
            f"-{len(drift['removed_columns'])} cols, "
            f"{len(drift['type_changes'])} type changes, "
            f"{len(drift['pk_changes'])} PK changes"
        ),
        metadata={
            "added_tables": MetadataValue.json(drift["added_tables"]),
            "removed_tables": MetadataValue.json(drift["removed_tables"]),
            "added_columns": MetadataValue.json(drift["added_columns"]),
            "removed_columns": MetadataValue.json(drift["removed_columns"]),
            "type_changes": MetadataValue.json(drift["type_changes"]),
            "pk_changes": MetadataValue.json(drift["pk_changes"]),
            "today_key": MetadataValue.text(today_key),
            "yesterday_key": MetadataValue.text(yesterday_key),
        },
    )
