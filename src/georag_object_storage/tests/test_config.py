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


def test_from_env_missing_credentials_raises(monkeypatch):
    with pytest.raises(ValueError):
        StorageConfig.from_env()


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
