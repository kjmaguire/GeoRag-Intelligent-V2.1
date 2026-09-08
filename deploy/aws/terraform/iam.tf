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
    resources = [
      aws_secretsmanager_secret.app.arn,
      aws_db_instance.this.master_user_secret[0].secret_arn,
    ]
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
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
      "bedrock:Converse",
      "bedrock:ConverseStream",
      "bedrock:Rerank",
    ]
    # Scoped to the four models this deployment uses, not "*". A wildcard
    # here would let a compromised task invoke any model in the account,
    # which is a cost problem before it is a security one.
    resources = [
      "arn:aws:bedrock:${local.bedrock_region}::foundation-model/${var.bedrock_embed_model_id}",
      "arn:aws:bedrock:${local.bedrock_region}::foundation-model/${var.bedrock_rerank_model_id}",
      "arn:aws:sagemaker:${local.bedrock_region}:${data.aws_caller_identity.current.account_id}:endpoint/${var.bedrock_chat_endpoint_name}",
      "arn:aws:sagemaker:${local.bedrock_region}:${data.aws_caller_identity.current.account_id}:endpoint/${var.bedrock_parse_endpoint_name}",
    ]
  }

  statement {
    sid     = "InvokeMarketplaceEndpoints"
    effect  = "Allow"
    actions = ["sagemaker:InvokeEndpoint"]
    resources = [
      "arn:aws:sagemaker:${local.bedrock_region}:${data.aws_caller_identity.current.account_id}:endpoint/${var.bedrock_chat_endpoint_name}",
      "arn:aws:sagemaker:${local.bedrock_region}:${data.aws_caller_identity.current.account_id}:endpoint/${var.bedrock_parse_endpoint_name}",
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
    actions = [
      "ecs:DescribeServices",
      "ecs:ListServices",
      "rds:DescribeDBInstances",
      "sagemaker:DescribeEndpoint",
      "sagemaker:ListEndpoints",
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
    actions   = ["rds:StopDBInstance", "rds:StartDBInstance"]
    resources = [aws_db_instance.this.arn]
  }

  statement {
    sid    = "CycleMarketplaceEndpoints"
    effect = "Allow"
    # Create and delete only. NOT UpdateEndpoint, and NOT anything against
    # the endpoint CONFIGS — those are retained across the nightly cycle
    # precisely so the sweep never has to recreate them, and a sweep that
    # could delete one would turn a bad night into a rebuild.
    actions = ["sagemaker:CreateEndpoint", "sagemaker:DeleteEndpoint"]
    resources = [
      "arn:aws:sagemaker:${local.bedrock_region}:${data.aws_caller_identity.current.account_id}:endpoint/${var.bedrock_chat_endpoint_name}",
      "arn:aws:sagemaker:${local.bedrock_region}:${data.aws_caller_identity.current.account_id}:endpoint/${var.bedrock_parse_endpoint_name}",
    ]
  }

  statement {
    sid       = "UseTheRetainedEndpointConfigs"
    effect    = "Allow"
    actions   = ["sagemaker:DescribeEndpointConfig"]
    resources = ["arn:aws:sagemaker:${local.bedrock_region}:${data.aws_caller_identity.current.account_id}:endpoint-config/*"]
  }

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
    resources = [
      aws_ecs_task_definition.shutdown_sweep.arn_without_revision,
      aws_ecs_task_definition.startup_sweep.arn_without_revision,
    ]
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
