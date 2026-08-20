"""Metadata-key rules shared by every backend.

Why this exists
---------------
S3 and Azure Blob do not agree on what a metadata key may look like, and
the difference is silent until production:

  * S3/MinIO accepts anything that is a legal HTTP header name, so
    ``x-georag-derived-from-tiff-sha256`` round-trips fine.
  * Azure Blob requires metadata names to "adhere to the naming rules for
    C# identifiers" (Set Blob Metadata, REST API 2009-09-19 and later).
    A hyphen is not legal in a C# identifier, so the same key is rejected
    with HTTP 400 ``InvalidMetadata``: "The metadata specified is invalid.
    It has characters that are not permitted."

Hiding that difference is the whole point of this package, but a
normalising rewrite would be the wrong way to hide it: ``a-b`` and ``a_b``
would collide on write and could not be told apart on read, so a caller
that wrote one key could read back another. Instead this module pins the
contract at the intersection of both backends and enforces it on *both*,
so a key that would 400 against Azure also raises against MinIO — on a
developer's machine, at the call site, before it ships.

The rule: start with a letter or underscore, then letters, digits and
underscores. ASCII only. Azure also treats names case-insensitively and
returns them lowercased, so ``Report_ID`` and ``report_id`` are the same
key; callers should write lowercase and not depend on case surviving.

One caveat on the underscore, since it will look wrong from the S3 side.
An HTTP intermediary that follows the CGI convention folds ``-`` to ``_``
when mapping header names into its environment, which makes
``x-amz-meta-a_b`` and ``x-amz-meta-a-b`` the same variable; werkzeug drops
the ambiguous one, and nginx refuses underscore headers by default for the
same reason. Real S3, MinIO and Azure Blob all handle them, and ingest_pdf
has been writing ``report_id`` / ``project_id`` / ``page`` / ``sha256``
through this interface against Azure for months — but if an S3-compatible
endpoint ever sits behind such a proxy, underscore keys are the thing that
will go quietly missing. Test harnesses hit this first: see the note in
tests/test_async_client.py.
"""

from __future__ import annotations

import re

from georag_object_storage.exceptions import ObjectStorageError

#: A metadata key legal on every supported backend. Deliberately narrower
#: than either backend alone: the intersection, not the union.
_VALID_METADATA_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

__all__ = ["InvalidMetadataKeyError", "is_valid_metadata_key", "validate_metadata"]


class InvalidMetadataKeyError(ObjectStorageError):
    """Raised when a metadata key is illegal on any supported backend.

    Subclasses ``ObjectStorageError`` so existing call sites that already
    catch the package's base error keep working; the distinct type exists
    so a caller that wants to tell "you passed a bad key" apart from "the
    backend was unreachable" can.
    """

    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(
            f"invalid object metadata key {key!r}: metadata keys must be a "
            "letter or underscore followed by letters, digits or underscores "
            "(Azure Blob requires C# identifier names and rejects hyphens "
            "with HTTP 400 InvalidMetadata). Use "
            f"{key.replace('-', '_').replace('.', '_').lower()!r} instead."
        )


def is_valid_metadata_key(key: str) -> bool:
    """True iff ``key`` is legal on every supported backend."""
    return bool(_VALID_METADATA_KEY.fullmatch(key))


def validate_metadata(metadata: dict[str, str] | None) -> dict[str, str] | None:
    """Return ``metadata`` unchanged, or raise on the first illegal key.

    Returns the input so call sites can wrap an argument inline:

        metadata=validate_metadata(metadata)

    ``None`` and ``{}`` pass through — "no metadata" is always legal.
    Keys are checked in sorted order so the error names the same key on
    every run rather than whichever one dict iteration happened to reach
    first.
    """
    if not metadata:
        return metadata
    for key in sorted(metadata):
        if not is_valid_metadata_key(key):
            raise InvalidMetadataKeyError(key)
    return metadata
