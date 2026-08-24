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

THE PACKAGE EXCLUSIONS, ALL DELIBERATE
    georag-object-storage / georag-geoparsers  (PATH_DEPS)
        Local path dependencies. uv exports them as relative paths
        (`../georag_object_storage`) which do not exist in the Docker
        build context -- the Dockerfile copies them to /georag_*. They
        are pre-installed by name earlier in the Dockerfile, so the
        requirement is already satisfied when this file is installed.

    torch, and torch's CUDA subtree  (TORCH_CUDA_SUBTREE)
        The image installs torch from https://download.pytorch.org/whl/cpu
        so the GPU-less FastAPI tier does not carry ~3.4 GB of CUDA (see
        the CPU-only work of 2026-08-19). The lock resolves torch from
        PyPI, whose default wheel IS the CUDA build. Leaving `torch==...`
        in this file lets a future resolver decide to satisfy it from
        PyPI and silently re-add CUDA. The version is pinned in the
        Dockerfile via ARG TORCH_VERSION, and this checker asserts the
        two agree.

        Excluding torch alone was not enough -- found live 2026-08-24.
        `--no-emit-package torch` drops only torch's OWN line; its
        CUDA-only dependencies (cuda-toolkit, nvidia-cublas, triton, ...)
        remained first-class pinned lines, each annotated `# via torch`
        for a parent no longer in the file, and `uv pip install -r`
        installed every one of them a layer AFTER the CPU wheel. Measured
        in the production image: 2,898 MB of site-packages/nvidia plus
        690 MB of triton on a tier with no GPU, and the CD run of
        2026-08-24 07:16 UTC aborted when the Trivy scan hit its deadline
        on libcusparseLt.so.0. So the whole subtree is excluded by name,
        and cuda_leakage() below fails the check if a future torch bump
        grows a CUDA child this list does not yet name.

        These packages exist in uv.lock only as torch dependencies; the
        +cpu wheel neither needs nor declares them, so nothing has to
        install them "some other way" -- they are meant to be absent.
        docker/fastapi.Dockerfile asserts that absence at build time
        (scripts/assert_cpu_only_torch.py in src/fastapi).

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

# Local path dependencies the Dockerfile installs by other means (from
# /georag_* copies of the sibling source trees). The consistency check in
# main() asserts the Dockerfile really does still install each of these.
PATH_DEPS = ("georag-object-storage", "georag-geoparsers")

# torch (installed from the CPU-only index by the Dockerfile) and every
# CUDA-only package that exists in uv.lock purely as a torch dependency.
# The +cpu wheel needs none of them; they are excluded so they are ABSENT
# from the image, not installed another way. The header above tells the
# 2026-08-24 story of what happened when this list was just ("torch",).
# Sources of the member list: `# via torch` / `# via cuda-bindings`
# annotations in an unfiltered `uv export`, cross-checked against the
# [[package]] name = "torch" dependency table in uv.lock.
TORCH_CUDA_SUBTREE = (
    "torch",
    "cuda-bindings",
    "cuda-pathfinder",
    "cuda-toolkit",
    "nvidia-cublas",
    "nvidia-cuda-cupti",
    "nvidia-cuda-nvrtc",
    "nvidia-cuda-runtime",
    "nvidia-cudnn-cu13",
    "nvidia-cufft",
    "nvidia-cufile",
    "nvidia-curand",
    "nvidia-cusolver",
    "nvidia-cusparse",
    "nvidia-cusparselt-cu13",
    "nvidia-nccl-cu13",
    "nvidia-nvjitlink",
    "nvidia-nvshmem-cu13",
    "nvidia-nvtx",
    "triton",
)

# Changing either list changes what the image gets, so they live in one
# place and the header above explains each entry.
EXCLUDED = PATH_DEPS + TORCH_CUDA_SUBTREE

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


def cuda_leakage(exported: str) -> list:
    """CUDA-subtree requirement lines that escaped the exclusion list.

    The explicit TORCH_CUDA_SUBTREE names only what torch depends on
    TODAY. A torch upgrade can grow a new nvidia-* child, and a
    dependency re-lock can pull a CUDA package in through some other
    parent (nvidia-nccl-cu12 arrived via plain `xgboost` until the
    xgboost-cpu split of 2026-08-24). Either way the failure mode is the
    same -- gigabytes of GPU libraries silently reinstated on a GPU-less
    tier -- so any requirement line whose package is nvidia-*, cuda-*,
    or triton fails the check by name here.
    """
    leaked = []
    for line in exported.splitlines():
        match = re.match(r"([A-Za-z0-9][A-Za-z0-9._-]*)==", line)
        if not match:
            continue
        name = match.group(1).lower()
        if name.startswith(("nvidia-", "cuda-")) or name == "triton":
            leaked.append(name)
    return leaked


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

    leaked = cuda_leakage(exported)
    if leaked:
        problems.append(
            "the export still contains CUDA-only packages: "
            + ", ".join(sorted(set(leaked)))
            + ". A torch bump probably grew a new CUDA child (add it to "
            "TORCH_CUDA_SUBTREE), or a re-lock pulled CUDA in through a "
            "non-torch parent (fix that parent, as xgboost -> xgboost-cpu "
            "was fixed)."
            + (" Refusing to write the export." if write else ""))

    if write and leaked:
        pass  # refuse to write a file that would reinstate CUDA
    elif write:
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

    # The path-dep exclusions only make sense while the Dockerfile really
    # does install those packages some other way. (TORCH_CUDA_SUBTREE is
    # different: torch's own install is covered by the version pairing
    # above, and the CUDA children are excluded to be absent, not to be
    # installed another way.)
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    for package in PATH_DEPS:
        directory = package.replace("-", "_")
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
