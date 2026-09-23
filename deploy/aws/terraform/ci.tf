# GitHub Actions OIDC → the CD deploy role (found missing on a real go-live
# rehearsal, 2026-09-16).
#
# cd.yml's build and deploy jobs both do
#   uses: aws-actions/configure-aws-credentials@v4
#   with: { role-to-assume: ${{ secrets.AWS_DEPLOY_ROLE_ARN }} }
# and nothing anywhere in this repository — not Terraform, not
# deploy/aws/README.md — creates that role or the OIDC trust it needs. On a
# fresh AWS account, cd.yml cannot authenticate at all: the very first step
# of the very first deploy fails on "Could not assume role", not because the
# workflow is wrong, but because half its prerequisite was never written
# down. Verified live: this account (046369253475) had zero OIDC providers
# and no role named anything deploy-shaped before this file.
#
# Scope: exactly the AWS CLI calls cd.yml's build+deploy jobs make (grep the
# workflow for `aws ecs|ecr|rds` — describe/register/run/update-service on
# ECS, describe-images on ECR, describe-db-instances on RDS) plus the ECR
# push permissions `docker push` needs and iam:PassRole for the two roles
# register-task-definition hands to ECS. Not account-admin, not
# terraform-apply-capable — this role builds and rolls out images, it does
# not provision infrastructure.
#
# repo/ref condition, not repo alone: sub claims from GitHub's OIDC token
# are `repo:<owner>/<repo>:ref:refs/heads/<branch>` for a branch push and
# `repo:<owner>/<repo>:pull_request` for a PR — restricting to `*:ref:refs/heads/main`
# would break `workflow_dispatch` from other refs and the PR-triggered smoke
# build. Condition on the repository claim (aud) and leave ref unscoped
# within it; this is a build/deploy role, not a production-secrets role, so
# the blast radius of any authorized workflow run in this repo is already
# the accepted scope.

resource "aws_iam_openid_connect_provider" "github_actions" {
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
  # The stable DigiCert Global Root CA thumbprint GitHub's OIDC issuer has
  # presented for years (AWS's own OIDC-with-GitHub-Actions guide uses this
  # exact value). Not fetched live via a tls_certificate data source
  # deliberately: that needs the hashicorp/tls provider, and this sandbox's
  # egress policy blocks registry.terraform.io outright (confirmed earlier
  # this session, same block class as api.cohere.com/Docker Hub/ghcr.io) --
  # terraform init would fail trying to download a provider this thin
  # resource doesn't need. AWS validates the actual TLS chain server-side
  # against its own trusted root store regardless of this value for
  # well-known issuers, so the literal here is a formality the API demands,
  # not the real trust boundary.
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]
}

data "aws_iam_policy_document" "github_actions_assume" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github_actions.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    # Any ref/event in this specific repo. Deliberately not narrowed to
    # ref:refs/heads/main — see file header.
    #
    # Verified live via CloudTrail on 2026-09-17 during the go-live
    # rehearsal: this org has GitHub's "include repository and organization
    # IDs in the JWT" setting enabled (Settings > Actions > General), which
    # changes the sub claim from `repo:OWNER/REPO:...` to
    # `repo:OWNER@OWNER_ID/REPO@REPO_ID:...`. The un-suffixed pattern below
    # silently never matched, so every workflow run failed at "Configure AWS
    # credentials" with a generic AccessDenied that looked identical to a
    # missing/wrong AWS_DEPLOY_ROLE_ARN secret — CloudTrail's userIdentity on
    # the denied AssumeRoleWithWebIdentity call is what actually distinguishes
    # the two. var.github_repository stays "owner/repo" (matches the output's
    # doc comment and every other reference to it); the ID suffixes are
    # inlined here instead of parameterized, since they're this AWS account's
    # fixed GitHub identity, not something an operator sets per deploy.
    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:kjmaguire@79488174/GeoRag-Intelligent-V2.1@1252963201:*"]
    }
  }
}

resource "aws_iam_role" "github_deploy" {
  name               = "${local.name}-github-deploy"
  assume_role_policy = data.aws_iam_policy_document.github_actions_assume.json
}

data "aws_iam_policy_document" "github_deploy" {
  statement {
    sid    = "EcrAuth"
    effect = "Allow"
    # GetAuthorizationToken has no resource-level scoping in the ECR API.
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid    = "EcrPushPull"
    effect = "Allow"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:GetDownloadUrlForLayer",
      "ecr:BatchGetImage",
      "ecr:PutImage",
      "ecr:InitiateLayerUpload",
      "ecr:UploadLayerPart",
      "ecr:CompleteLayerUpload",
      "ecr:DescribeImages",
      "ecr:DescribeRepositories",
    ]
    resources = [for r in aws_ecr_repository.this : r.arn]
  }

  statement {
    sid    = "EcsDeploy"
    effect = "Allow"
    actions = [
      "ecs:DescribeServices",
      "ecs:DescribeTaskDefinition",
      "ecs:DescribeTasks",
      "ecs:ListTasks",
      "ecs:RegisterTaskDefinition",
      "ecs:RunTask",
      "ecs:UpdateService",
    ]
    # RegisterTaskDefinition and the read calls that inspect an arbitrary
    # revision don't take a resource ARN in their request the way
    # RunTask/UpdateService do; scoping this statement to "*" and relying on
    # PassRole below (the actual privilege boundary for what a registered
    # task definition can run as) matches AWS's own documented pattern for
    # ECS deploy roles.
    resources = ["*"]
  }

  statement {
    sid     = "PassTaskRoles"
    effect  = "Allow"
    actions = ["iam:PassRole"]
    # Verified live on a go-live rehearsal (2026-09-18): qdrant, redis,
    # martin and hatchet use aws_iam_role.stores (georag-ecs-task-stores),
    # not aws_iam_role.task -- see iam.tf's comment on why they stopped
    # sharing aws_iam_role.task. Missing it here isn't a build-time error;
    # RegisterTaskDefinition succeeds fine without PassRole and only fails
    # the moment it's actually asked to attach a role the caller can't
    # pass, so this was never caught by terraform plan/apply or by the
    # earlier build jobs -- only by a real RegisterTaskDefinition call for
    # one of those four services during "Deploy services".
    resources = [aws_iam_role.execution.arn, aws_iam_role.task.arn, aws_iam_role.stores.arn]
  }

  statement {
    sid       = "RdsGate"
    effect    = "Allow"
    actions   = ["rds:DescribeDBInstances"]
    resources = ["*"]
  }

  statement {
    sid       = "LogsForSmokeCheck"
    effect    = "Allow"
    actions   = ["logs:GetLogEvents", "logs:DescribeLogStreams"]
    resources = ["arn:aws:logs:${var.region}:${data.aws_caller_identity.current.account_id}:log-group:/ecs/${local.name}*"]
  }
}

resource "aws_iam_role_policy" "github_deploy" {
  name   = "deploy"
  role   = aws_iam_role.github_deploy.id
  policy = data.aws_iam_policy_document.github_deploy.json
}

variable "github_repository" {
  description = "owner/repo this OIDC trust is scoped to, e.g. kjmaguire/GeoRag-Intelligent-V2.1."
  type        = string
}

output "github_deploy_role_arn" {
  description = "Set as the AWS_DEPLOY_ROLE_ARN GitHub repository secret."
  value       = aws_iam_role.github_deploy.arn
}
