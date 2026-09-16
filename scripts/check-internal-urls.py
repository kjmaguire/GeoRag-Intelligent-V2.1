#!/usr/bin/env python3
"""Fail if a service-to-service URL falls back to a compose hostname on AWS.

Two of these shipped, and both were invisible because the fallback is a
*plausible* string rather than an empty one:

    LARAVEL_INTERNAL_URL   app/services/laravel_bridge.py -> http://laravel.test
    MARTIN_INTERNAL_URL    config/services.php           -> http://martin:3000

Compose resolves `martin` on its shared docker network, and `laravel.test` is
the Herd default a developer's machine answers. Neither resolves inside the
VPC: an ECS task in awsvpc mode gets no search domain for the Cloud Map
namespace, so only the fully-qualified `<service>.<namespace>` form works.

The LARAVEL_INTERNAL_URL one is not hypothetical. It was unset on Azure too,
and every bridge call fell through to laravel.test and died on DNS — for
WEEKS, because the only symptom was a per-call "Name or service not known"
that reads like a network blip. Ingestion progress, workspace-data-updated,
report-build progress, the admin surfaces and the user inbox were all dead
while every container stayed healthy and /up returned 200. cd.yml's header
claims that instance "cannot recur" because the internal URLs live in
Terraform now. It was not in Terraform; this check is what makes the claim
true.

THE RULE. A variable whose code default is a URL pointing at a compose
service name must be set explicitly in deploy/aws/terraform/. Anything
deliberately left unset goes on ALLOWED below, with the reason, so the
decision is visible rather than absent.

Deliberately narrow, to stay quiet on the false positives that make a checker
get ignored: the default must be URL-SHAPED (a scheme, or host:port). That is
what separates `MARTIN_INTERNAL_URL = "http://martin:3000"` from
`HATCHET_PG_DB = "hatchet"` (a database name) and `REDIS_CLUSTER = "redis"`
(a Laravel cluster mode), neither of which is an address at all.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent
TF = ROOT / "deploy" / "aws" / "terraform"

#: Hostnames that only mean something on the compose network.
COMPOSE_HOSTS = {
    "martin", "fastapi", "qdrant", "redis", "sparse", "embedding", "reranker",
    "laravel-octane", "laravel-horizon", "laravel-reverb", "hatchet", "hatchet-lite",
    "postgresql", "pgbouncer", "minio", "seaweedfs", "laravel.test",
}

#: Left unset on purpose. Each entry is a decision, not an oversight.
ALLOWED = {
    "EMBEDDING_SERVICE_URL": "sidecar proxy; production sets EMBEDDING_BACKEND=bedrock, "
                             "which never reaches this path",
    "RERANKER_SERVICE_URL": "sidecar proxy; production sets RERANKER_BACKEND=bedrock",
    "SPARSE_SERVICE_URL": "explicitly emptied on hatchet-worker so it loads SPLADE++ "
                          "in-process rather than calling the sparse service",
    "PDF_VL_BACKEND_URL": "the vllm-vl sidecar is not deployed; figure_extractor "
                          "returns None on any backend failure and callers keep their "
                          "heuristic-text fallback",
}

#: A default is an ADDRESS only if it carries a scheme or an explicit port.
URLISH = re.compile(r"^(?:[a-z][a-z0-9+.-]*://|[^/\s:]+:\d+)")

PHP_ENV = re.compile(r"""env\(\s*['"]([A-Z][A-Z0-9_]{2,})['"]\s*,\s*['"]([^'"]+)['"]""")
PY_ENV = re.compile(
    r"""(?:os\.environ\.get|os\.getenv)\(\s*["']([A-Z][A-Z0-9_]{2,})["']\s*,\s*["']([^"']+)["']"""
)
#: Module-level constants used as a fallback, which is how LARAVEL_INTERNAL_URL hid.
PY_CONST = re.compile(r"""^_?DEFAULT_([A-Z0-9_]+)\s*=\s*["']([^"']+)["']""", re.M)


def _host(value: str) -> str:
    return re.sub(r"^[a-z][a-z0-9+.-]*://", "", value).split("/")[0].split(":")[0]


def terraform_sets() -> set[str]:
    text = "\n".join(p.read_text() for p in sorted(TF.glob("*.tf")))
    names = set(re.findall(r"^\s+([A-Z][A-Z0-9_]{2,})\s*=", text, re.M))
    names |= set(re.findall(r'name\s*=\s*"([A-Z][A-Z0-9_]{2,})"', text))
    return names


def findings() -> list[str]:
    provided = terraform_sets()
    out: list[str] = []
    seen: set[str] = set()

    def consider(var: str, default: str, where: str) -> None:
        if var in seen or var in ALLOWED or var in provided:
            return
        if not URLISH.match(default) or _host(default) not in COMPOSE_HOSTS:
            return
        seen.add(var)
        out.append(
            f"{var} (default {default!r}, {where})\n"
            f"      -> set it in deploy/aws/terraform/config.tf to "
            f"http://<service>.${{...namespace.name}}:<port>, or add it to ALLOWED "
            f"in this script with the reason it stays unset."
        )

    for php in sorted((ROOT / "config").glob("*.php")):
        for n, line in enumerate(php.read_text().splitlines(), 1):
            for m in PHP_ENV.finditer(line):
                consider(m.group(1), m.group(2), f"config/{php.name}:{n}")

    app = ROOT / "src" / "fastapi" / "app"
    for py in sorted(app.rglob("*.py")):
        if ".venv" in str(py):
            continue
        text = py.read_text(errors="ignore")
        rel = py.relative_to(ROOT)
        for n, line in enumerate(text.splitlines(), 1):
            for m in PY_ENV.finditer(line):
                consider(m.group(1), m.group(2), f"{rel}:{n}")
        for m in PY_CONST.finditer(text):
            # A module constant used as a fallback is how LARAVEL_INTERNAL_URL
            # hid: `os.environ.get("X")` with NO inline default, then `or
            # _DEFAULT_X`. The constant's name does not yield the variable's, so
            # instead take every env var this file reads without a default and
            # require at least one of them to be wired. The constant itself is
            # legitimate — it is what keeps local Herd development working — so
            # this reports only when nothing in the file is set on AWS.
            if not URLISH.match(m.group(2)) or _host(m.group(2)) not in COMPOSE_HOSTS:
                continue
            bare = set(re.findall(
                r"""(?:os\.environ\.get|os\.getenv)\(\s*["']([A-Z][A-Z0-9_]{2,})["']\s*\)""",
                text,
            ))
            if not bare or bare & provided or bare & set(ALLOWED):
                continue
            name = f"_DEFAULT_{m.group(1)}"
            if name in seen:
                continue
            seen.add(name)
            out.append(
                f"{name} = {m.group(2)!r} ({rel}) backs "
                f"{', '.join(sorted(bare))}, none of which Terraform sets\n"
                f"      -> that fallback cannot resolve inside the VPC. Set the "
                f"variable in deploy/aws/terraform/config.tf."
            )
    return out


def main() -> int:
    bad = findings()
    if bad:
        print(f"\n{len(bad)} internal URL(s) fall back to a compose host:", file=sys.stderr)
        for b in bad:
            print(f"  - {b}", file=sys.stderr)
        return 1
    print(
        f"Internal URLs: every service-to-service address is set in Terraform or "
        f"deliberately allowed ({len(ALLOWED)} documented exceptions)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
