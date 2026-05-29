"""SEG-Y Parser — metadata-only extraction from SEG-Y seismic files.

Uses segyio to read binary and textual headers without loading full trace data.
SEG-Y files can be many gigabytes; this parser intentionally avoids loading
any trace sample data so memory usage stays bounded regardless of file size.

Parse quality metrics and structured error reporting are returned in
SegyParseResult so the caller (Dagster Silver asset) can record them in
materialisation metadata.

NOTE: Do NOT add `from __future__ import annotations` to this file.
Dagster 1.13 Config classes use Pydantic for type introspection and that import
breaks runtime annotation evaluation.
"""

import hashlib
import logging
import os
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

PARSER_NAME = "segy_parser"
PARSER_VERSION = "1.0.0"


def _sha256_file(path: str) -> str:
    """Stream-hash the file at *path*, returning the hex digest."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class SegyParseResult:
    """Container for SEG-Y metadata extracted from a single file.

    No trace sample data is stored — only header-derived metadata.
    """

    source_file: str
    survey_type: str           # "2D" or "3D"
    num_traces: int
    num_samples_per_trace: int
    sample_interval_us: int    # microseconds (from binary header field 3217)
    record_length_ms: float    # num_samples * sample_interval_us / 1000.0
    inline_min: int            # None for 2D
    inline_max: int            # None for 2D
    xline_min: int             # None for 2D
    xline_max: int             # None for 2D
    segy_revision: str         # e.g. "1.0", "2.0", or None if unreadable
    header_text: str           # EBCDIC textual header (3200 bytes, decoded)
    file_size_bytes: int
    provenance: dict[str, Any] = field(default_factory=dict)


def parse_segy_file(path: str) -> SegyParseResult:
    """Parse SEG-Y metadata without loading trace sample data.

    Opens the file in ignore_geometry mode first to read the invariant header
    fields (trace count, sample count, sample interval, textual header,
    revision).  Then attempts a second open with geometry enabled to detect
    inline/crossline ranges for 3D surveys.  If the second open fails (common
    for 2D data where no INLINE/XLINE byte locations are set), the survey is
    classified as 2D and inline/crossline fields are left as None.

    Parameters
    ----------
    path:
        Absolute path to the SEG-Y file.

    Returns
    -------
    SegyParseResult
        Metadata dict ready for insertion into silver.seismic_surveys.
    """
    import segyio  # noqa: PLC0415

    if not os.path.isfile(path):
        raise FileNotFoundError(f"segy_parser: file not found at '{path}'")

    file_size = os.path.getsize(path)
    filename = path.split("/")[-1]
    sha256_hex = _sha256_file(path)

    logger.info(
        "SEG-Y parse start: file='%s' size=%d bytes", filename, file_size
    )

    # --- Step 1: Read invariant metadata with ignore_geometry=True ---
    # This is safe for any SEG-Y file (2D or 3D) and avoids the geometry-
    # detection overhead that can be very slow on large 3D volumes.
    with segyio.open(path, "r", ignore_geometry=True) as f:
        num_traces = f.tracecount
        num_samples = len(f.samples)

        # Binary header field 3217: sample interval in microseconds
        try:
            sample_interval_us = int(f.bin[segyio.BinField.Interval])
        except Exception:
            sample_interval_us = 0
            logger.warning(
                "SEG-Y: could not read sample interval from binary header for '%s'; defaulting to 0",
                filename,
            )

        record_length_ms = (
            (num_samples * sample_interval_us) / 1000.0
            if sample_interval_us > 0
            else 0.0
        )

        # Textual (EBCDIC) header — first 3200 bytes
        header_text = ""
        try:
            raw_header = f.text[0]
            if raw_header:
                # segyio.tools.wrap formats the 3200-byte block into 40x80 lines
                header_text = segyio.tools.wrap(raw_header)
        except Exception as exc:
            logger.warning(
                "SEG-Y: could not decode textual header for '%s': %s", filename, exc
            )

        # SEG-Y revision: binary header bytes 3501-3502 (big-endian uint16)
        # The high byte is the major revision, the low byte is the minor revision.
        segy_revision = None
        try:
            revision_raw = f.bin[segyio.BinField.SEGYRevision]
            major = (revision_raw >> 8) & 0xFF
            minor = revision_raw & 0xFF
            segy_revision = f"{major}.{minor}"
        except Exception:
            pass  # revision field absent or unreadable — leave None

    logger.info(
        "SEG-Y binary header: traces=%d samples_per_trace=%d interval_us=%d "
        "record_length_ms=%.1f revision=%s",
        num_traces,
        num_samples,
        sample_interval_us,
        record_length_ms,
        segy_revision,
    )

    # --- Step 2: Attempt 3D geometry detection ---
    inline_min = inline_max = xline_min = xline_max = None
    survey_type = "2D"

    try:
        with segyio.open(path, "r", ignore_geometry=False) as f3d:
            ilines = f3d.ilines
            xlines = f3d.xlines
            if ilines is not None and len(ilines) > 0:
                inline_min = int(ilines[0])
                inline_max = int(ilines[-1])
            if xlines is not None and len(xlines) > 0:
                xline_min = int(xlines[0])
                xline_max = int(xlines[-1])
            if inline_min is not None and xline_min is not None:
                survey_type = "3D"
                logger.info(
                    "SEG-Y: 3D geometry detected — inlines=%d..%d xlines=%d..%d",
                    inline_min,
                    inline_max,
                    xline_min,
                    xline_max,
                )
    except Exception as exc:
        # 2D data, missing geometry headers, or corrupt index — not fatal
        logger.info(
            "SEG-Y: geometry detection failed ('%s') — classifying as 2D: %s",
            filename,
            exc,
        )

    result = SegyParseResult(
        source_file=filename,
        survey_type=survey_type,
        num_traces=num_traces,
        num_samples_per_trace=num_samples,
        sample_interval_us=sample_interval_us,
        record_length_ms=record_length_ms,
        inline_min=inline_min,
        inline_max=inline_max,
        xline_min=xline_min,
        xline_max=xline_max,
        segy_revision=segy_revision,
        header_text=header_text,
        file_size_bytes=file_size,
        provenance={
            "source_file_sha256": sha256_hex,
            "parser_name": PARSER_NAME,
            "parser_version": PARSER_VERSION,
            "source_col_map": None,
        },
    )

    logger.info(
        "SEG-Y parse complete: file='%s' type=%s traces=%d record_len_ms=%.1f size_mb=%.1f",
        filename,
        survey_type,
        num_traces,
        record_length_ms,
        file_size / (1024 * 1024),
    )

    return result
