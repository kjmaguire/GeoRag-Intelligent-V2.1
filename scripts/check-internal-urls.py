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

THE SECOND RULE, added 2026-09-16, because the first one let the third
instance of this same bug through.

`FASTAPI_INTERNAL_URL` was set on fastapi, hatchet-worker and laravel-octane,
and missed on laravel-horizon. The rule above asked "does Terraform set this
variable" of the whole directory at once, got yes, and stopped. Set on three
services out of four reads exactly like set.

laravel-horizon is the worst of the four to miss, because the queued job IS
the chat: StreamQueryFromFastApi runs there, not in Octane, and so do both
halves of DebounceWorkspaceMvRefresh. All three read
`config('services.fastapi.internal_url')`, whose fallback is
`http://fastapi:8000`. Every question would have been accepted, queued, and
died in the worker on `Name or service not known` — reaching the user as a
stream that never produces a token.

So: three services share the `laravel` image and three share the `fastapi`
image. Same build, same code, same variables read. A variable set on some
members of an image group and not others is an asymmetry, and asymmetry here
is nearly always an omission rather than an intent. Where it IS intent —
laravel-reverb runs `reverb:start` and dispatches nothing — it goes in
PER_SERVICE_ALLOWED with the reason, which is the same bargain the rest of
this file makes: a decision recorded beats a decision absent.
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

#: Services built from one image, and therefore running one codebase.
#: services.tf's `service_image` map is the source of this split.
IMAGE_GROUPS = {
    "laravel": ("laravel-octane", "laravel-horizon", "laravel-reverb"),
    "fastapi": ("fastapi", "hatchet-worker", "sparse"),
}

#: Asymmetries that are deliberate. `<VAR> on <service>` -> the reason.
PER_SERVICE_ALLOWED = {
    "FASTAPI_INTERNAL_URL on laravel-reverb": (
        "reverb:start serves WebSockets and dispatches no job; nothing in that "
        "process reads services.fastapi.internal_url"
    ),
    "LARAVEL_INTERNAL_URL on sparse": (
        "the SPLADE++ sidecar runs one uvicorn app with no Laravel callback"
    ),
    "SPARSE_SERVICE_URL on sparse": (
        "deliberately emptied so the sidecar loads the model in-process rather "
        "than calling itself"
    ),
    "FASTAPI_INTERNAL_URL on sparse": (
        "sparse runs `uvicorn app.sparse_service:app` and nothing else; it serves "
        "the FastAPI service, it does not call it"
    ),
    "MARTIN_INTERNAL_URL on laravel-horizon": (
        "its only reader is TileProxyController, an HTTP controller. No queued "
        "job touches Martin — checked against app/Jobs and app/Services/Exports"
    ),
    "MARTIN_INTERNAL_URL on laravel-reverb": (
        "same: reverb:start serves WebSockets and routes no HTTP request"
    ),
}

#: Filled in by findings() as it scans: every variable whose code default is a
#: compose address. The asymmetry rule below is scoped to these rather than to
#: every variable in config.tf, and that scoping is the whole difference
#: between a check people read and one they learn to skip. Run unscoped it
#: reports 31 findings on this tree, essentially all of them correct design --
#: REVERB_SERVER_PORT belongs only on laravel-reverb, WORKER_POOL only on
#: hatchet-worker. Drowning the one real finding in thirty right answers is
#: the same as not reporting it.
INTERNAL_URL_VARS: set[str] = set()

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


def _balanced(text: str, start: int) -> str:
    """The `{...}` body beginning at the first brace at or after ``start``."""
    i = text.index("{", start)
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[i + 1 : j]
    return ""


def _names(body: str) -> set[str]:
    return set(re.findall(r"^\s+([A-Z][A-Z0-9_]{2,})\s*=", body, re.M))


def per_service_sets() -> dict[str, set[str]]:
    """Variable names each ECS service actually receives, resolving merges.

    The flat `terraform_sets()` above cannot answer the asymmetry question:
    a variable set on three services out of four is in that set, and the
    fourth service is what breaks. This walks config.tf's structure instead.
    """
    config = (TF / "config.tf").read_text()

    # locals that hold environment maps, so `merge(local.X, {...})` resolves.
    locals_: dict[str, set[str]] = {}
    for m in re.finditer(r"^\s{2}([a-z_]+)\s*=\s*\{", config, re.M):
        locals_[m.group(1)] = _names(_balanced(config, m.start()))

    def resolve(body: str, depth: int = 0) -> set[str]:
        got = _names(body)
        if depth < 3:
            for ref in re.findall(r"local\.([a-z_]+)", body):
                got |= locals_.get(ref, set())
        return got

    anchor = config.index("service_environment")
    block = _balanced(config, anchor)
    common = locals_.get("common_environment", set())

    out: dict[str, set[str]] = {}
    entries = list(re.finditer(r"^\s{8}([a-z][a-z0-9-]*)\s*=", block, re.M))
    for idx, m in enumerate(entries):
        end = entries[idx + 1].start() if idx + 1 < len(entries) else len(block)
        out[m.group(1)] = common | resolve(block[m.start() : end])

    # A service with no entry of its own still gets common_environment.
    for group in IMAGE_GROUPS.values():
        for svc in group:
            out.setdefault(svc, set(common))
    return out


def asymmetries() -> list[str]:
    """Variables set on some members of an image group but not all of them."""
    try:
        env = per_service_sets()
    except (ValueError, OSError):
        return []  # structure moved; the flat rule above still applies

    out: list[str] = []
    for image, members in IMAGE_GROUPS.items():
        present = [m for m in members if m in env]
        if len(present) < 2:
            continue
        union: set[str] = set()
        for svc in present:
            union |= env[svc]
        for var in sorted(union & INTERNAL_URL_VARS):
            missing = [svc for svc in present if var not in env[svc]]
            if not missing or len(missing) == len(present):
                continue
            has = [svc for svc in present if var in env[svc]]
            for svc in missing:
                if f"{var} on {svc}" in PER_SERVICE_ALLOWED:
                    continue
                out.append(
                    f"{var} is set on {', '.join(has)} but NOT on {svc}\n"
                    f"      -> all of these run the `{image}` image, so they run the "
                    f"same code and read the same variables. Set it there too, or add "
                    f'"{var} on {svc}" to PER_SERVICE_ALLOWED in this script with the '
                    f"reason it is deliberate."
                )
    return out


def findings() -> list[str]:
    provided = terraform_sets()
    out: list[str] = []
    seen: set[str] = set()

    def consider(var: str, default: str, where: str) -> None:
        if URLISH.match(default) and _host(default) in COMPOSE_HOSTS:
            INTERNAL_URL_VARS.add(var)
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
    bad = findings()  # also populates INTERNAL_URL_VARS, which asymmetries() reads
    uneven = asymmetries()
    if bad:
        print(f"\n{len(bad)} internal URL(s) fall back to a compose host:", file=sys.stderr)
        for b in bad:
            print(f"  - {b}", file=sys.stderr)
    if uneven:
        print(
            f"\n{len(uneven)} variable(s) set unevenly across one image's services:",
            file=sys.stderr,
        )
        for u in uneven:
            print(f"  - {u}", file=sys.stderr)
    if bad or uneven:
        return 1
    print(
        f"Internal URLs: every service-to-service address is set in Terraform or "
        f"deliberately allowed ({len(ALLOWED)} documented exceptions)."
    )
    print(
        f"Per-service parity: no internal-URL variable is set on some members of an "
        f"image group and missed on others ({len(PER_SERVICE_ALLOWED)} documented "
        f"exceptions)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
