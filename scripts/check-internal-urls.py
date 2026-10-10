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

THE THIRD RULE, added 2026-10-10: all of the above applies to the Helm chart
as well. The chart was the same bug a second time, in all of its forms at once:
FASTAPI_INTERNAL_URL was missing on Horizon, MARTIN_INTERNAL_URL on Octane,
and LARAVEL_INTERNAL_URL on both FastAPI and the Hatchet worker, so on-prem the
queued chat job, the tile proxy and every progress callback fell back to
compose hostnames (`http://fastapi:8000`, `http://martin:3000`,
`http://laravel.test`) that do not resolve in a cluster, with every pod
healthy. The chart is read through its committed renders
(kubernetes/manifests/<flavor>.yaml, written from charts/georag/ by
scripts/regenerate_k8s_manifests.sh) with regular expressions, because CI's job
has neither a helm binary nor a YAML library. Which means a chart edit that is
not re-rendered is not seen here; the render is what an operator applies, and
what tests/Unit/HelmTenantIsolationManifestTest.php reads.
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
    "EMBEDDING_SERVICE_URL": "sidecar proxy; production sets EMBEDDING_BACKEND=cohere, "
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

#: The Helm chart renders committed under kubernetes/manifests/.
CHART_FLAVORS = ("k3s", "vanilla", "airgap")

#: ALLOWED for the chart. The AWS reasons above do not carry over: on-prem runs
#: the self-hosted backends, so the chart DOES set EMBEDDING_SERVICE_URL,
#: RERANKER_SERVICE_URL and SPARSE_SERVICE_URL (each pointing at its sidecar).
CHART_ALLOWED = {
    "PDF_VL_BACKEND_URL": ALLOWED["PDF_VL_BACKEND_URL"],
}

#: The chart's image groups. The embedding, reranker and sparse sidecars run
#: the fastapi image too, but each serves one uvicorn app that calls nothing, so
#: they are not callers in the sense this rule is about.
CHART_IMAGE_GROUPS = {
    "laravel": ("laravel-octane", "laravel-horizon", "laravel-reverb"),
    "fastapi": ("fastapi", "hatchet-worker"),
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


def chart_env(flavor: str) -> dict[str, set[str]]:
    """Variable names each chart workload receives, by component label.

    Read off the committed render one YAML document at a time. Only Deployments
    and StatefulSets: the install Jobs have their own, different, environment.
    """
    text = (ROOT / "kubernetes" / "manifests" / f"{flavor}.yaml").read_text()
    out: dict[str, set[str]] = {}
    for doc in re.split(r"^---\s*$", text, flags=re.M):
        if not re.search(r"^kind:\s*(?:Deployment|StatefulSet)\s*$", doc, re.M):
            continue
        comp = re.search(r"^\s+app\.kubernetes\.io/component:\s*(\S+)\s*$", doc, re.M)
        if comp:
            out.setdefault(comp.group(1), set()).update(
                re.findall(r"^\s+- name: ([A-Z][A-Z0-9_]{2,})\s*$", doc, re.M)
            )
    return out


def chart_sets() -> set[str]:
    """Every variable any chart workload sets in any flavor."""
    names: set[str] = set()
    for flavor in CHART_FLAVORS:
        for got in chart_env(flavor).values():
            names |= got
    return names


def chart_literal_values(flavor: str) -> list[tuple[str, str, str]]:
    """(component, variable, value) for every literal `value:` a workload sets."""
    text = (ROOT / "kubernetes" / "manifests" / f"{flavor}.yaml").read_text()
    out: list[tuple[str, str, str]] = []
    for doc in re.split(r"^---\s*$", text, flags=re.M):
        if not re.search(r"^kind:\s*(?:Deployment|StatefulSet)\s*$", doc, re.M):
            continue
        comp = re.search(r"^\s+app\.kubernetes\.io/component:\s*(\S+)\s*$", doc, re.M)
        if not comp:
            continue
        for var, value in re.findall(
            r'^\s+- name: ([A-Z][A-Z0-9_]{2,})\s*\n\s+value: "?([^"\n]*)"?\s*$', doc, re.M
        ):
            out.append((comp.group(1), var, value))
    return out


def chart_compose_hosts() -> list[str]:
    """Internal-URL variables the chart sets to a compose hostname.

    Setting the variable is not enough: `http://fastapi:8000` is exactly what
    docker-compose.yml has, and in a cluster the Service is `<release>-fastapi`,
    so a value copied across resolves nowhere. Same bug as the unset variable,
    one step later, and equally silent.
    """
    by_message: dict[str, list[str]] = {}
    for flavor in CHART_FLAVORS:
        try:
            values = chart_literal_values(flavor)
        except OSError:
            continue
        for comp, var, value in values:
            if var in INTERNAL_URL_VARS and URLISH.match(value) and _host(value) in COMPOSE_HOSTS:
                msg = (
                    f"{var} = {value!r} on {comp} names a compose host\n"
                    f"      -> a Service in this chart is `<release>-{_host(value)}`, "
                    f"not `{_host(value)}`; use http://{{{{ .Release.Name }}}}-<service>:<port> "
                    f"in charts/georag/templates/ and re-render."
                )
                by_message.setdefault(msg, []).append(flavor)
    out = []
    for msg, flavors in by_message.items():
        head, _, rest = msg.partition("\n")
        out.append(f"[Helm chart: {', '.join(flavors)}] {head}\n{rest}")
    return out


def _group_asymmetries(
    env: dict[str, set[str]], groups: dict[str, tuple[str, ...]], fix: str
) -> list[str]:
    """Variables set on some members of an image group but not all of them."""
    out: list[str] = []
    for image, members in groups.items():
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
                    f"same code and read the same variables. Set it there too{fix}, or add "
                    f'"{var} on {svc}" to PER_SERVICE_ALLOWED in this script with the '
                    f"reason it is deliberate."
                )
    return out


def asymmetries() -> list[str]:
    """Image-group parity, for Terraform and then for each chart flavor."""
    out: list[str] = []
    try:
        out += _group_asymmetries(per_service_sets(), IMAGE_GROUPS, "")
    except (ValueError, OSError):
        pass  # structure moved; the flat rule still applies

    # The same finding in three flavors is one finding: merge them, and say
    # which flavors have it.
    by_message: dict[str, list[str]] = {}
    for flavor in CHART_FLAVORS:
        try:
            env = chart_env(flavor)
        except OSError:
            continue
        for msg in _group_asymmetries(
            env, CHART_IMAGE_GROUPS,
            " (charts/georag/templates/, then scripts/regenerate_k8s_manifests.sh)",
        ):
            by_message.setdefault(msg, []).append(flavor)
    for msg, flavors in by_message.items():
        head, _, rest = msg.partition("\n")
        out.append(f"[Helm chart: {', '.join(flavors)}] {head}\n{rest}")
    return out


def findings() -> list[str]:
    # Each deployment target must set the variable on its own: Terraform setting
    # it says nothing about the chart. (name, names it sets, allowed, fix, why)
    targets = [
        (
            "Terraform", terraform_sets(), ALLOWED,
            "set it in deploy/aws/terraform/config.tf to "
            "http://<service>.${...namespace.name}:<port>, or add it to ALLOWED "
            "in this script with the reason it stays unset.",
            "that fallback cannot resolve inside the VPC. Set the variable in "
            "deploy/aws/terraform/config.tf.",
        ),
        (
            "the Helm chart", chart_sets(), CHART_ALLOWED,
            "set it in charts/georag/templates/ to "
            "http://{{ .Release.Name }}-<service>:<port>, re-render with "
            "scripts/regenerate_k8s_manifests.sh, or add it to CHART_ALLOWED in "
            "this script with the reason it stays unset.",
            "that fallback cannot resolve in the cluster. Set the variable in "
            "charts/georag/templates/ and re-render with "
            "scripts/regenerate_k8s_manifests.sh.",
        ),
    ]
    out: list[str] = []
    seen: set[tuple[str, str]] = set()

    def consider(var: str, default: str, where: str) -> None:
        if not URLISH.match(default) or _host(default) not in COMPOSE_HOSTS:
            return
        INTERNAL_URL_VARS.add(var)
        for name, provided, allowed, fix, _why in targets:
            if var in allowed or var in provided or (name, var) in seen:
                continue
            seen.add((name, var))
            where_note = "" if name == "Terraform" else f" is not set in {name}"
            out.append(
                f"{var} (default {default!r}, {where}){where_note}\n"
                f"      -> {fix}"
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
            # this reports only when nothing in the file is set on the target.
            if not URLISH.match(m.group(2)) or _host(m.group(2)) not in COMPOSE_HOSTS:
                continue
            bare = set(re.findall(
                r"""(?:os\.environ\.get|os\.getenv)\(\s*["']([A-Z][A-Z0-9_]{2,})["']\s*\)""",
                text,
            ))
            # The constant is a URL fallback, so the variable that must be wired
            # is a *_URL one. Counting every bare variable lets an unrelated one
            # vouch for it: laravel_bridge.py also reads FASTAPI_SERVICE_KEY,
            # which any target that runs FastAPI sets, so dropping
            # LARAVEL_INTERNAL_URL everywhere used to go unreported. (Falls back
            # to every bare variable when none is URL-shaped.)
            bare = {b for b in bare if b.endswith("_URL")} or bare
            # Those URL variables are internal-URL variables too, so the parity
            # rule covers them: set on FastAPI but forgotten on the worker is
            # the same bug as everywhere else.
            INTERNAL_URL_VARS.update(b for b in bare if b.endswith("_URL"))
            name = f"_DEFAULT_{m.group(1)}"
            for target, provided, allowed, _fix, why in targets:
                if not bare or bare & provided or bare & set(allowed):
                    continue
                if (target, name) in seen:
                    continue
                seen.add((target, name))
                out.append(
                    f"{name} = {m.group(2)!r} ({rel}) backs "
                    f"{', '.join(sorted(bare))}, none of which {target} sets\n"
                    f"      -> {why}"
                )
    return out


def main() -> int:
    bad = findings()  # also populates INTERNAL_URL_VARS, which the rest read
    bad += chart_compose_hosts()
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
        f"Internal URLs: every service-to-service address is set in Terraform and in "
        f"the Helm chart, or deliberately allowed ({len(ALLOWED)} documented "
        f"exceptions for Terraform, {len(CHART_ALLOWED)} for the chart)."
    )
    print(
        f"Per-service parity: no internal-URL variable is set on some members of an "
        f"image group and missed on others, in Terraform or in any chart flavor "
        f"({len(PER_SERVICE_ALLOWED)} documented exceptions)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
