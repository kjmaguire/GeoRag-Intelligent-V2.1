#!/usr/bin/env python3
"""Check the SQL the code sends against a migrated database (§04e).

Database audit 2026-09-29 PG-15. ArchitectureDocSchemaParityTest checks the
architecture doc against the migrations; nothing checked the *code* against
them, and the audit found a dozen live surfaces naming tables, columns or
functions that no migration creates (PG-3 .. PG-13) — each one a 500, or a
swallowed exception that quietly returned nothing. This is that gate.

Run it after `php artisan migrate` (+ `db:apply-raw`) against the database
CI just built. It:

  1. extracts SQL string literals from every non-test Python module under
     src/ (AST: plain strings, f-strings, `+` concatenation) and PREPAREs
     each one. PREPARE parses and plans without executing, so it resolves
     every relation, column and function the statement names. f-string
     holes are retried with a few plausible fillers ('', TRUE, 1, AND TRUE)
     so a dynamic WHERE fragment does not count as a failure;
  2. does the same for PHP SQL literals under app/ (scripts/ci/
     extract_php_sql.php, token-based);
  3. checks DB::table('...') query-builder chains and Eloquent `$table`
     declarations against the catalog — resolving unqualified names against
     `public` only, because that is production's search_path (PG-4 was
     exactly an unqualified audit.* table);
  4. fails on undefined table / column / function / schema (42P01, 42703,
     42883, 3F000) unless the (file, missing-name) pair is in
     scripts/ci/schema-sql-allowlist.txt. Syntax errors from statically
     unreadable fragments (42601 etc.) are counted and reported, never
     failed: this gate is about names, not about reconstructing every
     dynamic string.

The connection runs with search_path=public, like production's pgsql
connection and asyncpg's default.

Usage:
    python scripts/ci/check_sql_against_schema.py [--dsn DSN]
                                                  [--allowlist PATH]
                                                  [--report PATH]
Without --dsn, libpq's PG* environment variables are used.
"""
from __future__ import annotations

import argparse
import ast
import asyncio
import itertools
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

import asyncpg

REPO = Path(__file__).resolve().parents[2]
DEFAULT_ALLOWLIST = REPO / "scripts" / "ci" / "schema-sql-allowlist.txt"

NAME_ERRORS = {
    "42P01": "undefined table",
    "42703": "undefined column",
    "42883": "undefined function",
    "3F000": "undefined schema",
}

SQL_START = re.compile(r"^\s*(--[^\n]*\n\s*)*(SELECT|INSERT|UPDATE|DELETE|WITH)\b", re.IGNORECASE | re.DOTALL)
PH = "__PH__"
FILLERS = ("", "TRUE", "1", "AND TRUE")


@dataclass
class Finding:
    file: str
    line: int
    kind: str
    missing: str
    detail: str

    @property
    def key(self) -> str:
        return f"{self.file}|{self.missing}"


# ── Python extraction (lifted from the auditor's pg_sqlcheck.py) ──────────


def _render(node: ast.AST) -> tuple[str, bool] | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value, False
    if isinstance(node, ast.JoinedStr):
        parts = [v.value if isinstance(v, ast.Constant) else PH for v in node.values]
        return "".join(str(p) for p in parts), True
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _render(node.left), _render(node.right)
        if left and right:
            return left[0] + right[0], left[1] or right[1]
    return None


def python_statements() -> list[tuple[str, int, str, bool]]:
    out: list[tuple[str, int, str, bool]] = []
    for path in sorted((REPO / "src").rglob("*.py")):
        if ".venv" in path.parts or "tests" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        best: dict[int, tuple[str, bool]] = {}
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Constant, ast.JoinedStr, ast.BinOp)):
                continue
            rendered = _render(node)
            if not rendered or not SQL_START.match(rendered[0]):
                continue
            line = getattr(node, "lineno", 0)
            # A concatenation's parts are nodes too: keep the longest per line.
            if line not in best or len(rendered[0]) > len(best[line][0]):
                best[line] = rendered
        rel = str(path.relative_to(REPO))
        out.extend((rel, line, sql, is_f) for line, (sql, is_f) in sorted(best.items()))
    return out


def _named_params(sql: str, start: int = 0) -> str:
    """SQLAlchemy-style :name -> $n (after `start` positional ones)."""
    names: dict[str, int] = {}

    def sub(m: re.Match[str]) -> str:
        names.setdefault(m.group(1), len(names) + start + 1)
        return f"${names[m.group(1)]}"

    return re.sub(r"(?<![:\w]):([a-zA-Z_]\w*)\b", sub, sql)


def _php_params(sql: str) -> str:
    count = 0

    def q(_m: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return f"${count}"

    sql = re.sub(r"\?(?![|&?])", q, sql)
    return _named_params(sql, start=count)


# ── Error classification ─────────────────────────────────────────────────


_MISSING = [
    re.compile(r'relation "([^"]+)" does not exist'),
    re.compile(r'column "([^"]+)" of relation "([^"]+)" does not exist'),
    re.compile(r'column ([\w.]+) does not exist'),
    re.compile(r'column "([^"]+)" does not exist'),
    re.compile(r"function ([\w.]+)\(.*\) does not exist"),
    re.compile(r'schema "([^"]+)" does not exist'),
]


def _missing_name(message: str) -> str:
    m = _MISSING[1].search(message)
    if m:
        return f"{m.group(2)}.{m.group(1)}"
    for pattern in _MISSING:
        m = pattern.search(message)
        if m:
            return m.group(1)
    return message.split("\n", 1)[0][:120]


async def _prepare(conn: asyncpg.Connection, sql: str) -> tuple[str, str] | None:
    try:
        await conn.prepare(sql)
    except asyncpg.PostgresError as exc:
        message = str(exc).split("\n", 1)[0]
        if message.startswith("operator does not exist"):
            # 42883 too, but a type mismatch (often a filler's), not a name.
            return "42883-operator", message
        return exc.sqlstate or "?", message
    except Exception as exc:  # noqa: BLE001 - driver-level oddities count as "unreadable"
        return "EXC", str(exc)[:200]
    return None


# A hole where an identifier goes (FROM {table}, public_geo.{name},
# t.{col}) cannot be checked statically: a filler there manufactures a
# "relation t does not exist" that is not in the code.
IDENT_HOLE = re.compile(
    r'(\b(FROM|JOIN|INTO|UPDATE|TABLE)\s+"?|\."?)' + PH + "|" + PH + r'"?\.', re.IGNORECASE,
)


async def _prepare_with_fillers(
    conn: asyncpg.Connection, sql: str, convert
) -> tuple[str, str] | None:
    """PREPARE; for templated SQL, succeed if ANY filler combination does.

    Reports the most informative failure: a name error beats a syntax error.
    """
    holes = sql.count(PH)
    if holes == 0:
        return await _prepare(conn, convert(sql))
    if IDENT_HOLE.search(sql):
        return "DYNAMIC", "identifier built at runtime"
    if holes > 5:
        combos = [(f,) * holes for f in FILLERS]
    else:
        combos = list(itertools.product(FILLERS, repeat=holes))
    best: tuple[str, str] | None = None
    for combo in combos:
        candidate = sql
        for filler in combo:
            candidate = candidate.replace(PH, filler, 1)
        result = await _prepare(conn, convert(candidate))
        if result is None:
            return None
        if best is None or (best[0] not in NAME_ERRORS and result[0] in NAME_ERRORS):
            best = result
    return best


# ── PHP builder chains / models against the catalog ──────────────────────


async def load_catalog(conn: asyncpg.Connection) -> dict[str, set[str]]:
    rows = await conn.fetch(
        """
        SELECT n.nspname AS schema, c.relname AS name, a.attname AS column
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
          LEFT JOIN pg_attribute a
            ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
         WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f')
           AND n.nspname <> 'pg_catalog'
        """
    )
    catalog: dict[str, set[str]] = defaultdict(set)
    for r in rows:
        if r["schema"] == "information_schema":
            catalog[f"information_schema.{r['name']}"].add(r["column"])
            continue
        catalog[f"{r['schema']}.{r['name']}"].add(r["column"])
        if r["schema"] == "public":
            catalog[r["name"]].add(r["column"])
    return catalog


_SKIP_REF = re.compile(r"[()*\s:>-]|^\$|^$")
_ALIAS_METHODS = {"orderBy", "orderByDesc", "groupBy", "having", "latest", "oldest"}
_OPERATORS = {"asc", "desc", "=", "<", ">", "<=", ">=", "!=", "<>", "like", "ilike", "in", "is", "not"}


def check_chains(extracted: dict, catalog: dict[str, set[str]]) -> list[Finding]:
    findings: list[Finding] = []
    for model in extracted["models"]:
        if model["table"] not in catalog:
            findings.append(Finding(model["file"], model["line"], "model table", model["table"],
                                    f"$table = '{model['table']}' is not a relation on search_path=public"))
    for chain in extracted["chains"]:
        aliases: dict[str, str] = {}
        unresolved = False
        for spec in chain["tables"]:
            m = re.match(r"^([\w.]+)(?:\s+as\s+(\w+))?$", spec.strip(), re.IGNORECASE)
            if not m:
                unresolved = True
                continue
            name, alias = m.group(1), m.group(2) or m.group(1).split(".")[-1]
            if name not in catalog:
                findings.append(Finding(chain["file"], chain["line"], "builder table", name,
                                        f"DB::table('{name}') is not a relation on search_path=public"))
                unresolved = True
                continue
            aliases[alias] = name
            aliases[name] = name
        if unresolved or not aliases:
            continue
        columns = set().union(*(catalog[t] for t in set(aliases.values())))
        # Names a select list introduces ("x AS y") may be grouped/ordered by;
        # a raw select can introduce any, so those refs go unchecked there.
        select_aliases = set()
        for _m, ref, _l in chain["refs"]:
            parts = re.split(r"\s+as\s+", ref, flags=re.IGNORECASE)
            if len(parts) == 2:
                select_aliases.add(parts[1].strip())
        raw_select = any(m.endswith("Raw") or m == "raw" for m in chain.get("methods", []))
        for method, ref, line in chain["refs"]:
            ref = re.split(r"\s+as\s+", ref, flags=re.IGNORECASE)[0].strip()
            if _SKIP_REF.search(ref) or ref.lower() in _OPERATORS:
                continue
            if method in _ALIAS_METHODS and (raw_select or ref in select_aliases):
                continue
            if "." in ref:
                qualifier, column = ref.rsplit(".", 1)
                if qualifier in aliases and column not in catalog[aliases[qualifier]]:
                    findings.append(Finding(chain["file"], line, f"builder column ({method})",
                                            f"{aliases[qualifier]}.{column}", ref))
            elif ref not in columns:
                table = "/".join(sorted(set(aliases.values())))
                findings.append(Finding(chain["file"], line, f"builder column ({method})",
                                        f"{table}.{ref}", ref))
    return findings


# ── Main ────────────────────────────────────────────────────────────────


def load_allowlist(path: Path) -> set[str]:
    if not path.exists():
        return set()
    entries = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            entries.add(line)
    return entries


def extract_php() -> dict:
    """Run the PHP extractor (blocking; done before the event loop starts)."""
    php = subprocess.run(
        ["php", str(REPO / "scripts" / "ci" / "extract_php_sql.php"), "app"],
        check=True, capture_output=True, text=True, cwd=REPO,
    )
    return json.loads(php.stdout)


async def run(args: argparse.Namespace, extracted: dict) -> int:
    conn = await asyncpg.connect(args.dsn, server_settings={"search_path": "public"})
    try:
        findings: list[Finding] = []
        other: Counter[str] = Counter()
        total = 0

        for file, line, sql, is_f in python_statements():
            total += 1
            result = await _prepare_with_fillers(conn, sql, _named_params) if is_f \
                else await _prepare(conn, _named_params(sql))
            if result is None:
                continue
            state, message = result
            if state in NAME_ERRORS:
                findings.append(Finding(file, line, NAME_ERRORS[state], _missing_name(message), message))
            else:
                other[state] += 1

        for item in extracted["strings"]:
            total += 1
            result = await _prepare_with_fillers(conn, item["sql"], _php_params)
            if result is None:
                continue
            state, message = result
            if state in NAME_ERRORS:
                findings.append(Finding(item["file"], item["line"], NAME_ERRORS[state],
                                        _missing_name(message), message))
            else:
                other[state] += 1

        catalog = await load_catalog(conn)
        findings.extend(check_chains(extracted, catalog))
        total += len(extracted["chains"]) + len(extracted["models"])
    finally:
        await conn.close()

    allow = load_allowlist(Path(args.allowlist))
    failing = [f for f in findings if f.key not in allow]
    matched = {f.key for f in findings}
    stale = sorted(allow - matched)

    if args.report:
        Path(args.report).write_text(json.dumps([f.__dict__ for f in findings], indent=1), encoding="utf-8")

    print(f"checked {total} statements / chains / models; "
          f"{len(findings)} name error(s), {len(findings) - len(failing)} allow-listed; "
          f"unreadable (not failed): {dict(other) or 0}")
    for key in stale:
        print(f"note: allow-list entry no longer needed, delete it: {key}")
    for f in sorted(failing, key=lambda x: (x.file, x.line)):
        print(f"::error file={f.file},line={f.line}::{f.kind}: {f.missing} — {f.detail}")
    if failing:
        print(f"\n{len(failing)} reference(s) to schema objects no migration creates. Fix the code "
              f"(or the migration); allow-list only with a comment naming the finding/PR that fixes it.")
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--dsn", default=None, help="libpq DSN (default: PG* env vars)")
    parser.add_argument("--allowlist", default=str(DEFAULT_ALLOWLIST))
    parser.add_argument("--report", default=None, help="write every finding as JSON here")
    args = parser.parse_args()
    return asyncio.run(run(args, extract_php()))


if __name__ == "__main__":
    sys.exit(main())
