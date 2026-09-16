#!/usr/bin/env python3
"""Fail if CD builds an image that CI never builds.

cd.yml built three images and docker-build.yml built two, so `martin` went to
production unbuilt. Its Dockerfile carried

    COPY martin/martin.yaml /config/martin.yaml

against a build context of the repository root, where there is no `martin/` —
the file lives at `docker/martin/martin.yaml`. The build could only ever fail:

    failed to compute cache key: "/martin/martin.yaml": not found

Four things each looked like coverage and were not. Compose does not build the
image at all (it runs the upstream one with a bind mount). ci.yml lints the
Dockerfile with hadolint, which checks syntax and never whether a COPY source
exists. docker-build.yml had no martin leg. And cd.yml's build matrix sets
`fail-fast: true`, so the first production deploy would have failed on martin
and cancelled the fastapi and laravel builds on its way out.

The invariant that would have caught it is the simple one: an image is not
deployable until CI has actually built it. Hence:

  * every image cd.yml builds is built by docker-build.yml too, and
  * with the SAME Dockerfile and the SAME context, because a path that
    resolves under one context and not the other is the whole failure mode
    this exists to prevent.

The reverse is allowed. CI may build something CD does not deploy.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent
CD = ROOT / ".github" / "workflows" / "cd.yml"
CI = ROOT / ".github" / "workflows" / "docker-build.yml"


def _matrix(path: Path, name_key: str) -> dict[str, dict[str, str]]:
    """Image name -> {dockerfile, context}, read out of a workflow's matrix.

    Deliberately a small regex reader rather than a YAML dependency: this
    runs in the same job as the other repo checkers, which import nothing.
    """
    text = path.read_text()
    out: dict[str, dict[str, str]] = {}
    # Each matrix entry starts at `- <name_key>: value` and runs to the next
    # one (or to a dedent out of the include block).
    starts = list(re.finditer(rf"^\s+- {name_key}:\s*(\S+)\s*$", text, re.M))
    for n, m in enumerate(starts):
        end = starts[n + 1].start() if n + 1 < len(starts) else len(text)
        body = text[m.end() : end]
        entry = {}
        for field in ("dockerfile", "context"):
            f = re.search(rf"^\s+{field}:\s*(\S+)\s*$", body, re.M)
            if f:
                entry[field] = f.group(1)
        out[m.group(1)] = entry
    return out


def _norm(ctx: str | None) -> str | None:
    """`./src` and `src` are the same context; compare them as such."""
    if ctx is None:
        return None
    ctx = ctx.rstrip("/")
    if ctx.startswith("./") and len(ctx) > 2:
        ctx = ctx[2:]
    return ctx or "."


def main() -> int:
    for f in (CD, CI):
        if not f.exists():
            print(f"missing workflow: {f}", file=sys.stderr)
            return 1

    cd = _matrix(CD, "image")
    ci = _matrix(CI, "service")
    problems: list[str] = []

    for name, cd_entry in sorted(cd.items()):
        ci_entry = ci.get(name)
        if ci_entry is None:
            problems.append(
                f"{name}: cd.yml deploys it, docker-build.yml never builds it\n"
                f"      -> add a matrix leg to docker-build.yml. Until then the "
                f"first deploy is this image's first build anywhere."
            )
            continue
        for field in ("dockerfile", "context"):
            a, b = cd_entry.get(field), ci_entry.get(field)
            if field == "context":
                a, b = _norm(a), _norm(b)
            if a != b:
                problems.append(
                    f"{name}: {field} differs — cd.yml {a!r}, docker-build.yml {b!r}\n"
                    f"      -> a COPY source that resolves under one and not the "
                    f"other passes CI and fails the deploy."
                )

    if problems:
        print(f"\n{len(problems)} image build-parity problem(s):", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1

    print(
        f"Image build parity: {len(cd)} image(s) deployed by cd.yml "
        f"({', '.join(sorted(cd))}), every one built by docker-build.yml "
        f"from the same Dockerfile and context."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
