"""Metadata keys are checked against the rule both backends can honour.

The bug this pins: ``tiff_normalize`` tagged its derived PDF with
``x-georag-derived-from-tiff-sha256``. S3 and MinIO accepted it, so every
test and every local run passed. Azure Blob requires metadata names to be
valid C# identifiers and answered HTTP 400 ``InvalidMetadata``, so on
Azure *every* TIFF upload failed after a successful wrap - the work was
done and then thrown away.

The check therefore has to run on the S3 path too. A rule enforced only
where it is already violated is a rule nobody finds until production.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from georag_object_storage.azure_config import AzureBlobConfig
from georag_object_storage.azure_sync_client import AzureBlobStorage
from georag_object_storage.buckets import Bucket
from georag_object_storage.exceptions import ObjectStorageError
from georag_object_storage.metadata import (
    InvalidMetadataKeyError,
    is_valid_metadata_key,
    validate_metadata,
)


class TestKeyRule:
    @pytest.mark.parametrize(
        "key",
        [
            "report_id",
            "page",
            "sha256",
            "_leading_underscore",
            "derived_from_tiff_sha256",
            "a",
            "A1",
        ],
    )
    def test_accepts_identifier_names(self, key):
        assert is_valid_metadata_key(key)

    @pytest.mark.parametrize(
        "key",
        [
            # The exact keys that broke production.
            "x-georag-derived-from-tiff-sha256",
            "x-georag-tiff-source-key",
            "x-georag-tiff-frames",
            "x-georag-tiff-truncated",
            "x-georag-vendor-profile-id",
            # Other shapes Azure also refuses.
            "1leading_digit",
            "has space",
            "has.dot",
            "has:colon",
            "",
        ],
    )
    def test_rejects_everything_azure_would(self, key):
        assert not is_valid_metadata_key(key)

    def test_error_names_the_key_and_suggests_a_fix(self):
        with pytest.raises(InvalidMetadataKeyError) as exc:
            validate_metadata({"x-georag-tiff-frames": "3"})
        msg = str(exc.value)
        assert "x-georag-tiff-frames" in msg
        assert "tiff_frames" in msg, "the message should say what to use instead"

    def test_is_an_object_storage_error(self):
        """Call sites already catching the package's base error keep working."""
        assert issubclass(InvalidMetadataKeyError, ObjectStorageError)

    def test_reports_the_same_key_regardless_of_dict_order(self):
        keys = ["z-bad", "a-bad"]
        first = {k: "v" for k in keys}
        second = {k: "v" for k in reversed(keys)}
        with pytest.raises(InvalidMetadataKeyError) as one:
            validate_metadata(first)
        with pytest.raises(InvalidMetadataKeyError) as two:
            validate_metadata(second)
        assert one.value.key == two.value.key == "a-bad"

    @pytest.mark.parametrize("empty", [None, {}])
    def test_no_metadata_is_always_legal(self, empty):
        assert validate_metadata(empty) == empty

    def test_valid_metadata_passes_through_unchanged(self):
        meta = {"report_id": "7", "page": "3"}
        assert validate_metadata(meta) is meta


@pytest.fixture
def azure_config():
    return AzureBlobConfig(
        connection_string=(
            "DefaultEndpointsProtocol=https;AccountName=x;AccountKey=eA==;"
            "EndpointSuffix=core.windows.net"
        ),
        account_url=None,
        container_names={Bucket.BRONZE: "bronze", Bucket.EXPORTS: "exports"},
    )


@pytest.fixture
def azure_service():
    with patch("georag_object_storage.azure_sync_client.BlobServiceClient") as mock_cls:
        service = MagicMock()
        mock_cls.from_connection_string.return_value = service
        yield service


class TestAzureClientEnforcement:
    def test_put_bytes_refuses_a_hyphenated_key_without_calling_the_sdk(
        self, azure_config, azure_service
    ):
        store = AzureBlobStorage(azure_config)
        with pytest.raises(InvalidMetadataKeyError):
            store.put_bytes(
                Bucket.BRONZE,
                "reports/x.pdf",
                b"%PDF-",
                metadata={"x-georag-tiff-frames": "1"},
            )
        # The point of failing before the upload: no bytes go over the wire
        # only to be rejected, and no partial blob is left behind.
        blob = azure_service.get_container_client.return_value.get_blob_client
        blob.return_value.upload_blob.assert_not_called()

    def test_error_type_survives_the_generic_exception_wrapper(
        self, azure_config, azure_service
    ):
        """The client re-wraps everything as ObjectStorageError; ours must not
        be flattened into one, or the offending key is lost."""
        store = AzureBlobStorage(azure_config)
        with pytest.raises(InvalidMetadataKeyError):
            store.put_bytes(
                Bucket.BRONZE, "k", b"x", metadata={"bad-key": "v"}
            )

    def test_identifier_keys_reach_the_sdk(self, azure_config, azure_service):
        store = AzureBlobStorage(azure_config)
        meta = {"derived_from_tiff_sha256": "abc", "tiff_frames": "2"}
        store.put_bytes(Bucket.BRONZE, "reports/x.pdf", b"%PDF-", metadata=meta)
        blob = azure_service.get_container_client.return_value.get_blob_client
        kwargs = blob.return_value.upload_blob.call_args.kwargs
        assert kwargs["metadata"] == meta


class TestS3ClientEnforcement:
    """The rule has to bite on the permissive backend too.

    This is the whole point of the fix. The moto-backed suites already
    exercised ``put_bytes`` with metadata and passed for months, because S3
    accepts hyphens; the identical call was failing against Azure Blob on
    every TIFF upload in production. A rule enforced only where it is
    already being broken is a rule nobody finds until it ships.

    No moto here on purpose: validation happens before boto3 is ever asked
    to do anything, so this runs anywhere.
    """

    @pytest.fixture
    def s3_config(self):
        from georag_object_storage.config import StorageConfig

        return StorageConfig(
            endpoint_url="http://localhost:9000",
            access_key="testing",
            secret_key="testing",
            region="us-east-1",
            bucket_names={Bucket.BRONZE: "bronze-test", Bucket.EXPORTS: "exports-test"},
        )

    def test_put_bytes_refuses_a_hyphenated_key(self, s3_config):
        from georag_object_storage.sync_client import S3CompatibleStorage

        store = S3CompatibleStorage(s3_config)
        with pytest.raises(InvalidMetadataKeyError):
            store.put_bytes(
                Bucket.BRONZE,
                "reports/derived.pdf",
                b"payload",
                metadata={"x-georag-derived-from-tiff-sha256": "abc123"},
            )

    def test_put_file_refuses_a_hyphenated_key(self, s3_config, tmp_path):
        from georag_object_storage.sync_client import S3CompatibleStorage

        local = tmp_path / "scan.tif"
        local.write_bytes(b"tiff-bytes")
        store = S3CompatibleStorage(s3_config)
        with pytest.raises(InvalidMetadataKeyError):
            store.put_file(
                Bucket.BRONZE, "tiff/scan.tif", str(local),
                metadata={"x-georag-tiff-frames": "1"},
            )
