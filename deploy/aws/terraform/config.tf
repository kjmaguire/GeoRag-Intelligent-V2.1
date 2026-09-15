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
#   FLOW_JWT_SECRET          signs and verifies the per-flow integration
#                            JWTs (services/flow_jwt.py), for callers with
#                            no per-flow key in workflow.flow_registry.
#                            Renamed from KESTRA_FLOW_JWT_SECRET (ADR-0022)
#   REVERB_APP_SECRET        signs requests to the Pusher events API.
#                            laravel-octane and laravel-horizon sign with
#                            it; laravel-reverb verifies. The paired
#                            REVERB_APP_KEY is public by design
#                            (config/reverb.php) and is a variable below,
#                            not a secret — the browser receives it.
#
# FLOW_JWT_SECRET was absent from this file until 2026-09-14, while
# docker-compose.yml marked it `${VAR:?}` required — so dev could not start
# without it and production ran without it entirely. app/config.py defaults
# it to "", so FastAPI started either way and flow_jwt.py would have raised
# 500 on the first verify that fell back to it. Inert only because nothing
# calls the integrations bridge; a latent 500 rather than a visible
# misconfiguration is exactly the shape this file exists to prevent.
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

  # Secrets only SOME application services read, so they are not in the
  # common set above. FLOW_JWT_SECRET is an HS256 signing key: the two
  # Python services import services/flow_jwt.py, and nothing else has any
  # use for it. Handing it to laravel-*, the hatchet engine or the sparse
  # model server would widen a signing secret's blast radius for nothing.
  #
  # Add to this map rather than to _secret_ref whenever a new secret has a
  # named reader instead of a general one.
  _extra_secret_ref = {
    fastapi        = ["FLOW_JWT_SECRET"]
    hatchet-worker = ["FLOW_JWT_SECRET"]

    # Only the three PHP services broadcast or serve WebSocket frames.
    # fastapi streams SSE to Laravel, which re-broadcasts; it never signs
    # a Pusher request itself.
    laravel-octane  = ["REVERB_APP_SECRET"]
    laravel-horizon = ["REVERB_APP_SECRET"]
    laravel-reverb  = ["REVERB_APP_SECRET"]
  }

  # The database password, under the two names the two language stacks read
  # it by. One secret, because it is one role.
  #
  # The application connects as georag_app, NOT as georag. georag is the RDS
  # master and owns every table, and `ENABLE ROW LEVEL SECURITY` does not
  # apply to a table's owner — only FORCE does. Connecting the app as the
  # owner would leave every tenancy guarantee resting on FORCE having been
  # applied to every table without exception, which is precisely the thing
  # scripts/check-rls-force-parity.php exists because we cannot assume.
  # georag_app is created NOSUPERUSER NOBYPASSRLS by
  # database/raw/phase1/10-georag-app-role.sql.
  #
  # That file creates it with a placeholder password committed to this
  # repository ('georag-app-dev-replace-via-alter-role'). The operator must
  # ALTER ROLE it to the value written here — see deploy/aws/README.md.
  _db_secret_ref = [
    # Laravel: config/database.php pgsql.password
    { name = "DB_PASSWORD", valueFrom = "${aws_secretsmanager_secret.app.arn}:GEORAG_APP_PASSWORD::" },
    # FastAPI and the Hatchet worker: app/config.py POSTGRES_PASSWORD, which
    # is a required field with no default — an unset value is a startup
    # ValidationError, not a degraded mode.
    { name = "POSTGRES_PASSWORD", valueFrom = "${aws_secretsmanager_secret.app.arn}:GEORAG_APP_PASSWORD::" },
  ]

  # Application services get the whole common set plus the database
  # credential, plus anything named for them above. The third-party
  # containers get only what they themselves read, under the names THEY
  # expect — qdrant reads QDRANT__SERVICE__API_KEY, not QDRANT_API_KEY, and
  # giving it the client-side name would leave auth off while looking
  # configured. The same trap caught three more containers below; each entry
  # here is the name that container's own image reads, not ours.
  service_secrets = {
    for name, _cfg in local.services : name => lookup({
      qdrant = [{ name = "QDRANT__SERVICE__API_KEY", valueFrom = local._secret_ref["QDRANT_API_KEY"] }]

      # The SERVER needs the password too, not just the clients. Without it
      # redis-server runs with no `requirepass` while every client is
      # configured to send AUTH, and Redis answers AUTH-with-no-password
      # set with an error — so cache, sessions, queues, Horizon and the
      # Reverb backplane all fail closed. See the command in services.tf.
      redis = [{ name = "REDIS_PASSWORD", valueFrom = local._secret_ref["REDIS_PASSWORD"] }]

      # docker/martin.Dockerfile deliberately gives DATABASE_URL no default
      # ("an absent one fails at boot with an obvious one") and
      # docker/martin/martin.yaml:39 reads `connection_string:
      # '${DATABASE_URL}'`. Nothing was supplying it, so every MVT tile in
      # the platform was one boot failure away.
      martin = [{ name = "DATABASE_URL", valueFrom = "${aws_secretsmanager_secret.app.arn}:MARTIN_DATABASE_URL::" }]

      # hatchet-lite keeps its own database, on the same instance. The role
      # and database are created by deploy/aws/bootstrap.sql; this is the
      # connection string for them.
      hatchet = [{ name = "DATABASE_URL", valueFrom = "${aws_secretsmanager_secret.app.arn}:HATCHET_DATABASE_URL::" }]
      }, name,
      concat(
        [for key, ref in local._secret_ref : { name = key, valueFrom = ref }],
        local._db_secret_ref,
        [for key in lookup(local._extra_secret_ref, name, []) : {
          name      = key
          valueFrom = "${aws_secretsmanager_secret.app.arn}:${key}::"
        }],
      )
    )
  }

  db_host = aws_db_instance.this.address

  # One number, three consumers — see EMBEDDING_DIMENSION below. 1024 is what
  # georag_chunks is built at and what Cohere Embed v4 is asked for; changing
  # it means re-embedding the corpus (scripts/reset_embeddings_for_reencode.py),
  # not just editing this line.
  embed_dimension = 1024

  # Values every service shares.
  common_environment = {
    # Gates main.py::_assert_production_posture — the only thing in the
    # system that reports a security control being off. Unset, it reports
    # nothing and the deployment looks healthy.
    GEORAG_ENV = "production"
    LOG_LEVEL  = "info"

    APP_ENV   = "production"
    APP_DEBUG = "false"

    # The public origin, and the thing a whole family of settings derives
    # from. Unset, config/app.php:54 falls back to `http://localhost` and
    # every absolute URL, signed URL and password-reset link points at the
    # container. Sanctum's stateful list also ends with
    # currentApplicationUrlWithPort() (config/sanctum.php:20), so this is
    # what puts the real domain in it.
    #
    # CORS_ALLOWED_ORIGINS is deliberately NOT set: the Inertia app is
    # same-origin and config/cors.php:39 already resolves an unset value to
    # an empty allowlist in production, which is the safe reading of
    # "nobody said". Add it only when a genuine cross-origin caller exists.
    APP_URL = "https://${var.app_domain}"

    POSTGRES_HOST        = local.db_host
    POSTGRES_DIRECT_HOST = local.db_host
    POSTGRES_PORT        = 5432
    POSTGRES_DB          = "georag"
    # app/config.py defaults this to "georag", the OWNER. See the argument
    # by _db_secret_ref above: the owner is not subject to plain ENABLE ROW
    # LEVEL SECURITY, so the application must never connect as it.
    POSTGRES_USER = "georag_app"
    # PgBouncer is compose-only and was already absent on Azure. asyncpg
    # runs statement_cache_size=0 regardless, so RDS Proxy drops in
    # cleanly if connection counts ever justify it. Not day one.
    DB_HOST = local.db_host
    DB_PORT = 5432

    # ── Laravel's own connection and drivers ────────────────────────
    # Every one of these was missing, and Laravel's defaults are not
    # "unconfigured" — they are wrong in a way that starts cleanly:
    #
    #   DB_CONNECTION    config/database.php:19 defaults to `sqlite`, so
    #                    Octane, Horizon and Reverb would each run against
    #                    a local file instead of RDS.
    #   DB_DATABASE      defaults to `laravel`
    #   DB_USERNAME      defaults to `root`
    #   QUEUE_CONNECTION config/queue.php:15 defaults to `database`, which
    #                    is the quiet one: Horizon only ever supervises
    #                    REDIS queues, so with the database driver jobs are
    #                    written to a table that nothing drains. Both
    #                    supervisors would sit idle and healthy while no
    #                    queued work ran at all.
    #   CACHE_STORE      config/cache.php:17 defaults to `database`
    #   SESSION_DRIVER   config/session.php:20 defaults to `database`
    #   FILESYSTEM_DISK  config/filesystems.php:15 defaults to `local`,
    #                    which puts uploads on a Fargate task's ephemeral
    #                    disk — gone on the next nightly stop/start.
    #
    # Values are .env.production.example's, except DB_USERNAME: that file
    # predates the georag_app split and still says `georag`.
    DB_CONNECTION = "pgsql"
    DB_DATABASE   = "georag"
    DB_USERNAME   = "georag_app"

    QUEUE_CONNECTION = "redis"
    CACHE_STORE      = "redis"
    SESSION_DRIVER   = "redis"
    FILESYSTEM_DISK  = "s3"

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
    BEDROCK_EMBED_DIMENSION = local.embed_dimension

    # The same number, under the name the OTHER two readers use, so all
    # three agree by construction rather than by all defaulting to 1024:
    #
    #   services/embedding.py:69   sizes the vectors it writes, from
    #                              BEDROCK_EMBED_DIMENSION
    #   scripts/init_qdrant.py     sizes the collection it creates
    #   main.py:592                refuses to serve when the live collection
    #                              disagrees with EMBEDDING_DIMENSION
    #
    # Cohere Embed v4 is Matryoshka — 256/512/1024/1536 are all selectable —
    # so this is a knob someone can reach for. Left split, moving it would
    # have moved the writer while the guard went on comparing against a
    # hardcoded 1024 and passing.
    EMBEDDING_DIMENSION     = local.embed_dimension
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

  # ── Reverb ──────────────────────────────────────────────────────────
  # None of this may go in common_environment. config/broadcasting.php:23
  # resolves the driver to `reverb` exactly when REVERB_APP_KEY is set, and
  # its comment records why the blanket default was reverted: a bare
  # `reverb` default fatals env-less artisan contexts, because
  # Pusher::__construct(null) throws at boot. Handing REVERB_* to fastapi,
  # the hatchet worker or the sparse server buys nothing and widens that.
  #
  # REVERB_APP_KEY is a variable rather than a secret on purpose. It is
  # public by design — config/reverb.php:85 says so, the browser receives
  # it, and it is baked into the JS bundle at build time. REVERB_APP_SECRET
  # is the half that authorises publishing, and that one is in Secrets
  # Manager (see _extra_secret_ref above).

  # What laravel-octane and laravel-horizon need to PUBLISH an event.
  reverb_client_environment = {
    # Stated rather than inherited from the ternary in
    # config/broadcasting.php:23. A deploy that dropped this once produced
    # green health checks and a chat UI that hung on every query with no
    # error anywhere (2026-08-11).
    BROADCAST_CONNECTION = "reverb"

    REVERB_APP_ID  = var.reverb_app_id
    REVERB_APP_KEY = var.reverb_app_key

    # Cloud Map, not the public domain. The publish is a server-to-server
    # HTTP call inside the VPC; sending it to the ALB would hairpin out
    # through the NAT and back in to reach a task two subnets away.
    REVERB_HOST   = "laravel-reverb.${aws_service_discovery_private_dns_namespace.this.name}"
    REVERB_PORT   = 8080
    REVERB_SCHEME = "http"
  }

  # What laravel-reverb needs to SERVE the app.
  reverb_server_environment = {
    REVERB_SERVER_HOST = "0.0.0.0"
    REVERB_SERVER_PORT = 8080

    REVERB_APP_ID  = var.reverb_app_id
    REVERB_APP_KEY = var.reverb_app_key

    # The WebSocket origin allowlist. config/reverb.php:95 defaults to a
    # localhost/georag.local list for dev and its comment says to set this
    # explicitly in production — nothing did, so every upgrade from the
    # real domain would have been rejected and no query would have
    # streamed. Bare host, no scheme and no port: Reverb matches against
    # the HOST parsed out of the Origin header.
    REVERB_ALLOWED_ORIGINS = var.app_domain

    # REVERB_HOST is deliberately absent here. On the server it lands in
    # config/reverb.php:34 as `hostname`, which is not the same knob as the
    # client-side host above, and the browser arrives through the ALB with
    # the public Host header. Left null so the server does not filter on a
    # name it is not reached by.

    # REQUIRED, and the reason is specific. Both default to 10000 bytes,
    # far below a completed LLM answer frame, and Reverb silently rejects
    # an oversized payload — so the `completed` frame carrying the answer
    # text and its citations never arrives and the stream appears to stall
    # on the last token. .env.example has carried both at 1000000 since the
    # day that was diagnosed.
    REVERB_MAX_REQUEST_SIZE     = 1000000
    REVERB_APP_MAX_MESSAGE_SIZE = 1000000

    # The Redis pub/sub backplane, and what makes desired = 2 correct in
    # main.tf rather than quietly broken. Each task holds only the
    # subscribers connected to IT, and Cloud Map's MULTIVALUE record hands
    # a publisher one task at random — so with two tasks and no backplane
    # roughly half of every query's frames would be published to a task
    # with none of that query's subscribers and be dropped silently.
    # Enabled and desired count move together; changing one alone is the
    # bug.
    #
    # config/reverb.php:43-51 reads REDIS_HOST, REDIS_PORT and
    # REDIS_PASSWORD directly rather than going through
    # config/database.php, and all three are already on this task — the
    # first two from common_environment, the password from _secret_ref.
    # REDIS_DB is left at its default 0, shared with the queue and session
    # connections: pub/sub channels are not part of the keyspace, so there
    # is nothing to collide with.
    REVERB_SCALING_ENABLED = "true"
    REVERB_SCALING_CHANNEL = "reverb"
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

          # The SDK's own health server, and the reason the worker's
          # container health check can detect a HANG rather than only a
          # crash. It answers only while the event loop is turning, so a
          # worker wedged holding queue leases — busy-looking, finishing
          # nothing — fails the probe and gets replaced. compose greps
          # /proc/1/cmdline instead, which proves the process exists and
          # nothing more.
          #
          # The threshold is set EXPLICITLY. The SDK default is 5 seconds,
          # which a large embed batch can exceed without anything being
          # wrong — that would flap. 30 s is what Azure's probes.json used
          # and what the Ch 12 §6 entry describes.
          HATCHET_CLIENT_WORKER_HEALTHCHECK_ENABLED                            = "true"
          HATCHET_CLIENT_WORKER_HEALTHCHECK_PORT                               = "8001"
          HATCHET_CLIENT_WORKER_HEALTHCHECK_EVENT_LOOP_BLOCK_THRESHOLD_SECONDS = "30"
        }
        sparse = {
          # The sidecar serves the model; it must not also try to reach
          # itself over SPARSE_SERVICE_URL.
          SPARSE_SERVICE_URL = ""
        }
        laravel-octane = merge(local.reverb_client_environment, {
          FASTAPI_INTERNAL_URL = "http://fastapi.${aws_service_discovery_private_dns_namespace.this.name}:8000"
          # LOG_STACK=stderr, NOT the `single` default. On Azure the
          # default routed the application log to a file inside a
          # container nobody could read, and Ch 12 records that which
          # LOG_STACK production actually ran is not known. On ECS only
          # stdout/stderr reach CloudWatch, so this is stated.
          LOG_STACK     = "stderr"
          OCTANE_SERVER = "swoole"
        })
        # Horizon broadcasts too: the queued jobs behind a query dispatch
        # QueryStreamEvent, so it needs the publish credentials as much as
        # Octane does.
        laravel-horizon = merge(local.reverb_client_environment, {
          LOG_STACK = "stderr"
        })
        laravel-reverb = merge(local.reverb_server_environment, {
          LOG_STACK = "stderr"
        })
        qdrant = {
          # The API key arrives through `secrets`, under the name qdrant
          # itself reads. Setting it here too would put it in the task
          # definition in plaintext.
          QDRANT__STORAGE__STORAGE_PATH = "/qdrant/storage"
        }

        # The Hatchet engine had NO configuration here at all — it got the
        # common set and nothing else, and hatchet-lite reads none of that.
        # It would have failed at boot on the missing DATABASE_URL (supplied
        # through `secrets`), taking all 51 registered workflows with it:
        # every ingestion path and every cron in the platform.
        #
        # These are docker-compose.yml's hatchet-lite values, with the two
        # that cannot carry over corrected for ECS.
        hatchet = {
          SERVER_MSGQUEUE_KIND          = "postgres"
          SERVER_DEFAULT_ENGINE_VERSION = "V1"
          SERVER_GRPC_BIND_ADDRESS      = "0.0.0.0"
          SERVER_GRPC_PORT              = "7077"

          # gRPC stays plaintext, and the SG is what keeps it private: the
          # engine is not in a target group, so nothing outside the VPC can
          # reach 7077. Terminating TLS here would mean issuing and rotating
          # an internal certificate for a port only sibling tasks dial.
          SERVER_GRPC_INSECURE = "t"

          # The two that compose sets to `localhost`. A worker connects to
          # whatever the engine ADVERTISES here, so localhost would send
          # every worker back to its own task and no workflow would ever be
          # picked up. It has to be the Cloud Map name.
          SERVER_GRPC_BROADCAST_ADDRESS                          = "hatchet.${aws_service_discovery_private_dns_namespace.this.name}:7077"
          SERVER_INTERNAL_CLIENT_INTERNAL_GRPC_BROADCAST_ADDRESS = "hatchet.${aws_service_discovery_private_dns_namespace.this.name}:7077"

          SERVER_URL                  = "https://${var.app_domain}"
          SERVER_AUTH_COOKIE_DOMAIN   = var.app_domain
          SERVER_AUTH_COOKIE_INSECURE = "f"

          SERVER_AUTH_SET_EMAIL_VERIFIED = "t"
        }
      }, name, {})
    )
  }
}

# Declared here rather than in variables.tf because this file is what
# consumes them, and because the certificate and the name it certifies are
# one fact stated twice if they live apart.

variable "acm_certificate_arn" {
  description = "ACM certificate for the public HTTPS listener."
  type        = string
}

variable "app_domain" {
  description = <<-EOT
    Public hostname the ALB serves, without scheme — e.g. georag.example.com.
    Must match a name on acm_certificate_arn. Drives APP_URL and the Reverb
    WebSocket origin allowlist, and through APP_URL, Sanctum's stateful
    domain list.
  EOT
  type        = string

  validation {
    # A scheme here would produce `https://https://…` in APP_URL and an
    # origin entry that matches nothing, both of which fail at request time
    # rather than at apply time.
    condition     = !can(regex("://", var.app_domain)) && !can(regex("/", var.app_domain))
    error_message = "app_domain must be a bare hostname: no scheme, no path."
  }
}

variable "reverb_app_id" {
  description = "Reverb application id. An identifier, not a credential."
  type        = string
  default     = "georag-app"
}

variable "reverb_app_key" {
  description = <<-EOT
    Reverb application key. Public by design (config/reverb.php): the
    browser receives it and CD bakes it into the JS bundle as
    VITE_REVERB_APP_KEY, which must carry the SAME value. The secret half
    is REVERB_APP_SECRET in Secrets Manager.
  EOT
  type        = string
}
