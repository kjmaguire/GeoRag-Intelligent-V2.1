# IAM — task roles, execution role, scheduler role, CD role (ADR-0022).
#
# Replaces Azure's managed identities. The thing worth carrying forward is
# not the mechanism but the lesson: the two nightly scheduler jobs held
# Contributor over the whole `georag` resource group until 2026-08-23,
# which let two cron jobs delete the database. They were then narrowed to a
# custom role of `*/read` plus exactly the three write actions they use.
# The scheduler role below starts where that one ended up.

data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

# ---------------------------------------------------------------------------
# Execution role — pulls images and injects secrets. Not the app's identity.
# ---------------------------------------------------------------------------

resource "aws_iam_role" "execution" {
  name               = "${local.name}-ecs-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy_attachment" "execution_managed" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "execution_secrets" {
  statement {
    sid    = "ReadInjectedSecrets"
    effect = "Allow"
    actions = [
      "secretsmanager:GetSecretValue",
      "kms:Decrypt",
    ]
    # compact() drops the RDS master secret when the database is powered
    # off. The role itself is not gated — it is free, and recreating it on
    # every power-on would churn the trust policy for no saving.
    resources = compact([
      aws_secretsmanager_secret.app.arn,
      try(local.db.master_user_secret[0].secret_arn, ""),
    ])
  }
}

resource "aws_iam_role_policy" "execution_secrets" {
  name   = "secrets"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.execution_secrets.json
}

# ---------------------------------------------------------------------------
# Task role — the application's own identity
# ---------------------------------------------------------------------------
# This is what replaces every static credential the Azure deployment
# carried: the storage account key, the Foundry API key, and the account
# key that the Azure Files mount used and that could not be rotated without
# breaking Qdrant's mount on the next restart.

resource "aws_iam_role" "task" {
  name               = "${local.name}-ecs-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

data "aws_iam_policy_document" "task" {
  statement {
    sid    = "ObjectStorage"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:ListBucket",
      "s3:GetObjectVersion",
      "s3:AbortMultipartUpload",
    ]
    resources = concat(
      [for b in aws_s3_bucket.this : b.arn],
      [for b in aws_s3_bucket.this : "${b.arn}/*"],
    )
  }

  statement {
    sid    = "BedrockServerless"
    effect = "Allow"
    # Converse and ConverseStream are gone with ADR-0023: chat left Bedrock,
    # and a grant kept "in case someone flips LLM_BACKEND=bedrock" is a
    # standing permission for a call nothing makes. Re-add it with the
    # endpoint if that ever happens.
    actions = [
      "bedrock:InvokeModel",
      "bedrock:Rerank",
    ]
    # Scoped to the two models this deployment uses, not "*". A wildcard
    # here would let a compromised task invoke any model in the account,
    # which is a cost problem before it is a security one.
    #
    # Two, not four. Command A+ and Parse 5 were never Bedrock models —
    # they are AWS Marketplace SageMaker packages, and ADR-0023 moved both
    # to Cohere's own API rather than pay for endpoints that bill idle. The
    # `sagemaker:InvokeEndpoint` statement that sat here went with them.
    resources = [
      "arn:aws:bedrock:${local.bedrock_region}::foundation-model/${var.bedrock_embed_model_id}",
      "arn:aws:bedrock:${local.bedrock_region}::foundation-model/${var.bedrock_rerank_model_id}",
    ]
  }

  statement {
    sid    = "BedrockModelDiscovery"
    effect = "Allow"
    # Read-only catalogue listing so app.services._bedrock.discover_cohere_rerank_v4_model_id()
    # can notice the moment Bedrock adds Cohere Rerank v4, without an
    # operator having to know to go check. Deliberately its own statement,
    # not folded into BedrockServerless above: ListFoundationModels is a
    # list-the-account's-whole-catalogue action, not a per-model one, and
    # AWS does not support resource-level scoping for it — it only accepts
    # "*". Grants no invoke capability of its own; a compromised task can
    # see what models exist, not call any of them (that is still gated by
    # the two ARNs above). Absence of this permission is not a failure mode
    # either — discovery is fail-safe to "not found" if the call is denied.
    actions = [
      "bedrock:ListFoundationModels",
    ]
    resources = ["*"]
  }

  statement {
    sid    = "OwnLogs"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["${aws_cloudwatch_log_group.services.arn}:*"]
  }

  statement {
    sid    = "EfsMounts"
    effect = "Allow"
    actions = [
      "elasticfilesystem:ClientMount",
      "elasticfilesystem:ClientWrite",
    ]
    resources = [aws_efs_file_system.this.arn]
  }
}

resource "aws_iam_role_policy" "task" {
  name   = "georag"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task.json
}

# ---------------------------------------------------------------------------
# Stores role — the four containers that run somebody else's image
# ---------------------------------------------------------------------------
# qdrant, redis, martin and hatchet used to share `aws_iam_role.task` with the
# application tier, which meant four unmodified third-party images each held
# s3:DeleteObject over every bucket — bronze, bronze-raster, exports and
# backups — plus bedrock:InvokeModel. The corpus is the one irreplaceable
# thing in this deployment (data.tf: no backup workflow, S3 versioning IS the
# backup), so a container that cannot name a bucket should not be able to
# empty one.
#
# None of the four has any reason to hold those grants, and this is checkable
# rather than assumed: `local.service_environment` in config.tf gives not one
# of them an AWS_*, S3_*, BEDROCK_* or *_BUCKET variable, so there is no
# bucket name or model id in their environment to act on. Qdrant's S3 snapshot
# feature is not configured; Martin reads Postgres for MVT tiles; Hatchet's
# queue is Postgres. What they actually touch is EFS, and only two of them
# even do that.
#
# The EFS grant covers all four rather than splitting a third role for a
# one-statement difference: martin and hatchet mount nothing, and an unused
# mount permission on a filesystem they have no volume for is a far smaller
# surface than delete on the corpus.
#
# NOT extended to `sparse`, although its own docstring says it imports only
# the sparse encoder — no Settings, no DB, no Qdrant. It runs the fastapi
# image, which does carry boto3, and the saving over confirming that import
# graph against a running task is not worth a service that fails in
# production. Revisit with the first deployed build.
#
# Secrets are NOT fetched by this role. The execution role pulls every
# `valueFrom` before the container starts, which is why Martin and Hatchet can
# reach Postgres through MARTIN_DATABASE_URL and HATCHET_DATABASE_URL with an
# otherwise empty task role.

resource "aws_iam_role" "stores" {
  name               = "${local.name}-ecs-task-stores"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

data "aws_iam_policy_document" "stores" {
  statement {
    sid    = "OwnLogs"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["${aws_cloudwatch_log_group.services.arn}:*"]
  }

  statement {
    sid    = "EfsMounts"
    effect = "Allow"
    actions = [
      "elasticfilesystem:ClientMount",
      "elasticfilesystem:ClientWrite",
    ]
    resources = [aws_efs_file_system.this.arn]
  }
}

resource "aws_iam_role_policy" "stores" {
  name   = "georag"
  role   = aws_iam_role.stores.id
  policy = data.aws_iam_policy_document.stores.json
}

locals {
  # Which task role each service runs under. Anything not named here gets the
  # application role, so a service added without a thought keeps working and
  # the tightening is the deliberate act — the safe direction for a default.
  stores_role_services = toset(["qdrant", "redis", "martin", "hatchet"])

  task_role_for = {
    for name, _ in local.services :
    name => (
      contains(local.stores_role_services, name)
      ? aws_iam_role.stores.arn
      : aws_iam_role.task.arn
    )
  }
}

# ---------------------------------------------------------------------------
# Scheduler role — the nightly sweeps
# ---------------------------------------------------------------------------
# Narrow from the start. On Azure these two jobs held Contributor over the
# whole resource group until 2026-08-23, and two cron jobs deleted the
# database with it. The custom role that replaced it was `*/read` plus
# containerApps/write plus the flexible-server start/stop actions; this is
# the same shape, plus the SageMaker endpoint lifecycle the Bedrock route
# added.

resource "aws_iam_role" "scheduler_task" {
  name               = "${local.name}-scheduler-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

data "aws_iam_policy_document" "scheduler_task" {
  statement {
    sid    = "ReadEverythingItTouches"
    effect = "Allow"
    # sagemaker:DescribeEndpoint and :ListEndpoints are gone with ADR-0023.
    # The sweeps have no endpoints left to cycle, so a read grant for them
    # would outlive its only caller.
    actions = [
      "ecs:DescribeServices",
      "ecs:ListServices",
      "rds:DescribeDBInstances",
    ]
    resources = ["*"]
  }

  statement {
    sid       = "ScaleServices"
    effect    = "Allow"
    actions   = ["ecs:UpdateService"]
    resources = [for s in aws_ecs_service.this : s.id]
  }

  statement {
    sid    = "StopAndStartTheDatabase"
    effect = "Allow"
    # Deliberately NOT rds:DeleteDBInstance, rds:ModifyDBInstance or
    # anything else. This is the exact list the sweeps call, and the
    # Contributor incident is why.
    actions = ["rds:StopDBInstance", "rds:StartDBInstance"]
    # A statement with an empty resource list is invalid, so this degrades
    # to a resource that matches nothing rather than to []. The sweeps are
    # gated anyway; this only has to stay syntactically valid.
    resources = [try(local.db.arn, "arn:aws:rds:::db:none")]
  }

  # CycleMarketplaceEndpoints and UseTheRetainedEndpointConfigs were here
  # until 2026-09-15. They granted sagemaker:CreateEndpoint /
  # :DeleteEndpoint so the nightly sweeps could delete Marketplace
  # endpoints overnight and recreate them in the morning, because those
  # endpoints bill for as long as they exist.
  #
  # ADR-0023 removed the endpoints, so the grants have no caller. They are
  # deleted rather than left dormant: the scheduler role could delete a
  # SageMaker endpoint in this account, and a permission whose only
  # justification has gone is exactly what the "Contributor incident" note
  # above is about.

  statement {
    sid    = "OwnLogs"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["${aws_cloudwatch_log_group.scheduler.arn}:*"]
  }
}

resource "aws_iam_role_policy" "scheduler_task" {
  name   = "sweeps"
  role   = aws_iam_role.scheduler_task.id
  policy = data.aws_iam_policy_document.scheduler_task.json
}

# The role EventBridge Scheduler itself assumes, to launch the task.
data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "${local.name}-scheduler"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
}

data "aws_iam_policy_document" "scheduler" {
  statement {
    effect  = "Allow"
    actions = ["ecs:RunTask"]
    # The sweeps are gated with the rest of the compute. This policy
    # document is not — the role is free and keeping it stable across power
    # cycles avoids churning its trust relationship — so the resource list
    # degrades to a non-matching ARN rather than to [], which is invalid.
    resources = coalescelist(compact([
      try(one(aws_ecs_task_definition.shutdown_sweep).arn_without_revision, ""),
      try(one(aws_ecs_task_definition.startup_sweep).arn_without_revision, ""),
    ]), ["arn:aws:ecs:::task-definition/none"])
    condition {
      test     = "ArnLike"
      variable = "ecs:cluster"
      values   = [aws_ecs_cluster.this.arn]
    }
  }

  statement {
    effect    = "Allow"
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.scheduler_task.arn, aws_iam_role.execution.arn]
  }
}

resource "aws_iam_role_policy" "scheduler" {
  name   = "run-sweeps"
  role   = aws_iam_role.scheduler.id
  policy = data.aws_iam_policy_document.scheduler.json
}
