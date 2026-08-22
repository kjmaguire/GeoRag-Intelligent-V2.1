#!/usr/bin/env python3
"""Keep src/fastapi/requirements.lock.txt in step with src/fastapi/uv.lock.

WHY THIS FILE EXISTS
    docker/fastapi.Dockerfile copied uv.lock into the image and then never
    used it. Dependencies were installed with `uv pip install -r
    pyproject.toml`, which is uv's PIP interface: it re-resolves the
    version RANGES in pyproject.toml from scratch at build time and does
    not consult uv.lock at all. CI, meanwhile, installs with `uv sync
    --extra dev`, which is uv's PROJECT interface and does honour the
    lock.

    So CI proved one set of versions worked and CD shipped a different
    set, resolved minutes later. Nothing compared them. Any release
    published inside a pyproject range between the two resolutions went
    straight to production untested.

WHY AN EXPORTED FILE RATHER THAN `uv sync --frozen` IN THE IMAGE
    `uv sync` builds a virtualenv. The image deliberately installs into
    the system Python and its multi-stage build copies
    /usr/local/lib/python3.13/site-packages -- not a source tree -- into
    the runtime stage (see [tool.uv.sources] in pyproject.toml, which
    documents that the path deps are non-editable for exactly this
    reason). Moving to a venv means changing the runtime stage, PATH, and
    both path-dependency installs at once. Exporting the lock to a
    requirements file keeps the existing, working install model and
    changes only WHICH versions it installs -- which is the actual bug.

TWO PACKAGE EXCLUSIONS, BOTH DELIBERATE
    georag-object-storage / georag-geoparsers
        Local path dependencies. uv exports them as relative paths
        (`../georag_object_storage`) which do not exist in the Docker
        build context -- the Dockerfile copies them to /georag_*. They
        are pre-installed by name earlier in the Dockerfile, so the
        requirement is already satisfied when this file is installed.

    torch
        The image installs torch from https://download.pytorch.org/whl/cpu
        so the GPU-less FastAPI tier does not carry ~3.4 GB of CUDA (see
        the CPU-only work of 2026-08-19). The lock resolves torch from
        PyPI, whose default wheel IS the CUDA build. Leaving `torch==...`
        in this file lets a future resolver decide to satisfy it from
        PyPI and silently re-add CUDA. The version is pinned in the
        Dockerfile via ARG TORCH_VERSION, and this checker asserts the
        two agree.

Usage:
    python scripts/check_fastapi_lock_export.py            # verify
    python scripts/check_fastapi_lock_export.py --write    # regenerate
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FASTAPI = REPO / "src" / "fastapi"
EXPORT = FASTAPI / "requirements.lock.txt"
DOCKERFILE = REPO / "docker" / "fastapi.Dockerfile"

# Packages the Dockerfile installs by other means. Changing this list
# changes what the image gets, so it lives in one place and the header
# above explains each entry.
EXCLUDED = ("georag-object-storage", "georag-geoparsers", "torch")

EXPORT_ARGS = [
    "export",
    "--frozen",
    "--no-dev",
    "--format", "requirements-txt",
    "--no-emit-project",
    "--no-hashes",
]
for _package in EXCLUDED:
    EXPORT_ARGS += ["--no-emit-package", _package]

BANNER = """\
# GENERATED FILE -- do not edit by hand.
#
# Exported from uv.lock by scripts/check_fastapi_lock_export.py, which CI
# re-runs and diffs. docker/fastapi.Dockerfile installs from this file so
# the image gets the versions CI actually tested, instead of re-resolving
# pyproject.toml's ranges at build time.
#
# Regenerate after any dependency change:
#   python scripts/check_fastapi_lock_export.py --write
#
"""


def run_export() -> str:
    """uv export output, or exit with a readable message if uv is absent."""
    for command in (["uv"], [sys.executable, "-m", "uv"]):
        try:
            result = subprocess.run(
                command + EXPORT_ARGS, cwd=FASTAPI,
                capture_output=True, text=True, check=False,
            )
        except (OSError, ValueError):
            continue
        if result.returncode == 0:
            return result.stdout
        # uv ran but refused -- that is a real answer, not a missing tool.
        if "--frozen" in result.stderr or "lock" in result.stderr.lower():
            sys.exit(
                "uv export failed. If uv.lock is out of step with "
                "pyproject.toml, run `uv lock` first.\n" + result.stderr)
    sys.exit(
        "uv is not available. Install it, or run this on a machine that "
        "has it:  pip install uv")


def normalise(text: str) -> str:
    """Drop uv's own command-line banner so the diff is about packages.

    uv writes the exact invocation into the file's first lines, which
    differ between `uv export` and `python -m uv export` -- a difference
    that says nothing about dependencies and would fail CI for no reason.
    """
    lines = [
        line for line in text.splitlines()
        if not line.startswith("#")
    ]
    return "\n".join(lines).strip() + "\n"


def torch_versions() -> tuple:
    """(Dockerfile ARG version, uv.lock version) for torch."""
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    arg = re.search(r"^ARG TORCH_VERSION=(\S+)", dockerfile, re.MULTILINE)

    lock = (FASTAPI / "uv.lock").read_text(encoding="utf-8")
    locked = re.search(
        r'\[\[package\]\]\nname = "torch"\nversion = "([^"]+)"', lock)
    return (arg.group(1) if arg else None,
            locked.group(1) if locked else None)


def main() -> int:
    write = "--write" in sys.argv
    exported = normalise(run_export())
    problems = []

    if write:
        EXPORT.write_text(BANNER + exported, encoding="utf-8", newline="\n")
        count = sum(1 for line in exported.splitlines()
                    if line and not line.startswith((" ", "#")))
        print(f"wrote {EXPORT.relative_to(REPO)} ({count} packages)")
    elif not EXPORT.exists():
        problems.append(
            f"{EXPORT.relative_to(REPO)} does not exist. Run this script "
            "with --write.")
    elif normalise(EXPORT.read_text(encoding="utf-8")) != exported:
        problems.append(
            f"{EXPORT.relative_to(REPO)} is out of step with uv.lock. The "
            "image would install versions nobody tested. Regenerate with "
            "`python scripts/check_fastapi_lock_export.py --write`.")
    else:
        count = sum(1 for line in exported.splitlines()
                    if line and not line.startswith((" ", "#")))
        print(f"OK   requirements.lock.txt matches uv.lock ({count} packages)")

    # The Dockerfile installs torch itself, from the CPU index. If its ARG
    # drifts from the lock, the image ships a torch CI never tested -- the
    # same class of bug this whole file exists to close, one package wide.
    arg_version, locked_version = torch_versions()
    if arg_version is None:
        problems.append("docker/fastapi.Dockerfile has no ARG TORCH_VERSION")
    elif locked_version is None:
        problems.append("uv.lock has no torch package")
    elif arg_version != locked_version:
        problems.append(
            f"docker/fastapi.Dockerfile pins torch {arg_version} but uv.lock "
            f"resolves {locked_version}. torch is excluded from the export "
            "because it comes from the CPU-only index, so this pairing is "
            "the only thing keeping the two in step.")
    else:
        print(f"OK   torch {arg_version} agrees between Dockerfile and uv.lock")

    # The exclusions only make sense while the Dockerfile really does
    # install those packages some other way.
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    for package in EXCLUDED:
        directory = package.replace("-", "_")
        if package == "torch":
            continue
        if f"/{directory}" not in dockerfile:
            problems.append(
                f"{package} is excluded from the export but the Dockerfile "
                f"no longer installs /{directory}. It would be missing from "
                "the image entirely.")

    if problems:
        print("\nFAIL:", file=sys.stderr)
        for problem in problems:
            print("  - " + problem, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
