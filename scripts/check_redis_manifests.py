#!/usr/bin/env python3
"""Enforce two Redis invariants across every deployment manifest.

Both rules exist because a real defect got through review by looking
correct in isolation. Neither is about style.

RULE 1 -- persistence requires a durable path.
    `--appendonly yes` (or an active RDB save policy) with no volume
    mounted at the data directory is not persistence. It is fsyncs and
    rewrite forks writing to a disk that is discarded on restart. This
    is exactly what shipped to Azure: the container app's manifest
    inherited `--appendonly yes` from docker-compose.yml and did not
    inherit `volumes: redis_data:/data`, so the queue durability the flag
    exists to provide never existed there -- while the scheduler restarted
    the app twice a day. `--save ""` was dropped in the same port,
    silently leaving Redis's default RDB snapshots running too. (That
    manifest was deleted with the rest of deploy/azure/ on 2026-09-08; it
    is `deploy/azure/containerapps/redis.yaml` in git history.)

    That defect is FIXED as of 2026-09-08 (ADR-0022): the AWS deployment
    mounts EFS at /data with AOF on. The rule stays, and now guards the
    fix rather than documenting the wound.

RULE 2 -- a container memory limit requires a reachable maxmemory cap.
    Redis's maxmemory bounds its own dataset accounting. Client output
    buffers, allocator fragmentation and the copy-on-write of every fork
    sit on top of it. Two ways to get this wrong, and this repo had both:
      * no --maxmemory at all (all four k8s/Helm targets, under a 2Gi
        limit) -- Redis grows until the kernel kills the pod;
      * --maxmemory exactly equal to the limit (Azure was 512mb in 0.5Gi)
        -- the platform OOM-kills the container before Redis's own guard
        can engage, which makes the eviction policy unreachable code. It
        did not matter which policy was set.
    docker/compose.redis-staging.yml already states the house rule in its
    own comments, three times: container memory = maxmemory + 25%. This
    checker is that comment, enforced.

WHAT THIS DELIBERATELY DOES NOT CHECK
    The eviction policy itself. volatile-lru is correct here only because
    queue jobs share the instance with cache and carry no TTL, and only
    while nothing calls Cache::forever(). That is a judgement about the
    application, not a property of the manifest, so it lives in
    deploy/aws/terraform/services.tf's comment where the reasoning can be
    read -- not in a regex that would go stale silently.

A NOTE ON SCOPING, WHICH THIS CHECKER GOT WRONG ONCE
    kubernetes/manifests/*.yaml are multi-document files holding a dozen
    workloads, and charts/georag/values.yaml holds every component's
    resources. A `limits:` regex run over a whole file finds SOME
    container's memory limit, reports a confident number, and is wrong.
    Every lookup below is therefore bounded twice: to the YAML document
    containing the redis-server invocation, and to the text after that
    invocation (the resources block follows the command in all of these
    files). Helm resolves through values.yaml's `redis:` section only.

Usage:  python scripts/check_redis_manifests.py
Exit:   0 all invariants hold, 1 otherwise.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

# Overridable so scripts/tests/redis_manifests_test.sh can point the same
# code at deliberately broken copies. Without that, the only thing this
# file could demonstrate is that it prints OK against a tree someone
# already fixed -- which is not evidence that it would catch anything.
REPO = Path(os.environ.get("GEORAG_REPO_ROOT")
            or Path(__file__).resolve().parent.parent)

# Headroom the house rule requires: container memory >= maxmemory * 1.25.
HEADROOM = 1.25

# Kubernetes: Mi/Gi are binary, M/G are decimal.
_K8S_UNITS = {
    "": 1, "b": 1,
    "k": 1000, "ki": 1024,
    "m": 1000 ** 2, "mi": 1024 ** 2,
    "g": 1000 ** 3, "gi": 1024 ** 3,
}
# Redis: kb/mb/gb are BINARY, bare k/m/g are decimal. The opposite of the
# SI reading, and the reason these are two tables instead of one.
_REDIS_UNITS = {
    "": 1, "b": 1,
    "k": 1000, "kb": 1024,
    "m": 1000 ** 2, "mb": 1024 ** 2,
    "g": 1000 ** 3, "gb": 1024 ** 3,
}


def _size(text: str, units: dict, flavour: str) -> int:
    match = re.fullmatch(r"\s*([0-9]*\.?[0-9]+)\s*([a-zA-Z]*)\s*", text)
    if not match:
        raise ValueError(f"unparseable {flavour} size: {text!r}")
    value, suffix = float(match.group(1)), match.group(2).lower()
    if suffix not in units:
        raise ValueError(f"unknown {flavour} unit in {text!r}")
    return int(value * units[suffix])


def k8s_size(text: str) -> int:
    return _size(text, _K8S_UNITS, "kubernetes")


def redis_size(text: str) -> int:
    return _size(text, _REDIS_UNITS, "redis")


def redis_document(text: str) -> tuple:
    """(document, offset of redis-server within it) for the Redis workload.

    Bounds every later lookup to one YAML document, so a `limits:` block
    belonging to Postgres or SeaweedFS in the same file cannot be read as
    Redis's.
    """
    for document in re.split(r"\n---\s*\n", text):
        index = document.find("redis-server")
        if index != -1:
            return document, index
    raise ValueError("no redis-server invocation in any document")


def redis_command(document: str, index: int) -> str:
    """The redis-server invocation, from `redis-server` to its last flag."""
    lines = []
    for line in document[index:].splitlines():
        stripped = line.strip()
        if not lines:
            lines.append(line)
            continue
        if not stripped or not stripped.startswith(("-", "#")):
            break
        lines.append(line)
    return "\n".join(lines)


def flag(command: str, name: str):
    """Value of `--name X`, or None when the flag is absent."""
    match = re.search(r"--" + re.escape(name) + r"[ =]+([^\s\\]+)", command)
    return match.group(1).strip('"') if match else None


class Target:
    """One deployment manifest, and where each fact lives inside it.

    Volume presence is located by an EVIDENCE pattern rather than declared
    as a boolean. A boolean would record what was true the day this file
    was written; an evidence pattern that stops matching fails the check,
    which is the point.
    """

    def __init__(self, path, limit_pattern, volume_evidence,
                 limit_units=k8s_size, values_path=None, values_section=None):
        self.path = path
        self.limit_pattern = limit_pattern
        self.volume_evidence = volume_evidence
        self.limit_units = limit_units
        self.values_path = values_path
        self.values_section = values_section


# Kubernetes nests the memory limit under `resources: limits:`. The
# Container Apps pattern that used to sit beside this went with the Azure
# deployment on 2026-09-08; production is Terraform now, read by
# check_terraform().
_K8S_LIMIT = r"limits:\s*\n\s*cpu:[^\n]*\n\s*memory:\s*(\S+)"

TARGETS = [
    # The production deployment is Terraform, not YAML, so it is checked
    # by check_terraform() below rather than by a Target: the extractors
    # here split documents on `---` and read flags off indented `-` lines,
    # and HCL has neither. Forcing one regex over both shapes is exactly
    # the scoping mistake this file's own header records having made once.
    Target(
        path="charts/georag/templates/redis.yaml",
        limit_pattern=r"limits:\s*\{[^}]*memory:\s*\"?([^\",}]+)\"?",
        volume_evidence=r"mountPath:\s*/data",
        values_path="charts/georag/values.yaml",
        values_section="redis",
    ),
    Target(
        path="kubernetes/manifests/k3s.yaml",
        limit_pattern=_K8S_LIMIT,
        volume_evidence=r"mountPath:\s*/data",
    ),
    Target(
        path="kubernetes/manifests/vanilla.yaml",
        limit_pattern=_K8S_LIMIT,
        volume_evidence=r"mountPath:\s*/data",
    ),
    Target(
        path="kubernetes/manifests/airgap.yaml",
        limit_pattern=_K8S_LIMIT,
        volume_evidence=r"mountPath:\s*/data",
    ),
]

# Compose files resolve maxmemory through ${REDIS_MAXMEMORY:-...} and set
# limits under `deploy:`, so a static read cannot say what a given run
# will use. They are named here rather than omitted: a checker that
# quietly covers 5 of 7 files reads, later, as if it covered all 7.
NOT_CHECKED = [
    ("docker-compose.yml", "maxmemory resolves from ${REDIS_MAXMEMORY} at run time"),
    ("docker/compose.redis-staging.yml", "maxmemory resolves from ${REDIS_MAXMEMORY} at run time"),
]


def values_section(text: str, name: str) -> str:
    """The top-level `name:` block of a values.yaml, and nothing else."""
    match = re.search(r"^" + re.escape(name) + r":\s*\n(.*?)(?=^\S)", text, re.MULTILINE | re.DOTALL)
    if not match:
        raise ValueError(f"no top-level '{name}:' section")
    return match.group(1)


#: The production deployment. Separate from TARGETS because HCL is not YAML.
TERRAFORM_SERVICES = "deploy/aws/terraform/services.tf"
TERRAFORM_SIZING = "deploy/aws/terraform/main.tf"


def check_terraform(problems: list) -> None:
    """The same two rules against deploy/aws/terraform.

    Its own function rather than a Target because the shapes genuinely
    differ: the YAML extractors split documents on `---` and read flags off
    indented `-` lines, and HCL has neither. The RULES are identical; only
    the reading is.
    """
    services = (REPO / TERRAFORM_SERVICES).read_text(encoding="utf-8")
    sizing = (REPO / TERRAFORM_SIZING).read_text(encoding="utf-8")

    # Anchored INSIDE `service_command`, not on any `redis = [` in the file.
    # There is more than one: `service_healthcheck` also keys on service name,
    # so a bare search finds whichever comes first and silently reads the
    # wrong list if the file is reordered. This is the same failure the
    # `attaches` pattern below was tightened for — two different facts that a
    # loose pattern conflates.
    commands = re.search(r"service_command\s*=\s*\{(.*?)\n  \}", services, re.S)
    block = re.search(r"redis\s*=\s*\[(.*?)\n\s*\]", commands.group(1), re.S) if commands else None
    if not block:
        problems.append(
            f"{TERRAFORM_SERVICES}: no `redis = [ ... ]` inside `service_command` "
            "-- the task definition changed shape and this check is no longer "
            "reading it")
        return

    # Flatten the HCL list into the flag string `flag()` already understands.
    command = re.sub(r"[\"',]", " ", block.group(1))

    maxmemory = flag(command, "maxmemory")
    policy = flag(command, "maxmemory-policy")
    appendonly = flag(command, "appendonly")
    save = flag(command, "save")

    # --- rule 1: persistence requires a durable path -------------------
    rdb_active = save is not None and save != ""
    persists = (appendonly == "yes") or rdb_active
    # The EFS volume is attached by a `dynamic "volume"` block gated on the
    # service name, and mounted at /data for redis specifically. BOTH halves
    # are required: a volume that is not mounted at the data directory is
    # the same non-persistence in a different costume.
    # Anchored on `for_each =` specifically. The same contains() gate
    # appears twice — once attaching the EFS volume, once adding the
    # mountPoints — and matching either would let a mount that points at
    # no volume read as durable. Two different facts; the loose pattern
    # conflated them.
    attaches = re.search(
        r'for_each\s*=\s*contains\(\["qdrant", ?"redis"\], ?each\.key\)', services)
    mounts = re.search(r'each\.key == "qdrant" \? "/qdrant/storage" : "/data"', services)
    has_volume = bool(attaches and mounts)

    if persists and not has_volume:
        why = []
        if appendonly == "yes":
            why.append("--appendonly yes")
        if rdb_active:
            why.append(f"--save {save}")
        problems.append(
            "{}: {} but redis has no EFS volume mounted at /data -- the data "
            "directory is ephemeral, so this is cost without durability. "
            "That is precisely the defect the Azure deployment shipped."
            .format(TERRAFORM_SERVICES, " and ".join(why)))
    if not persists:
        problems.append(
            f"{TERRAFORM_SERVICES}: redis persistence is OFF. It was off on "
            "Azure too, and that is why every restart and every nightly "
            "scale-to-zero dropped all sessions and any queued Horizon job. "
            "Turning it back off needs a recorded reason, not a default.")

    # --- rule 2: a memory limit requires a reachable cap ---------------
    limit_match = re.search(r"redis\s*=\s*\{[^}]*memory\s*=\s*(\d+)", sizing)
    if not limit_match:
        problems.append(
            f"{TERRAFORM_SIZING}: could not locate redis's task memory -- the "
            "services map changed shape and this check is no longer reading "
            "it")
        return
    limit = int(limit_match.group(1)) * 1024 ** 2  # Fargate memory is MiB

    if maxmemory is None:
        problems.append(
            f"{TERRAFORM_SERVICES}: task memory is {limit_match.group(1)} MiB "
            "but no --maxmemory is set. Redis will grow until the platform "
            "kills the task; the eviction policy never runs.")
        return

    cap = redis_size(maxmemory)
    required = int(cap * HEADROOM)
    if required > limit:
        problems.append(
            "{}: --maxmemory {} needs a task memory of at least {:.0f} MiB "
            "({}x headroom) but it is {:.0f} MiB. The platform kills the task "
            "before Redis can evict, so --maxmemory-policy {} is unreachable."
            .format(TERRAFORM_SERVICES, maxmemory, required / 1024 ** 2,
                    HEADROOM, limit / 1024 ** 2, policy or "<unset>"))
        return

    print("OK   {:<45} maxmemory {} in {:.0f} MiB ({:.0f}% headroom), policy "
          "{}, persistence {}".format(
              TERRAFORM_SERVICES, maxmemory, limit / 1024 ** 2,
              (limit - cap) / cap * 100, policy or "<unset>",
              "on with volume" if persists and has_volume else "off"))


def check(target: Target, problems: list) -> None:
    text = (REPO / target.path).read_text(encoding="utf-8")
    document, index = redis_document(text)
    command = redis_command(document, index)

    maxmemory = flag(command, "maxmemory")
    policy = flag(command, "maxmemory-policy")
    appendonly = flag(command, "appendonly")
    save = flag(command, "save")

    # Resources live after the command in every one of these files, so
    # searching forward from it keeps a sidecar's block out of range.
    scope = document[index:]

    if target.values_path:
        section = values_section(
            (REPO / target.values_path).read_text(encoding="utf-8"),
            target.values_section)
        scope = section
        for name, raw in (("maxmemory", maxmemory), ("maxmemoryPolicy", policy)):
            if not raw or not raw.startswith("{{"):
                continue
            found = re.search(r"^\s*" + name + r":\s*\"?([^\"\n]+?)\"?\s*$",
                              section, re.MULTILINE)
            if not found:
                problems.append(
                    f"{target.path}: templates {{{{ .Values.redis.{name} }}}} but {target.values_path} has no "
                    "such key under 'redis:'")
                continue
            if name == "maxmemory":
                maxmemory = found.group(1).strip()
            else:
                policy = found.group(1).strip()

    # --- rule 1: persistence requires a durable path -------------------
    rdb_active = save is not None and save != ""
    persists = (appendonly == "yes") or rdb_active
    has_volume = re.search(target.volume_evidence, document) is not None
    if persists and not has_volume:
        why = []
        if appendonly == "yes":
            why.append("--appendonly yes")
        if rdb_active:
            why.append(f"--save {save}")
        problems.append(
            "{}: {} but nothing matches {!r} -- the data directory is "
            "ephemeral, so this is cost without durability".format(
                target.path, " and ".join(why), target.volume_evidence))
    if appendonly != "yes" and save is None:
        problems.append(
            f"{target.path}: --appendonly is not 'yes' and --save is absent, so Redis's "
            "default RDB save points are silently active. Set --save \"\" to "
            "state the intent.")

    # --- rule 2: a memory limit requires a reachable cap ---------------
    limit_match = re.search(target.limit_pattern, scope, re.MULTILINE)
    if not limit_match:
        problems.append(
            f"{target.path}: could not locate Redis's memory limit with {target.limit_pattern!r} -- the "
            "manifest changed shape and this check is no longer reading "
            "it")
        return
    limit = target.limit_units(limit_match.group(1))

    if maxmemory is None:
        problems.append(
            f"{target.path}: container memory limit is {limit_match.group(1)} but no --maxmemory is set. "
            "Redis will grow until the kernel kills it; the eviction policy "
            "never runs.")
        return
    cap = redis_size(maxmemory)
    required = int(cap * HEADROOM)
    if required > limit:
        problems.append(
            "{}: --maxmemory {} needs a container limit of at least {:.0f} MiB "
            "({}x headroom) but the limit is {:.0f} MiB. The platform kills "
            "the container before Redis can evict, so --maxmemory-policy {} "
            "is unreachable.".format(
                target.path, maxmemory, required / 1024 ** 2, HEADROOM,
                limit / 1024 ** 2, policy or "<unset>"))
        return
    print("OK   {:<45} maxmemory {} in {:.0f} MiB ({:.0f}% headroom), policy "
          "{}, persistence {}".format(
              target.path, maxmemory, limit / 1024 ** 2,
              (limit - cap) / cap * 100, policy or "<unset>",
              "on with volume" if persists else "off"))


def main() -> int:
    problems: list = []
    try:
        check_terraform(problems)
    except (OSError, ValueError) as exc:
        problems.append(f"{TERRAFORM_SERVICES}: {exc}")

    for target in TARGETS:
        try:
            check(target, problems)
        except (OSError, ValueError) as exc:
            problems.append(f"{target.path}: {exc}")

    for path, reason in NOT_CHECKED:
        print(f"SKIP {path:<45} {reason}")

    if problems:
        print(f"\nFAIL: {len(problems)} problem(s)", file=sys.stderr)
        for problem in problems:
            print("  - " + problem, file=sys.stderr)
        return 1
    print("\nAll Redis manifests satisfy both invariants.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
