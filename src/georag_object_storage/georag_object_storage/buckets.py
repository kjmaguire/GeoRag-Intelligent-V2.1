"""Logical bucket names.

Application code should reference these logical names rather than hardcoded
bucket strings, so the pending bucket-naming decision (tracked in
``ops/backlog/module-10-doc-sweep.md``) can change in one place —
``StorageConfig`` — without another call-site sweep.
"""

from __future__ import annotations

from enum import Enum


class Bucket(str, Enum):
    BRONZE = "bronze"
    BRONZE_RASTER = "bronze_raster"
    EXPORTS = "exports"
    BACKUPS = "backups"
