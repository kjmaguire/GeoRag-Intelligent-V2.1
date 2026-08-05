import pytest

from georag_object_storage.async_client import AsyncS3CompatibleStorage
from georag_object_storage.azure_async_client import AsyncAzureBlobStorage
from georag_object_storage.azure_sync_client import AzureBlobStorage
from georag_object_storage.config import StorageConfig
from georag_object_storage.factory import get_async_storage_client, get_storage_client
from georag_object_storage.sync_client import S3CompatibleStorage


def _config():
    return StorageConfig(
        endpoint_url="http://localhost:9000",
        access_key="k",
        secret_key="s",
        region="us-east-1",
        bucket_names={},
    )


# Well-formed but fake — base64-valid AccountKey so BlobServiceClient's
# internal SharedKeyCredential construction doesn't raise on parsing; no
# network call happens at construction time, so this never touches a real
# Azure account.
_FAKE_AZURE_CONN_STR = (
    "DefaultEndpointsProtocol=https;"
    "AccountName=fakeaccount;"
    "AccountKey=ZmFrZWtleWZha2VrZXlmYWtla2V5ZmFrZWtleWZha2VrZXlmYWtla2V5ZmFrZWtleT09;"
    "EndpointSuffix=core.windows.net"
)


def test_get_storage_client_defaults_to_s3_compatible(monkeypatch):
    monkeypatch.delenv("STORAGE_BACKEND", raising=False)

    client = get_storage_client(_config())

    assert isinstance(client, S3CompatibleStorage)


def test_get_async_storage_client_defaults_to_s3_compatible(monkeypatch):
    monkeypatch.delenv("STORAGE_BACKEND", raising=False)

    client = get_async_storage_client(_config())

    assert isinstance(client, AsyncS3CompatibleStorage)


def test_get_storage_client_reads_config_from_env_when_not_passed(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "k")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "s")

    client = get_storage_client()

    assert isinstance(client, S3CompatibleStorage)


def test_unknown_backend_raises_not_implemented(monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "totally_unknown_backend")

    with pytest.raises(NotImplementedError):
        get_storage_client(_config())

    with pytest.raises(NotImplementedError):
        get_async_storage_client(_config())


def test_get_storage_client_azure_blob(monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "azure_blob")
    monkeypatch.setenv("AZURE_STORAGE_CONNECTION_STRING", _FAKE_AZURE_CONN_STR)

    # The `config` param is S3-shaped and ignored for azure_blob — verify
    # passing one doesn't blow up, since existing callers pass it
    # unconditionally.
    client = get_storage_client(_config())

    assert isinstance(client, AzureBlobStorage)


def test_get_async_storage_client_azure_blob(monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "azure_blob")
    monkeypatch.setenv("AZURE_STORAGE_CONNECTION_STRING", _FAKE_AZURE_CONN_STR)

    client = get_async_storage_client(_config())

    assert isinstance(client, AsyncAzureBlobStorage)


def test_azure_blob_without_credentials_raises_value_error(monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "azure_blob")
    monkeypatch.delenv("AZURE_STORAGE_CONNECTION_STRING", raising=False)
    monkeypatch.delenv("AZURE_STORAGE_ACCOUNT_URL", raising=False)

    with pytest.raises(ValueError, match="AZURE_STORAGE"):
        get_storage_client()
