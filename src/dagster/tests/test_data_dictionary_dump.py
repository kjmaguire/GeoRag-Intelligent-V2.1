"""Smoke + unit tests for the data_dictionary_dump asset (Appendix F / Z.7).

Covers the pure helper functions with hand-crafted fixtures and exercises
the asset end-to-end against a mocked Postgres + mocked S3 resource. The
asset's database access goes through a single ``postgres.get_connection``
context manager — easy to stand in for via a minimal cursor double.
"""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Asset module shape
# ---------------------------------------------------------------------------


def test_asset_imports_clean() -> None:
    """The asset module must import without side effects."""
    from georag_dagster.assets.data_dictionary_dump import (
        data_dictionary_drift_check,
        data_dictionary_dump,
    )
    assert data_dictionary_dump is not None
    assert data_dictionary_drift_check is not None


def test_asset_in_catalogs_group() -> None:
    """Asset belongs to the 'catalogs' group per the appendix."""
    from georag_dagster.assets.data_dictionary_dump import data_dictionary_dump
    groups = set(data_dictionary_dump.group_names_by_key.values())
    assert "catalogs" in groups


def test_asset_compute_kind_is_postgres() -> None:
    from georag_dagster.assets.data_dictionary_dump import data_dictionary_dump
    # Dagster 1.13 exposes compute_kind on the underlying op definition.
    assert data_dictionary_dump.op.tags.get("dagster/compute_kind") == "postgres"


def test_default_schemas_are_silver_and_gold() -> None:
    """Bronze is deliberately excluded (raw vendor zone)."""
    from georag_dagster.assets import data_dictionary_dump as mod
    assert mod.SCHEMAS == ("silver", "gold")


def test_s3_prefix_template_matches_appendix() -> None:
    """``catalogs/data_dictionary/<date>/data_dictionary.json`` — locked
    by the appendix; if it ever moves the FastAPI catalog endpoint
    needs the same update."""
    from georag_dagster.assets import data_dictionary_dump as mod
    assert mod.S3_BUCKET == "catalogs"
    assert mod.S3_PREFIX_TEMPLATE == "data_dictionary/{date}"
    assert mod.JSON_OBJECT_NAME == "data_dictionary.json"


# ---------------------------------------------------------------------------
# Pure helpers — build_dictionary
# ---------------------------------------------------------------------------


def _fixture_rows() -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    tables = [
        {"schema": "silver", "table_name": "collars", "table_comment": "drillhole collars"},
        {"schema": "silver", "table_name": "samples_assays_v2", "table_comment": None},
    ]
    columns = [
        {
            "table_schema": "silver", "table_name": "collars",
            "column_name": "collar_id", "ordinal_position": 1,
            "data_type": "uuid", "udt_name": "uuid", "is_nullable": "NO",
            "column_default": "gen_random_uuid()", "column_comment": "PK",
        },
        {
            "table_schema": "silver", "table_name": "collars",
            "column_name": "project_id", "ordinal_position": 2,
            "data_type": "uuid", "udt_name": "uuid", "is_nullable": "NO",
            "column_default": None, "column_comment": None,
        },
        {
            "table_schema": "silver", "table_name": "samples_assays_v2",
            "column_name": "sample_id", "ordinal_position": 1,
            "data_type": "uuid", "udt_name": "uuid", "is_nullable": "NO",
            "column_default": None, "column_comment": None,
        },
    ]
    primary_keys = [
        {
            "table_schema": "silver", "table_name": "collars",
            "column_name": "collar_id", "ordinal_position": 1,
        },
        {
            "table_schema": "silver", "table_name": "samples_assays_v2",
            "column_name": "sample_id", "ordinal_position": 1,
        },
    ]
    foreign_keys = [
        {
            "schema": "silver", "table_name": "collars",
            "constraint_name": "collars_project_id_fkey",
            "column_name": "project_id", "ordinal_position": 1,
            "ref_schema": "silver", "ref_table": "projects",
            "ref_column": "project_id",
        },
    ]
    return tables, columns, primary_keys, foreign_keys


def test_build_dictionary_groups_columns_and_pks_per_table() -> None:
    from georag_dagster.assets.data_dictionary_dump import build_dictionary
    tables, columns, pks, fks = _fixture_rows()
    out = build_dictionary(tables, columns, pks, fks)
    assert len(out) == 2
    collars = next(e for e in out if e["table"] == "collars")
    assert collars["schema"] == "silver"
    assert collars["comment"] == "drillhole collars"
    assert collars["primary_key"] == ["collar_id"]
    assert len(collars["columns"]) == 2
    # is_nullable normalised from "NO"/"YES" → bool.
    assert collars["columns"][0]["is_nullable"] is False
    # FK shape.
    fk = collars["foreign_keys"][0]
    assert fk["name"] == "collars_project_id_fkey"
    assert fk["columns"] == ["project_id"]
    assert fk["references_schema"] == "silver"
    assert fk["references_table"] == "projects"
    assert fk["references_columns"] == ["project_id"]


def test_build_dictionary_table_with_no_columns_returns_empty_list() -> None:
    """Empty table is rare but must not blow up the fold."""
    from georag_dagster.assets.data_dictionary_dump import build_dictionary
    out = build_dictionary(
        [{"schema": "gold", "table_name": "empty", "table_comment": None}],
        [], [], [],
    )
    assert out == [
        {
            "schema": "gold", "table": "empty", "comment": None,
            "primary_key": [], "columns": [], "foreign_keys": [],
        }
    ]


# ---------------------------------------------------------------------------
# Pure helpers — group_for_table + build_erd_groups
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("table", "expected"),
    [
        ("collars", "collars"),
        ("samples_assays_v2", "samples"),
        ("cog_rasters", "cog"),
        ("misc", "misc"),
        ("", "misc"),
    ],
)
def test_group_for_table_uses_leading_prefix(table: str, expected: str) -> None:
    from georag_dagster.assets.data_dictionary_dump import _group_for_table
    assert _group_for_table(table) == expected


def test_build_erd_groups_sorts_deterministically() -> None:
    from georag_dagster.assets.data_dictionary_dump import build_erd_groups
    dictionary = [
        {"schema": "silver", "table": "samples_assays_v2", "columns": [], "primary_key": [], "foreign_keys": [], "comment": None},
        {"schema": "silver", "table": "samples", "columns": [], "primary_key": [], "foreign_keys": [], "comment": None},
        {"schema": "gold", "table": "collars_aggregate", "columns": [], "primary_key": [], "foreign_keys": [], "comment": None},
    ]
    groups = build_erd_groups(dictionary)
    assert list(groups.keys()) == ["collars", "samples"]
    assert groups["samples"] == ["silver.samples", "silver.samples_assays_v2"]


# ---------------------------------------------------------------------------
# Pure helpers — fallback DOT
# ---------------------------------------------------------------------------


def test_fallback_dot_includes_tables_and_fk_edges() -> None:
    from georag_dagster.assets.data_dictionary_dump import (
        build_dictionary,
        build_fallback_dot,
    )
    dictionary = build_dictionary(*_fixture_rows())
    dot = build_fallback_dot(dictionary)
    assert dot.startswith("digraph data_dictionary {")
    assert '"silver.collars"' in dot
    assert '"silver.samples_assays_v2"' in dot
    # FK edge from collars → projects
    assert '"silver.collars" -> "silver.projects"' in dot


# ---------------------------------------------------------------------------
# Pure helpers — compare_dictionaries (drift detection)
# ---------------------------------------------------------------------------


def _entry(schema: str, table: str, columns: list[dict], pk: list[str]) -> dict[str, Any]:
    return {
        "schema": schema, "table": table, "comment": None,
        "primary_key": pk, "columns": columns, "foreign_keys": [],
    }


def test_compare_dictionaries_detects_no_drift() -> None:
    from georag_dagster.assets.data_dictionary_dump import compare_dictionaries
    snap = [_entry("silver", "collars", [{"name": "a", "data_type": "uuid"}], ["a"])]
    drift = compare_dictionaries(snap, snap)
    assert drift == {
        "added_tables": [], "removed_tables": [],
        "added_columns": [], "removed_columns": [],
        "type_changes": [], "pk_changes": [],
    }


def test_compare_dictionaries_detects_added_and_removed_tables() -> None:
    from georag_dagster.assets.data_dictionary_dump import compare_dictionaries
    today = [_entry("silver", "collars", [], [])]
    yest = [_entry("silver", "samples", [], [])]
    drift = compare_dictionaries(today, yest)
    assert drift["added_tables"] == ["silver.collars"]
    assert drift["removed_tables"] == ["silver.samples"]


def test_compare_dictionaries_detects_column_changes() -> None:
    from georag_dagster.assets.data_dictionary_dump import compare_dictionaries
    today = [_entry("silver", "collars",
                    [{"name": "a", "data_type": "uuid"},
                     {"name": "b", "data_type": "bigint"}], ["a"])]
    yest = [_entry("silver", "collars",
                   [{"name": "a", "data_type": "uuid"},
                    {"name": "c", "data_type": "int"}], ["a"])]
    drift = compare_dictionaries(today, yest)
    assert drift["added_columns"] == ["silver.collars.b"]
    assert drift["removed_columns"] == ["silver.collars.c"]


def test_compare_dictionaries_detects_type_change() -> None:
    from georag_dagster.assets.data_dictionary_dump import compare_dictionaries
    today = [_entry("silver", "collars", [{"name": "a", "data_type": "bigint"}], ["a"])]
    yest = [_entry("silver", "collars", [{"name": "a", "data_type": "int"}], ["a"])]
    drift = compare_dictionaries(today, yest)
    assert drift["type_changes"] == [
        {"column": "silver.collars.a", "from": "int", "to": "bigint"},
    ]


def test_compare_dictionaries_detects_pk_shape_change() -> None:
    from georag_dagster.assets.data_dictionary_dump import compare_dictionaries
    today = [_entry("silver", "collars", [{"name": "a", "data_type": "uuid"}], ["a"])]
    yest = [_entry("silver", "collars", [{"name": "a", "data_type": "uuid"}], ["a", "workspace_id"])]
    drift = compare_dictionaries(today, yest)
    assert drift["pk_changes"] == [
        {"table": "silver.collars", "from": ["a", "workspace_id"], "to": ["a"]},
    ]


# ---------------------------------------------------------------------------
# End-to-end smoke — mocked postgres + S3
# ---------------------------------------------------------------------------


class _FakeCursor:
    """Bare-minimum psycopg2 cursor double — supports execute + description
    + fetchall keyed by a pre-seeded results map."""

    def __init__(self, fixtures: list[tuple[list[str], list[tuple]]]) -> None:
        self._fixtures = list(fixtures)
        self.description: list[Any] = []
        self._rows: list[tuple] = []

    def execute(self, sql: str, params: Any = None) -> None:  # noqa: ARG002
        cols, rows = self._fixtures.pop(0)
        self.description = [(c,) for c in cols]
        self._rows = rows

    def fetchall(self) -> list[tuple]:
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, cur: _FakeCursor) -> None:
        self._cur = cur

    def cursor(self, *a, **kw):  # noqa: ARG002
        return self._cur

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        pass


def test_smoke_materialise_writes_json_to_s3(monkeypatch: pytest.MonkeyPatch) -> None:
    """Full asset run against an in-memory Postgres + S3 — verifies the
    end-to-end happy path, including S3 key layout + metadata."""
    from georag_dagster.assets import data_dictionary_dump as mod

    tables, columns, pks, fks = _fixture_rows()
    cur = _FakeCursor([
        (["schema", "table_name", "table_comment"],
         [(t["schema"], t["table_name"], t["table_comment"]) for t in tables]),
        (["table_schema", "table_name", "column_name", "ordinal_position",
          "data_type", "udt_name", "is_nullable", "column_default", "column_comment"],
         [(c["table_schema"], c["table_name"], c["column_name"],
           c["ordinal_position"], c["data_type"], c["udt_name"],
           c["is_nullable"], c["column_default"], c["column_comment"])
          for c in columns]),
        (["table_schema", "table_name", "column_name", "ordinal_position"],
         [(p["table_schema"], p["table_name"], p["column_name"], p["ordinal_position"])
          for p in pks]),
        (["schema", "table_name", "constraint_name", "column_name",
          "ordinal_position", "ref_schema", "ref_table", "ref_column"],
         [(f["schema"], f["table_name"], f["constraint_name"], f["column_name"],
           f["ordinal_position"], f["ref_schema"], f["ref_table"], f["ref_column"])
          for f in fks]),
    ])
    conn = _FakeConn(cur)

    postgres = MagicMock()
    postgres.user = "georag"
    postgres.password = "x"
    postgres.host = "pg"
    postgres.port = 6432
    postgres.dbname = "georag"
    postgres.get_connection.return_value = conn

    uploads: list[tuple[str, str, bytes]] = []

    minio = MagicMock()

    def _upload(bucket: str, object_name: str, data: bytes, content_type: str = "application/octet-stream") -> str:  # noqa: ARG001
        uploads.append((bucket, object_name, data))
        return f"{bucket}/{object_name}"

    minio.upload_bytes.side_effect = _upload

    # Freeze date so we can assert keys.
    monkeypatch.setattr(mod, "_today_utc", lambda: "2026-05-29")
    # Disable ERD generation in the smoke path to keep the test tight.
    config = mod.DataDictionaryConfig(generate_erd=False)

    # Call the wrapped compute function directly to avoid going through
    # Dagster's op-invocation machinery, which insists on a real
    # AssetExecutionContext with bind() / per_invocation_properties.
    from dagster import build_asset_context
    context = build_asset_context()
    fn = mod.data_dictionary_dump.op.compute_fn.decorated_fn
    result = fn(
        context=context, config=config, postgres=postgres, minio=minio,
    )

    keys_written = {object_name for _, object_name, _ in uploads}
    assert "data_dictionary/2026-05-29/data_dictionary.json" in keys_written
    assert "data_dictionary/2026-05-29/erd_groups.json" in keys_written

    # Inspect the JSON payload — table count should be 2.
    json_blob = next(
        data for _, object_name, data in uploads
        if object_name.endswith("data_dictionary.json")
    )
    payload = json.loads(json_blob.decode("utf-8"))
    assert payload["table_count"] == 2
    assert payload["schemas"] == ["silver", "gold"]
    table_names = {t["table"] for t in payload["tables"]}
    assert table_names == {"collars", "samples_assays_v2"}

    # Materialisation metadata should expose the S3 URLs + table count.
    meta = result.metadata
    assert meta["table_count"].value == 2
    assert meta["json_s3_url"].url.endswith("/data_dictionary.json")
    assert meta["erd_kind"].text == "none"


def test_drift_check_returns_warn_when_no_yesterday_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from georag_dagster.assets import data_dictionary_dump as mod
    monkeypatch.setattr(mod, "_today_utc", lambda: "2026-05-29")
    monkeypatch.setattr(mod, "_yesterday_utc", lambda: "2026-05-28")

    minio = MagicMock()
    # Today exists, yesterday does not.
    minio.object_exists.side_effect = lambda bucket, key: key.endswith("2026-05-29/data_dictionary.json")  # noqa: ARG005

    from dagster import build_asset_check_context
    ctx = build_asset_check_context()
    fn = mod.data_dictionary_drift_check.node_def.compute_fn.decorated_fn
    result = fn(context=ctx, minio=minio)
    assert result.passed is True
    assert "baseline" in (result.description or "").lower()


def test_drift_check_fails_when_columns_removed(monkeypatch: pytest.MonkeyPatch) -> None:
    from georag_dagster.assets import data_dictionary_dump as mod
    monkeypatch.setattr(mod, "_today_utc", lambda: "2026-05-29")
    monkeypatch.setattr(mod, "_yesterday_utc", lambda: "2026-05-28")

    today_doc = {
        "tables": [
            {"schema": "silver", "table": "collars",
             "columns": [{"name": "a", "data_type": "uuid"}],
             "primary_key": ["a"], "foreign_keys": []},
        ]
    }
    yest_doc = {
        "tables": [
            {"schema": "silver", "table": "collars",
             "columns": [
                 {"name": "a", "data_type": "uuid"},
                 {"name": "b", "data_type": "text"},
             ],
             "primary_key": ["a"], "foreign_keys": []},
        ]
    }

    minio = MagicMock()
    minio.object_exists.return_value = True
    minio.download_bytes.side_effect = lambda bucket, key: (  # noqa: ARG005
        json.dumps(today_doc).encode("utf-8")
        if "2026-05-29" in key
        else json.dumps(yest_doc).encode("utf-8")
    )

    from dagster import build_asset_check_context
    ctx = build_asset_check_context()
    fn = mod.data_dictionary_drift_check.node_def.compute_fn.decorated_fn
    result = fn(context=ctx, minio=minio)
    assert result.passed is False
    assert "removed_columns" in result.metadata
