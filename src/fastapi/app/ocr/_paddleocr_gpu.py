"""Phase 9 (2026-05-22) — PaddleOCR GPU/CPU selection.

Shared helper used by both PaddleOCR call sites
(`services/pdf_ocr.py` and `ocr/parse_scanned.py`) so the routing
logic lives in exactly one place.

Decision tree:
  1. Read PADDLEOCR_USE_GPU env:
       - "false" / "0" / "no" → force CPU
       - "true" / "1" / "yes" → request GPU
       - "" / unset           → auto-detect
  2. If forcing CPU → return False
  3. If forcing GPU OR auto-detect:
       - paddle.device.is_compiled_with_cuda() must be True
       - torch.cuda.mem_get_info(0)[0] / MB must be ≥ MIN_FREE_VRAM_MB
       - Both checks pass → return True
       - Any check fails → log warning + return False (graceful CPU fallback)

This gates today's CPU build cleanly: detection returns False because
`paddle 3.3.1` (CPU wheel) reports `is_compiled_with_cuda() == False`.
After the Phase 9b image rebuild swaps in `paddlepaddle-gpu`,
detection returns True automatically and the OCR path moves to GPU
without further code changes.

Env knobs:
  PADDLEOCR_USE_GPU            — empty/auto, true, false. Default empty.
  PADDLEOCR_MIN_FREE_VRAM_MB   — sanity check before grabbing GPU. Default 1024.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

log = logging.getLogger(__name__)


_TRUE_VALUES = frozenset({"true", "1", "yes", "on"})
_FALSE_VALUES = frozenset({"false", "0", "no", "off"})


def _parse_tri_state(raw: str) -> Optional[bool]:
    """Parse PADDLEOCR_USE_GPU into (True/False/None).

    None means "auto-detect"; True/False means user override.
    """
    v = (raw or "").strip().lower()
    if v in _TRUE_VALUES:
        return True
    if v in _FALSE_VALUES:
        return False
    return None


def _paddle_compiled_with_cuda() -> bool:
    """Return True only when paddle is installed AND compiled with CUDA."""
    try:
        import paddle  # noqa: PLC0415
    except ImportError:
        return False
    try:
        return bool(paddle.device.is_compiled_with_cuda())
    except Exception as exc:  # noqa: BLE001
        log.debug("paddleocr_gpu: paddle.device check raised: %s", exc)
        return False


def _free_vram_mb() -> Optional[float]:
    """Return available VRAM in MB on device 0, or None if unavailable."""
    try:
        import torch  # noqa: PLC0415
    except ImportError:
        return None
    if not torch.cuda.is_available():
        return None
    try:
        free_bytes, _total = torch.cuda.mem_get_info(0)
        return free_bytes / (1024 * 1024)
    except Exception as exc:  # noqa: BLE001
        log.debug("paddleocr_gpu: torch.cuda.mem_get_info raised: %s", exc)
        return None


def paddleocr_use_gpu() -> bool:
    """Resolve whether to instantiate PaddleOCR with use_gpu=True.

    Returns False on any uncertainty so the caller can safely pass the
    result straight into `PaddleOCR(use_gpu=...)`.
    """
    env_raw = os.environ.get("PADDLEOCR_USE_GPU", "")
    override = _parse_tri_state(env_raw)

    # Explicit false → CPU, no further checks
    if override is False:
        log.debug("paddleocr_gpu: PADDLEOCR_USE_GPU=false → CPU")
        return False

    compiled = _paddle_compiled_with_cuda()
    if not compiled:
        if override is True:
            log.warning(
                "paddleocr_gpu: PADDLEOCR_USE_GPU=true but paddle is "
                "not compiled with CUDA — falling back to CPU. "
                "Install paddlepaddle-gpu to enable the GPU path."
            )
        return False

    try:
        min_free_mb = int(os.environ.get("PADDLEOCR_MIN_FREE_VRAM_MB", "1024"))
    except ValueError:
        min_free_mb = 1024

    free_mb = _free_vram_mb()
    if free_mb is None:
        # Paddle says it's CUDA-compiled but torch can't see the GPU;
        # bail to CPU rather than risk paddle's allocator failing.
        log.info(
            "paddleocr_gpu: paddle reports CUDA but torch can't read VRAM "
            "→ falling back to CPU"
        )
        return False
    if free_mb < min_free_mb:
        log.info(
            "paddleocr_gpu: free VRAM %.0fMB < threshold %dMB → CPU",
            free_mb, min_free_mb,
        )
        return False

    log.info(
        "paddleocr_gpu: enabled (free VRAM %.0fMB ≥ threshold %dMB)",
        free_mb, min_free_mb,
    )
    return True
