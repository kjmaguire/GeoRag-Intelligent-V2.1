"""Tests for the 2026-05-23 bronze ↔ MinIO unification.

Covers:
  * resolve_bronze_source dispatches on object_key vs local_path
  * stream_minio_to_temp hashes correctly
  * _build_sensor_run_config now wires object_key into the bronze op
  * sensor short-circuit when neither is set
"""
from __future__ import annotations

import hashlib
import io
import os

import pytest


class _FakeS3:
    """Stand-in for the boto3 S3 client returned by minio.get_client()."""

    def __init__(self, payloads: dict):
        # payloads: (bucket, key) -> bytes
        self.payloads = payloads

    def get_object(self, Bucket, Key):
        body_bytes = self.payloads[(Bucket, Key)]
        return {"Body": io.BytesIO(body_bytes)}


class _FakeMinio:
    """Stand-in for the S3Resource dagster resource."""

    def __init__(self, payloads: dict = None, existing: dict = None):
        # existing: (bucket, key) -> {"size": int} for object_exists/stat_object
        self.payloads = payloads or {}
        self.existing = existing or {}
        self.uploads = []  # list of (bucket, key, local_path, content_type)
        self._client = _FakeS3(self.payloads)

    def get_client(self):
        return self._client

    def object_exists(self, bucket, key):
        return (bucket, key) in self.existing

    def stat_object(self, bucket, key):
        return self.existing[(bucket, key)]

    def upload_file(self, bucket, object_name, file_path, content_type):
        self.uploads.append((bucket, object_name, file_path, content_type))
        return f"{bucket}/{object_name}"


def test_resolve_dispatches_on_object_key():
    from georag_dagster.assets._minio_bronze_helpers import resolve_bronze_source

    payload = b"col1,col2\n1,2\n3,4\n"
    minio = _FakeMinio(payloads={("bronze", "collars/file.csv"): payload})

    source = resolve_bronze_source(
        minio=minio,
        bucket="bronze",
        prefix="collars",
        object_key="collars/file.csv",
        local_path=None,
        upload_content_type="text/csv",
    )

    assert source.sourced_from_minio is True
    assert source.object_key == "collars/file.csv"
    assert source.file_size == len(payload)
    assert source.sha256 == hashlib.sha256(payload).hexdigest()
    # Temp file was created and contains the bytes
    with open(source.local_path, "rb") as fh:
        assert fh.read() == payload
    os.unlink(source.local_path)


def test_resolve_dispatches_on_local_path(tmp_path):
    from georag_dagster.assets._minio_bronze_helpers import resolve_bronze_source

    local_file = tmp_path / "sample.csv"
    payload = b"a,b\n1,2\n"
    local_file.write_bytes(payload)

    minio = _FakeMinio()
    source = resolve_bronze_source(
        minio=minio,
        bucket="bronze",
        prefix="samples",
        object_key=None,
        local_path=str(local_file),
        upload_content_type="text/csv",
    )

    assert source.sourced_from_minio is False
    assert source.object_key == "samples/sample.csv"
    assert source.file_size == len(payload)
    assert source.sha256 == hashlib.sha256(payload).hexdigest()
    # Uploaded exactly once
    assert minio.uploads == [
        ("bronze", "samples/sample.csv", str(local_file), "text/csv")
    ]


def test_resolve_skips_upload_when_same_size_already_in_bucket(tmp_path):
    from georag_dagster.assets._minio_bronze_helpers import resolve_bronze_source

    local_file = tmp_path / "x.csv"
    local_file.write_bytes(b"hello,world\n")

    minio = _FakeMinio(
        existing={("bronze", "samples/x.csv"): {"size": len(b"hello,world\n")}}
    )
    source = resolve_bronze_source(
        minio=minio,
        bucket="bronze",
        prefix="samples",
        object_key=None,
        local_path=str(local_file),
        upload_content_type="text/csv",
    )

    assert source.sourced_from_minio is False
    assert minio.uploads == []  # idempotency skip


def test_resolve_raises_when_neither_set():
    from georag_dagster.assets._minio_bronze_helpers import resolve_bronze_source

    with pytest.raises(ValueError):
        resolve_bronze_source(
            minio=_FakeMinio(),
            bucket="bronze",
            prefix="collars",
            object_key=None,
            local_path=None,
            upload_content_type="text/csv",
        )


def test_resolve_raises_when_local_file_missing(tmp_path):
    from georag_dagster.assets._minio_bronze_helpers import resolve_bronze_source

    with pytest.raises(FileNotFoundError):
        resolve_bronze_source(
            minio=_FakeMinio(),
            bucket="bronze",
            prefix="collars",
            object_key=None,
            local_path=str(tmp_path / "does-not-exist.csv"),
            upload_content_type="text/csv",
        )


def test_resolve_object_key_wins_when_both_set(tmp_path):
    """Sensor is authoritative — object_key always takes precedence."""
    from georag_dagster.assets._minio_bronze_helpers import resolve_bronze_source

    local_file = tmp_path / "local.csv"
    local_file.write_bytes(b"local content")

    minio_payload = b"minio content"
    minio = _FakeMinio(
        payloads={("bronze", "collars/from-minio.csv"): minio_payload}
    )

    source = resolve_bronze_source(
        minio=minio,
        bucket="bronze",
        prefix="collars",
        object_key="collars/from-minio.csv",
        local_path=str(local_file),
        upload_content_type="text/csv",
    )

    assert source.sourced_from_minio is True
    assert source.object_key == "collars/from-minio.csv"
    assert source.sha256 == hashlib.sha256(minio_payload).hexdigest()
    assert minio.uploads == []  # no upload happened — sensor path
    os.unlink(source.local_path)


def test_sensor_run_config_includes_object_key():
    from georag_dagster.sensor_helpers import build_sensor_run_config

    config = build_sensor_run_config(
        triggered_assets={"bronze_collars", "bronze_lithology"},
        asset_vendor_profile={"bronze_collars": 42, "bronze_lithology": None},
        asset_object_key={
            "bronze_collars": "collars/c1.csv",
            "bronze_lithology": "lithology/l1.csv",
        },
    )

    ops = config["ops"]
    assert ops["bronze_collars"] == {"config": {"object_key": "collars/c1.csv"}}
    assert ops["bronze_lithology"] == {"config": {"object_key": "lithology/l1.csv"}}
    # vendor_profile_id still flows to the silver pair
    assert ops["silver_collars"] == {"config": {"vendor_profile_id": 42}}
    assert ops["silver_lithology"] == {"config": {"vendor_profile_id": None}}


def test_sensor_run_config_back_compat_without_object_key_map():
    """Pre-2026-05-23 callers that don't pass asset_object_key still work."""
    from georag_dagster.sensor_helpers import build_sensor_run_config

    config = build_sensor_run_config(
        triggered_assets={"bronze_collars"},
        asset_vendor_profile={"bronze_collars": 99},
    )

    ops = config["ops"]
    assert "bronze_collars" not in ops  # no object_key entry
    assert ops["silver_collars"] == {"config": {"vendor_profile_id": 99}}
