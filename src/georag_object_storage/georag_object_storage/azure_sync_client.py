"""azure-storage-blob-backed synchronous Azure Blob object storage.

Same method surface and error-handling shape as :mod:`sync_client`
(S3-compatible backend) — implements the :class:`~georag_object_storage.
protocols.ObjectStorage` protocol structurally, no shared base class needed.
"""

from __future__ import annotations

from collections.abc import Iterator

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.storage.blob import BlobServiceClient, ContentSettings

from georag_object_storage.azure_config import AzureBlobConfig
from georag_object_storage.buckets import Bucket
from georag_object_storage.exceptions import ObjectNotFoundError, ObjectStorageError
from georag_object_storage.metadata import validate_metadata


def build_blob_service_client(config: AzureBlobConfig) -> BlobServiceClient:
    """Construct a ``BlobServiceClient`` from an ``AzureBlobConfig``.

    Public — mirrors :func:`sync_client.build_boto3_client` — for callers
    that need the raw SDK client rather than the higher-level
    ``ObjectStorage`` interface.
    """
    if config.connection_string:
        return BlobServiceClient.from_connection_string(config.connection_string)
    # Managed-identity path. Imported lazily so azure-identity is only a
    # hard dependency when this branch actually runs (connection-string
    # deployments and CI/Azurite testing don't need it).
    from azure.identity import DefaultAzureCredential

    return BlobServiceClient(account_url=config.account_url, credential=DefaultAzureCredential())


class AzureBlobStorage:
    """Synchronous ``ObjectStorage`` implementation backed by azure-storage-blob.

    "Bucket" in the protocol maps to Azure "container" throughout.
    """

    def __init__(self, config: AzureBlobConfig) -> None:
        self._config = config
        self._service = build_blob_service_client(config)

    def _container_name(self, bucket: Bucket) -> str:
        return self._config.container_name(bucket)

    def _container_client(self, bucket: Bucket):
        return self._service.get_container_client(self._container_name(bucket))

    def _blob_client(self, bucket: Bucket, key: str):
        return self._container_client(bucket).get_blob_client(key)

    def bucket_exists(self, bucket: Bucket) -> bool:
        try:
            return self._container_client(bucket).exists()
        except Exception as exc:
            raise ObjectStorageError(str(exc)) from exc

    def ensure_bucket(self, bucket: Bucket) -> None:
        try:
            self._container_client(bucket).create_container()
        except ResourceExistsError:
            pass
        except Exception as exc:
            raise ObjectStorageError(str(exc)) from exc

    def put_bytes(
        self,
        bucket: Bucket,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> None:
        # Checked before the try: the generic handler below re-wraps
        # everything as ObjectStorageError, which would flatten the
        # InvalidMetadataKeyError and lose the offending key.
        metadata = validate_metadata(metadata)
        try:
            self._blob_client(bucket, key).upload_blob(
                data,
                overwrite=True,
                content_settings=ContentSettings(content_type=content_type),
                metadata=metadata,
            )
        except Exception as exc:
            raise ObjectStorageError(str(exc)) from exc

    def put_file(
        self,
        bucket: Bucket,
        key: str,
        file_path: str,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> None:
        # Checked before the try: the generic handler below re-wraps
        # everything as ObjectStorageError, which would flatten the
        # InvalidMetadataKeyError and lose the offending key.
        metadata = validate_metadata(metadata)
        try:
            with open(file_path, "rb") as fh:
                self._blob_client(bucket, key).upload_blob(
                    fh,
                    overwrite=True,
                    content_settings=ContentSettings(content_type=content_type),
                    metadata=metadata,
                )
        except Exception as exc:
            raise ObjectStorageError(str(exc)) from exc

    def get_bytes(self, bucket: Bucket, key: str) -> bytes:
        name = self._container_name(bucket)
        try:
            return self._blob_client(bucket, key).download_blob().readall()
        except ResourceNotFoundError as exc:
            raise ObjectNotFoundError(name, key) from exc
        except Exception as exc:
            raise ObjectStorageError(str(exc)) from exc

    def get_file(self, bucket: Bucket, key: str, file_path: str) -> None:
        name = self._container_name(bucket)
        try:
            with open(file_path, "wb") as fh:
                self._blob_client(bucket, key).download_blob().readinto(fh)
        except ResourceNotFoundError as exc:
            raise ObjectNotFoundError(name, key) from exc
        except Exception as exc:
            raise ObjectStorageError(str(exc)) from exc

    def exists(self, bucket: Bucket, key: str) -> bool:
        try:
            return self._blob_client(bucket, key).exists()
        except Exception as exc:
            raise ObjectStorageError(str(exc)) from exc

    def head(self, bucket: Bucket, key: str) -> dict:
        name = self._container_name(bucket)
        try:
            props = self._blob_client(bucket, key).get_blob_properties()
        except ResourceNotFoundError as exc:
            raise ObjectNotFoundError(name, key) from exc
        except Exception as exc:
            raise ObjectStorageError(str(exc)) from exc
        return {
            "size": props.size,
            "etag": (props.etag or "").strip('"'),
            "last_modified": props.last_modified,
            "content_type": props.content_settings.content_type if props.content_settings else None,
            "metadata": props.metadata or {},
        }

    def list_keys(self, bucket: Bucket, prefix: str = "") -> Iterator[str]:
        try:
            for blob in self._container_client(bucket).list_blobs(name_starts_with=prefix):
                yield blob.name
        except Exception as exc:
            raise ObjectStorageError(str(exc)) from exc

    def delete(self, bucket: Bucket, key: str) -> None:
        try:
            self._blob_client(bucket, key).delete_blob()
        except ResourceNotFoundError:
            # S3's delete_object is idempotent (no error on missing key) —
            # match that contract rather than surfacing a not-found error
            # on delete, which no existing caller expects.
            pass
        except Exception as exc:
            raise ObjectStorageError(str(exc)) from exc

    def copy(
        self,
        src_bucket: Bucket,
        src_key: str,
        dst_bucket: Bucket,
        dst_key: str,
        metadata: dict[str, str] | None = None,
        content_type: str | None = None,
    ) -> None:
        # Before the try, for the same reason as put_bytes above.
        metadata = validate_metadata(metadata)
        src_client = self._blob_client(src_bucket, src_key)
        dst_client = self._blob_client(dst_bucket, dst_key)
        try:
            # Azure copy is server-side async even for same-account copies;
            # poll until it lands so this call's synchronous-completion
            # contract matches S3's copy_object (which blocks until done).
            dst_client.start_copy_from_url(src_client.url)
            props = dst_client.get_blob_properties()
            _POLL_MAX = 50  # ~5s at 100ms/poll — same-account copies are near-instant
            _polls = 0
            while props.copy and props.copy.status == "pending" and _polls < _POLL_MAX:
                import time as _time

                _time.sleep(0.1)
                props = dst_client.get_blob_properties()
                _polls += 1
            if props.copy and props.copy.status not in (None, "success"):
                raise ObjectStorageError(
                    f"copy did not complete: status={props.copy.status!r} "
                    f"src={src_bucket}/{src_key} dst={dst_bucket}/{dst_key}"
                )
            if metadata is not None or content_type is not None:
                new_metadata = (
                    metadata if metadata is not None else (props.metadata or {})
                )
                new_content_settings = ContentSettings(
                    content_type=content_type or (props.content_settings.content_type if props.content_settings else None)
                )
                dst_client.set_blob_metadata(new_metadata)
                dst_client.set_http_headers(content_settings=new_content_settings)
        except ResourceNotFoundError as exc:
            raise ObjectNotFoundError(self._container_name(src_bucket), src_key) from exc
        except Exception as exc:
            if isinstance(exc, ObjectStorageError):
                raise
            raise ObjectStorageError(str(exc)) from exc

    def presign_get(self, bucket: Bucket, key: str, expires_in: int = 3600) -> str:
        return self._generate_sas_url(bucket, key, expires_in, read=True)

    def presign_put(
        self, bucket: Bucket, key: str, expires_in: int = 3600, content_type: str = "application/octet-stream"
    ) -> str:
        return self._generate_sas_url(bucket, key, expires_in, write=True)

    def _generate_sas_url(
        self, bucket: Bucket, key: str, expires_in: int, *, read: bool = False, write: bool = False
    ) -> str:
        if self._config.uses_managed_identity:
            # Account-key SAS isn't available under managed identity (no
            # key in the environment at all, by design). The
            # managed-identity-compatible equivalent is a user-delegation
            # SAS (BlobServiceClient.get_user_delegation_key() +
            # generate_blob_sas(..., user_delegation_key=...)) — a real,
            # separate implementation, not wired up here. Deployments that
            # need presigned URLs under managed identity need this added;
            # deployments that don't (most of GeoRAG's actual usage, per
            # the ObjectStorage protocol docstring) are unaffected.
            raise NotImplementedError(
                "presign_get/presign_put need an account-key SAS, which isn't "
                "available under AZURE_STORAGE_ACCOUNT_URL (managed-identity) "
                "auth. Use AZURE_STORAGE_CONNECTION_STRING instead, or add "
                "user-delegation-key SAS support if managed identity is required."
            )
        import datetime

        from azure.storage.blob import BlobSasPermissions, generate_blob_sas

        account_name = self._service.account_name
        account_key = self._service.credential.account_key
        sas = generate_blob_sas(
            account_name=account_name,
            container_name=self._container_name(bucket),
            blob_name=key,
            account_key=account_key,
            permission=BlobSasPermissions(read=read, write=write, create=write),
            expiry=datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=expires_in),
        )
        blob_url = self._blob_client(bucket, key).url
        return f"{blob_url}?{sas}"
