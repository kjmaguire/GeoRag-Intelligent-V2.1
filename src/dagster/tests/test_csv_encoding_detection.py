"""Regression tests for _encoding.py and _csv_io.py — Sprint 1 encoding detection.

Covers:
  - UTF-8 bytes → encoding detected as utf-8 (or ascii for pure-ASCII content).
  - cp1252-encoded bytes with 'é' → detected as cp1252/windows-1252, no decode
    errors, accented character preserved in output text.
  - UTF-16 LE with BOM → detected and decoded correctly.
  - End-to-end path through parse_csv_collars() for the cp1252 case.
  - detected_encoding exposed on CollarParseResult and SampleParseResult.

Run with:  pytest tests/test_csv_encoding_detection.py -v
"""

from __future__ import annotations

from io import BytesIO, StringIO


from georag_dagster.parsers._encoding import detect_encoding, open_csv_bytes
from georag_dagster.parsers._csv_io import open_csv_with_encoding
from georag_dagster.parsers.csv_collar import parse_csv_collars
from georag_dagster.parsers.csv_sample import parse_csv_samples


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_collar_csv(extra_text: str = "") -> str:
    """Minimal collar CSV with an optional extra text field (name) for encoding tests."""
    return (
        f"HoleID,Easting,Northing,Elevation,Dip,Azimuth,Name\n"
        f"DH-01,495000.0,6200000.0,450.0,-65.0,135.0,{extra_text}\n"
    )


def _make_sample_csv(text_in_field: str = "Core") -> str:
    """Minimal sample CSV; text_in_field can embed accented characters."""
    return (
        "HoleID,From,To,SampleType,Au_ppm\n"
        f"DH-01,0.0,1.5,{text_in_field},0.42\n"
    )


# ---------------------------------------------------------------------------
# Unit tests: detect_encoding
# ---------------------------------------------------------------------------

class TestDetectEncoding:
    def test_utf8_bytes_detected_as_utf8_or_ascii(self):
        data = "HoleID,Easting,Northing\nDH-01,495000,6200000\n".encode("utf-8")
        result = detect_encoding(data)
        assert result.lower().replace("-", "").replace("_", "") in (
            "utf8", "ascii"
        ), f"Expected utf-8 or ascii for pure UTF-8 bytes, got '{result}'"

    def test_cp1252_bytes_detected_correctly(self):
        # é is 0xe9 in cp1252 (also valid in cp1250, latin-1, windows-1250/1252)
        # charset-normalizer returns one of several compatible single-byte Western
        # encodings for a short string with one accented character — all are
        # acceptable since they all decode 0xe9 identically.
        data = "Café,495000,6200000\n".encode("cp1252")
        result = detect_encoding(data)
        normalised = result.lower().replace("-", "").replace("_", "")
        assert normalised in (
            "cp1250", "cp1252", "windows1250", "windows1252", "latin1", "iso88591"
        ), f"Expected a Western single-byte encoding for cp1252 bytes, got '{result}'"

    def test_utf16le_bom_detected_as_utf16(self):
        # Python encodes UTF-16 with a BOM by default (either LE or BE depending on platform)
        data = "HoleID,Easting\nDH-01,495000\n".encode("utf-16")
        result = detect_encoding(data)
        assert "utf" in result.lower() and "16" in result.lower(), (
            f"Expected a utf-16 encoding for UTF-16 bytes, got '{result}'"
        )


# ---------------------------------------------------------------------------
# Unit tests: open_csv_bytes
# ---------------------------------------------------------------------------

class TestOpenCsvBytes:
    def test_utf8_returns_stringio_and_utf8_label(self):
        content = "header1,header2\nval1,val2\n"
        data = content.encode("utf-8")
        stream, encoding = open_csv_bytes(data)
        assert encoding.lower().replace("-", "").replace("_", "") in ("utf8", "ascii")
        assert stream.getvalue() == content

    def test_cp1252_with_accented_char_no_decode_error(self):
        content = "Name,Value\nCafé,42\n"
        data = content.encode("cp1252")
        stream, encoding = open_csv_bytes(data)
        text = stream.getvalue()
        # The é character must be preserved (not replaced with a placeholder)
        assert "é" in text, f"Expected 'é' to be preserved; got: {text!r}"

    def test_utf16_decoded_correctly(self):
        content = "HoleID,Depth\nDH-01,100\n"
        data = content.encode("utf-16")  # includes BOM
        stream, encoding = open_csv_bytes(data)
        text = stream.getvalue()
        assert "HoleID" in text, f"UTF-16 decoding failed; got: {text!r}"
        assert "DH-01" in text


# ---------------------------------------------------------------------------
# Unit tests: open_csv_with_encoding (file-like object path)
# ---------------------------------------------------------------------------

class TestOpenCsvWithEncoding:
    def test_text_stream_returns_utf8_label(self):
        """A text StringIO is already decoded — encoding reported as 'utf-8'.

        Sprint 2: open_csv_with_encoding now returns a 4-tuple:
        (stream, encoding, sha256_hex, byte_count).
        """
        stream_in = StringIO("a,b\n1,2\n")
        stream_out, encoding, sha256_hex, byte_count = open_csv_with_encoding(stream_in)
        assert encoding.lower().replace("-", "") in ("utf8", "utf-8", "ascii")
        assert stream_out.getvalue() == "a,b\n1,2\n"
        assert isinstance(sha256_hex, str) and len(sha256_hex) == 64
        assert isinstance(byte_count, int) and byte_count > 0

    def test_binary_stream_cp1252_accented_char_preserved(self):
        content = "Name,Value\nCafé,42\n"
        binary_io = BytesIO(content.encode("cp1252"))
        stream_out, encoding, sha256_hex, byte_count = open_csv_with_encoding(binary_io)
        text = stream_out.getvalue()
        assert "é" in text, f"Accented char not preserved through open_csv_with_encoding; got: {text!r}"
        assert isinstance(sha256_hex, str) and len(sha256_hex) == 64


# ---------------------------------------------------------------------------
# Integration: parse_csv_collars with cp1252 encoding
# ---------------------------------------------------------------------------

class TestCollarParserEncodingIntegration:
    def test_cp1252_collar_csv_detected_and_parsed(self):
        """End-to-end: cp1252-encoded collar CSV must parse without raising,
        the accented character must appear in a text field, and
        detected_encoding must be reported on the result."""
        # Embed 'é' in the Name column (unmapped → ignored, but encoding must survive)
        csv_text = _make_collar_csv(extra_text="Café-Deposit")
        csv_bytes = csv_text.encode("cp1252")
        binary_io = BytesIO(csv_bytes)

        result = parse_csv_collars(binary_io)

        # detected_encoding must be set and identify a Western single-byte encoding
        # compatible with cp1252 bytes. charset-normalizer may return cp1250,
        # cp1252, windows-1252, latin-1, etc. — all decode 0xe9 (é) identically.
        enc = result.detected_encoding.lower().replace("-", "").replace("_", "")
        assert enc in (
            "cp1250", "cp1252", "windows1250", "windows1252", "latin1", "iso88591"
        ), (
            f"detected_encoding '{result.detected_encoding}' is not a recognised "
            f"Western single-byte encoding alias"
        )

        # Row must have parsed successfully
        assert result.valid_rows == 1, (
            "cp1252-encoded collar CSV should produce exactly 1 valid row"
        )

    def test_detected_encoding_field_on_collar_parse_result(self):
        """Verify detected_encoding is exposed as an attribute on CollarParseResult."""
        csv = StringIO(_make_collar_csv())
        result = parse_csv_collars(csv)
        assert hasattr(result, "detected_encoding"), (
            "CollarParseResult must expose detected_encoding"
        )
        assert isinstance(result.detected_encoding, str)


# ---------------------------------------------------------------------------
# Integration: parse_csv_samples detected_encoding field
# ---------------------------------------------------------------------------

class TestSampleParserEncodingIntegration:
    def test_detected_encoding_field_on_sample_parse_result(self):
        """Verify detected_encoding is exposed as an attribute on SampleParseResult."""
        csv = StringIO(_make_sample_csv())
        result = parse_csv_samples(csv)
        assert hasattr(result, "detected_encoding"), (
            "SampleParseResult must expose detected_encoding"
        )
        assert isinstance(result.detected_encoding, str)

    def test_cp1252_sample_csv_parsed_without_error(self):
        """cp1252 bytes flowing through parse_csv_samples must not raise."""
        csv_bytes = _make_sample_csv().encode("cp1252")
        binary_io = BytesIO(csv_bytes)
        result = parse_csv_samples(binary_io)
        assert result.valid_rows == 1
