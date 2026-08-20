"""azure-storage-blob.aio-backed asynchronous Azure Blob object storage.

Same method surface as :mod:`azure_sync_client`. Unlike the aioboto3 async
S3 client (which opens a fresh client per call — aioboto3 sessions aren't
safe to share across concurrent tasks), the Azure async SDK's clients ARE
safe to hold open and reuse for the process lifetime; that's the documented
pattern, so one ``BlobServiceClient`` is built once in ``__init__`` rather
than per-call.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.storage.blob import ContentSettings
from azure.storage.blob.aio import BlobServiceClient

from georag_object_storage.azure_config import AzureBlobConfig
from georag_object_storage.buckets import Bucket
from georag_object_storage.exceptions import ObjectNotFoundError, ObjectStorageError
from georag_object_storage.metadata import validate_metadata


def _read_file(file_path: str) -> bytes:
    """Blocking file read, run off the event loop via ``asyncio.to_thread``."""
    with open(file_path, "rb") as fh:
        return fh.read()


def _write_file(file_path: str, data: bytes) -> None:
    """Blocking file write, run off the event loop via ``asyncio.to_thread``."""
    with open(file_path, "wb") as fh:
        fh.write(data)


def build_async_blob_service_client(config: AzureBlobConfig) -> BlobServiceClient:
    """Construct an async ``BlobServiceClient`` from an ``AzureBlobConfig``."""
    if config.connection_string:
        return BlobServiceClient.from_connection_string(config.connection_string)
    from azure.identity.aio import DefaultAzureCredential

    return BlobServiceClient(account_url=config.account_url, credential=DefaultAzureCredential())


class AsyncAzureBlobStorage:
    """Async ``AsyncObjectStorage`` implementation backed by azure-storage-blob.aio."""

    def __init__(self, config: AzureBlobConfig) -> None:
        self._config = config
        self._service = build_async_blob_service_client(config)

    def _container_name(self, bucket: Bucket) -> str:
        return self._config.container_name(bucket)

    def _container_client(self, bucket: Bucket):
        return self._service.get_container_client(self._container_name(bucket))

    def _blob_client(self, bucket: Bucket, key: str):
        return self._container_client(bucket).get_blob_client(key)

    async def bucket_exists(self, bucket: Bucket) -> bool:
        try:
            return await self._container_client(bucket).exists()
        except Exception as exc:
            raise ObjectStorageError(str(exc)) from exc

    async def ensure_bucket(self, bucket: Bucket) -> None:
        try:
            await self._container_client(bucket).create_container()
        except ResourceExistsError:
            pass
        except Exception as exc:
            raise ObjectStorageError(str(exc)) from exc

    async def put_bytes(
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
            await self._blob_client(bucket, key).upload_blob(
                data,
                overwrite=True,
                content_settings=ContentSettings(content_type=content_type),
                metadata=metadata,
            )
        except Exception as exc:
            raise ObjectStorageError(str(exc)) from exc

    async def put_file(
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
            data = await asyncio.to_thread(_read_file, file_path)
            await self._blob_client(bucket, key).upload_blob(
                data,
                overwrite=True,
                content_settings=ContentSettings(content_type=content_type),
                metadata=metadata,
            )
        except Exception as exc:
            raise ObjectStorageError(str(exc)) from exc

    async def get_bytes(self, bucket: Bucket, key: str) -> bytes:
        name = self._container_name(bucket)
        try:
            stream = await self._blob_client(bucket, key).download_blob()
            return await stream.readall()
        except ResourceNotFoundError as exc:
            raise ObjectNotFoundError(name, key) from exc
        except Exception as exc:
            raise ObjectStorageError(str(exc)) from exc

    async def get_file(self, bucket: Bucket, key: str, file_path: str) -> None:
        name = self._container_name(bucket)
        try:
            stream = await self._blob_client(bucket, key).download_blob()
            data = await stream.readall()
            await asyncio.to_thread(_write_file, file_path, data)
        except ResourceNotFoundError as exc:
            raise ObjectNotFoundError(name, key) from exc
        except Exception as exc:
            raise ObjectStorageError(str(exc)) from exc

    async def exists(self, bucket: Bucket, key: str) -> bool:
        try:
            return await self._blob_client(bucket, key).exists()
        except Exception as exc:
            raise ObjectStorageError(str(exc)) from exc

    async def head(self, bucket: Bucket, key: str) -> dict:
        name = self._container_name(bucket)
        try:
            props = await self._blob_client(bucket, key).get_blob_properties()
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

    async def list_keys(self, bucket: Bucket, prefix: str = "") -> AsyncIterator[str]:
        try:
            async for blob in self._container_client(bucket).list_blobs(name_starts_with=prefix):
                yield blob.name
        except Exception as exc:
            raise ObjectStorageError(str(exc)) from exc

    async def delete(self, bucket: Bucket, key: str) -> None:
        try:
            await self._blob_client(bucket, key).delete_blob()
        except ResourceNotFoundError:
            pass  # idempotent delete, matches the S3 backend's contract
        except Exception as exc:
            raise ObjectStorageError(str(exc)) from exc

    async def copy(
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
            await dst_client.start_copy_from_url(src_client.url)
            props = await dst_client.get_blob_properties()
            _POLL_MAX = 50
            _polls = 0
            while props.copy and props.copy.status == "pending" and _polls < _POLL_MAX:
                await asyncio.sleep(0.1)
                props = await dst_client.get_blob_properties()
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
                await dst_client.set_blob_metadata(new_metadata)
                await dst_client.set_http_headers(content_settings=new_content_settings)
        except ResourceNotFoundError as exc:
            raise ObjectNotFoundError(self._container_name(src_bucket), src_key) from exc
        except Exception as exc:
            if isinstance(exc, ObjectStorageError):
                raise
            raise ObjectStorageError(str(exc)) from exc

    async def presign_get(self, bucket: Bucket, key: str, expires_in: int = 3600) -> str:
        return self._generate_sas_url(bucket, key, expires_in, read=True)

    async def presign_put(
        self, bucket: Bucket, key: str, expires_in: int = 3600, content_type: str = "application/octet-stream"
    ) -> str:
        return self._generate_sas_url(bucket, key, expires_in, write=True)

    def _generate_sas_url(
        self, bucket: Bucket, key: str, expires_in: int, *, read: bool = False, write: bool = False
    ) -> str:
        # Sync SAS-token math (no network call), same account-key
        # constraint as the sync backend — see azure_sync_client's
        # docstring for the managed-identity caveat.
        if self._config.uses_managed_identity:
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

    async def aclose(self) -> None:
        """Close the underlying async client. Call at app shutdown."""
        await self._service.close()
