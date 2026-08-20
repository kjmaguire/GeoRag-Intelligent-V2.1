import urllib.request

import aioboto3
import pytest
from moto.server import ThreadedMotoServer

from georag_object_storage.async_client import AsyncS3CompatibleStorage, async_client_kwargs
from georag_object_storage.buckets import Bucket
from georag_object_storage.config import StorageConfig
from georag_object_storage.exceptions import ObjectNotFoundError

# aioboto3/aiobotocore's async client does not go through the same HTTP-layer
# patching moto's `mock_aws` decorator uses for sync boto3 — several
# aiobotocore versions hit real, open moto/aiobotocore compatibility bugs
# against that patched-internals approach (getmoto/moto#8694: moto returns a
# sync AWSResponse instead of an AioAWSResponse, breaking `await
# http_response.content`; older aiobotocore avoids that but then moto's
# MockRawResponse is missing attributes the older client expects). A real
# HTTP server sidesteps all of this: from aiobotocore's perspective it's just
# a normal HTTP request to a normal server, no internals patched at all.


@pytest.fixture(scope="module")
def moto_server_url():
    server = ThreadedMotoServer(ip_address="127.0.0.1", port=0, verbose=False)
    server.start()
    host, port = server.get_host_and_port()
    yield f"http://{host}:{port}"
    server.stop()


@pytest.fixture(autouse=True)
def _reset_moto_server(moto_server_url):
    """Clear all mocked AWS state between tests so buckets/objects don't leak across cases."""
    yield
    urllib.request.urlopen(urllib.request.Request(f"{moto_server_url}/moto-api/reset", method="POST"))


@pytest.fixture
def config(moto_server_url):
    return StorageConfig(
        endpoint_url=moto_server_url,
        access_key="testing",
        secret_key="testing",
        region="us-east-1",
        bucket_names={Bucket.BRONZE: "bronze-test", Bucket.EXPORTS: "exports-test"},
    )


async def test_ensure_bucket_creates_when_missing(config):
    store = AsyncS3CompatibleStorage(config)
    assert not await store.bucket_exists(Bucket.BRONZE)
    await store.ensure_bucket(Bucket.BRONZE)
    assert await store.bucket_exists(Bucket.BRONZE)


async def test_put_and_get_bytes_roundtrip(config):
    store = AsyncS3CompatibleStorage(config)
    await store.ensure_bucket(Bucket.BRONZE)
    await store.put_bytes(Bucket.BRONZE, "pdfs/abc.pdf", b"hello world", content_type="application/pdf")

    assert await store.exists(Bucket.BRONZE, "pdfs/abc.pdf")
    assert await store.get_bytes(Bucket.BRONZE, "pdfs/abc.pdf") == b"hello world"


async def test_get_bytes_missing_key_raises_object_not_found(config):
    store = AsyncS3CompatibleStorage(config)
    await store.ensure_bucket(Bucket.BRONZE)

    with pytest.raises(ObjectNotFoundError):
        await store.get_bytes(Bucket.BRONZE, "missing.pdf")


async def test_put_bytes_with_metadata(config):
    store = AsyncS3CompatibleStorage(config)
    await store.ensure_bucket(Bucket.BRONZE)
    await store.put_bytes(
        Bucket.BRONZE,
        "reports/derived.pdf",
        b"payload",
        content_type="application/pdf",
        metadata={"derived_from_tiff_sha256": "abc123"},
    )

    info = await store.head(Bucket.BRONZE, "reports/derived.pdf")
    assert info["metadata"] == {"derived_from_tiff_sha256": "abc123"}


# The key here used to be "x-georag-derived-from-tiff-sha256", copied from
# tiff_normalize. S3 accepts it, so this test passed for months while the
# same call 400ed against Azure Blob on every single TIFF upload. The
# rule is now enforced on this backend too, which is the point: a test
# that only exercises the permissive backend cannot catch the bug.


async def test_put_file_with_metadata(config, tmp_path):
    store = AsyncS3CompatibleStorage(config)
    await store.ensure_bucket(Bucket.BRONZE)
    local = tmp_path / "scan.tif"
    local.write_bytes(b"tiff-bytes")

    await store.put_file(
        Bucket.BRONZE,
        "tiff/scan.tif",
        str(local),
        content_type="image/tiff",
        metadata={"source": "field-upload"},
    )

    info = await store.head(Bucket.BRONZE, "tiff/scan.tif")
    assert info["metadata"] == {"source": "field-upload"}
    assert info["content_type"] == "image/tiff"


async def test_get_file_downloads_to_disk(config, tmp_path):
    store = AsyncS3CompatibleStorage(config)
    await store.ensure_bucket(Bucket.BRONZE)
    await store.put_bytes(Bucket.BRONZE, "archive.zip", b"zip-bytes")

    dest = tmp_path / "downloaded.zip"
    await store.get_file(Bucket.BRONZE, "archive.zip", str(dest))

    assert dest.read_bytes() == b"zip-bytes"


async def test_get_file_missing_key_raises_object_not_found(config, tmp_path):
    store = AsyncS3CompatibleStorage(config)
    await store.ensure_bucket(Bucket.BRONZE)

    with pytest.raises(ObjectNotFoundError):
        await store.get_file(Bucket.BRONZE, "missing.zip", str(tmp_path / "out.zip"))


async def test_list_keys_paginated(config):
    store = AsyncS3CompatibleStorage(config)
    await store.ensure_bucket(Bucket.BRONZE)
    for i in range(3):
        await store.put_bytes(Bucket.BRONZE, f"pdfs/{i}.pdf", b"x")

    keys = sorted([key async for key in store.list_keys(Bucket.BRONZE, prefix="pdfs/")])
    assert keys == [f"pdfs/{i}.pdf" for i in range(3)]


async def test_delete_removes_object(config):
    store = AsyncS3CompatibleStorage(config)
    await store.ensure_bucket(Bucket.BRONZE)
    await store.put_bytes(Bucket.BRONZE, "pdfs/a.pdf", b"x")

    await store.delete(Bucket.BRONZE, "pdfs/a.pdf")

    assert not await store.exists(Bucket.BRONZE, "pdfs/a.pdf")


async def test_copy_between_buckets(config):
    store = AsyncS3CompatibleStorage(config)
    await store.ensure_bucket(Bucket.BRONZE)
    await store.ensure_bucket(Bucket.EXPORTS)
    await store.put_bytes(Bucket.BRONZE, "pdfs/a.pdf", b"payload")

    await store.copy(Bucket.BRONZE, "pdfs/a.pdf", Bucket.EXPORTS, "exports/a.pdf")

    assert await store.get_bytes(Bucket.EXPORTS, "exports/a.pdf") == b"payload"


async def test_copy_with_metadata_and_content_type_override(config):
    store = AsyncS3CompatibleStorage(config)
    await store.ensure_bucket(Bucket.BRONZE)
    await store.ensure_bucket(Bucket.EXPORTS)
    await store.put_bytes(Bucket.BRONZE, "pdfs/a.png", b"payload", content_type="application/octet-stream")

    await store.copy(
        Bucket.BRONZE,
        "pdfs/a.png",
        Bucket.EXPORTS,
        "exports/a.png",
        metadata={"report_id": "42"},
        content_type="image/png",
    )

    info = await store.head(Bucket.EXPORTS, "exports/a.png")
    # content_type round-trips through moto's real HTTP server; the
    # equivalent Metadata assertion is covered instead in
    # test_sync_client.py, since moto's server-mode copy_object handler
    # doesn't apply MetadataDirective=REPLACE's Metadata dict (server-mode
    # gap, confirmed by isolated repro against a bare aioboto3+moto server
    # — content_type applies, Metadata comes back empty either way,
    # independent of anything this package's code does). The sync test
    # exercises the identical params-construction logic in copy() via
    # moto's patched-internals mode, which does apply it correctly.
    assert info["content_type"] == "image/png"


async def test_head_returns_metadata(config):
    store = AsyncS3CompatibleStorage(config)
    await store.ensure_bucket(Bucket.BRONZE)
    await store.put_bytes(Bucket.BRONZE, "pdfs/a.pdf", b"payload", content_type="application/pdf")

    info = await store.head(Bucket.BRONZE, "pdfs/a.pdf")

    assert info["size"] == len(b"payload")
    assert info["content_type"] == "application/pdf"


async def test_async_client_kwargs_builds_a_working_raw_client(config):
    """async_client_kwargs() is the escape hatch for dynamic/arbitrary bucket
    names (backup_seaweedfs.py, outbox_dispatcher.py) — verify the kwargs it
    returns actually produce a working aioboto3 client, independent of
    AsyncS3CompatibleStorage."""
    kwargs = async_client_kwargs(config)
    session = aioboto3.Session()
    async with session.client("s3", **kwargs) as client:
        await client.create_bucket(Bucket="arbitrary-bucket-name")
        await client.put_object(Bucket="arbitrary-bucket-name", Key="k", Body=b"v")
        resp = await client.get_object(Bucket="arbitrary-bucket-name", Key="k")
        async with resp["Body"] as stream:
            assert await stream.read() == b"v"
