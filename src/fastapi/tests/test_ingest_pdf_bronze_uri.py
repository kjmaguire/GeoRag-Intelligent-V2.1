"""ingest_pdf records the bronze bucket the storage layer really uses.

persist built the OCR-review rows' ``bronze_uri`` from ``MINIO_BUCKET_BRONZE``
with a default of ``bronze``. Terraform sets ``AWS_BUCKET_BRONZE`` (and not the
MINIO_ name), so on AWS the URI named a bucket called ``bronze`` that the account
does not have, and a reviewer following it found nothing.
"""
from __future__ import annotations

import inspect

import pytest

from app.hatchet_workflows import ingest_pdf as ipdf

_ENV = (
    "AWS_BUCKET_BRONZE", "S3_BUCKET_BRONZE", "MINIO_BUCKET_BRONZE", "S3_BUCKET",
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "S3_ACCESS_KEY", "S3_SECRET_KEY",
    "MINIO_ROOT_USER", "MINIO_ROOT_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)


def test_the_bucket_terraform_sets_is_the_one_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_BUCKET_BRONZE", "georag-bronze-123456789012")
    assert ipdf._bronze_bucket_name() == "georag-bronze-123456789012"


def test_compose_names_still_resolve_through_the_storage_layer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINIO_BUCKET_BRONZE", "compose-bronze")
    assert ipdf._bronze_bucket_name() == "compose-bronze"


def test_nothing_set_is_the_storage_layers_default(monkeypatch: pytest.MonkeyPatch) -> None:
    assert ipdf._bronze_bucket_name() == "bronze"


def test_a_broken_credential_pair_does_not_fail_persist_over_a_uri(monkeypatch: pytest.MonkeyPatch) -> None:
    """StorageConfig refuses half a credential pair; every storage call will say
    so. A string that only records where the file is must not be what fails."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE")  # no secret
    monkeypatch.setenv("AWS_BUCKET_BRONZE", "georag-bronze-123456789012")
    assert ipdf._bronze_bucket_name() == "georag-bronze-123456789012"


def test_persist_does_not_read_the_compose_only_variable_any_more() -> None:
    assert "environ.get('MINIO_BUCKET_BRONZE'" not in inspect.getsource(ipdf._persist_body)
    assert 'environ.get("MINIO_BUCKET_BRONZE"' not in inspect.getsource(ipdf._persist_body)
