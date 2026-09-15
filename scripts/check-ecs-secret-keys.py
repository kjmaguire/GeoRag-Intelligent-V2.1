#!/usr/bin/env python3
"""Keep the ECS secret wiring and its operator documentation in agreement.

Terraform creates `georag/app` in Secrets Manager and deliberately writes no
values into it: a value passed through `terraform apply` is a value in
Terraform state. The keys are therefore written out of band by a person, from
a list in deploy/aws/README.md, and nothing connected that list to the
Terraform that consumes it.

That gap is not theoretical. Every one of these was found in the Terraform on
2026-09-15, the night before a planned cutover:

  * martin was handed no secrets at all, while docker/martin/martin.yaml:39
    reads `connection_string: '${DATABASE_URL}'` and the Dockerfile gives it
    no default on purpose. Every MVT tile in the platform.
  * the hatchet engine was handed no DATABASE_URL either, which with
    SERVER_MSGQUEUE_KIND=postgres is both its schema store and its queue:
    51 workflows and every cron.
  * redis-server was never given REDIS_PASSWORD, so it would have run with
    no requirepass while every client authenticates.
  * no service got a database password under ANY name, so Laravel would have
    fallen back to config/database.php's sqlite default and FastAPI would
    have failed its POSTGRES_PASSWORD field validation at import.

None of that is visible to `terraform validate`: every one of those configs
is schema-valid. It is only wrong against what the images actually read.

So this checks the two directions that catch it:

  1. every key the Terraform references is documented for the operator —
     otherwise a container waits on a secret nobody was told to create;
  2. every documented key is referenced by the Terraform — otherwise the
     runbook asks for values that nothing consumes, which is how a list
     stops being read.

It does NOT check that a key's VALUE is right. Nothing in a repository can.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TERRAFORM = REPO / "deploy" / "aws" / "terraform"
README = REPO / "deploy" / "aws" / "README.md"

# `"${aws_secretsmanager_secret.app.arn}:KEY::"` is the shape ECS accepts for
# a single JSON key out of a secret, so a literal reference looks like this.
REFERENCE = re.compile(r"aws_secretsmanager_secret\.app\.arn\}:([A-Z0-9_]+)::")

# Not every reference is a literal. config.tf builds the common set by
# interpolating a loop variable — `:${key}::` — so the key names live in the
# list being looped over, and a scan for the literal shape above sees none of
# them. Missing that would have this script report the five most important
# keys in the deployment as documented-but-unused, which is worse than not
# checking: it teaches the reader to ignore the output.
LOOPED_BLOCKS = (
    ("_secret_ref = { for key in [", "]"),
    ("_extra_secret_ref = {", "\n  }"),
)
QUOTED_KEY = re.compile(r'"([A-Z][A-Z0-9_]{2,})"')


def looped_keys(source: str) -> set[str]:
    """Key names from the list-driven blocks the literal pattern cannot see."""
    found: set[str] = set()
    for opener, closer in LOOPED_BLOCKS:
        start = source.find(opener)
        while start != -1:
            end = source.find(closer, start + len(opener))
            if end == -1:
                break
            found.update(QUOTED_KEY.findall(source[start + len(opener) : end]))
            start = source.find(opener, end)
    return found

# The documentation is a markdown table whose first column is the key in
# backticks. Anchored to the table row so prose mentioning a key in passing
# does not count as documenting it.
DOCUMENTED = re.compile(r"^\|\s*`([A-Z0-9_]+)`\s*\|", re.MULTILINE)


def redis_uses_its_password() -> bool:
    """Handing redis the password is only half of it; it has to USE it.

    Injecting REDIS_PASSWORD and never passing --requirepass leaves a store
    with no authentication that looks configured from the task definition —
    and because the clients DO authenticate, Redis answers their AUTH with an
    error and cache, sessions, queues, Horizon and the Reverb backplane all
    fail on connect. The two halves are one fact, so they are checked
    together rather than left to line up by luck.

    `--requirepass` needs a shell to expand the variable, because ECS runs a
    command array with no shell; that is why the command is `sh -c` here and
    a plain array everywhere else in this file's sibling checker.
    """
    services = TERRAFORM / "services.tf"
    source = services.read_text(encoding="utf-8")

    start = source.find("redis = [")
    if start == -1:
        print(
            "FAIL  no `redis = [` command found in deploy/aws/terraform/"
            "services.tf — this check has gone stale.",
            file=sys.stderr,
        )
        return False

    end = source.find("]", source.find("join(", start))
    block = source[start : end if end != -1 else start + 2000]

    if "--requirepass" not in block:
        print(
            "FAIL  deploy/aws/terraform/services.tf starts redis-server with "
            "no --requirepass.\n"
            "      Every other topology in this repository sets it: "
            "docker-compose.yml, charts/georag/templates/redis.yaml and all "
            "three kubernetes/manifests variants.\n"
            "      Without it this store has no authentication, and because "
            "the clients still send AUTH it is not a weaker Redis — it is a "
            "Redis nothing can connect to.",
            file=sys.stderr,
        )
        return False

    if "$REDIS_PASSWORD" not in block:
        print(
            "FAIL  services.tf passes --requirepass without expanding "
            "$REDIS_PASSWORD.\n"
            "      ECS runs a command array with no shell, so a literal "
            "would become the password itself.",
            file=sys.stderr,
        )
        return False

    return True


def main() -> int:
    if not TERRAFORM.is_dir():
        print(f"FAIL  {TERRAFORM} does not exist", file=sys.stderr)
        return 1
    if not README.is_file():
        print(f"FAIL  {README} does not exist", file=sys.stderr)
        return 1

    referenced: dict[str, set[str]] = {}
    for path in sorted(TERRAFORM.glob("*.tf")):
        source = path.read_text(encoding="utf-8")
        for key in set(REFERENCE.findall(source)) | looped_keys(source):
            referenced.setdefault(key, set()).add(path.name)

    documented = set(DOCUMENTED.findall(README.read_text(encoding="utf-8")))

    if not referenced:
        print(
            "FAIL  no secret references found in deploy/aws/terraform/*.tf.\n"
            "      Either the wiring is gone or this script's pattern is "
            "stale; both are worth stopping for.",
            file=sys.stderr,
        )
        return 1

    undocumented = sorted(set(referenced) - documented)
    unused = sorted(documented - set(referenced))

    if undocumented:
        print(
            "FAIL  the Terraform injects secret keys that deploy/aws/README.md "
            "does not tell the operator to create.\n"
            "      A container will start, wait on a key that is not there, "
            "and fail in a way that looks like a bug in the container:\n",
            file=sys.stderr,
        )
        for key in undocumented:
            where = ", ".join(sorted(referenced[key]))
            print(f"        {key}  (referenced in {where})", file=sys.stderr)
        print(
            "\n      Add a row for each to the key table in "
            "deploy/aws/README.md.",
            file=sys.stderr,
        )

    if unused:
        print(
            "\nFAIL  deploy/aws/README.md documents secret keys that no "
            "Terraform references.\n"
            "      Either the wiring was dropped, or the runbook is asking "
            "for values nothing reads — which is how the list stops being "
            "trusted:\n",
            file=sys.stderr,
        )
        for key in unused:
            print(f"        {key}", file=sys.stderr)

    if undocumented or unused:
        return 1

    if not redis_uses_its_password():
        return 1

    print(
        f"ECS secret keys: {len(documented)} documented, all referenced; "
        "no undocumented references. redis-server consumes REDIS_PASSWORD."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
