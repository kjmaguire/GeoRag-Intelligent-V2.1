"""Shape + contract tests for the tiff_normalize Hatchet workflow (ADR-0005).

Doesn't exercise the live MinIO + Hatchet runtime — that's smoke-test
territory. This locks the workflow's input/output schema and the
deterministic derived-key shape so accidental edits to either break
loudly.
"""
from __future__ import annotations


def test_workflow_loads_with_input_validator():
    """Cold import must succeed and expose IngestPdfInput-shaped input."""
    from app.hatchet_workflows.tiff_normalize import (
        TiffNormalizeInput,
        TiffNormalizeOutput,
        tiff_normalize,
    )

    assert tiff_normalize.name == "tiff_normalize"

    # Input schema mirrors IngestPdfInput so Laravel can use one payload.
    fields = TiffNormalizeInput.model_fields
    for required in (
        "workspace_id", "project_id", "minio_key", "file_size",
        "correlation_token",
    ):
        assert required in fields, f"missing required field {required!r}"

    # Output schema must surface the downstream ingest_pdf workflow run id
    # so callers can chain audit lineage.
    out_fields = TiffNormalizeOutput.model_fields
    for required in (
        "source_sha256", "derived_minio_key", "page_count",
        "normalize_skipped", "ingest_pdf_workflow_run_id",
    ):
        assert required in out_fields, f"missing output field {required!r}"


def test_derived_key_is_deterministic_and_under_reports_prefix():
    """Idempotency depends on derived_pdf_key returning the same value
    for the same (project_id, source_key, source_sha) tuple. Different
    SHAs → different keys; same SHA → same key."""
    from app.hatchet_workflows.tiff_normalize import derived_pdf_key

    sha_a = "a" * 64
    sha_b = "b" * 64
    project = "550e8400-e29b-41d4-a716-446655440000"
    src_key = "tiff/550e8400-e29b-41d4-a716-446655440000/20260523_080000_scan_001.tiff"

    k_a = derived_pdf_key(project, src_key, sha_a)
    k_a_again = derived_pdf_key(project, src_key, sha_a)
    k_b = derived_pdf_key(project, src_key, sha_b)

    assert k_a == k_a_again, "derived key must be deterministic for same SHA"
    assert k_a != k_b, "different SHA must produce a different derived key"

    # Lives under the canonical reports prefix so the existing bronze
    # ingest path (PDF) picks it up.
    assert k_a.startswith(f"reports/{project}/"), k_a
    assert k_a.endswith(".pdf")

    # 8-hex SHA shard appears in the key so audit can map derivative
    # back to source without a DB lookup.
    assert sha_a[:8] in k_a


def test_derived_key_sanitises_unsafe_stem_characters():
    """Source filenames with spaces, slashes, control chars must not
    leak into the derived key."""
    from app.hatchet_workflows.tiff_normalize import derived_pdf_key

    project = "550e8400-e29b-41d4-a716-446655440000"
    nasty_key = "tiff/proj/some weird name with spaces & punctuation!.tif"
    sha = "f" * 64

    derived = derived_pdf_key(project, nasty_key, sha)
    # No spaces, no special chars in the derived basename (only the
    # path separator before the basename and the .pdf extension).
    basename = derived.rsplit("/", 1)[-1]
    for ch in (" ", "&", "!", "?"):
        assert ch not in basename, f"unsafe char {ch!r} leaked into {basename!r}"


def test_provenance_metadata_keys_are_legal_on_every_storage_backend():
    """The keys tiff_normalize writes must survive Azure Blob.

    This is the bug that made the RedStar Test import fail: the tags were
    ``x-georag-derived-from-tiff-sha256`` and friends. S3 and MinIO take
    hyphenated metadata names, so nothing here or in staging ever
    complained. Azure Blob requires C# identifier names and answered

        HTTP 400 InvalidMetadata - The metadata specified is invalid.
        It has characters that are not permitted.

    on the upload of the derived PDF - after a successful multi-page wrap,
    so every TIFF was decoded, re-encoded and then thrown away.

    The keys are asserted against georag_object_storage's shared rule
    rather than a copy of it here, so this test tracks the contract
    instead of restating it.
    """
    import re
    from pathlib import Path

    from georag_object_storage.metadata import is_valid_metadata_key

    from app.hatchet_workflows import tiff_normalize as mod

    # The idempotency tag is the one that also has to round-trip on read.
    assert is_valid_metadata_key(mod._TIFF_DERIVED_TAG), mod._TIFF_DERIVED_TAG

    # And every other literal key in the metadata dict the workflow builds.
    # Read from source: the dict is assembled inside an async task that
    # cannot be called without a live Hatchet context and object store.
    source = Path(mod.__file__).read_text(encoding="utf-8")
    block = source.split("metadata={", 1)[1].split("},", 1)[0]
    keys = re.findall(r'"([^"]+)":', block)

    assert keys, "expected to find the provenance metadata keys in the source"
    for key in keys:
        assert is_valid_metadata_key(key), (
            f"{key!r} is not a legal metadata key on Azure Blob"
        )
