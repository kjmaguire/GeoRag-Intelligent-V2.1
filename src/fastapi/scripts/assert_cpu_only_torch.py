"""Build-time gate: fail the image build if any CUDA package got installed.

Invoked once by docker/fastapi.Dockerfile after the last dependency-
installing layer — not a runtime script.

This is the assertion that was missing on 2026-08-24, when the deliberate
CPU-only-torch work was silently defeated twice at once: excluding `torch`
from requirements.lock.txt left torch's CUDA children (cuda-toolkit,
nvidia-*, triton) as first-class pinned lines that installed a layer AFTER
the +cpu wheel, and a stale uv.lock still resolved plain `xgboost`, whose
linux metadata drags in nvidia-nccl-cu12. Net effect measured live: 3.6 GB
of GPU libraries in a 7.5 GB image on a tier with no GPU, and a CD run
aborted by a Trivy scan deadline on libcusparseLt.so.0.

Both root causes are closed upstream (scripts/check_fastapi_lock_export.py
excludes the whole CUDA subtree and refuses exports containing nvidia-*/
cuda-*/triton; the re-lock resolves xgboost-cpu on linux). This script is
the backstop that makes ANY future route to the same outcome a loud build
failure instead of a quiet 3.6 GB regression: it checks the final installed
state, not any one install step.

Three checks:
  1. No installed distribution named nvidia-*, cuda-*, or triton.
  2. No site-packages directory named nvidia* or triton* (catches wheels
     that unpack a vendored tree without registering a matching dist).
  3. `import torch` works and reports a CPU-only build — the SPLADE++
     sparse encoder (app/services/sparse_encoder.py) genuinely needs
     torch, so "no CUDA" must never regress into "no torch".
"""

from __future__ import annotations

import importlib.metadata
import sys
import sysconfig
from pathlib import Path


def main() -> int:
    problems = []

    cuda_dists = sorted(
        name
        for dist in importlib.metadata.distributions()
        if (name := (dist.metadata["Name"] or "").lower())
        and (name.startswith(("nvidia-", "cuda-")) or name == "triton")
    )
    if cuda_dists:
        problems.append(
            "CUDA packages are installed on the GPU-less FastAPI tier: "
            + ", ".join(cuda_dists))

    site_packages = Path(sysconfig.get_paths()["purelib"])
    cuda_dirs = sorted(
        entry.name for entry in site_packages.iterdir()
        if entry.is_dir() and entry.name.startswith(("nvidia", "triton")))
    if cuda_dirs:
        problems.append(
            f"CUDA directories present under {site_packages}: "
            + ", ".join(cuda_dirs))

    try:
        import torch
    except ImportError as exc:
        problems.append(
            f"`import torch` failed ({exc}). The SPLADE++ sparse encoder "
            "requires torch — the CPU-only wheel must still be installed.")
    else:
        if torch.version.cuda is not None:
            problems.append(
                f"torch {torch.__version__} is a CUDA build "
                f"(torch.version.cuda={torch.version.cuda}). The image must "
                "install the +cpu wheel from download.pytorch.org/whl/cpu.")
        else:
            print(f"OK   torch {torch.__version__} is CPU-only "
                  "(torch.version.cuda is None)")

    if problems:
        print("FAIL: the image is carrying CUDA it can never use "
              "(no deployment tier has a GPU):", file=sys.stderr)
        for problem in problems:
            print("  - " + problem, file=sys.stderr)
        print("See scripts/check_fastapi_lock_export.py (repo root) for "
              "how torch and its CUDA subtree are kept out of the locked "
              "export.", file=sys.stderr)
        return 1

    print("OK   no nvidia-*/cuda-*/triton distributions or directories in "
          "site-packages")
    return 0


if __name__ == "__main__":
    sys.exit(main())
