# APP_KEY rotation (ADR-0022).
#
# The successor to deploy/azure/containerapps/rotate-app-key.sh, which was
# deleted with the Azure tree on 2026-09-08 and recorded in
# ops/runbooks/secret-rotation.md §2 as a capability loss. This file is the
# infrastructure half; deploy/aws/rotation/ is the operator half.
#
# ---------------------------------------------------------------------
# WHY A TASK DEFINITION AND NOT AN EXEC
# ---------------------------------------------------------------------
# On Azure the dump and the restore ran INSIDE the single live
# laravel-octane-cc replica, over `az containerapp exec`, because that was
# the only place they could run: the plaintext dump has to sit on a disk
# between the two steps, and the only disk available was the serving
# container's. Three of the Azure runbook's eight findings are consequences
# of that one constraint — the TTY wrapper, the probe-safe `down
# --status=200`, and "the dump and the restore must happen in one exec
# session".
#
# ECS Exec is not enabled on this cluster (no `enable_execute_command` in
# services.tf, and no ssmmessages grant in iam.tf), and it is not enabled
# here either. It does not need to be: a one-off task is the AWS-native
# shape for this, the same shape aws_ecs_task_definition.migrate already
# uses for migrations. The dump and the restore run in ONE task, on that
# task's own ephemeral disk, which Fargate destroys when the task stops.
# No serving container ever holds plaintext PII.
#
# ---------------------------------------------------------------------
# WHY APP_KEY_NEXT IS A SECRET AND NOT AN OVERRIDE
# ---------------------------------------------------------------------
# The new key has to reach this task without passing through a terminal, a
# log, or an API parameter. ECS `containerOverrides` supports `environment`
# but NOT `secrets`, so anything handed to the task at RunTask time is
# plaintext in the RunTask request — which CloudTrail records. Hence a
# second injected secret: the operator script writes the minted key to the
# APP_KEY_NEXT key of the app secret BEFORE starting the task, and the
# execution role injects it the same way it injects every other secret.
#
# The key therefore exists in exactly two places for the duration of a
# rotation — Secrets Manager, and the memory of this one task. It never
# reaches the operator's terminal at all. That is a real improvement on the
# Azure script, which minted the key inside the replica, read it back over
# stdout, and wrote it 0600 to the operator's laptop because a variable was
# otherwise its only copy (secret-rotation.md §2, finding 6). Secrets
# Manager IS that copy now, with a 30-day recovery window (config.tf).
#
# ---------------------------------------------------------------------
# THIS TASK DEFINITION CANNOT BE RUN OUTSIDE A ROTATION
# ---------------------------------------------------------------------
# APP_KEY_NEXT does not exist in the app secret except while a rotation is
# in flight. ECS fails a task whose referenced secret key is absent, so
# running this at any other time fails at task start, before the container
# exists. That is deliberate: a task definition whose whole job is to
# re-encrypt the audit ledger should not be startable by accident.
#
# Terraform registers it anyway, because registration does not resolve
# secrets — only task start does.
resource "aws_ecs_task_definition" "app_key_rotation" {
  family                   = "${local.name}-app-key-rotation"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"

  # Sized above laravel-octane deliberately. audit:dump-pii streams
  # query_audit_log in chunks so memory is bounded, but it decrypts and
  # re-encrypts every row in the table twice, single-threaded, and the task
  # runs with nothing serving traffic — so the only thing this sizing costs
  # is the minutes it saves.
  cpu                = 2048
  memory             = 4096
  execution_role_arn = aws_iam_role.execution.arn
  task_role_arn      = aws_iam_role.task.arn

  # The dump is JSONL plaintext of every audit row. Fargate's default is 20
  # GiB, which is ample, but it is a default rather than a promise and this
  # is the one task whose failure mode on a full disk is a half-written
  # dump. Stated, so a growing ledger runs out of headroom in review rather
  # than at 3am. Raise it before the table gets near it.
  ephemeral_storage {
    size_in_gib = 21
  }

  container_definitions = jsonencode([{
    name       = "app-key-rotation"
    essential  = true
    image      = "${aws_ecr_repository.this["laravel"].repository_url}:latest"
    entryPoint = ["/bin/sh", "-c"]

    # The script itself arrives as a containerOverrides command from
    # deploy/aws/rotation/rotate-app-key.sh, base64'd. It contains no
    # secrets — both keys arrive through the environment below — so it is
    # safe in a CloudTrail record of the RunTask call.
    #
    # This default exists so that a task started WITHOUT that override does
    # nothing and says why, rather than inheriting the image's Octane
    # entrypoint and quietly starting a web server that nothing routes to.
    command = [
      "echo 'app-key-rotation: no command override supplied. Start this through deploy/aws/rotation/rotate-app-key.sh, never by hand.' >&2; exit 64",
    ]

    # laravel-octane's environment verbatim: the same DB, Redis and cache
    # wiring, because this is the same application reading the same rows.
    environment = [
      for k, v in local.service_environment["laravel-octane"] : { name = k, value = tostring(v) }
    ]

    # The common set gives APP_KEY — the CURRENT key, which is what the
    # dump must run under. APP_KEY_NEXT is the one the restore re-encrypts
    # to. Two keys in one task is the whole point of the design: it is what
    # lets the re-encryption happen without either key touching a person.
    secrets = concat(local.service_secrets["laravel-octane"], [{
      name      = "APP_KEY_NEXT"
      valueFrom = "${aws_secretsmanager_secret.app.arn}:APP_KEY_NEXT::"
    }])

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"  = aws_cloudwatch_log_group.services.name
        "awslogs-region" = var.region
        # A stream prefix of its own so a rotation is greppable a year
        # later. The runbook asks for the task ARN to be recorded in
        # authz_audit; this is how you find what it did.
        "awslogs-stream-prefix" = "app-key-rotation"
      }
    }
  }])
}
