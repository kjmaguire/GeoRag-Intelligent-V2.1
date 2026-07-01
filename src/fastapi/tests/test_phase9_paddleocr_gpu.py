"""Phase 9 (2026-05-22) — PaddleOCR GPU/CPU selection tests.

Covers the _paddleocr_gpu detection helper used by both PaddleOCR
call sites (services/pdf_ocr.py + ocr/parse_scanned.py).

Run with:
    pytest src/fastapi/tests/test_phase9_paddleocr_gpu.py -v
"""

from __future__ import annotations

import sys
import types

import pytest


def _install_fake_paddle(compiled_with_cuda: bool):
    """Inject a paddle stub into sys.modules with controlled CUDA flag."""
    fake_paddle = types.ModuleType("paddle")
    fake_paddle.device = types.SimpleNamespace(
        is_compiled_with_cuda=lambda: compiled_with_cuda,
    )
    sys.modules["paddle"] = fake_paddle


def _install_fake_torch(cuda_available: bool, free_mb: float = 4096):
    """Inject a torch stub with controlled CUDA availability + VRAM."""
    fake_torch = types.ModuleType("torch")

    def _mem_get_info(idx):
        return (int(free_mb * 1024 * 1024), int(8 * 1024 * 1024 * 1024))

    fake_torch.cuda = types.SimpleNamespace(
        is_available=lambda: cuda_available,
        mem_get_info=_mem_get_info,
    )
    sys.modules["torch"] = fake_torch


@pytest.fixture
def gpu_module(monkeypatch):
    """Re-import the helper fresh so env + module stubs apply."""
    sys.modules.pop("app.ocr._paddleocr_gpu", None)
    from app.ocr import _paddleocr_gpu as mod
    return mod


@pytest.fixture(autouse=True)
def reset_envs(monkeypatch):
    """Clear PADDLEOCR_* envs before each test."""
    for k in ("PADDLEOCR_USE_GPU", "PADDLEOCR_MIN_FREE_VRAM_MB"):
        monkeypatch.delenv(k, raising=False)
    yield


# ---------------------------------------------------------------------------
# 1. PADDLEOCR_USE_GPU=true + paddle CUDA-built + ample VRAM → True
# ---------------------------------------------------------------------------

def test_use_gpu_when_env_true_and_cuda_compiled(gpu_module, monkeypatch):
    monkeypatch.setenv("PADDLEOCR_USE_GPU", "true")
    _install_fake_paddle(compiled_with_cuda=True)
    _install_fake_torch(cuda_available=True, free_mb=4096)
    assert gpu_module.paddleocr_use_gpu() is True


# ---------------------------------------------------------------------------
# 2. PADDLEOCR_USE_GPU=true + paddle NOT compiled with CUDA → False
# ---------------------------------------------------------------------------

def test_use_gpu_false_when_paddle_cpu_build(gpu_module, monkeypatch, caplog):
    monkeypatch.setenv("PADDLEOCR_USE_GPU", "true")
    _install_fake_paddle(compiled_with_cuda=False)
    _install_fake_torch(cuda_available=True, free_mb=4096)
    with caplog.at_level("WARNING", logger="app.ocr._paddleocr_gpu"):
        assert gpu_module.paddleocr_use_gpu() is False
    # Warning should call out the cpu-build mismatch
    assert any(
        "not compiled with CUDA" in r.message for r in caplog.records
    )


# ---------------------------------------------------------------------------
# 3. PADDLEOCR_USE_GPU=false → False regardless of GPU
# ---------------------------------------------------------------------------

def test_use_gpu_false_when_env_false(gpu_module, monkeypatch):
    monkeypatch.setenv("PADDLEOCR_USE_GPU", "false")
    _install_fake_paddle(compiled_with_cuda=True)
    _install_fake_torch(cuda_available=True, free_mb=4096)
    assert gpu_module.paddleocr_use_gpu() is False


# ---------------------------------------------------------------------------
# 4. Auto (empty env) + paddle CUDA + VRAM → True
# ---------------------------------------------------------------------------

def test_auto_detect_returns_true_when_everything_aligned(gpu_module):
    _install_fake_paddle(compiled_with_cuda=True)
    _install_fake_torch(cuda_available=True, free_mb=4096)
    assert gpu_module.paddleocr_use_gpu() is True


# ---------------------------------------------------------------------------
# 5. Auto + paddle missing → False (no crash)
# ---------------------------------------------------------------------------

def test_auto_detect_no_paddle_returns_false(gpu_module):
    sys.modules.pop("paddle", None)
    # Force ImportError when paddle is referenced
    sys.modules["paddle"] = None
    assert gpu_module.paddleocr_use_gpu() is False


# ---------------------------------------------------------------------------
# 6. VRAM below threshold → False
# ---------------------------------------------------------------------------

def test_use_gpu_false_when_vram_below_threshold(gpu_module, monkeypatch):
    monkeypatch.setenv("PADDLEOCR_USE_GPU", "true")
    monkeypatch.setenv("PADDLEOCR_MIN_FREE_VRAM_MB", "2048")
    _install_fake_paddle(compiled_with_cuda=True)
    _install_fake_torch(cuda_available=True, free_mb=512)  # under threshold
    assert gpu_module.paddleocr_use_gpu() is False


# ---------------------------------------------------------------------------
# 7. Auto + paddle CUDA-compiled but torch can't see GPU → False
# ---------------------------------------------------------------------------

def test_paddle_says_cuda_but_torch_says_no_falls_back(gpu_module):
    _install_fake_paddle(compiled_with_cuda=True)
    _install_fake_torch(cuda_available=False)
    assert gpu_module.paddleocr_use_gpu() is False


# ---------------------------------------------------------------------------
# 8. tri-state env parser
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("true", True), ("True", True), ("TRUE", True), ("1", True), ("yes", True), ("on", True),
        ("false", False), ("False", False), ("0", False), ("no", False), ("off", False),
        ("", None), ("auto", None), ("maybe", None),
    ],
)
def test_tri_state_parser(gpu_module, raw, expected):
    assert gpu_module._parse_tri_state(raw) is expected


# ---------------------------------------------------------------------------
# 9. Invalid PADDLEOCR_MIN_FREE_VRAM_MB falls back to default 1024
# ---------------------------------------------------------------------------

def test_invalid_vram_threshold_falls_back(gpu_module, monkeypatch):
    monkeypatch.setenv("PADDLEOCR_USE_GPU", "true")
    monkeypatch.setenv("PADDLEOCR_MIN_FREE_VRAM_MB", "not-a-number")
    _install_fake_paddle(compiled_with_cuda=True)
    _install_fake_torch(cuda_available=True, free_mb=1500)  # above default 1024
    assert gpu_module.paddleocr_use_gpu() is True


# ---------------------------------------------------------------------------
# 10. Both PaddleOCR call sites import the detection helper
# ---------------------------------------------------------------------------

def test_pdf_ocr_imports_detection_helper():
    """Regression guard — services/pdf_ocr.py must wire use_gpu via helper."""
    src = open("/app/app/services/pdf_ocr.py").read()  # noqa: SIM115
    assert "from app.ocr._paddleocr_gpu import paddleocr_use_gpu" in src
    assert "use_gpu=use_gpu" in src


def test_parse_scanned_imports_detection_helper():
    """Regression guard — ocr/parse_scanned.py must wire use_gpu via helper."""
    src = open("/app/app/ocr/parse_scanned.py").read()  # noqa: SIM115
    assert "from app.ocr._paddleocr_gpu import paddleocr_use_gpu" in src
    assert "use_gpu=use_gpu" in src
