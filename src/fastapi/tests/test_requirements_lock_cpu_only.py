"""The locked export must never reinstate CUDA on the GPU-less tier.

Both of 2026-08-24's silent defeats of the CPU-only-torch work were
visible as plain text in requirements.lock.txt long before they cost
3.6 GB of image and a Trivy-deadline CD abort:

  - torch was excluded from the export, but its CUDA-only children
    (cuda-toolkit, nvidia-*, triton) remained first-class pinned lines,
    each annotated `# via torch` for a parent no longer in the file.
  - a stale uv.lock still resolved plain `xgboost` with no platform
    marker, whose linux metadata drags in nvidia-nccl-cu12 (288 MB),
    even though pyproject.toml had already split to xgboost-cpu.

These tests read the committed export directly — stdlib only, no Docker,
any platform — so either regression fails the ordinary unit-test run.
The build-time counterpart, scripts/assert_cpu_only_torch.py, checks the
image's actual site-packages; scripts/check_fastapi_lock_export.py (repo
root) keeps the export itself in step with uv.lock and rejects CUDA
lines at regeneration time. This file is the cheapest and earliest of
the three gates.
"""

from __future__ import annotations

import re
from pathlib import Path

LOCK_EXPORT = Path(__file__).resolve().parents[1] / "requirements.lock.txt"

_REQUIREMENT = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==\S+")


def _requirements() -> dict[str, str]:
    """{package name: environment marker (may be empty)} from the export."""
    requirements: dict[str, str] = {}
    for line in LOCK_EXPORT.read_text(encoding="utf-8").splitlines():
        match = _REQUIREMENT.match(line)
        if not match:
            continue
        _, _, marker = line.partition(";")
        requirements[match.group(1).lower()] = marker.strip()
    return requirements


def test_export_parses_to_a_plausible_package_count():
    # Guards the other tests against a format change that would make
    # _requirements() silently match nothing and vacuously pass.
    assert len(_requirements()) > 100


def test_no_cuda_packages_in_lock_export():
    leaked = sorted(
        name for name in _requirements()
        if name.startswith(("nvidia-", "cuda-")) or name == "triton"
    )
    assert leaked == [], (
        f"CUDA-only packages in requirements.lock.txt: {leaked}. "
        "No deployment tier has a GPU. If a torch bump grew a new CUDA "
        "child, add it to TORCH_CUDA_SUBTREE in "
        "scripts/check_fastapi_lock_export.py and regenerate."
    )


def test_torch_absent_from_lock_export():
    # torch is installed from download.pytorch.org/whl/cpu by the
    # Dockerfile; a torch line here would let a resolver satisfy it from
    # PyPI, whose default linux wheel is the CUDA build.
    assert "torch" not in _requirements()


def test_xgboost_is_the_cpu_build_on_linux():
    requirements = _requirements()
    assert "sys_platform == 'linux'" in requirements.get("xgboost-cpu", ""), (
        "requirements.lock.txt must pin xgboost-cpu for linux — the plain "
        "xgboost package depends on nvidia-nccl-cu12 there. If this fails "
        "after a re-lock, uv.lock has drifted from pyproject.toml's "
        "xgboost-cpu split: run `uv lock` and regenerate the export."
    )
    if "xgboost" in requirements:
        assert "sys_platform != 'linux'" in requirements["xgboost"], (
            "an unmarked xgboost pin would install the CUDA-dependent "
            "build on linux alongside (or instead of) xgboost-cpu"
        )
