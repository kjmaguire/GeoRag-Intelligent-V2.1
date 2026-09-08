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
    monkeypatch.setenv("STORAGE_BACKEND", "totally_unknown_backend")

    with pytest.raises(NotImplementedError):
        get_storage_client(_config())

    with pytest.raises(NotImplementedError):
        get_async_storage_client(_config())


@pytest.mark.parametrize(
    "getter", [get_storage_client, get_async_storage_client]
)
def test_the_retired_azure_backend_names_its_replacement(monkeypatch, getter):
    """ADR-0022. `azure_blob` was a real value until 2026-09-08, so this is
    the one wrong setting a deployment is actually likely to have — and a
    generic "not implemented (known: ('s3_compatible',))" would leave the
    reader to work out which of the two they used to be on.

    The message has to say what to do, because the fix is not just
    switching the value: the AZURE_STORAGE_* variables have to go, and
    AWS_ENDPOINT_URL must NOT be set in their place — an explicit endpoint
    pins every call to one host rather than resolving the regional S3
    endpoint, which is the compose default and wrong on AWS.
    """
    monkeypatch.setenv("STORAGE_BACKEND", "azure_blob")

    with pytest.raises(NotImplementedError) as exc:
        getter(_config())

    message = str(exc.value)
    assert "2026-09-08" in message
    assert "s3_compatible" in message
    assert "AWS_ENDPOINT_URL" in message
