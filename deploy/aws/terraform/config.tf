# Application configuration (ADR-0022).
#
# This file is the answer to the gap the Azure README recorded under "What
# is NOT here" and never closed (ADR-0022, Consequences): there was no
# Bicep, Terraform or ARM template for the container apps, so ~55
# environment variables per app were set by hand and drifted freely from
# `.env.production.example`. Nothing in the running system could be diffed
# against anything in the repository.
#
# So: every non-secret value is here, in code. Every secret is a Secrets
# Manager reference injected by the execution role — its value never passes
# through Terraform state, CI, or a person's terminal.

resource "aws_secretsmanager_secret" "app" {
  name        = "${local.name}/app"
  description = "Application secrets, injected into every task by ARN"

  # Long enough to actually recover from a bad rotation. The APP_KEY
  # rotation runbook's whole design assumes the old value is retrievable
  # if the in-replica half fails.
  recovery_window_in_days = 30
}

# Deliberately NOT managed here. Terraform would put every value in state,
# which is the thing a secret store exists to avoid. The keys this expects
# are listed so the shape is reviewable even though the values are not:
#
#   APP_KEY                  Laravel encryption key (rotation: ops/runbooks)
#   FASTAPI_SERVICE_KEY      the X-Service-Key both sides check on every
#                            internal hop; rotation accepts the previous
#                            value on both sides simultaneously
#   FASTAPI_SERVICE_KEY_PREVIOUS
#   QDRANT_API_KEY           one read-write key
#   HATCHET_CLIENT_TOKEN
#   REDIS_PASSWORD
#   MARTIN_DATABASE_URL      connects as martin_readonly, which has EXECUTE
#                            on the silver.pg_* tile functions and nothing
#                            else
#
# There is no Foundry key here and no storage account key. Both are gone:
# Bedrock and S3 authenticate with the task role.
resource "aws_secretsmanager_secret_version" "app_placeholder" {
  secret_id     = aws_secretsmanager_secret.app.id
  secret_string = jsonencode({ PLACEHOLDER = "set-these-out-of-band" })

  lifecycle {
    ignore_changes = [secret_string]
  }
}

locals {
  # Secrets injected into every task, by ARN. The execution role reads
  # them; the container never sees the ARN, only the value.
  _secret_ref = { for key in [
    "APP_KEY",
    "FASTAPI_SERVICE_KEY",
    "QDRANT_API_KEY",
    "HATCHET_CLIENT_TOKEN",
    "REDIS_PASSWORD",
    ] : key => "${aws_secretsmanager_secret.app.arn}:${key}::"
  }

  # Application services get the whole set. The third-party containers get
  # only what they themselves read, under the names THEY expect — qdrant
  # reads QDRANT__SERVICE__API_KEY, not QDRANT_API_KEY, and giving it the
  # client-side name would leave auth off while looking configured.
  service_secrets = {
    for name, _cfg in local.services : name => lookup({
      qdrant = [{ name = "QDRANT__SERVICE__API_KEY", valueFrom = local._secret_ref["QDRANT_API_KEY"] }]
      redis  = []
      martin = []
      }, name,
      [for key, ref in local._secret_ref : { name = key, valueFrom = ref }]
    )
  }

  db_host = aws_db_instance.this.address

  # Values every service shares.
  common_environment = {
    # Gates main.py::_assert_production_posture — the only thing in the
    # system that reports a security control being off. Unset, it reports
    # nothing and the deployment looks healthy.
    GEORAG_ENV = "production"
    LOG_LEVEL  = "info"

    APP_ENV   = "production"
    APP_DEBUG = "false"

    POSTGRES_HOST        = local.db_host
    POSTGRES_DIRECT_HOST = local.db_host
    POSTGRES_PORT        = 5432
    POSTGRES_DB          = "georag"
    # PgBouncer is compose-only and was already absent on Azure. asyncpg
    # runs statement_cache_size=0 regardless, so RDS Proxy drops in
    # cleanly if connection counts ever justify it. Not day one.
    DB_HOST = local.db_host
    DB_PORT = 5432

    REDIS_HOST = "redis.${aws_service_discovery_private_dns_namespace.this.name}"
    REDIS_PORT = 6379

    QDRANT_HOST = "qdrant.${aws_service_discovery_private_dns_namespace.this.name}"
    QDRANT_PORT = 6333
    # Plain HTTP inside the VPC. On Azure these were 443/true because
    # clients reached Qdrant through the environment's internal ingress,
    # which terminated TLS; Cloud Map hands out a task IP and there is no
    # ingress in between.
    QDRANT_HTTPS = "false"

    HATCHET_CLIENT_HOST_PORT = "hatchet.${aws_service_discovery_private_dns_namespace.this.name}:7077"

    # ── Object storage ──────────────────────────────────────────────
    # No AWS_ACCESS_KEY_ID and no AWS_SECRET_ACCESS_KEY: the task role
    # supplies credentials and georag_object_storage now omits the keys
    # rather than passing None, so boto3's chain resolves them. No
    # AWS_ENDPOINT_URL either — an explicit endpoint pins every call to
    # that host, which is right for SeaweedFS and wrong for S3.
    STORAGE_BACKEND          = "s3_compatible"
    AWS_DEFAULT_REGION       = var.region
    AWS_BUCKET_BRONZE        = aws_s3_bucket.this["bronze"].id
    AWS_BUCKET_BRONZE_RASTER = aws_s3_bucket.this["bronze-raster"].id
    AWS_BUCKET_EXPORTS       = aws_s3_bucket.this["exports"].id
    AWS_BUCKET_BACKUPS       = aws_s3_bucket.this["backups"].id

    # ── Models ──────────────────────────────────────────────────────
    # Set explicitly on every service even though `bedrock` is the code
    # default. EMBEDDING_BACKEND in particular MUST match between the
    # query path (fastapi) and the ingest path (hatchet-worker): a
    # mismatch writes one vector space and queries another, which is
    # ADR-0021's migration step 2 and the reason it is stated rather than
    # inherited.
    BEDROCK_REGION          = local.bedrock_region
    LLM_BACKEND             = "bedrock"
    EMBEDDING_BACKEND       = "bedrock"
    RERANKER_BACKEND        = "bedrock"
    BEDROCK_EMBED_MODEL_ID  = var.bedrock_embed_model_id
    BEDROCK_EMBED_DIMENSION = 1024
    BEDROCK_RERANK_MODEL_ID = var.bedrock_rerank_model_id
    BEDROCK_CHAT_MODEL_ID   = "arn:aws:sagemaker:${local.bedrock_region}:${data.aws_caller_identity.current.account_id}:endpoint/${var.bedrock_chat_endpoint_name}"
    BEDROCK_PARSE_MODEL_ID  = "arn:aws:sagemaker:${local.bedrock_region}:${data.aws_caller_identity.current.account_id}:endpoint/${var.bedrock_parse_endpoint_name}"
    OCR_ENGINE              = "cohere_parse"

    # SPLADE++ has no managed equivalent anywhere. This is what makes the
    # sparse leg of hybrid retrieval exist; unset, sparse_encoder falls
    # back to loading the model in-process, once per uvicorn worker —
    # the pattern that OOM-killed the container on 2026-06-24.
    SPARSE_SERVICE_URL = "http://sparse.${aws_service_discovery_private_dns_namespace.this.name}:8000"
  }

  # Per-service additions. Everything not listed gets only the common set.
  service_environment = {
    for name, _cfg in local.services : name => merge(
      local.common_environment,
      lookup({
        fastapi = {
          FASTAPI_INTERNAL_URL = "http://fastapi.${aws_service_discovery_private_dns_namespace.this.name}:8000"
        }
        hatchet-worker = {
          # `all` is what both compose and Azure ran. There is no separate
          # ingestion or AI worker service, and WORKER_POOL exists mainly
          # as the seam that would let the every-minute crons move to a
          # small always-on pool if the nightly shutdown ever needs the
          # big worker off for longer.
          WORKER_POOL          = "all"
          FASTAPI_INTERNAL_URL = "http://fastapi.${aws_service_discovery_private_dns_namespace.this.name}:8000"
        }
        sparse = {
          # The sidecar serves the model; it must not also try to reach
          # itself over SPARSE_SERVICE_URL.
          SPARSE_SERVICE_URL = ""
        }
        laravel-octane = {
          FASTAPI_INTERNAL_URL = "http://fastapi.${aws_service_discovery_private_dns_namespace.this.name}:8000"
          # LOG_STACK=stderr, NOT the `single` default. On Azure the
          # default routed the application log to a file inside a
          # container nobody could read, and Ch 12 records that which
          # LOG_STACK production actually ran is not known. On ECS only
          # stdout/stderr reach CloudWatch, so this is stated.
          LOG_STACK     = "stderr"
          OCTANE_SERVER = "swoole"
        }
        laravel-horizon = {
          LOG_STACK = "stderr"
        }
        laravel-reverb = {
          LOG_STACK          = "stderr"
          REVERB_SERVER_HOST = "0.0.0.0"
          REVERB_SERVER_PORT = 8080
        }
        qdrant = {
          # The API key arrives through `secrets`, under the name qdrant
          # itself reads. Setting it here too would put it in the task
          # definition in plaintext.
          QDRANT__STORAGE__STORAGE_PATH = "/qdrant/storage"
        }
      }, name, {})
    )
  }
}

variable "acm_certificate_arn" {
  description = "ACM certificate for the public HTTPS listener."
  type        = string
}
