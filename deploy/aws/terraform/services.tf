# ALB, task definitions, ECS services (ADR-0022).
#
# Ten services, one of them public. Task definitions here carry only the
# shape — image, resources, mounts, log config, health check — and get
# their environment from Secrets Manager plus the SSM parameters in
# config.tf. The point is that NOTHING is set by hand: the ~55 env vars per
# app that the Azure deployment set in the portal, and that drifted freely
# from .env.production.example, are the gap this file exists to close.

resource "aws_cloudwatch_log_group" "services" {
  name              = "/ecs/${local.name}"
  retention_in_days = 30
}

resource "aws_cloudwatch_log_group" "scheduler" {
  name              = "/ecs/${local.name}/scheduler"
  retention_in_days = 90
}

# ---------------------------------------------------------------------------
# Load balancer
# ---------------------------------------------------------------------------

resource "aws_lb" "this" {
  count = local.on

  name               = local.name
  load_balancer_type = "application"
  subnets            = aws_subnet.public[*].id
  security_groups    = [aws_security_group.alb.id]

  # Reverb holds WebSocket connections open. The default 60s idle timeout
  # closes them between heartbeats, which the client reads as a dropped
  # connection and reconnects through — a reconnect storm that looks like
  # a Reverb fault and is not.
  idle_timeout = 300

  drop_invalid_header_fields = true
}

resource "aws_lb_target_group" "octane" {
  count = local.on

  name        = "${local.name}-octane"
  port        = 80
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = aws_vpc.this.id

  health_check {
    path                = "/up"
    healthy_threshold   = 2
    unhealthy_threshold = 3
    timeout             = 5
    interval            = 30
    matcher             = "200"
  }

  # Octane keeps no per-request state — session, cache and queue are all
  # Redis — so requests need not pin to a task. This is the difference
  # between it and Reverb below.
  deregistration_delay = 30
}

resource "aws_lb_target_group" "reverb" {
  count = local.on

  name        = "${local.name}-reverb"
  port        = 8080
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = aws_vpc.this.id

  health_check {
    path                = "/up"
    healthy_threshold   = 2
    unhealthy_threshold = 3
    timeout             = 5
    interval            = 30
    matcher             = "200,404"
  }

  # A WebSocket lives on ONE task for its whole life, and there are two of
  # them now (main.tf). What makes a reconnect onto the other task safe is
  # REVERB_SCALING_ENABLED, not this: the Redis backplane means either task
  # can serve any subscriber. Stickiness is kept because it keeps a
  # returning client on the task that already holds its subscriptions and
  # saves the fan-out hop — a preference, not a correctness guarantee.
  #
  # It was load-bearing before the backplane existed, and the comment here
  # used to say so.
  stickiness {
    type            = "lb_cookie"
    enabled         = true
    cookie_duration = 86400
  }
}

resource "aws_lb_listener" "https" {
  count = local.on * local.alb_edge

  load_balancer_arn = aws_lb.this[0].arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"

  # local.certificate_arn, not var.acm_certificate_arn: with manage_dns on,
  # this resolves through aws_acm_certificate_validation, so the listener is
  # not created until ACM reports the certificate ISSUED. Referencing the
  # certificate directly would let the attach race validation.
  certificate_arn = local.certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.octane[0].arn
  }

  # `acm_certificate_arn` has a default now, so aws-preflight.sh A-01 — which
  # reports variables with no default and no value — no longer covers it. That
  # is correct for the normal path, where dns.tf issues the certificate and
  # there is nothing for an operator to supply. It leaves exactly one hole:
  # manage_dns = false with no certificate brought in its place, where
  # local.certificate_arn is null and the attach fails at apply, after the
  # VPC, the RDS instance and the ALB already exist.
  lifecycle {
    precondition {
      condition     = var.manage_dns || var.acm_certificate_arn != ""
      error_message = "manage_dns is false, so Terraform issues no certificate — set acm_certificate_arn to one that certifies app_domain and lives in var.region, or set manage_dns = true to have Route 53 issue it."
    }

    # app_domain gained a default of "" so that edge = "cloudfront" needs no
    # domain. That takes it out of aws-preflight.sh A-01 as well, so this is
    # what catches an "alb" edge with nothing to certify — at plan time,
    # rather than at the ACM call after the VPC and RDS already exist.
    precondition {
      condition     = var.app_domain != ""
      error_message = "edge = \"alb\" serves a certificate for app_domain, which is empty. Set app_domain to the hostname you own, or leave edge = \"cloudfront\" to be served on the distribution's own *.cloudfront.net name with no domain at all."
    }
  }
}

# The WebSocket path, on whichever listener this edge created. `one()` of an
# empty list is null, so coalesce picks the mode's listener without either
# branch indexing a resource that does not exist.
resource "aws_lb_listener_rule" "reverb" {
  count = local.on

  listener_arn = coalesce(
    one(aws_lb_listener.origin[*].arn),
    one(aws_lb_listener.https[*].arn),
  )
  priority = 10

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.reverb[0].arn
  }

  condition {
    path_pattern {
      values = ["/app/*", "/apps/*"]
    }
  }

  # In CloudFront mode with a secret set, the listener's DEFAULT action is a
  # 403 and every rule must re-prove the request came from our distribution.
  # Without this the WebSocket path would be the one unauthenticated hole
  # through the origin check.
  dynamic "condition" {
    for_each = local.cf == 1 && var.cloudfront_origin_secret != "" ? [1] : []
    content {
      http_header {
        http_header_name = "X-Origin-Verify"
        values           = [var.cloudfront_origin_secret]
      }
    }
  }
}

# ---------------------------------------------------------------------------
# The CloudFront-mode listener
# ---------------------------------------------------------------------------
# Plain HTTP, because the load balancer has no certificate when there is no
# domain. It is not open to the internet: the security group in main.tf admits
# only the CloudFront origin-facing prefix list, and with
# `cloudfront_origin_secret` set the default action refuses anything that does
# not carry the header our distribution adds.
resource "aws_lb_listener" "origin" {
  count = local.on * local.cf

  load_balancer_arn = aws_lb.this[0].arn
  port              = 80
  protocol          = "HTTP"

  dynamic "default_action" {
    for_each = var.cloudfront_origin_secret == "" ? [1] : []
    content {
      type             = "forward"
      target_group_arn = aws_lb_target_group.octane[0].arn
    }
  }

  dynamic "default_action" {
    for_each = var.cloudfront_origin_secret != "" ? [1] : []
    content {
      type = "fixed-response"
      fixed_response {
        content_type = "text/plain"
        message_body = "Direct origin access is refused. Reach this service through its CloudFront distribution."
        status_code  = "403"
      }
    }
  }
}

# Only exists when the default action above is the 403: this is what lets a
# verified request through to the application.
resource "aws_lb_listener_rule" "origin_verified" {
  count = local.on * local.cf * (var.cloudfront_origin_secret != "" ? 1 : 0)

  listener_arn = aws_lb_listener.origin[0].arn
  priority     = 20

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.octane[0].arn
  }

  condition {
    http_header {
      http_header_name = "X-Origin-Verify"
      values           = [var.cloudfront_origin_secret]
    }
  }
}

resource "aws_lb_listener" "http_redirect" {
  count = local.on * local.alb_edge

  load_balancer_arn = aws_lb.this[0].arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "redirect"
    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }
}

# ---------------------------------------------------------------------------
# Task definitions
# ---------------------------------------------------------------------------

locals {
  # Which image each service runs. Three images, ten services — the same
  # relationship compose has, where laravel-octane/horizon/reverb and
  # fastapi/hatchet-worker/sparse each share a build.
  service_image = {
    laravel-octane  = "laravel"
    laravel-horizon = "laravel"
    laravel-reverb  = "laravel"
    fastapi         = "fastapi"
    hatchet-worker  = "fastapi"
    sparse          = "fastapi"
    martin          = "martin"
  }

  # Services running a third-party image rather than one of ours.
  external_image = {
    hatchet = "ghcr.io/hatchet-dev/hatchet/hatchet-lite:v0.86.12"
    qdrant  = "qdrant/qdrant:v1.17.1"
    redis   = "redis:8.10.0-alpine"
  }

  service_command = {
    laravel-octane  = ["php", "artisan", "octane:start", "--host=0.0.0.0", "--port=80"]
    laravel-horizon = ["php", "artisan", "horizon"]
    laravel-reverb  = ["php", "artisan", "reverb:start", "--host=0.0.0.0", "--port=8080"]
    hatchet-worker  = ["python", "-m", "app.hatchet_workflows.worker"]
    sparse          = ["uvicorn", "app.sparse_service:app", "--host", "0.0.0.0", "--port", "8000"]
    # AOF ON and a volume — the two halves of the fix for the one store in
    # this platform that genuinely had no persistence. `volatile-lru` is
    # non-negotiable and enforced by scripts/check_redis_manifests.py:
    # one instance holds queue jobs with no TTL beside TTL'd cache and
    # sessions, and `allkeys-lru` would evict a queued job.
    # Run through a shell ONLY so "$REDIS_PASSWORD" expands: ECS runs a
    # command array with no shell, so the literal string would otherwise be
    # handed to redis-server as the password. `exec` keeps redis-server as
    # PID 1 so it still receives SIGTERM directly on task stop — which
    # matters here, because that is what flushes the AOF cleanly on every
    # one of the nightly shutdowns.
    redis = [
      "sh", "-c",
      join(" ", [
        "exec redis-server",
        # Every other topology in this repository sets this — compose
        # (docker-compose.yml:403), the Helm chart, and all three
        # kubernetes/manifests variants. ECS was the only one that did not,
        # so this store would have come up with NO authentication while
        # every client is configured to send AUTH. Redis answers AUTH on a
        # server with no requirepass with an error, so the effect was not a
        # weaker Redis, it was no working Redis: cache, sessions, queues,
        # Horizon and the Reverb backplane all fail on connect.
        "--requirepass \"$REDIS_PASSWORD\"",
        "--appendonly yes",
        "--appendfsync everysec",
        # Explicit, not omitted. Silence is NOT "off": Redis's built-in save
        # points stay active, so an AOF-only intent quietly runs RDB
        # snapshots as well. Dropping this line is precisely half of what
        # the Azure lift got wrong — it inherited `--appendonly yes` from
        # compose, did not inherit the volume, and did not inherit this
        # either. scripts/check_redis_manifests.py enforces both halves.
        "--save ''",
        "--maxmemory 384mb",
        "--maxmemory-policy volatile-lru",
        "--databases 4",
      ]),
    ]
  }

  service_port = {
    laravel-octane = 80
    laravel-reverb = 8080
    fastapi        = 8000
    sparse         = 8000
    hatchet        = 7077
    qdrant         = 6333
    redis          = 6379
    martin         = 3000
  }
}

# ---------------------------------------------------------------------------
# Container health checks
# ---------------------------------------------------------------------------
# The ALB health-checks the two services behind it. Everything else — nine
# containers, including every store and both Hatchet halves — had NO liveness
# signal at all in the first draft of this deployment. Azure did better: it
# carried per-app probes in `probes.json`, and that file went with the Azure
# tree.
#
# What that costs is not theoretical. ECS only replaces a task whose PROCESS
# has exited; a process that is running and wedged is a healthy task forever.
# The Hatchet worker is the case that matters most, and Ch 12 §6 already named
# it: the SDK health server is "the probe that catches a hung worker holding
# its queue lease". A wedged worker holds leases, finishes nothing, and looks
# entirely fine — the exact symptom the on-call runbook's "ingestion has
# stopped moving" section sends you hunting for in the logs.
#
# These are the compose commands, unchanged, because they are already proven
# against these exact images. Two are worth reading twice:
#
#   qdrant  — speaks /readyz over bash's /dev/tcp because the image has no
#             curl and no wget. Do not "simplify" it to curl.
#   worker  — compose greps /proc/1/cmdline, which proves only that the
#             process exists. Production asks the SDK's health server
#             instead, which answers only while the event loop is turning
#             (config.tf enables it). That is the whole difference between
#             detecting a crash and detecting a hang.
locals {
  service_healthcheck = {
    # -a, because requirepass is now set: an unauthenticated PING answers
    # NOAUTH, which would fail this check forever and put the task in a
    # restart loop that looks like a Redis fault rather than a config one.
    redis   = ["CMD-SHELL", "redis-cli -a \"$REDIS_PASSWORD\" --no-auth-warning ping | grep -q PONG"]
    qdrant  = ["CMD", "bash", "-c", "exec 3<>/dev/tcp/localhost/6333 && printf 'GET /readyz HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n' >&3 && grep -q '200 OK' <&3"]
    martin  = ["CMD", "wget", "--spider", "-q", "http://127.0.0.1:3000/health"]
    hatchet = ["CMD-SHELL", "wget -q -O - http://localhost:8888/api/ready >/dev/null 2>&1 || exit 1"]
    # curl, NOT wget. This runs the FASTAPI image, which installs curl and has
    # never carried wget (docker/fastapi.Dockerfile — zero occurrences; its own
    # HEALTHCHECK uses curl, and the runtime stage is python:3.13-slim, which
    # ships neither). The wget form was copied from the hatchet-engine and
    # martin checks above, whose images do have it.
    #
    # It could never have passed. `sh -c` reports "wget: not found" and exits
    # 127, the `|| exit 1` fires, the container is marked UNHEALTHY, and since
    # essential = true ECS stops and replaces the task — forever. The check
    # written specifically to catch a HUNG worker holding its queue lease would
    # instead have killed a perfectly healthy one every few minutes, and the
    # symptom (a worker that keeps restarting) looks like the hang it was
    # meant to detect.
    hatchet-worker  = ["CMD", "curl", "-f", "http://localhost:8001/health"]
    fastapi         = ["CMD", "curl", "-f", "http://localhost:8000/health"]
    sparse          = ["CMD", "curl", "-f", "http://localhost:8000/health"]
    laravel-octane  = ["CMD", "curl", "-f", "http://localhost:80/up"]
    laravel-horizon = ["CMD-SHELL", "php artisan horizon:status | grep -qE 'running|paused' || exit 1"]
    laravel-reverb  = ["CMD", "curl", "-f", "http://localhost:8080/up"]
  }

  # startPeriod is the grace before failures count. Hatchet runs its own
  # schema migration on boot (Azure allowed 45 s); the Laravel and FastAPI
  # images boot slower than the stores.
  healthcheck_start_period = {
    hatchet         = 90
    hatchet-worker  = 60
    fastapi         = 60
    laravel-octane  = 60
    laravel-horizon = 60
    laravel-reverb  = 60

    # Both of these were absent, so `lookup(..., 30)` below gave them 30s —
    # a 2x and 4x cut from the only numbers anyone has measured, on the two
    # services with the slowest honest starts.
    #
    # martin answers /health only once its sources are loaded and validated,
    # which on this config means after it has resolved all 19 PostGIS tile
    # functions. compose allows 60s and says why.
    #
    # sparse force-loads SPLADE++ on /health and 503s until it is in memory.
    # compose allows 120s. The weights are baked into the image, so this is
    # load time and not download time — but 30s of grace plus 3 failed 30s
    # intervals is ~120s to the kill, i.e. the measured time is the deadline
    # rather than comfortably inside it. A restart loop here presents as
    # retrieval quietly returning nothing, which is the hardest failure in
    # this system to attribute.
    martin = 60
    sparse = 120
  }
}

resource "aws_ecs_task_definition" "this" {
  for_each = local.on == 1 ? local.services : {}

  family                   = "${local.name}-${each.key}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = each.value.cpu
  memory                   = each.value.memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = local.task_role_for[each.key]

  dynamic "volume" {
    for_each = contains(["qdrant", "redis"], each.key) ? [each.key] : []
    content {
      name = volume.value
      efs_volume_configuration {
        file_system_id     = aws_efs_file_system.this.id
        transit_encryption = "ENABLED"
        authorization_config {
          access_point_id = volume.value == "qdrant" ? aws_efs_access_point.qdrant.id : aws_efs_access_point.redis.id
          iam             = "ENABLED"
        }
      }
    }
  }

  container_definitions = jsonencode([
    merge(
      {
        name      = each.key
        essential = true
        image = lookup(
          local.external_image,
          each.key,
          "${aws_ecr_repository.this[lookup(local.service_image, each.key, "fastapi")].repository_url}:${var.image_tag}",
        )
        environment = [
          for k, v in local.service_environment[each.key] : { name = k, value = tostring(v) }
        ]
        secrets = local.service_secrets[each.key]
        logConfiguration = {
          logDriver = "awslogs"
          options = {
            "awslogs-group"         = aws_cloudwatch_log_group.services.name
            "awslogs-region"        = var.region
            "awslogs-stream-prefix" = each.key
          }
        }
      },
      lookup(local.service_command, each.key, null) != null ? {
        command = local.service_command[each.key]
      } : {},
      lookup(local.service_port, each.key, null) != null ? {
        portMappings = [{
          containerPort = local.service_port[each.key]
          protocol      = "tcp"
        }]
      } : {},
      contains(["qdrant", "redis"], each.key) ? {
        mountPoints = [{
          sourceVolume  = each.key
          containerPath = each.key == "qdrant" ? "/qdrant/storage" : "/data"
          readOnly      = false
        }]
      } : {},
      lookup(local.service_healthcheck, each.key, null) != null ? {
        healthCheck = {
          command     = local.service_healthcheck[each.key]
          interval    = 30
          timeout     = 5
          retries     = 3
          startPeriod = lookup(local.healthcheck_start_period, each.key, 30)
        }
      } : {},
    )
  ])
}

# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------

resource "aws_ecs_service" "this" {
  for_each = local.on == 1 ? local.services : {}

  name            = each.key
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.this[each.key].arn
  desired_count   = each.value.desired
  # `launch_type` and `capacity_provider_strategy` are mutually exclusive, so
  # this replaces it. local.capacity_for is defined in spot.tf and resolves
  # per service, so one name can sit on on-demand while the rest run Spot.
  capacity_provider_strategy {
    capacity_provider = local.capacity_for[each.key]
    weight            = 1
  }

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.tasks.id]
    assign_public_ip = false
  }

  service_registries {
    registry_arn = aws_service_discovery_service.this[each.key].arn
  }

  dynamic "load_balancer" {
    for_each = each.key == "laravel-octane" ? [1] : []
    content {
      target_group_arn = aws_lb_target_group.octane[0].arn
      container_name   = each.key
      container_port   = 80
    }
  }

  dynamic "load_balancer" {
    for_each = each.key == "laravel-reverb" ? [1] : []
    content {
      target_group_arn = aws_lb_target_group.reverb[0].arn
      container_name   = each.key
      container_port   = 8080
    }
  }

  # Replaces the hand-rolled "roll back to the previously-active digest"
  # logic in the Azure CD workflow, which had to shell out to read the
  # previous image and could only ever be best-effort.
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  # A single-task service cannot do a rolling deploy without going to zero, so
  # the eight at desired 1 accept a gap. The two at desired 2 must not, and
  # until 2026-09-16 only Octane was configured that way.
  #
  # Reverb was the omission, and it defeated the reason it runs two tasks at
  # all. main.tf says it plainly: "at desired 1 every deploy and every task
  # replacement drops every open WebSocket, which on this platform means every
  # in-flight answer stream." With min 0 / max 100 ECS may stop BOTH tasks
  # before starting a replacement, so a routine deploy of the laravel image
  # could drop every live answer stream — the precise failure the second task
  # was paid for to prevent.
  deployment_minimum_healthy_percent = contains(local.zero_downtime_services, each.key) ? 50 : 0
  deployment_maximum_percent         = contains(local.zero_downtime_services, each.key) ? 200 : 100

  # The nightly sweeps own desired_count between 17:00 and 08:30 local.
  # Without this, every `terraform apply` during the window would start
  # the whole platform back up and quietly undo the cost saving.
  lifecycle {
    ignore_changes = [desired_count, task_definition]
  }

  depends_on = [aws_lb_listener.https]
}

# ---------------------------------------------------------------------------
# Schema task — migrations AND the raw SQL layer
# ---------------------------------------------------------------------------
# Run by CD before any service rolls. CD overrides the image to the SHA
# being deployed; everything else about the task lives here.
#
# THE `db:apply-raw` HALF IS THE POINT. Azure CD ran `php artisan migrate`
# and nothing else, so every object created only in `database/raw/` has
# never existed in production — while live code queries several of them
# (routers/interpretation.py reads all four interpretation.* tables; the
# phase0 ops agents read silver.corpus_health_findings and
# silver.store_reconciliation_findings; app/agent/egress_gate.py reads
# silver.workspace_settings). `scripts/raw-parity-baseline.txt` is the
# running list and `ops/runbooks/raw-sql-layer.md` the procedure.
#
# Order matters: migrations first, raw second. Several raw files are
# guarded on tables the migrations create, and every file in
# `database/raw/manifest.json` earns its place by being idempotent and
# re-run-safe — so running it every deploy is cheap, and running it never
# is how the gap opened.
#
# `--database=pgsql_migrations` is the connection the runbook specifies:
# the same one CD's migrations use, with the ownership split the
# `bootstrap` role needs. `db:apply-raw` has no `--force`; it is not
# interactive.
#
# ONE CAUTION, from ops/runbooks/raw-sql-layer.md. The phase-0 tenancy
# files in the manifest add columns, backfill them, flip them NOT NULL,
# add FK CASCADEs and enable RLS — they change DATA, not just shape. The
# runbook says to dry-run (`--pretend`) against any database that has not
# had them applied before, and to do the first real apply in a maintenance
# window with a confirmed restore point. On a FRESH deployment, which is
# what this migration is, there is no data to change and that caution does
# not bind. It binds again the first time this runs against a populated
# database.
resource "aws_ecs_task_definition" "migrate" {
  count = local.on

  family                   = "${local.name}-migrate"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 1024
  memory                   = 2048
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([{
    name       = "migrate"
    essential  = true
    image      = "${aws_ecr_repository.this["laravel"].repository_url}:${var.image_tag}"
    entryPoint = ["/bin/sh", "-c"]
    command = [
      # --database=pgsql_migrations on BOTH commands, not just db:apply-raw.
      # Verified live on a go-live rehearsal (2026-09-18): MIGRATE_DB_CONNECTION
      # below does NOT do what its own comment says. Laravel's
      # MigrationServiceProvider reads config('database.migrations') only for
      # the ['table'] key (vendor/laravel/framework/.../MigrationServiceProvider.php);
      # it never looks at ['connection']. MigrateCommand separately resolves
      # its connection from $this->option('database') — the --database CLI
      # flag — not from config at all. So `php artisan migrate --force` alone
      # ran on the default `pgsql` connection as georag_app and failed with
      # "permission denied for schema public" trying to create the
      # `migrations` tracking table, exactly the failure mode the comment
      # below anticipated but the config it points at cannot prevent.
      "php artisan migrate --force --database=pgsql_migrations && php artisan db:apply-raw --database=pgsql_migrations",
    ]
    environment = [
      for k, v in merge(local.service_environment["laravel-octane"], {
        # Flips config/database.php's migrations.connection resolver to the
        # `pgsql_migrations` entry, which connects DIRECTLY rather than
        # through a pooler and as the owner role rather than the runtime
        # one. Unset, migrations silently run on `pgsql` as georag_app —
        # which lacks CREATE on the schemas and would fail late rather
        # than loudly.
        MIGRATE_DB_CONNECTION = "pgsql_migrations"
        MIGRATE_DB_HOST       = local.db_host
        MIGRATE_DB_PORT       = 5432
        MIGRATE_DB_USERNAME   = "georag"
      }) : { name = k, value = tostring(v) }
    ]
    secrets = concat(local.service_secrets["laravel-octane"], [{
      # RDS generates and rotates the master password into its own secret;
      # nothing in this repository or in CI ever holds it.
      name      = "MIGRATE_DB_PASSWORD"
      valueFrom = "${local.db.master_user_secret[0].secret_arn}:password::"
    }])
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.services.name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = "migrate"
      }
    }
  }])
}
