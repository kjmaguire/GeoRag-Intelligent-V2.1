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
