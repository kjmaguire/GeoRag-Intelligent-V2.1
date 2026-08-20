import pytest
from moto import mock_aws

from georag_object_storage.buckets import Bucket
from georag_object_storage.config import StorageConfig
from georag_object_storage.exceptions import ObjectNotFoundError
from georag_object_storage.sync_client import S3CompatibleStorage


@pytest.fixture
def config():
    return StorageConfig(
        endpoint_url="http://localhost:9000",
        access_key="testing",
        secret_key="testing",
        region="us-east-1",
        bucket_names={Bucket.BRONZE: "bronze-test", Bucket.EXPORTS: "exports-test"},
    )


@mock_aws
def test_ensure_bucket_creates_when_missing(config):
    store = S3CompatibleStorage(config)
    assert not store.bucket_exists(Bucket.BRONZE)
    store.ensure_bucket(Bucket.BRONZE)
    assert store.bucket_exists(Bucket.BRONZE)


@mock_aws
def test_put_and_get_bytes_roundtrip(config):
    store = S3CompatibleStorage(config)
    store.ensure_bucket(Bucket.BRONZE)
    store.put_bytes(Bucket.BRONZE, "pdfs/abc.pdf", b"hello world", content_type="application/pdf")

    assert store.exists(Bucket.BRONZE, "pdfs/abc.pdf")
    assert store.get_bytes(Bucket.BRONZE, "pdfs/abc.pdf") == b"hello world"


@mock_aws
def test_get_bytes_missing_key_raises_object_not_found(config):
    store = S3CompatibleStorage(config)
    store.ensure_bucket(Bucket.BRONZE)

    with pytest.raises(ObjectNotFoundError):
        store.get_bytes(Bucket.BRONZE, "does/not/exist.pdf")


@mock_aws
def test_put_bytes_with_metadata(config):
    store = S3CompatibleStorage(config)
    store.ensure_bucket(Bucket.BRONZE)
    store.put_bytes(
        Bucket.BRONZE,
        "reports/derived.pdf",
        b"payload",
        content_type="application/pdf",
        metadata={"derived_from_tiff_sha256": "abc123"},
    )

    info = store.head(Bucket.BRONZE, "reports/derived.pdf")
    assert info["metadata"] == {"derived_from_tiff_sha256": "abc123"}


# The key here used to be "x-georag-derived-from-tiff-sha256", copied from
# tiff_normalize. S3 accepts it, so this test passed for months while the
# same call 400ed against Azure Blob on every single TIFF upload. The
# rule is now enforced on this backend too, which is the point: a test
# that only exercises the permissive backend cannot catch the bug.


@mock_aws
def test_put_file_with_metadata(config, tmp_path):
    store = S3CompatibleStorage(config)
    store.ensure_bucket(Bucket.BRONZE)
    local = tmp_path / "scan.tif"
    local.write_bytes(b"tiff-bytes")

    store.put_file(
        Bucket.BRONZE,
        "tiff/scan.tif",
        str(local),
        content_type="image/tiff",
        metadata={"source": "field-upload"},
    )

    info = store.head(Bucket.BRONZE, "tiff/scan.tif")
    assert info["metadata"] == {"source": "field-upload"}
    assert info["content_type"] == "image/tiff"


@mock_aws
def test_get_file_downloads_to_disk(config, tmp_path):
    store = S3CompatibleStorage(config)
    store.ensure_bucket(Bucket.BRONZE)
    store.put_bytes(Bucket.BRONZE, "archive.zip", b"zip-bytes")

    dest = tmp_path / "downloaded.zip"
    store.get_file(Bucket.BRONZE, "archive.zip", str(dest))

    assert dest.read_bytes() == b"zip-bytes"


@mock_aws
def test_get_file_missing_key_raises_object_not_found(config, tmp_path):
    store = S3CompatibleStorage(config)
    store.ensure_bucket(Bucket.BRONZE)

    with pytest.raises(ObjectNotFoundError):
        store.get_file(Bucket.BRONZE, "missing.zip", str(tmp_path / "out.zip"))


@mock_aws
def test_list_keys_paginated(config):
    store = S3CompatibleStorage(config)
    store.ensure_bucket(Bucket.BRONZE)
    for i in range(5):
        store.put_bytes(Bucket.BRONZE, f"pdfs/{i}.pdf", b"x")

    keys = sorted(store.list_keys(Bucket.BRONZE, prefix="pdfs/"))
    assert keys == [f"pdfs/{i}.pdf" for i in range(5)]


@mock_aws
def test_delete_removes_object(config):
    store = S3CompatibleStorage(config)
    store.ensure_bucket(Bucket.BRONZE)
    store.put_bytes(Bucket.BRONZE, "pdfs/a.pdf", b"x")

    store.delete(Bucket.BRONZE, "pdfs/a.pdf")

    assert not store.exists(Bucket.BRONZE, "pdfs/a.pdf")


@mock_aws
def test_copy_between_buckets(config):
    store = S3CompatibleStorage(config)
    store.ensure_bucket(Bucket.BRONZE)
    store.ensure_bucket(Bucket.EXPORTS)
    store.put_bytes(Bucket.BRONZE, "pdfs/a.pdf", b"payload")

    store.copy(Bucket.BRONZE, "pdfs/a.pdf", Bucket.EXPORTS, "exports/a.pdf")

    assert store.get_bytes(Bucket.EXPORTS, "exports/a.pdf") == b"payload"


@mock_aws
def test_copy_with_metadata_and_content_type_override(config):
    store = S3CompatibleStorage(config)
    store.ensure_bucket(Bucket.BRONZE)
    store.ensure_bucket(Bucket.EXPORTS)
    store.put_bytes(Bucket.BRONZE, "pdfs/a.png", b"payload", content_type="application/octet-stream")

    store.copy(
        Bucket.BRONZE,
        "pdfs/a.png",
        Bucket.EXPORTS,
        "exports/a.png",
        metadata={"report_id": "42"},
        content_type="image/png",
    )

    info = store.head(Bucket.EXPORTS, "exports/a.png")
    assert info["content_type"] == "image/png"
    assert info["metadata"] == {"report_id": "42"}


@mock_aws
def test_head_returns_metadata(config):
    store = S3CompatibleStorage(config)
    store.ensure_bucket(Bucket.BRONZE)
    store.put_bytes(Bucket.BRONZE, "pdfs/a.pdf", b"payload", content_type="application/pdf")

    info = store.head(Bucket.BRONZE, "pdfs/a.pdf")

    assert info["size"] == len(b"payload")
    assert info["content_type"] == "application/pdf"


@mock_aws
def test_head_missing_key_raises_object_not_found(config):
    store = S3CompatibleStorage(config)
    store.ensure_bucket(Bucket.BRONZE)

    with pytest.raises(ObjectNotFoundError):
        store.head(Bucket.BRONZE, "missing.pdf")


@mock_aws
def test_presign_get_returns_url(config):
    store = S3CompatibleStorage(config)
    store.ensure_bucket(Bucket.BRONZE)
    store.put_bytes(Bucket.BRONZE, "pdfs/a.pdf", b"payload")

    url = store.presign_get(Bucket.BRONZE, "pdfs/a.pdf")

    assert "pdfs/a.pdf" in url


@mock_aws
def test_presign_put_returns_url(config):
    store = S3CompatibleStorage(config)
    store.ensure_bucket(Bucket.BRONZE)

    url = store.presign_put(Bucket.BRONZE, "pdfs/new.pdf")

    assert "pdfs/new.pdf" in url
