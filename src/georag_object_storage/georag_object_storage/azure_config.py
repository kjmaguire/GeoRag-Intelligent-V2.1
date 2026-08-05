"""Environment-based configuration for the Azure Blob backend.

Separate dataclass from :class:`~georag_object_storage.config.StorageConfig`
rather than extending it — the shapes genuinely differ (S3 is
endpoint/key/secret/region; Azure Blob is account/credential/container), and
forcing them into one dataclass would mean every S3 caller carries dead
Azure fields and vice versa. ``factory.py`` picks which one to build based
on ``STORAGE_BACKEND``.

Two auth modes, mirroring the two ways Azure Blob is actually configured in
practice:

- **Connection string** (``AZURE_STORAGE_CONNECTION_STRING``) — bundles
  account name + key. Simplest, works everywhere, but is a long-lived
  secret. Required for :meth:`AzureBlobStorage.presign_get`/``presign_put``,
  since SAS-token generation needs the account key directly (a
  user-delegation-key SAS, the managed-identity-compatible path, is a
  separate, more involved flow — not implemented here; presign calls raise
  ``NotImplementedError`` under managed-identity auth).
- **Managed identity** (``AZURE_STORAGE_ACCOUNT_URL`` set, no connection
  string) — no secret in the environment at all; the Container App's
  managed identity authenticates via ``DefaultAzureCredential``. Preferred
  for the actual Azure deployment; the connection-string path exists for
  local/CI testing against Azurite or a real account without wiring up
  identity federation.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from georag_object_storage.buckets import Bucket

# (canonical env var, default container name) — mirrors config.py's
# _BUCKET_ENV shape but Azure containers don't have the S3-era legacy
# MINIO_*/SEAWEEDFS_* env var history to fall back to, so no legacy chain.
_CONTAINER_ENV: dict[Bucket, tuple[str, str]] = {
    Bucket.BRONZE: ("AZURE_STORAGE_CONTAINER_BRONZE", "bronze"),
    Bucket.BRONZE_RASTER: ("AZURE_STORAGE_CONTAINER_BRONZE_RASTER", "bronze-raster"),
    Bucket.EXPORTS: ("AZURE_STORAGE_CONTAINER_EXPORTS", "exports"),
    Bucket.BACKUPS: ("AZURE_STORAGE_CONTAINER_BACKUPS", "georag-backups"),
}


@dataclass(frozen=True)
class AzureBlobConfig:
    """Resolved Azure Blob Storage connection settings.

    Build via :meth:`from_env` rather than constructing directly.

    Exactly one of ``connection_string`` or ``account_url`` must be set.
    ``account_url`` (managed-identity mode) takes precedence if both are
    somehow present, since it's the more secure path.
    """

    connection_string: str | None
    account_url: str | None
    container_names: dict[Bucket, str] = field(default_factory=dict)

    def container_name(self, bucket: Bucket) -> str:
        return self.container_names[bucket]

    @property
    def uses_managed_identity(self) -> bool:
        return self.account_url is not None

    @classmethod
    def from_env(cls) -> AzureBlobConfig:
        connection_string = os.environ.get("AZURE_STORAGE_CONNECTION_STRING") or None
        account_url = os.environ.get("AZURE_STORAGE_ACCOUNT_URL") or None
        if not connection_string and not account_url:
            raise ValueError(
                "one of AZURE_STORAGE_CONNECTION_STRING or "
                "AZURE_STORAGE_ACCOUNT_URL must be set for STORAGE_BACKEND=azure_blob"
            )
        # Managed identity takes precedence if both happen to be set.
        if account_url:
            connection_string = None

        container_names = {
            bucket: os.environ.get(canonical) or default
            for bucket, (canonical, default) in _CONTAINER_ENV.items()
        }

        return cls(
            connection_string=connection_string,
            account_url=account_url,
            container_names=container_names,
        )
