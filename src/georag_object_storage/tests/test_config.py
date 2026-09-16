import pytest

from georag_object_storage.buckets import Bucket
from georag_object_storage.config import StorageConfig


def test_from_env_canonical(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "canonical-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "canonical-secret")
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://seaweedfs:8333")

    config = StorageConfig.from_env()

    assert config.access_key == "canonical-key"
    assert config.secret_key == "canonical-secret"
    assert config.endpoint_url == "http://seaweedfs:8333"
    assert config.region == "us-east-1"
    assert config.bucket_name(Bucket.BRONZE) == "bronze"


def test_from_env_legacy_minio_fallback(monkeypatch):
    monkeypatch.setenv("MINIO_ROOT_USER", "legacy-key")
    monkeypatch.setenv("MINIO_ROOT_PASSWORD", "legacy-secret")
    monkeypatch.setenv("MINIO_ENDPOINT", "http://minio:8333")

    config = StorageConfig.from_env()

    assert config.access_key == "legacy-key"
    assert config.secret_key == "legacy-secret"
    assert config.endpoint_url == "http://minio:8333"


def test_from_env_legacy_seaweedfs_s3_fallback(monkeypatch):
    """backup_seaweedfs.py's own env-var naming (found during PR5b migration) —
    SEAWEEDFS_S3_ACCESS_KEY/SECRET_KEY/REGION, distinct from the
    SEAWEEDFS_ACCESS_KEY/SECRET_KEY names other call sites use."""
    monkeypatch.setenv("SEAWEEDFS_S3_ACCESS_KEY", "sw-key")
    monkeypatch.setenv("SEAWEEDFS_S3_SECRET_KEY", "sw-secret")
    monkeypatch.setenv("SEAWEEDFS_S3_REGION", "us-west-2")

    config = StorageConfig.from_env()

    assert config.access_key == "sw-key"
    assert config.secret_key == "sw-secret"
    assert config.region == "us-west-2"


def test_from_env_canonical_takes_priority_over_legacy(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "canonical-key")
    monkeypatch.setenv("MINIO_ROOT_USER", "legacy-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "canonical-secret")
    monkeypatch.setenv("MINIO_ROOT_PASSWORD", "legacy-secret")

    config = StorageConfig.from_env()

    assert config.access_key == "canonical-key"
    assert config.secret_key == "canonical-secret"


def test_from_env_with_no_credentials_defers_to_the_credential_chain(monkeypatch):
    """ADR-0022: absent keys mean "use the task/instance role", not an error.

    This used to raise. On ECS the task role supplies credentials through the
    container credential provider and boto3 resolves them itself, so
    requiring explicit keys made from_env() fail before boto3 ever got the
    chance — with a message about a variable the deployment correctly did
    not set.
    """
    for var in (
        "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
        "S3_ACCESS_KEY", "S3_SECRET_KEY",
        "MINIO_ROOT_USER", "MINIO_ROOT_PASSWORD",
        "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY",
        "SEAWEEDFS_ACCESS_KEY", "SEAWEEDFS_SECRET_KEY",
        "SEAWEEDFS_S3_ACCESS_KEY", "SEAWEEDFS_S3_SECRET_KEY",
    ):
        monkeypatch.delenv(var, raising=False)

    config = StorageConfig.from_env()

    assert config.access_key is None
    assert config.secret_key is None


@pytest.mark.parametrize(
    "present,absent",
    [
        ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"),
        ("AWS_SECRET_ACCESS_KEY", "AWS_ACCESS_KEY_ID"),
    ],
)
def test_from_env_half_a_credential_pair_still_raises(monkeypatch, present, absent):
    """One without the other is always a mistake, and a silent one.

    boto3 would fall through to the credential chain and either succeed with
    different credentials than intended or fail with NoCredentialsError
    naming nothing.
    """
    for var in (
        "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
        "S3_ACCESS_KEY", "S3_SECRET_KEY",
        "MINIO_ROOT_USER", "MINIO_ROOT_PASSWORD",
        "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY",
        "SEAWEEDFS_ACCESS_KEY", "SEAWEEDFS_SECRET_KEY",
        "SEAWEEDFS_S3_ACCESS_KEY", "SEAWEEDFS_S3_SECRET_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv(present, "value")

    with pytest.raises(ValueError, match="together or not at all"):
        StorageConfig.from_env()


def test_from_env_endpoint_defaults_to_none_for_real_s3(monkeypatch):
    """ADR-0022: no default endpoint.

    It used to default to `http://minio:8333`, which is right for compose
    and wrong for AWS — boto3 given an explicit endpoint_url talks to that
    host and nothing else, so an unset variable on an ECS task pointed every
    S3 call at a service name that does not resolve there.
    """
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "k")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "s")
    for var in (
        "AWS_ENDPOINT_URL", "S3_ENDPOINT_URL", "S3_ENDPOINT",
        "MINIO_ENDPOINT", "SEAWEEDFS_S3_ENDPOINT",
    ):
        monkeypatch.delenv(var, raising=False)

    assert StorageConfig.from_env().endpoint_url is None


def test_client_kwargs_omit_unset_endpoint_and_credentials():
    """Omitted, not None. Explicit None credentials can short-circuit
    boto3's resolution, which would leave an ECS task role unused and fail
    with NoCredentialsError while a perfectly good role sat there."""
    from georag_object_storage.sync_client import _client_kwargs

    kwargs = _client_kwargs(
        StorageConfig(
            endpoint_url=None,
            access_key=None,
            secret_key=None,
            region="ca-central-1",
            bucket_names={},
        )
    )

    assert "endpoint_url" not in kwargs
    assert "aws_access_key_id" not in kwargs
    assert "aws_secret_access_key" not in kwargs
    assert kwargs["region_name"] == "ca-central-1"


def test_client_kwargs_include_them_when_set():
    from georag_object_storage.sync_client import _client_kwargs

    kwargs = _client_kwargs(
        StorageConfig(
            endpoint_url="http://minio:8333",
            access_key="k",
            secret_key="s",
            region="us-east-1",
            bucket_names={},
        )
    )

    assert kwargs["endpoint_url"] == "http://minio:8333"
    assert kwargs["aws_access_key_id"] == "k"
    assert kwargs["aws_secret_access_key"] == "s"


def test_bucket_env_overrides(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "k")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "s")
    monkeypatch.setenv("AWS_BUCKET_BRONZE", "custom-bronze")

    config = StorageConfig.from_env()

    assert config.bucket_name(Bucket.BRONZE) == "custom-bronze"


def test_bucket_legacy_fallback(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "k")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "s")
    monkeypatch.setenv("MINIO_BUCKET_BRONZE", "legacy-bronze")

    config = StorageConfig.from_env()

    assert config.bucket_name(Bucket.BRONZE) == "legacy-bronze"


def test_bucket_default_when_unset(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "k")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "s")

    config = StorageConfig.from_env()

    assert config.bucket_name(Bucket.EXPORTS) == "exports"
    assert config.bucket_name(Bucket.BACKUPS) == "georag-backups"
