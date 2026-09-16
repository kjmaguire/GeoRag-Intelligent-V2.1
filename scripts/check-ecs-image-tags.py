#!/usr/bin/env python3
"""Fail if an ECS task definition hardcodes the tag it pulls from ECR.

Every `aws_ecr_repository` in this deployment sets
`image_tag_mutability = "IMMUTABLE"`, which makes a floating tag a
contradiction: a tag can be written once and never moved. Until 2026-09-16
every task definition here pulled `:latest` anyway — inherited from
docker-compose.yml, where `georag/laravel:latest` is simply what the last
local `docker build` left behind.

Nothing ever pushed that tag. cd.yml pushes `:<short-sha>` and only that;
docker-build.yml pushes to GHCR, not ECR. So the reference could not resolve,
and the two halves of the deployment failed differently:

  * The ten services and `georag-migrate` recovered, because cd.yml
    re-registers each task definition with `:<short-sha>` before rolling it.
    What that cost was the window between the first apply and the first
    deploy: every task fails CannotPullContainerError, the deployment circuit
    breaker trips with no previous revision to roll back to, and the ALB
    alarms fire on a stack that looks applied.

  * `georag-app-key-rotation` did NOT recover, and this is why the rule is
    worth a checker rather than a code review. Nothing re-registers it.
    rotate-app-key.sh overrides `command` only, and ECS RunTask cannot
    override an image at all. The APP_KEY rotation would have failed on an
    unpullable image the first time anyone ran it — in the middle of a
    procedure that has already taken the platform down, with the runbook
    mid-flight.

So: an image built from `aws_ecr_repository...repository_url` must take its
tag from a variable. A literal is reported wherever it appears, `latest`
or not, because the failure is the immutability mismatch rather than that
one word.

Images from OTHER registries are not the subject. `external_image` pins
hatchet-lite, qdrant and redis to exact versions, which is correct for a
third-party registry that does move its tags.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

TF = (
    Path(sys.argv[1])
    if len(sys.argv) > 1
    else Path(__file__).resolve().parent.parent / "deploy" / "aws" / "terraform"
)

#: An interpolation ending in `.repository_url` followed by `:<something>`.
#: The tag runs to the closing quote or brace of the enclosing string.
ECR_IMAGE = re.compile(r"repository_url\}:(?P<tag>[^\"\s]+)")

#: What a tag must look like to be acceptable: an interpolation, not a
#: literal. `${var.image_tag}` passes; `latest` and `v1.2.3` do not.
INTERPOLATED = re.compile(r"^\$\{[^}]+\}\"?,?$")


def findings() -> list[str]:
    out = []
    for tf in sorted(TF.glob("*.tf")):
        for n, line in enumerate(tf.read_text().splitlines(), 1):
            for m in ECR_IMAGE.finditer(line):
                tag = m.group("tag")
                if INTERPOLATED.match(tag):
                    continue
                shown = tag.rstrip('",')
                out.append(f"{tf.name}:{n}  pulls ECR tag `{shown}`")
    return out


def main() -> int:
    bad = findings()
    if bad:
        print(
            f"\n{len(bad)} ECS image reference(s) hardcode an ECR tag:", file=sys.stderr
        )
        for b in bad:
            print(f"  - {b}", file=sys.stderr)
        print(
            "  -> ECR here is IMMUTABLE and nothing pushes a floating tag, so this\n"
            "     names an image that cannot be pulled. Use `${var.image_tag}`.",
            file=sys.stderr,
        )
        return 1

    total = sum(
        len(ECR_IMAGE.findall(tf.read_text())) for tf in sorted(TF.glob("*.tf"))
    )
    print(f"ECS image tags: {total} ECR reference(s), every one from a variable.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
