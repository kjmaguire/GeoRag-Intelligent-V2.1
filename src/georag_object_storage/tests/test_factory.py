import pytest

from georag_object_storage.async_client import AsyncS3CompatibleStorage
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
    monkeypatch.setenv("STORAGE_BACKEND", "azure_blob")

    with pytest.raises(NotImplementedError):
        get_storage_client(_config())

    with pytest.raises(NotImplementedError):
        get_async_storage_client(_config())
