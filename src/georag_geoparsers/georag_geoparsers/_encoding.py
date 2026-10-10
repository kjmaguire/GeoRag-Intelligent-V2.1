"""Shared encoding detection helper for CSV parsers.

Detection order (audit finding 11, 2026-10)
-------------------------------------------
charset-normalizer's best guess alone mislabelled Windows-1252: ``Rødberg``
(0xF8 = ``ø``) came back as cp1250 and decoded as ``Rřdberg``, and a short
``Café`` came back as cp1006 (an Urdu code page). A mislabelled single-byte
page still "decodes", so nothing failed - the accented letters were simply
wrong in every hole name, lithology and comment. The order is therefore:

  1. a byte-order mark (UTF-8-SIG / UTF-16 / UTF-32);
  2. strict UTF-8 - a file that decodes cleanly as UTF-8 almost certainly is
     UTF-8, whereas a Windows-1252 file with an accented letter almost never
     is valid UTF-8;
  3. UTF-8 with damaged bytes - valid multi-byte sequences at least as
     numerous as the bad bytes (a truncated field in an otherwise UTF-8
     export). Decoded with replacement and REPORTED
     (:func:`decode_warnings` names the replaced characters);
  4. strict cp1252 - what Windows survey and spreadsheet software writes;
  5. only then charset-normalizer, isolated to the code pages a mining
     delivery can plausibly use (cp1252 has five undefined bytes, so a file
     that uses one of them lands here), and UTF-16/32 without a BOM.

Step 4 is a deliberate preference, not a proof: a non-Latin single-byte file
(cp1251 Cyrillic, cp1253 Greek) usually decodes under cp1252 too, as
different letters. :func:`decode_warnings` therefore puts the first few
non-ASCII tokens of the decoded text in the warning, so a person can see
whether ``Rødberg`` arrived as ``Rødberg`` or as something else.
"""

from __future__ import annotations

import codecs
import logging
import re
from io import StringIO
from typing import Any

logger = logging.getLogger(__name__)

_CONFIDENCE_THRESHOLD = 0.5

#: Code pages charset-normalizer may choose among once UTF-8 and cp1252 have
#: been ruled out. Western and central European single-byte pages, the two
#: DOS pages mining software still writes, and the Unicode transformation
#: formats (a BOM-less UTF-16 export has no other way to be recognised).
_PLAUSIBLE_CODE_PAGES: tuple[str, ...] = (
    "cp1252", "cp1250", "latin_1", "iso8859_15", "iso8859_2", "mac_roman",
    "cp850", "cp437",
    "utf_16", "utf_16_le", "utf_16_be", "utf_32", "utf_32_le", "utf_32_be",
    "utf_8", "ascii",
)

#: Valid UTF-8 multi-byte sequences are counted against replaced bytes in
#: step 3; both are measured on the lenient decode.
_REPLACEMENT_CHAR = "�"
_NON_ASCII_VALID = re.compile("[^\\x00-\\x7f" + _REPLACEMENT_CHAR + "]")

#: A whitespace-delimited chunk of text containing a non-ASCII character.
_NON_ASCII_TOKEN = re.compile(r"[^\s,;|\t\"']*[^\x00-\x7f][^\s,;|\t\"']*")


def _charset_normalizer_guess(data: bytes, *, isolated: bool) -> str | None:
    """charset-normalizer's best guess, or None when it has none it trusts."""
    try:
        from charset_normalizer import from_bytes
    except ImportError:
        logger.debug("charset_normalizer not available")
        return None

    if isolated:
        results = from_bytes(data, cp_isolation=list(_PLAUSIBLE_CODE_PAGES))
    else:
        results = from_bytes(data)
    best = results.best()
    if best is None:
        return None

    encoding = best.encoding
    # charset-normalizer exposes confidence differently depending on version.
    # We check the 'chaos' score: low chaos = high confidence.  Convert to
    # a 0-1 confidence scale: confidence = 1 - chaos (chaos is 0..1).
    actual_confidence = 1.0 - best.chaos

    if actual_confidence < _CONFIDENCE_THRESHOLD:
        logger.debug(
            "Encoding detection confidence %.2f below threshold - ignoring "
            "(detected: %s)",
            actual_confidence,
            encoding,
        )
        return None
    return encoding


def _detect(data: bytes) -> tuple[str, str | None]:
    """``(encoding, text)`` - *text* is the decode when detection already made it."""
    # 1. Byte-order marks. UTF-32-LE's BOM starts with UTF-16-LE's, so 32 first.
    if data.startswith(codecs.BOM_UTF8):
        return "utf-8-sig", data.decode("utf-8-sig", errors="replace")
    if data.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        return "utf-32", None
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return "utf-16", None

    # NUL bytes mean this is not single-byte text: BOM-less UTF-16/32 (or not
    # text at all). Steps 2-4 would "succeed" on it and produce garbage.
    if b"\x00" not in data:
        # 2. Strict UTF-8.
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            logger.debug("not strict UTF-8; trying the damaged-UTF-8 and single-byte readings")
        else:
            return ("ascii" if data.isascii() else "utf-8"), text

        # 3. UTF-8 with damaged bytes.
        lenient = data.decode("utf-8", errors="replace")
        bad = lenient.count(_REPLACEMENT_CHAR)
        good = len(_NON_ASCII_VALID.findall(lenient))
        if good and good >= bad:
            logger.warning(
                "csv_io: %d byte(s) are not valid UTF-8 beside %d valid "
                "multi-byte character(s) - read as UTF-8 with replacement",
                bad, good,
            )
            return "utf-8", lenient

        # 4. Strict cp1252.
        try:
            return "cp1252", data.decode("cp1252")
        except UnicodeDecodeError:
            logger.debug("csv_io: not strict cp1252 (an undefined byte) - asking charset-normalizer")

    # 5. Everything else.
    guess = _charset_normalizer_guess(data, isolated=b"\x00" not in data)
    return (guess or "utf-8"), None


def detect_encoding(data: bytes) -> str:
    """Detect the character encoding of *data*.

    Returns the encoding name (e.g. ``"utf-8"``, ``"cp1252"``, ``"utf-16"``).
    Falls back to ``"utf-8"`` when nothing is trusted.
    """
    return _detect(data)[0]


def is_utf8_compatible(encoding: str | None) -> bool:
    """True when *encoding* names UTF-8 (with or without a BOM) or plain ASCII.

    charset-normalizer reports UTF-8 as ``"utf_8"`` (underscore), and a BOM'd
    file as ``"utf_8"`` too, while callers' own default is ``"utf-8"``. The five
    drill parsers compared ``name.lower().replace("-", "")`` against
    ``("utf8", "utf-8", "ascii")``, which ``"utf_8"`` never matches, so every
    UTF-8 file with a single non-ASCII character (``°``, ``µ``, ``Å``, ``m³``,
    an Excel "CSV UTF-8" BOM) was reported as ``encoding_non_utf8 ... decoded
    with replacement`` and the run went amber for a file that decoded
    perfectly.
    """
    if not encoding:
        return True
    name = encoding.lower().replace("-", "").replace("_", "").replace(" ", "")
    return name in ("utf8", "utf8sig", "ascii", "usascii")


def open_csv_bytes(data: bytes) -> tuple[StringIO, str]:
    """Detect encoding of *data*, decode, and return (StringIO, encoding_name).

    Callers should log the returned encoding at INFO level if it is not utf-8.
    """
    encoding, text = _detect(data)
    if text is None:
        try:
            text = data.decode(encoding, errors="replace")
        except (LookupError, UnicodeDecodeError):
            logger.debug("cannot decode as %s; decoding as UTF-8 with replacements", encoding, exc_info=True)
            text = data.decode("utf-8", errors="replace")
            encoding = "utf-8"
    return StringIO(text), encoding


#: How many non-ASCII tokens a decoding warning quotes.
_MAX_TOKENS = 5
_MAX_TOKEN_CHARS = 40


def non_ascii_tokens(text: str, limit: int = _MAX_TOKENS) -> list[str]:
    """The first *limit* distinct non-ASCII words of *text*, shortest context.

    Scanned over a bounded prefix: the point is to let a person see how the
    accented letters decoded, not to inventory the file.
    """
    seen: dict[str, None] = {}
    for match in _NON_ASCII_TOKEN.finditer(text[:200_000]):
        token = match.group(0)[:_MAX_TOKEN_CHARS]
        if token and token not in seen:
            seen[token] = None
            if len(seen) >= limit:
                break
    return list(seen)


def decode_warnings(encoding: str, text: str) -> list[dict[str, Any]]:
    """The warnings a decode earned: a non-UTF-8 source, replaced bytes.

    ``encoding_non_utf8`` keeps its historical code and message prefix, and now
    quotes the first non-ASCII tokens as decoded - ``Rødberg`` read as cp1252
    shows as ``Rødberg``; read wrongly it shows as something a person can
    recognise as wrong. ``encoding_replacement_characters`` is new: a decoded
    text containing U+FFFD has lost those bytes, which is data altered, and a
    file labelled UTF-8 used to be able to do that with no trace.
    """
    out: list[dict[str, Any]] = []
    if not is_utf8_compatible(encoding):
        tokens = non_ascii_tokens(text)
        shown = ", ".join(repr(t) for t in tokens)
        out.append({
            "row": None,
            "code": "encoding_non_utf8",
            "message": (
                f"detected encoding '{encoding}' (not UTF-8) - decoded with "
                f"replacement"
                + (f"; first non-ASCII text: {shown}" if tokens else "")
            ),
            "detail": (
                f"The file is not UTF-8, so it was read as '{encoding}'. "
                + (
                    f"Accented text came out as {shown}; if that is not what "
                    f"the source says, re-save the file as UTF-8 and upload "
                    f"it again."
                    if tokens else
                    "No non-ASCII characters were found in it."
                )
            ),
            "context": {"encoding": encoding, "non_ascii_samples": tokens},
        })
    replaced = text.count(_REPLACEMENT_CHAR)
    if replaced:
        out.append({
            "row": None,
            "code": "encoding_replacement_characters",
            "message": (
                f"{replaced} character(s) could not be decoded as '{encoding}' "
                f"and were replaced with U+FFFD"
            ),
            "detail": (
                f"{replaced} byte sequence(s) in this file are not valid "
                f"'{encoding}', so those characters were replaced by the "
                f"replacement character (U+FFFD) rather than guessed. Check "
                f"the values they sit in, or re-save the file as UTF-8."
            ),
            "context": {"encoding": encoding, "count": replaced},
        })
    return out
