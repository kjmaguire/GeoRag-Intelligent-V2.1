"""Phase 2.0 (2026-05-22) — docling rapidocr OCR config tests.

These tests verify that `_parse_with_docling` correctly wires up
docling's rapidocr OCR engine when DOCLING_OCR_ENABLED=true, including:
  - opts.do_ocr toggled per env var
  - RapidOcrOptions instance built with the right lang + model cache path
  - graceful fallback when RapidOcrOptions can't be imported
  - graceful fallback when the cache dir can't be created
  - env-var defaults (RAPIDOCR_MODEL_DIR=/tmp/rapidocr_models)

No real docling install required — the relevant docling symbols are
patched via sys.modules injection.

Run with:
    pytest src/dagster/tests/test_pdf_docling_ocr_config.py -v
"""

from __future__ import annotations

import importlib
import sys
import types
from unittest.mock import MagicMock

import pytest


# Inject fake boto3 / botocore.config so the parser module imports cleanly
# on dev hosts that don't have boto3 installed.
sys.modules.setdefault("boto3", MagicMock())
sys.modules.setdefault("botocore", MagicMock())
sys.modules.setdefault("botocore.config", MagicMock())


# ---------------------------------------------------------------------------
# Helper: build a fake docling stack and inject it into sys.modules.
# Returns (PdfPipelineOptions_class, RapidOcrOptions_class, captured_calls)
# so each test can assert on what _parse_with_docling configured.
# ---------------------------------------------------------------------------


def _install_fake_docling(include_rapidocr: bool = True):
    """Replace docling.* modules with stubs. Returns the stub classes so
    tests can inspect what was assigned to PdfPipelineOptions instances.
    """
    # PdfPipelineOptions — a plain class so attribute assignment works
    class _PdfPipelineOptions:
        def __init__(self):
            self.do_ocr = False
            self.do_table_structure = False
            self.generate_picture_images = False
            self.images_scale = 1.0
            self.accelerator_options = None
            self.ocr_options = None

    class _AcceleratorOptions:
        def __init__(self, device=None):
            self.device = device

    class _AcceleratorDevice:
        CUDA = "cuda"
        CPU = "cpu"

    class _RapidOcrOptions:
        def __init__(self, lang=None, backend="onnxruntime",
                     print_verbose=False, rapidocr_params=None,
                     **kwargs):
            self.lang = lang if lang is not None else ["chinese"]
            self.backend = backend
            self.print_verbose = print_verbose
            self.rapidocr_params = rapidocr_params or {}
            self.kwargs = kwargs

    # DocumentConverter stub that records what it was called with and
    # returns a fake result containing an empty doc.
    class _FakeDoc:
        texts = []
        tables = []
        pictures = []

        def export_to_markdown(self):
            return "# Fake\n\nbody"

    class _FakeResult:
        document = _FakeDoc()

    class _DocumentConverter:
        last_format_options = None

        def __init__(self, format_options=None):
            type(self).last_format_options = format_options

        def convert(self, path):
            return _FakeResult()

    class _PdfFormatOption:
        def __init__(self, pipeline_options=None):
            self.pipeline_options = pipeline_options

    class _InputFormat:
        PDF = "pdf"

    mod_doc_conv = types.ModuleType("docling.document_converter")
    mod_doc_conv.DocumentConverter = _DocumentConverter
    mod_doc_conv.PdfFormatOption = _PdfFormatOption

    mod_base = types.ModuleType("docling.datamodel.base_models")
    mod_base.InputFormat = _InputFormat

    mod_pipe = types.ModuleType("docling.datamodel.pipeline_options")
    mod_pipe.PdfPipelineOptions = _PdfPipelineOptions
    mod_pipe.AcceleratorOptions = _AcceleratorOptions
    mod_pipe.AcceleratorDevice = _AcceleratorDevice
    if include_rapidocr:
        mod_pipe.RapidOcrOptions = _RapidOcrOptions

    mod_datamodel = types.ModuleType("docling.datamodel")
    mod_root = types.ModuleType("docling")

    sys.modules["docling"] = mod_root
    sys.modules["docling.datamodel"] = mod_datamodel
    sys.modules["docling.datamodel.base_models"] = mod_base
    sys.modules["docling.datamodel.pipeline_options"] = mod_pipe
    sys.modules["docling.document_converter"] = mod_doc_conv

    return _PdfPipelineOptions, _RapidOcrOptions if include_rapidocr else None, _DocumentConverter


@pytest.fixture
def docling_stack_with_rapidocr():
    yield _install_fake_docling(include_rapidocr=True)
    # Cleanup — remove the fake modules so other tests can re-inject
    for k in (
        "docling",
        "docling.datamodel",
        "docling.datamodel.base_models",
        "docling.datamodel.pipeline_options",
        "docling.document_converter",
    ):
        sys.modules.pop(k, None)


@pytest.fixture
def docling_stack_without_rapidocr():
    yield _install_fake_docling(include_rapidocr=False)
    for k in (
        "docling",
        "docling.datamodel",
        "docling.datamodel.base_models",
        "docling.datamodel.pipeline_options",
        "docling.document_converter",
    ):
        sys.modules.pop(k, None)


@pytest.fixture
def parser_module():
    from georag_dagster.parsers import pdf_report
    importlib.reload(pdf_report)
    return pdf_report


# ---------------------------------------------------------------------------
# 1. DOCLING_OCR_ENABLED unset → opts.do_ocr stays False
# ---------------------------------------------------------------------------

def test_docling_ocr_disabled_by_default(
    docling_stack_with_rapidocr, parser_module, monkeypatch
):
    monkeypatch.delenv("DOCLING_OCR_ENABLED", raising=False)
    PdfPipelineOptions, _, DocumentConverter = docling_stack_with_rapidocr

    parser_module._parse_with_docling("/tmp/fake.pdf", pdf_sha256=None)

    pipe_opts = DocumentConverter.last_format_options["pdf"].pipeline_options
    assert pipe_opts.do_ocr is False
    assert pipe_opts.ocr_options is None


# ---------------------------------------------------------------------------
# 2. DOCLING_OCR_ENABLED=false → opts.do_ocr stays False
# ---------------------------------------------------------------------------

def test_docling_ocr_explicit_false_keeps_off(
    docling_stack_with_rapidocr, parser_module, monkeypatch
):
    monkeypatch.setenv("DOCLING_OCR_ENABLED", "false")
    PdfPipelineOptions, _, DocumentConverter = docling_stack_with_rapidocr

    parser_module._parse_with_docling("/tmp/fake.pdf", pdf_sha256=None)

    pipe_opts = DocumentConverter.last_format_options["pdf"].pipeline_options
    assert pipe_opts.do_ocr is False
    assert pipe_opts.ocr_options is None


# ---------------------------------------------------------------------------
# 3. DOCLING_OCR_ENABLED=true → do_ocr=True + RapidOcrOptions wired
# ---------------------------------------------------------------------------

def test_docling_ocr_enabled_wires_rapidocr_options(
    docling_stack_with_rapidocr, parser_module, monkeypatch, tmp_path
):
    monkeypatch.setenv("DOCLING_OCR_ENABLED", "true")
    monkeypatch.setenv("RAPIDOCR_MODEL_DIR", str(tmp_path / "rapidocr_cache"))
    monkeypatch.delenv("DOCLING_OCR_LANGS", raising=False)

    PdfPipelineOptions, RapidOcrOptions, DocumentConverter = docling_stack_with_rapidocr

    parser_module._parse_with_docling("/tmp/fake.pdf", pdf_sha256=None)

    pipe_opts = DocumentConverter.last_format_options["pdf"].pipeline_options
    assert pipe_opts.do_ocr is True
    assert isinstance(pipe_opts.ocr_options, RapidOcrOptions)
    assert pipe_opts.ocr_options.lang == ["english"]
    assert pipe_opts.ocr_options.backend == "onnxruntime"
    assert pipe_opts.ocr_options.rapidocr_params == {
        "Global.model_root_dir": str(tmp_path / "rapidocr_cache"),
    }
    # The cache dir was created
    assert (tmp_path / "rapidocr_cache").exists()
    assert (tmp_path / "rapidocr_cache").is_dir()


# ---------------------------------------------------------------------------
# 4. DOCLING_OCR_LANGS env var is honored (comma-separated, trimmed)
# ---------------------------------------------------------------------------

def test_docling_ocr_langs_env_var_honored(
    docling_stack_with_rapidocr, parser_module, monkeypatch, tmp_path
):
    monkeypatch.setenv("DOCLING_OCR_ENABLED", "true")
    monkeypatch.setenv("RAPIDOCR_MODEL_DIR", str(tmp_path))
    monkeypatch.setenv("DOCLING_OCR_LANGS", " english , chinese , french ")

    _, _, DocumentConverter = docling_stack_with_rapidocr

    parser_module._parse_with_docling("/tmp/fake.pdf", pdf_sha256=None)

    pipe_opts = DocumentConverter.last_format_options["pdf"].pipeline_options
    assert pipe_opts.ocr_options.lang == ["english", "chinese", "french"]


# ---------------------------------------------------------------------------
# 5. RAPIDOCR_MODEL_DIR unset → defaults to /tmp/rapidocr_models
# ---------------------------------------------------------------------------

def test_rapidocr_model_dir_defaults_to_tmp_path(
    docling_stack_with_rapidocr, parser_module, monkeypatch
):
    monkeypatch.setenv("DOCLING_OCR_ENABLED", "true")
    monkeypatch.delenv("RAPIDOCR_MODEL_DIR", raising=False)

    _, _, DocumentConverter = docling_stack_with_rapidocr

    parser_module._parse_with_docling("/tmp/fake.pdf", pdf_sha256=None)

    pipe_opts = DocumentConverter.last_format_options["pdf"].pipeline_options
    assert pipe_opts.ocr_options.rapidocr_params[
        "Global.model_root_dir"
    ] == "/tmp/rapidocr_models"


# ---------------------------------------------------------------------------
# 6. RapidOcrOptions ImportError → degrade to do_ocr=False, no crash
# ---------------------------------------------------------------------------

def test_docling_ocr_falls_back_when_rapidocr_options_missing(
    docling_stack_without_rapidocr, parser_module, monkeypatch, tmp_path
):
    monkeypatch.setenv("DOCLING_OCR_ENABLED", "true")
    monkeypatch.setenv("RAPIDOCR_MODEL_DIR", str(tmp_path))

    PdfPipelineOptions, _, DocumentConverter = docling_stack_without_rapidocr

    # Should not raise
    parser_module._parse_with_docling("/tmp/fake.pdf", pdf_sha256=None)

    pipe_opts = DocumentConverter.last_format_options["pdf"].pipeline_options
    assert pipe_opts.do_ocr is False
    assert pipe_opts.ocr_options is None


# ---------------------------------------------------------------------------
# 7. Cache dir creation failure → degrade to do_ocr=False (no exception)
# ---------------------------------------------------------------------------

def test_docling_ocr_falls_back_when_cache_dir_unwritable(
    docling_stack_with_rapidocr, parser_module, monkeypatch, tmp_path
):
    monkeypatch.setenv("DOCLING_OCR_ENABLED", "true")
    monkeypatch.setenv("RAPIDOCR_MODEL_DIR", str(tmp_path / "unused"))

    _, _, DocumentConverter = docling_stack_with_rapidocr

    # Simulate an unwritable parent (portable across Windows/Linux) by
    # patching os.makedirs on the parser module so the makedirs attempt
    # raises. The parser must catch this and degrade to do_ocr=False
    # without surfacing the exception.
    def _raise(*args, **kwargs):
        raise PermissionError("simulated unwritable parent")

    monkeypatch.setattr(parser_module.os, "makedirs", _raise)

    parser_module._parse_with_docling("/tmp/fake.pdf", pdf_sha256=None)

    pipe_opts = DocumentConverter.last_format_options["pdf"].pipeline_options
    assert pipe_opts.do_ocr is False


# ---------------------------------------------------------------------------
# 8. opts.do_table_structure / generate_picture_images stay on regardless
#    of OCR setting (the figure-handoff fix from Phase 1 must not regress)
# ---------------------------------------------------------------------------

def test_table_and_figure_settings_unchanged_when_ocr_on(
    docling_stack_with_rapidocr, parser_module, monkeypatch, tmp_path
):
    monkeypatch.setenv("DOCLING_OCR_ENABLED", "true")
    monkeypatch.setenv("RAPIDOCR_MODEL_DIR", str(tmp_path))

    _, _, DocumentConverter = docling_stack_with_rapidocr

    parser_module._parse_with_docling("/tmp/fake.pdf", pdf_sha256=None)

    pipe_opts = DocumentConverter.last_format_options["pdf"].pipeline_options
    assert pipe_opts.do_table_structure is True
    assert pipe_opts.generate_picture_images is True
    assert pipe_opts.images_scale == 1.5


def test_table_and_figure_settings_unchanged_when_ocr_off(
    docling_stack_with_rapidocr, parser_module, monkeypatch
):
    monkeypatch.setenv("DOCLING_OCR_ENABLED", "false")

    _, _, DocumentConverter = docling_stack_with_rapidocr

    parser_module._parse_with_docling("/tmp/fake.pdf", pdf_sha256=None)

    pipe_opts = DocumentConverter.last_format_options["pdf"].pipeline_options
    assert pipe_opts.do_table_structure is True
    assert pipe_opts.generate_picture_images is True


# ---------------------------------------------------------------------------
# 9. DOCLING_OCR_ENABLED comparison is case-insensitive
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw, expected_do_ocr",
    [
        ("true", True),
        ("True", True),
        ("TRUE", True),
        ("false", False),
        ("False", False),
        ("0", False),
        ("1", False),  # only literal "true" enables
        ("yes", False),
    ],
)
def test_docling_ocr_enabled_parses_env_string(
    docling_stack_with_rapidocr, parser_module, monkeypatch, tmp_path,
    raw, expected_do_ocr,
):
    monkeypatch.setenv("DOCLING_OCR_ENABLED", raw)
    monkeypatch.setenv("RAPIDOCR_MODEL_DIR", str(tmp_path))

    _, _, DocumentConverter = docling_stack_with_rapidocr

    parser_module._parse_with_docling("/tmp/fake.pdf", pdf_sha256=None)

    pipe_opts = DocumentConverter.last_format_options["pdf"].pipeline_options
    assert pipe_opts.do_ocr is expected_do_ocr


# ---------------------------------------------------------------------------
# 10. RapidOcrOptions construction failure → degrade gracefully
# ---------------------------------------------------------------------------

def test_docling_ocr_falls_back_on_rapidocr_construction_error(
    docling_stack_with_rapidocr, parser_module, monkeypatch, tmp_path
):
    monkeypatch.setenv("DOCLING_OCR_ENABLED", "true")
    monkeypatch.setenv("RAPIDOCR_MODEL_DIR", str(tmp_path))

    _, RapidOcrOptions, DocumentConverter = docling_stack_with_rapidocr
    # Make RapidOcrOptions raise on construction
    pipe_mod = sys.modules["docling.datamodel.pipeline_options"]

    class _BadRapidOcrOptions:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("simulated rapidocr config failure")

    pipe_mod.RapidOcrOptions = _BadRapidOcrOptions

    parser_module._parse_with_docling("/tmp/fake.pdf", pdf_sha256=None)

    pipe_opts = DocumentConverter.last_format_options["pdf"].pipeline_options
    assert pipe_opts.do_ocr is False


# ---------------------------------------------------------------------------
# 11. _FIGURE_TEMPDIR_ROOT remains unchanged (Phase 1 regression guard)
# ---------------------------------------------------------------------------

def test_figure_tempdir_root_unchanged_in_phase_2(parser_module):
    assert parser_module._FIGURE_TEMPDIR_ROOT == "/tmp/georag_figures"
    assert hasattr(parser_module, "_figure_tempdir")


# ---------------------------------------------------------------------------
# 12. Legacy module-scope OCR cache symbols stay removed
# ---------------------------------------------------------------------------

def test_no_legacy_ocr_cache_symbols(parser_module):
    assert not hasattr(parser_module, "_DOCLING_FIGURE_CACHE")
    assert not hasattr(parser_module, "_extract_docling_figures")
