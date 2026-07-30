"""Normalized object-storage exceptions.

Backend-specific errors (botocore's ``ClientError``, a future Azure SDK's
exception types, etc.) are caught at the client boundary and re-raised as
these types, so call sites and any future non-S3 backend never need to
import a vendor SDK's exception classes.
"""

from __future__ import annotations


class ObjectStorageError(Exception):
    """Base class for all georag_object_storage errors."""


class ObjectNotFoundError(ObjectStorageError):
    """Raised when a requested object key does not exist."""

    def __init__(self, bucket: str, key: str) -> None:
        self.bucket = bucket
        self.key = key
        super().__init__(f"object not found: {bucket}/{key}")


class BucketNotFoundError(ObjectStorageError):
    """Raised when a requested bucket does not exist."""

    def __init__(self, bucket: str) -> None:
        self.bucket = bucket
        super().__init__(f"bucket not found: {bucket}")
