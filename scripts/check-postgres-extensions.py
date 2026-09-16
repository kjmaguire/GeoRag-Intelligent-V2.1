#!/usr/bin/env python3
"""Fail if dev and production disagree about Postgres extensions.

Compose gets its extensions from docker/postgresql/init/*.sql, which Postgres
runs automatically out of docker-entrypoint-initdb.d. RDS has no such
mechanism: deploy/aws/bootstrap.sql is run ONCE, BY HAND, by the operator
(deploy/aws/README.md Step 1). Two lists, two mechanisms, and nothing keeping
them in step — so an extension added to dev would simply never exist in
production.

That failure is not silent at first contact: a migration using a type the
extension provides dies at CD's migrate step with "type geometry does not
exist", which is loud and comprehensible. It is silent in the case that
matters more — an extension whose absence only shows up in a query path
nobody exercises until a user does.

ASSERTIONS:

  1. Every extension compose creates is created by bootstrap.sql too, unless
     it is in ALLOWED_OMISSIONS below with a stated reason.
  2. Nothing is in bootstrap.sql that compose does not create — production
     must not carry an extension dev has never run against.
  3. The omissions justified by "no call sites" STILL have no call sites.
     That is the load-bearing half: pg_ivm and pg_stat_kcache were dropped
     because nothing uses them, and the day something does, the justification
     expires without anyone noticing.

Note on what this does NOT check: whether RDS actually offers a given
extension. That rests on the ADR-0022 audit and is proven only by the first
real `bootstrap.sql` run against the account, which is also where it fails
loudly if the audit was wrong (see the h3 comment in bootstrap.sql).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent
COMPOSE_INIT = ROOT / "docker" / "postgresql" / "init"
AWS_BOOTSTRAP = ROOT / "deploy" / "aws" / "bootstrap.sql"

#: Extension -> (reason, symbols whose presence would invalidate the reason).
#: An empty symbol tuple means the reason does not depend on call sites.
ALLOWED_OMISSIONS: dict[str, tuple[str, tuple[str, ...]]] = {
    "auto_explain": (
        "on RDS this is a shared_preload_libraries PARAMETER, not a CREATE "
        "EXTENSION; deploy/aws/terraform/data.tf sets it in the parameter group",
        (),
    ),
    "pg_ivm": (
        "not offered by RDS for PG 18, and no call sites (ADR-0022)",
        ("pgivm", "INCREMENTAL MATERIALIZED", "create_immv", "refresh_immv"),
    ),
    "pg_stat_kcache": (
        "not offered by RDS for PG 18, and no call sites (ADR-0022)",
        ("pg_stat_kcache",),
    ),
}

#: Where a real call site could live. The init scripts and bootstrap.sql are
#: excluded — they NAME these extensions by definition, in CREATE statements,
#: comments and their own verification blocks.
CALL_SITE_GLOBS = ("database/**/*.sql", "database/**/*.php", "app/**/*.php",
                   "src/fastapi/app/**/*.py", "scripts/**/*.py", "ops/**/*.sql")

#: This file names every symbol in ALLOWED_OMISSIONS, by design. Scanning
#: itself would make the check permanently red on its own table.
SELF = Path(__file__).resolve()

CREATE_EXT = re.compile(
    r'^\s*CREATE\s+EXTENSION\s+(?:IF\s+NOT\s+EXISTS\s+)?"?([a-zA-Z0-9_-]+)"?',
    re.I | re.M,
)


def _extensions(paths) -> set[str]:
    found = set()
    for p in paths:
        for line in p.read_text().splitlines():
            if line.lstrip().startswith("--"):
                continue
            m = CREATE_EXT.match(line)
            if m:
                found.add(m.group(1).lower())
    return found


def _call_sites(symbols: tuple[str, ...]) -> list[str]:
    if not symbols:
        return []
    hits = []
    pattern = re.compile("|".join(re.escape(s) for s in symbols), re.I)
    for glob in CALL_SITE_GLOBS:
        for p in ROOT.glob(glob):
            if ".venv" in str(p) or not p.is_file() or p.resolve() == SELF:
                continue
            for n, line in enumerate(p.read_text(errors="ignore").splitlines(), 1):
                stripped = line.lstrip()
                if stripped.startswith(("--", "#", "//", "*")):
                    continue
                if pattern.search(line):
                    hits.append(f"{p.relative_to(ROOT)}:{n}")
    return hits


def main() -> int:
    if not COMPOSE_INIT.is_dir() or not AWS_BOOTSTRAP.exists():
        print("missing docker/postgresql/init/ or deploy/aws/bootstrap.sql", file=sys.stderr)
        return 1

    compose = _extensions(sorted(COMPOSE_INIT.glob("*.sql")))
    aws = _extensions([AWS_BOOTSTRAP])
    problems: list[str] = []

    for ext in sorted(compose - aws):
        if ext not in ALLOWED_OMISSIONS:
            problems.append(
                f"{ext}: compose creates it, deploy/aws/bootstrap.sql does not\n"
                f"      -> production would never have it. Add it to bootstrap.sql, or\n"
                f"         to ALLOWED_OMISSIONS in this script with the reason."
            )

    for ext in sorted(aws - compose):
        problems.append(
            f"{ext}: bootstrap.sql creates it, compose does not\n"
            f"      -> production would carry an extension dev has never run against."
        )

    for ext, (reason, symbols) in sorted(ALLOWED_OMISSIONS.items()):
        if ext in aws:
            problems.append(
                f"{ext}: listed as a deliberate omission but bootstrap.sql creates it\n"
                f"      -> remove it from ALLOWED_OMISSIONS; the reason on record is "
                f"\"{reason}\"."
            )
            continue
        hits = _call_sites(symbols)
        if hits:
            shown = ", ".join(hits[:3]) + (f" (+{len(hits) - 3} more)" if len(hits) > 3 else "")
            problems.append(
                f"{ext}: omitted from production because \"{reason}\", but it now HAS "
                f"call sites\n      -> {shown}\n"
                f"      -> the justification has expired. Either drop those call sites or\n"
                f"         find a replacement; the extension is not available on RDS."
            )

    if problems:
        print(f"\n{len(problems)} Postgres extension problem(s):", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1

    print(
        f"Postgres extensions: {len(aws)} created in both compose and RDS bootstrap; "
        f"{len(ALLOWED_OMISSIONS)} deliberate omissions, each still justified."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
