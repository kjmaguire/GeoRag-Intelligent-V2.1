#!/usr/bin/env python3
"""Fail if an ECS health check cannot possibly pass.

A health check that always fails is worse than no health check. The container
is marked UNHEALTHY, and because every container here is `essential = true`,
ECS stops and replaces the task — forever. The symptom is a service that keeps
restarting, which looks exactly like the fault the probe was added to detect.

That shipped. `hatchet-worker` probed with

    ["CMD-SHELL", "wget -q -O - http://localhost:8001/health ... || exit 1"]

while running the FASTAPI image, which installs curl and has never carried
wget — zero occurrences in docker/fastapi.Dockerfile, whose own HEALTHCHECK
uses curl, on a python:3.13-slim runtime that ships neither by default. `sh -c`
would report "wget: not found", exit 127, and the `|| exit 1` would fire. The
form was copied from the hatchet-engine and martin checks, whose third-party
images do have wget.

It was also the probe with the most riding on it: it is the only thing that
can catch a worker WEDGED holding its queue leases, as opposed to one that has
crashed. Compose greps /proc/1/cmdline, which proves only that the process
exists.

TWO ASSERTIONS:

  1. For a service running an image this repository BUILDS, the binary its
     health check invokes must be installed by that Dockerfile. Third-party
     images are skipped and counted — their commands are carried over from
     compose, where they are already proven against the same pinned digest.

  2. A service named in `local.zero_downtime_services` must run desired >= 2.
     The 50% minimum-healthy floor is unsatisfiable at desired 1, so naming a
     single-task service there buys nothing while reading as if it did.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent
TF = ROOT / "deploy" / "aws" / "terraform"

#: Which Dockerfile builds each image name used by `local.service_image`.
DOCKERFILE = {
    "fastapi": "docker/fastapi.Dockerfile",
    "laravel": "docker/laravel.Dockerfile",
    "martin": "docker/martin.Dockerfile",
}

#: A Dockerfile that installs packages is one whose binary set we control.
INSTALLS = re.compile(r"apt-get install|apk add|yum install|dnf install")

#: Always present in any image with a shell; never needs installing.
SHELL_BUILTINS = {"test", "[", "echo", "exit", "true", "false", "sh", "cd"}


def _tf_text() -> str:
    return "\n".join(p.read_text() for p in sorted(TF.glob("*.tf")))


def _block(text: str, name: str) -> str:
    """Body of a `<name> = { ... }` local, matched by brace depth."""
    m = re.search(rf"^\s+{re.escape(name)}\s*=\s*\{{", text, re.M)
    if not m:
        return ""
    depth, i = 0, m.end() - 1
    while i < len(text):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[m.end() : i]
        i += 1
    return ""


def _binary(command: str) -> str | None:
    """The executable a healthCheck command actually runs."""
    parts = re.findall(r'"([^"]*)"', command)
    if not parts:
        return None
    if parts[0] == "CMD-SHELL":
        body = parts[1] if len(parts) > 1 else ""
        first = body.strip().split()
        return first[0] if first else None
    if parts[0] == "CMD":
        rest = [p for p in parts[1:] if p]
        # `CMD bash -c '<script>'` runs whatever the script's first word is.
        if len(rest) >= 3 and rest[0] in {"bash", "sh"} and rest[1] == "-c":
            inner = rest[2].strip().split()
            return inner[0] if inner else None
        return rest[0] if rest else None
    return None


def main() -> int:
    text = _tf_text()
    checks = _block(text, "service_healthcheck")
    images = _block(text, "service_image")
    external = _block(text, "external_image")
    services = _block(text, "services")

    image_of = dict(re.findall(r"^\s*([a-z0-9-]+)\s*=\s*\"([a-z0-9-]+)\"", images, re.M))
    third_party = set(re.findall(r"^\s*([a-z0-9-]+)\s*=", external, re.M))
    desired = {
        m.group(1): int(m.group(2))
        for m in re.finditer(r"^\s*([a-z0-9-]+)\s*=\s*\{[^}]*desired\s*=\s*(\d+)", services, re.M)
    }

    problems: list[str] = []
    verified = skipped = 0

    for m in re.finditer(r"^\s*([a-z0-9-]+)\s*=\s*(\[[^\]]*\])", checks, re.M):
        svc, command = m.group(1), m.group(2)
        binary = _binary(command)
        if binary is None or binary in SHELL_BUILTINS:
            continue
        if svc in third_party:
            skipped += 1
            continue
        df = DOCKERFILE.get(image_of.get(svc, ""))
        if df is None:
            skipped += 1
            continue
        path = ROOT / df
        if not path.exists():
            problems.append(f"{svc}: {df} not found")
            continue
        dockerfile = path.read_text()
        # A Dockerfile that installs NO packages cannot tell us its binary set —
        # everything comes from the base. docker/martin.Dockerfile is exactly
        # this: FROM the upstream maplibre image plus a COPY of martin.yaml, and
        # its wget lives in that base (docker-compose.yml records "Martin has
        # wget at /usr/bin/wget (verified 2026-04-19)" and runs the identical
        # command today). Verifying only what we control is the honest line;
        # claiming to have checked a base image we never open is not.
        if not INSTALLS.search(dockerfile):
            skipped += 1
            continue
        if re.search(rf"(?<![\w/-]){re.escape(binary)}(?![\w-])", dockerfile):
            verified += 1
        else:
            problems.append(
                f"{svc}: health check runs `{binary}`, which {df} never installs\n"
                f"      -> the probe exits non-zero every time, the container is marked\n"
                f"         UNHEALTHY, and essential=true makes ECS replace the task forever."
            )

    # Assertion 2 — a 50% floor is unsatisfiable at desired 1.
    zdt = re.search(r"zero_downtime_services\s*=\s*toset\(\[([^\]]*)\]\)", text)
    for svc in re.findall(r'"([a-z0-9-]+)"', zdt.group(1) if zdt else ""):
        if desired.get(svc, 0) < 2:
            problems.append(
                f"{svc}: named in zero_downtime_services but desired = "
                f"{desired.get(svc)}\n"
                f"      -> a 50% minimum-healthy floor cannot be met by one task."
            )

    if problems:
        print(f"\n{len(problems)} ECS health-check problem(s):", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1

    print(
        f"ECS health checks: {verified} verified against the Dockerfile that builds "
        f"the image, {skipped} on images whose binaries come from a third-party base."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
