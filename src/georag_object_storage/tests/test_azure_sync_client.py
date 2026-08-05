"""Unit tests for AzureBlobStorage against a mocked SDK boundary.

No Azurite/live Azure account available in this environment, so these mock
at the ``azure.storage.blob`` client boundary (patching
``BlobServiceClient.from_connection_string``) rather than exercising a real
network call — same spirit as moto for the S3 backend, but Azure has no
equivalent free-and-simple emulator library, so plain unittest.mock stands
in. This verifies the translation logic (bucket->container mapping,
exception normalization, idempotent delete) is correct; it does NOT prove
the real Azure wire protocol works — that needs a real account or Azurite.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError

from georag_object_storage.azure_config import AzureBlobConfig
from georag_object_storage.azure_sync_client import AzureBlobStorage
from georag_object_storage.buckets import Bucket
from georag_object_storage.exceptions import ObjectNotFoundError, ObjectStorageError


@pytest.fixture
def config():
    return AzureBlobConfig(
        connection_string="DefaultEndpointsProtocol=https;AccountName=x;AccountKey=eA==;EndpointSuffix=core.windows.net",
        account_url=None,
        container_names={Bucket.BRONZE: "bronze", Bucket.EXPORTS: "exports"},
    )


@pytest.fixture
def mock_service():
    with patch("georag_object_storage.azure_sync_client.BlobServiceClient") as mock_cls:
        service = MagicMock()
        mock_cls.from_connection_string.return_value = service
        yield service


def _client(config, mock_service):
    return AzureBlobStorage(config)


def test_put_bytes_calls_upload_blob_with_overwrite(config, mock_service):
    client = _client(config, mock_service)
    blob_client = MagicMock()
    mock_service.get_container_client.return_value.get_blob_client.return_value = blob_client

    client.put_bytes(Bucket.BRONZE, "reports/foo.pdf", b"data", content_type="application/pdf")

    mock_service.get_container_client.assert_called_with("bronze")
    blob_client.upload_blob.assert_called_once()
    _, kwargs = blob_client.upload_blob.call_args
    assert kwargs["overwrite"] is True
    assert kwargs["content_settings"].content_type == "application/pdf"


def test_get_bytes_returns_body(config, mock_service):
    client = _client(config, mock_service)
    blob_client = MagicMock()
    blob_client.download_blob.return_value.readall.return_value = b"hello"
    mock_service.get_container_client.return_value.get_blob_client.return_value = blob_client

    result = client.get_bytes(Bucket.BRONZE, "k")

    assert result == b"hello"


def test_get_bytes_missing_raises_object_not_found(config, mock_service):
    client = _client(config, mock_service)
    blob_client = MagicMock()
    blob_client.download_blob.side_effect = ResourceNotFoundError("nope")
    mock_service.get_container_client.return_value.get_blob_client.return_value = blob_client

    with pytest.raises(ObjectNotFoundError):
        client.get_bytes(Bucket.BRONZE, "missing-key")


def test_delete_is_idempotent_on_missing_key(config, mock_service):
    client = _client(config, mock_service)
    blob_client = MagicMock()
    blob_client.delete_blob.side_effect = ResourceNotFoundError("nope")
    mock_service.get_container_client.return_value.get_blob_client.return_value = blob_client

    # Must NOT raise -- matches S3 delete_object's idempotent contract.
    client.delete(Bucket.BRONZE, "already-gone")


def test_exists_true_and_false(config, mock_service):
    client = _client(config, mock_service)
    blob_client = MagicMock()
    mock_service.get_container_client.return_value.get_blob_client.return_value = blob_client

    blob_client.exists.return_value = True
    assert client.exists(Bucket.BRONZE, "k") is True

    blob_client.exists.return_value = False
    assert client.exists(Bucket.BRONZE, "k") is False


def test_ensure_bucket_ignores_already_exists(config, mock_service):
    client = _client(config, mock_service)
    container_client = MagicMock()
    container_client.create_container.side_effect = ResourceExistsError("already there")
    mock_service.get_container_client.return_value = container_client

    # Must NOT raise.
    client.ensure_bucket(Bucket.BRONZE)


def test_list_keys_yields_blob_names(config, mock_service):
    client = _client(config, mock_service)
    container_client = MagicMock()
    fake_blob_1 = MagicMock(name="b1")
    fake_blob_1.name = "reports/a.pdf"
    fake_blob_2 = MagicMock(name="b2")
    fake_blob_2.name = "reports/b.pdf"
    container_client.list_blobs.return_value = [fake_blob_1, fake_blob_2]
    mock_service.get_container_client.return_value = container_client

    keys = list(client.list_keys(Bucket.BRONZE, prefix="reports/"))

    assert keys == ["reports/a.pdf", "reports/b.pdf"]
    container_client.list_blobs.assert_called_with(name_starts_with="reports/")


def test_head_maps_blob_properties(config, mock_service):
    client = _client(config, mock_service)
    blob_client = MagicMock()
    props = MagicMock()
    props.size = 123
    props.etag = '"abc123"'
    props.metadata = {"foo": "bar"}
    props.content_settings.content_type = "application/pdf"
    blob_client.get_blob_properties.return_value = props
    mock_service.get_container_client.return_value.get_blob_client.return_value = blob_client

    result = client.head(Bucket.BRONZE, "k")

    assert result["size"] == 123
    assert result["etag"] == "abc123"
    assert result["content_type"] == "application/pdf"
    assert result["metadata"] == {"foo": "bar"}


def test_generic_sdk_error_normalized_to_object_storage_error(config, mock_service):
    client = _client(config, mock_service)
    blob_client = MagicMock()
    blob_client.upload_blob.side_effect = RuntimeError("network blew up")
    mock_service.get_container_client.return_value.get_blob_client.return_value = blob_client

    with pytest.raises(ObjectStorageError):
        client.put_bytes(Bucket.BRONZE, "k", b"data")


def test_presign_raises_not_implemented_under_managed_identity():
    config = AzureBlobConfig(
        connection_string=None,
        account_url="https://fake.blob.core.windows.net",
        container_names={Bucket.BRONZE: "bronze"},
    )
    with patch("georag_object_storage.azure_sync_client.BlobServiceClient"), \
         patch("azure.identity.DefaultAzureCredential"):
        client = AzureBlobStorage(config)

    with pytest.raises(NotImplementedError, match="managed-identity"):
        client.presign_get(Bucket.BRONZE, "k")
