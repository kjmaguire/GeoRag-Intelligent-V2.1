"""GeoRAG Dagster resources — PostgreSQL (via pgbouncer), S3 (boto3), and Qdrant.

All resources use ConfigurableResource so Dagster can inject them into assets
and they show up in the Dagster UI's resource configuration panel.

psycopg2 (sync) is used deliberately — Dagster's asset execution model is
synchronous; asyncpg is reserved for the FastAPI service layer.

NOTE: Do NOT add `from __future__ import annotations` to this file.
Dagster 1.13 Config/ConfigurableResource classes use Pydantic for type
introspection and that import breaks runtime annotation evaluation.
"""

import contextlib
import io
from typing import Generator, Iterator

import boto3
import psycopg2
import psycopg2.extras
from botocore.client import Config
from botocore.exceptions import ClientError
from dagster import ConfigurableResource, get_dagster_logger

logger = get_dagster_logger()


# ---------------------------------------------------------------------------
# PostgreSQL Resource
# ---------------------------------------------------------------------------

class PostgresResource(ConfigurableResource):
    """Synchronous PostgreSQL connection resource routing through pgbouncer.

    Wraps psycopg2 and exposes a context-manager-based connection helper so
    assets can obtain a connection, run queries, and have it properly closed
    (and committed or rolled back) without boilerplate.
    """

    host: str = "pgbouncer"
    port: int = 6432
    dbname: str = "georag"
    user: str = "georag"
    password: str

    def _connect(self) -> psycopg2.extensions.connection:
        """Open and return a raw psycopg2 connection."""
        conn = psycopg2.connect(
            host=self.host,
            port=self.port,
            dbname=self.dbname,
            user=self.user,
            password=self.password,
            # pgbouncer transaction-pooling mode works best with autocommit off
            # and explicit BEGIN/COMMIT in each logical unit of work.
            connect_timeout=10,
        )
        conn.autocommit = False
        return conn

    @contextlib.contextmanager
    def get_connection(self) -> Generator[psycopg2.extensions.connection, None, None]:
        """Yield a psycopg2 connection; commit on clean exit, rollback on exception."""
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @contextlib.contextmanager
    def get_cursor(
        self,
        cursor_factory=psycopg2.extras.RealDictCursor,
    ) -> Generator[psycopg2.extensions.cursor, None, None]:
        """Yield a cursor inside a managed connection.

        Uses RealDictCursor by default so SELECT results come back as dicts.
        """
        with self.get_connection() as conn:
            with conn.cursor(cursor_factory=cursor_factory) as cur:
                yield cur

    def execute(self, sql: str, params=None) -> None:
        """Execute a single statement and commit. Convenience wrapper."""
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)

    def execute_many(self, sql: str, params_seq: list) -> int:
        """Execute a statement for each item in params_seq. Returns row count."""
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                psycopg2.extras.execute_batch(cur, sql, params_seq, page_size=500)
                return cur.rowcount


# ---------------------------------------------------------------------------
# S3 Resource  (addendum §02a — boto3 vendor-neutral implementation)
# ---------------------------------------------------------------------------

class S3Resource(ConfigurableResource):
    """S3-compatible object storage resource backed by boto3.

    Replaces the previous minio-py MinIOResource per addendum §02a vendor-purity
    rule.  All GeoRAG application code must access object storage through the
    standard S3 API via boto3 with endpoint_url.

    Reads from environment variables injected by docker-compose:
        S3_ENDPOINT_URL      — e.g. http://minio:8333
        MINIO_ROOT_USER      — S3 access key (reused from existing compose env)
        MINIO_ROOT_PASSWORD  — S3 secret key (reused from existing compose env)

    Configured in definitions.py under the "s3" resource key.
    """

    endpoint_url: str = "http://minio:8333"  # from S3_ENDPOINT_URL
    access_key: str                            # from MINIO_ROOT_USER
    secret_key: str                            # from MINIO_ROOT_PASSWORD
    region: str = "us-east-1"                 # SeaweedFS accepts any region; required by boto3

    def get_client(self):
        """Return a boto3 S3 client configured for the project endpoint."""
        return boto3.client(
            "s3",
            endpoint_url=self.endpoint_url,
            aws_access_key_id=self.access_key,
            aws_secret_access_key=self.secret_key,
            region_name=self.region,
            config=Config(signature_version="s3v4"),
        )

    def bucket_exists(self, bucket: str) -> bool:
        """Return True if the bucket exists and is accessible."""
        try:
            self.get_client().head_bucket(Bucket=bucket)
            return True
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code in ("404", "NoSuchBucket"):
                return False
            raise

    def ensure_bucket(self, bucket: str) -> None:
        """Create the bucket if it does not already exist."""
        if not self.bucket_exists(bucket):
            self.get_client().create_bucket(Bucket=bucket)
            logger.info("S3: created bucket '%s'", bucket)

    def upload_file(
        self,
        bucket: str,
        object_name: str,
        file_path: str,
        content_type: str = "application/octet-stream",
    ) -> str:
        """Upload a local file to S3. Returns the full object path."""
        self.ensure_bucket(bucket)
        self.get_client().upload_file(
            Filename=file_path,
            Bucket=bucket,
            Key=object_name,
            ExtraArgs={"ContentType": content_type},
        )
        full_path = f"{bucket}/{object_name}"
        logger.info("S3: uploaded '%s' -> '%s'", file_path, full_path)
        return full_path

    def upload_bytes(
        self,
        bucket: str,
        object_name: str,
        data: bytes,
        content_type: str = "application/octet-stream",
    ) -> str:
        """Upload raw bytes to S3. Returns the full object path."""
        self.ensure_bucket(bucket)
        self.get_client().put_object(
            Bucket=bucket,
            Key=object_name,
            Body=io.BytesIO(data),
            ContentType=content_type,
        )
        full_path = f"{bucket}/{object_name}"
        logger.info("S3: uploaded bytes -> '%s'", full_path)
        return full_path

    def download_bytes(self, bucket: str, object_name: str) -> bytes:
        """Download an object and return its raw bytes."""
        try:
            resp = self.get_client().get_object(Bucket=bucket, Key=object_name)
            return resp["Body"].read()
        except ClientError as exc:
            logger.error("S3: failed to download '%s/%s': %s", bucket, object_name, exc)
            raise

    def stat_object(self, bucket: str, object_name: str) -> dict:
        """Return a dict with size, etag, last_modified, content_type, and metadata for an object.

        boto3 head_object returns metadata under the 'Metadata' key (lowercase, stripped of the
        x-amz-meta- prefix).  The helper _extract_vendor_profile_id in definitions.py handles both
        the boto3 form and the legacy minio-py form, so no adaptation is needed here.
        """
        resp = self.get_client().head_object(Bucket=bucket, Key=object_name)
        return {
            "size": resp.get("ContentLength"),
            "etag": resp.get("ETag", "").strip('"'),
            "last_modified": resp.get("LastModified"),
            "content_type": resp.get("ContentType"),
            "metadata": resp.get("Metadata", {}),
        }

    def object_exists(self, bucket: str, object_name: str) -> bool:
        """Return True if the object exists."""
        try:
            self.get_client().head_object(Bucket=bucket, Key=object_name)
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("404", "NoSuchBucket"):
                return False
            raise

    def list_keys(self, bucket: str, prefix: str = "") -> Iterator[str]:
        """Yield object keys under the given prefix using paginated list_objects_v2."""
        paginator = self.get_client().get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                yield obj["Key"]


# ---------------------------------------------------------------------------
# Backward-compatibility alias — callers still importing MinIOResource will
# get S3Resource.  All call sites are being updated to S3Resource directly;
# this alias exists only to avoid a hard failure if any import is missed.
# ---------------------------------------------------------------------------

MinIOResource = S3Resource


# ---------------------------------------------------------------------------
# Qdrant Resource
# ---------------------------------------------------------------------------

class QdrantResource(ConfigurableResource):
    """Qdrant vector database resource.

    Wraps the qdrant-client and exposes a thin factory so assets can obtain a
    connected client without carrying host/port config themselves.

    The georag_chunks collection (384-dim, cosine, all-MiniLM-L6-v2) is
    assumed to already exist — provisioned by the FastAPI service on startup.
    """

    host: str = "qdrant"
    port: int = 6333

    def get_client(self):
        """Return a connected QdrantClient.

        Import is deferred so the rest of the pipeline does not hard-depend on
        qdrant-client being installed in environments that never run the index
        asset (e.g., lightweight CI).
        """
        from qdrant_client import QdrantClient  # noqa: PLC0415

        return QdrantClient(host=self.host, port=self.port)


# ---------------------------------------------------------------------------
# Neo4j Resource
# ---------------------------------------------------------------------------

class Neo4jResource(ConfigurableResource):
    """Synchronous Neo4j driver resource for the knowledge graph.

    Uses the official neo4j Python driver (sync variant) because Dagster's
    asset execution model is synchronous.  The async driver is reserved for
    the FastAPI service layer.

    Auth is disabled for local development (NEO4J_AUTH=none).  Set
    auth_enabled=True and supply username/password for production deployments.

    Connection pool size is set to 50 to match the FastAPI fan-out budget
    defined in Section 06b; Dagster assets are typically single-threaded so
    the pool is deliberately generous rather than tight.
    """

    uri: str = "bolt://neo4j:7687"
    auth_enabled: bool = False
    username: str = "neo4j"
    password: str = ""
    # Max connections in the driver-managed pool (Section 06b).
    max_connection_pool_size: int = 50

    def get_driver(self):
        """Return a neo4j.Driver (sync).

        The caller is responsible for calling driver.close() when finished.
        Prefer wrapping usage in a try/finally to ensure driver.close() is
        called even on errors; Dagster assets have one driver lifetime per
        materialisation.
        """
        from neo4j import GraphDatabase  # noqa: PLC0415

        auth = (self.username, self.password) if self.auth_enabled else None
        return GraphDatabase.driver(
            self.uri,
            auth=auth,
            max_connection_pool_size=self.max_connection_pool_size,
        )


# ---------------------------------------------------------------------------
# vLLM Resource
# ---------------------------------------------------------------------------

class VllmResource(ConfigurableResource):
    """vLLM OpenAI-compatible inference endpoint.

    vLLM serves the cluster's Qwen3-14B-AWQ model on the
    ``/v1`` OpenAI chat-completions API. Dagster assets that need synthetic
    text generation (e.g. the reranker-label dataset asset) instantiate
    this resource and call ``get_client()`` to obtain an ``openai.OpenAI``
    instance scoped to the right base URL and model.

    The ``api_key`` field is set to a literal ``"EMPTY"`` because the
    OpenAI Python SDK refuses to construct a client with an empty string,
    yet the vLLM server itself is launched without ``--api-key`` and
    ignores the header. Do not override this unless you've also enabled
    auth on the upstream vLLM service.
    """

    base_url: str = "http://vllm:8000/v1"
    model: str = "Qwen/Qwen3-14B-AWQ"
    api_key: str = "EMPTY"
    timeout_s: float = 60.0

    def get_client(self):
        """Return a configured ``openai.OpenAI`` client targeting vLLM."""
        from georag_dagster.clients.vllm_openai import build_default_client  # noqa: PLC0415

        return build_default_client(
            base_url=self.base_url,
            api_key=self.api_key,
            timeout_s=self.timeout_s,
        )
