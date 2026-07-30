import os

import pytest

_STORAGE_ENV_PREFIXES = ("AWS_", "S3_", "MINIO_", "SEAWEEDFS_")

# moto only intercepts requests to a non-AWS endpoint_url (SeaweedFS/MinIO,
# what every client/sync/async fixture in this suite uses) when that
# endpoint is explicitly whitelisted here; otherwise it falls through to a
# real connection attempt. See moto's "custom endpoints" docs.
_TEST_ENDPOINT = "http://localhost:9000"


@pytest.fixture(autouse=True)
def _clean_storage_env(monkeypatch):
    """Strip storage-related env vars before each test so tests control their own env precisely."""
    for key in list(os.environ):
        if key.startswith(_STORAGE_ENV_PREFIXES) or key == "STORAGE_BACKEND":
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MOTO_S3_CUSTOM_ENDPOINTS", _TEST_ENDPOINT)
