#!/usr/bin/env python3
"""Fail if a role the platform connects AS cannot log in.

Postgres splits "has grants" from "may connect". A role can own every
privilege in the database and still be refused at the door, and the refusal
is not a permissions error an operator recognises — it is

    FATAL:  role "georag_app" is not permitted to log in

at container start, on every task, forever. On 2026-09-16 that was true of
three roles at once on a fresh AWS deployment:

  georag_app       every application container. Migration 2026_04_09_173750
                   creates it NOLOGIN on ANY pgsql connection (its only guard
                   is a driver check; the `_for_test_db` in the filename is
                   misleading). The role WITH login lives in
                   database/raw/phase1/10-georag-app-role.sql, which is not
                   listed in database/raw/manifest.json and so has never been
                   applied by `db:apply-raw` -- and could not win anyway,
                   because services.tf runs `migrate` BEFORE `db:apply-raw`
                   and phase1/10's CREATE is itself IF NOT EXISTS.
  martin_readonly  the martin task. Created NOLOGIN by migration
                   2026_04_22_130000 under a "grant-holder roles are not
                   connected as" rule that is true of georag_read/write/audit
                   and false of this one -- config.tf:36 says in so many words
                   that MARTIN_DATABASE_URL "connects as martin_readonly".
  hatchet          the engine's own database.

None of it was caught anywhere. Compose connects martin as georag_app
(docker-compose.yml:1993), and ci.yml applies only database/raw/phase0/*.sql
and migrates as the owner role, so dev and CI both work and only production
breaks. `ALTER ROLE ... PASSWORD`, which is what README Step 2 told the
operator to run, leaves rolcanlogin false -- verified against PostgreSQL 16,
not inferred.

ASSERTIONS:

  1. Every role in CONNECT_AS gets LOGIN in bootstrap.sql.
  2. It gets it UNCONDITIONALLY, via a bare ALTER ROLE -- not only inside a
     create-if-absent. Every database that already exists has these roles
     NOLOGIN, so a fix that only fires on creation repairs none of them, and
     bootstrap.sql's contract is that re-running it is how you verify it.
  3. No psql `:'var'` appears inside a dollar-quoted block in any deploy
     .sql file. psql substitutes variables in its own lexer and that lexer
     treats a $$...$$ body as one opaque token, so the server receives a
     literal colon and answers `syntax error at or near ":"`. With
     ON_ERROR_STOP that aborts bootstrap.sql -- the first command of the
     whole go-live sequence. This one is general, not a list: it is the
     mistake, not the instance.
  4. Migration 2026_04_09_173750 still guards its CREATE ROLE with
     IF NOT EXISTS. Assertion 1's fix works by getting there first; drop the
     guard and the migration fails outright on a bootstrapped database.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BOOTSTRAP = REPO / "deploy" / "aws" / "bootstrap.sql"

# Roles some container authenticates as, with where that is declared.
CONNECT_AS = {
    "georag_app": "config.tf POSTGRES_USER / DB_USERNAME — every app service",
    "martin_readonly": "config.tf:36 + :163 — MARTIN_DATABASE_URL, the martin task",
    "hatchet": "config.tf HATCHET_DATABASE_URL — the engine",
}

# Roles that hold grants and are deliberately never connected as. If one of
# these ever gains LOGIN, that is a finding in the other direction.
GRANT_HOLDERS = ("georag_read", "georag_write", "georag_audit")

NOLOGIN_MIGRATION = (
    REPO
    / "database"
    / "migrations"
    / "2026_04_09_173750_provision_core_schemas_and_roles_for_test_db.php"
)

failures: list[str] = []


def fail(msg: str) -> None:
    failures.append(msg)


def strip_sql_comments(text: str) -> str:
    """Drop `-- ...` line comments so prose about SQL is not read as SQL."""
    return "\n".join(line.split("--", 1)[0] for line in text.splitlines())


def main() -> int:
    if not BOOTSTRAP.is_file():
        print(f"missing {BOOTSTRAP}", file=sys.stderr)
        return 1

    raw = BOOTSTRAP.read_text(encoding="utf-8")
    sql = strip_sql_comments(raw)

    # 1 + 2 — LOGIN, and unconditionally.
    for role, where in CONNECT_AS.items():
        grants_login = re.search(
            rf"\bROLE\s+{re.escape(role)}\b[^;]*\bLOGIN\b", sql, re.IGNORECASE
        )
        if not grants_login:
            fail(
                f"{role}: bootstrap.sql never grants it LOGIN, but it is the role "
                f"{where} connects as. Every task using it dies at startup on "
                f'FATAL: role "{role}" is not permitted to log in.'
            )
            continue

        repairs = re.search(
            rf"^\s*ALTER\s+ROLE\s+{re.escape(role)}\b[^;]*\bLOGIN\b",
            sql,
            re.IGNORECASE | re.MULTILINE,
        )
        if not repairs:
            fail(
                f"{role}: LOGIN is granted only where the role is created. Every "
                f"database that already exists has it NOLOGIN, so this repairs "
                f"none of them. Add a bare `ALTER ROLE {role} LOGIN PASSWORD ...`."
            )

    # 1b — and the grant-holders must NOT have gained it.
    for role in GRANT_HOLDERS:
        if re.search(rf"\bROLE\s+{re.escape(role)}\b[^;]*\bLOGIN\b", sql, re.IGNORECASE):
            fail(
                f"{role} is a grant-holder role and nothing connects as it, but "
                f"bootstrap.sql now gives it LOGIN. That widens the credential "
                f"surface for no reason — remove it, or move it to CONNECT_AS "
                f"with a citation if something genuinely connects as it now."
            )

    # 3 — the psql interpolation trap, across every deploy .sql file.
    for path in sorted((REPO / "deploy").rglob("*.sql")):
        body = path.read_text(encoding="utf-8")
        for block in re.finditer(r"\$(\w*)\$(.*?)\$\1\$", body, re.DOTALL):
            hit = re.search(r":'(\w+)'", block.group(2))
            if hit:
                line = body[: block.start() + hit.start()].count("\n") + 1
                fail(
                    f"{path.relative_to(REPO)}:{line}: :'{hit.group(1)}' sits inside "
                    f"a dollar-quoted block. psql does not substitute there — the "
                    f"server receives a literal ':' and answers `syntax error at or "
                    f'near ":"`, aborting the file under ON_ERROR_STOP. Do the '
                    f"substitution at the top level (SELECT format(...) + \\gexec)."
                )

    # 5 — every secret key bootstrap.sql tells the operator to read exists.
    # `jq -r` on a key that is absent prints the string "null", so following a
    # wrong key sets a password to the literal four characters `null` from a
    # command that appeared to succeed. HATCHET_DB_PASSWORD was exactly this:
    # a real key name, in compose, that georag/app has never had.
    readme = REPO / "deploy" / "aws" / "README.md"
    if readme.is_file():
        readme_text = readme.read_text(encoding="utf-8")
        documented = set(re.findall(r"^\|\s*`([A-Z][A-Z0-9_]+)`", readme_text, re.MULTILINE))
        if documented:
            for source in (BOOTSTRAP, readme):
                body = source.read_text(encoding="utf-8")
                for m in re.finditer(r"jq -r \.([A-Z][A-Z0-9_]+)", body):
                    key = m.group(1)
                    if key in documented:
                        continue
                    line = body[: m.start()].count("\n") + 1
                    fail(
                        f"{source.relative_to(REPO)}:{line}: tells the operator to read "
                        f"`{key}` out of georag/app, but that key is not in the secret "
                        f"table in deploy/aws/README.md. `jq -r` prints the string "
                        f'"null" for a missing key, so this sets a value to the literal '
                        f"four characters `null` from a command that looked like it "
                        f"worked."
                    )

    # 4 — the coupling assertion 1's fix depends on.
    if NOLOGIN_MIGRATION.is_file():
        mig = NOLOGIN_MIGRATION.read_text(encoding="utf-8")
        if "CREATE ROLE georag_app" in mig and "IF NOT EXISTS" not in mig:
            fail(
                f"{NOLOGIN_MIGRATION.relative_to(REPO)}: CREATE ROLE georag_app is "
                f"no longer guarded by IF NOT EXISTS. bootstrap.sql creates that "
                f"role first, with LOGIN, so an unguarded CREATE now fails the "
                f"whole migrate task on a correctly bootstrapped database."
            )

    if failures:
        print("Bootstrap role check FAILED\n", file=sys.stderr)
        for f in failures:
            print(f"  ✗ {f}\n", file=sys.stderr)
        print(
            "A role that cannot log in is not a permissions problem an operator\n"
            "recognises. See deploy/aws/bootstrap.sql for why each of these is\n"
            "created there rather than in a migration.",
            file=sys.stderr,
        )
        return 1

    roles = ", ".join(CONNECT_AS)
    print(f"✓ bootstrap.sql grants LOGIN to every connect-as role ({roles})")
    print("✓ each is repaired unconditionally, not only on creation")
    print("✓ no psql :'var' inside a dollar-quoted block in deploy/**/*.sql")
    print("✓ every secret key the operator is told to read is one that exists")
    return 0


if __name__ == "__main__":
    sys.exit(main())
