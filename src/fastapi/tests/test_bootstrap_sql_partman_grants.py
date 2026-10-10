"""georag_app can read partman's configuration, and only read it (DB-13, 2026-10-10).

``pg_partman_maintenance.py`` connects as ``georag_app`` and runs
``SELECT count(*) FROM partman.part_config`` and then
``CALL partman.run_maintenance_proc()``. ``deploy/aws/bootstrap.sql`` created the
``partman`` schema and granted that role nothing on it, and the raw file that
did (``database/raw/phase1/10-georag-app-role.sql``) is not in
``database/raw/manifest.json``, so it has never run. Both statements failed with
"permission denied for schema partman" every night.

Two properties of the fix are pinned here because each fails quietly.

ORDER. bootstrap.sql runs under ``\\set ON_ERROR_STOP on``. The grants read
naturally next to ``CREATE SCHEMA partman`` near the top of the file, and there
they name a role that does not exist yet on a fresh database: the run ends at
that line, before the roles, the Hatchet database or anything else a go-live
needs. They have to come after ``CREATE ROLE georag_app``.

READ-ONLY. With no partman parent registered the job only has to read
``part_config``. Once one IS registered, ``run_maintenance_proc()`` has to
create and drop partitions in schemas ``georag_app`` cannot even see, so a
broader grant would not make the Hatchet run a maintainer -- it would make it a
role that can edit partitioning configuration for the audit ledger. The pg_cron
job in the same file runs as the owner and is the one maintainer; this role
reads. (Both halves were checked against PostgreSQL 16 with pg_partman 5.0.1:
the two statements succeed with exactly these grants and an empty
``part_config``, and the ``CALL`` fails with a registered parent.)
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
BOOTSTRAP = REPO / "deploy" / "aws" / "bootstrap.sql"

#: Privileges georag_app may hold on anything in the partman schema.
READ_ONLY = frozenset({"USAGE", "SELECT"})

_CREATE_ROLE = re.compile(r"CREATE\s+ROLE\s+georag_app\b", re.I)
_GRANT = re.compile(
    r"\bGRANT\s+(?P<privs>[^;]*?)\s+ON\s+(?P<object>[^;]*?)\s+TO\s+georag_app\s*;",
    re.I | re.S,
)


def _live(sql: str) -> str:
    """The statements, without `--` comments (bootstrap.sql explains itself at
    length, including the SQL an operator pastes, and none of that is a
    grant)."""
    return "\n".join(line.split("--", 1)[0] for line in sql.splitlines())


def partman_grants(sql: str) -> list[tuple[int, frozenset[str], str]]:
    """(offset, privileges, object) of every grant to georag_app that touches
    the partman schema."""
    found = []
    for match in _GRANT.finditer(_live(sql)):
        obj = " ".join(match.group("object").split()).lower()
        if not re.search(r"\bpartman\b", obj):
            continue
        privs = frozenset(
            p.strip().upper() for p in match.group("privs").split(",") if p.strip()
        )
        found.append((match.start(), privs, obj))
    return found


def role_offset(sql: str) -> int:
    match = _CREATE_ROLE.search(_live(sql))
    assert match, "bootstrap.sql no longer creates georag_app"
    return match.start()


def problems(sql: str) -> list[str]:
    grants = partman_grants(sql)
    out = []

    if not any(p == {"USAGE"} and o == "schema partman" for _, p, o in grants):
        out.append("no GRANT USAGE ON SCHEMA partman TO georag_app")
    if not any(
        "SELECT" in p and o in ("partman.part_config", "all tables in schema partman")
        for _, p, o in grants
    ):
        out.append("no GRANT SELECT on partman.part_config to georag_app")

    created = role_offset(sql)
    out += [
        f"'GRANT {', '.join(sorted(p))} ON {o}' comes before CREATE ROLE georag_app"
        for at, p, o in grants
        if at < created
    ]
    out += [
        f"'GRANT {', '.join(sorted(p))} ON {o}' is more than read-only"
        for _, p, o in grants
        if not p <= READ_ONLY
    ]
    return out


def test_bootstrap_sql_lets_georag_app_read_partman_config() -> None:
    found = problems(BOOTSTRAP.read_text(encoding="utf-8"))
    assert not found, (
        "deploy/aws/bootstrap.sql and the georag_app grants on partman:\n  "
        + "\n  ".join(found)
        + "\n\nSee this module's docstring for why the order and the "
          "read-only limit both matter."
    )


# --- the checker itself --------------------------------------------------

_ROLE = "SELECT format('CREATE ROLE georag_app LOGIN PASSWORD %L', :'p')\n"
_GOOD = (
    "GRANT USAGE ON SCHEMA partman TO georag_app;\n"
    "GRANT SELECT ON partman.part_config TO georag_app;\n"
)


def test_the_checker_accepts_the_intended_shape() -> None:
    assert problems(_ROLE + _GOOD) == []


def test_the_checker_accepts_the_whole_schema_form() -> None:
    sql = _ROLE + (
        "GRANT USAGE ON SCHEMA partman TO georag_app;\n"
        "GRANT SELECT ON ALL TABLES IN SCHEMA partman TO georag_app;\n"
    )
    assert problems(sql) == []


def test_the_checker_catches_a_grant_written_before_the_role_exists() -> None:
    found = problems(_GOOD + _ROLE)
    assert len(found) == 2 and all("before CREATE ROLE" in p for p in found)


def test_the_checker_catches_a_missing_grant() -> None:
    assert problems(_ROLE + "GRANT USAGE ON SCHEMA partman TO georag_app;\n") == [
        "no GRANT SELECT on partman.part_config to georag_app"
    ]
    assert problems(_ROLE) == [
        "no GRANT USAGE ON SCHEMA partman TO georag_app",
        "no GRANT SELECT on partman.part_config to georag_app",
    ]


def test_the_checker_catches_a_grant_that_is_more_than_read_only() -> None:
    for widened in (
        "GRANT ALL ON SCHEMA partman TO georag_app;\n",
        "GRANT INSERT, UPDATE ON partman.part_config TO georag_app;\n",
        "GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA partman TO georag_app;\n",
        "GRANT EXECUTE ON ALL PROCEDURES IN SCHEMA partman TO georag_app;\n",
    ):
        found = problems(_ROLE + _GOOD + widened)
        assert any("more than read-only" in p for p in found), widened


def test_the_checker_reads_statements_not_comments() -> None:
    commented = (
        "--   GRANT USAGE ON SCHEMA partman TO georag_app;\n"
        "--   GRANT SELECT ON partman.part_config TO georag_app;\n"
    )
    assert problems(_ROLE + commented) == [
        "no GRANT USAGE ON SCHEMA partman TO georag_app",
        "no GRANT SELECT on partman.part_config to georag_app",
    ]
    # ...and a commented-out WIDE grant is not a violation either.
    assert problems(_ROLE + _GOOD + "-- GRANT ALL ON SCHEMA partman TO georag_app;\n") == []


def test_grants_on_other_schemas_are_not_this_tests_business() -> None:
    assert problems(
        _ROLE + _GOOD + "GRANT ALL ON ALL TABLES IN SCHEMA silver TO georag_app;\n"
    ) == []
