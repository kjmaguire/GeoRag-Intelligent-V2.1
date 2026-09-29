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
# TRUST: main only, since 2026-09-29 (audit AWS-6). The first version of this
# file trusted every ref in the repository, on the argument that narrowing it
# would break workflow_dispatch from other refs and "the PR-triggered smoke
# build", and that this was a build/deploy role rather than a
# production-secrets role. Both premises were wrong:
#
#   * it IS a production-secrets role in effect. It may RegisterTaskDefinition
#     and RunTask with iam:PassRole on the execution role, which reads every
#     key in georag/app and the RDS master secret. So any branch or same-repo
#     PR workflow with `id-token: write` could register a task that echoes
#     those secrets and read them back through logs:GetLogEvents.
#   * nothing that runs on a PR or a branch uses it. Its only callers are
#     cd.yml (workflow_run on main, or dispatch), run-seeder.yml,
#     project-diagnostics.yml and parse-comparison.yml (dispatch only). PR CI
#     (ci.yml, docker-build.yml) never assumes it — docker-build's
#     id-token:write is for SBOM attestation, not AWS.
#
# So the sub claim is pinned to refs/heads/main. workflow_run always runs on
# the default branch, so cd.yml's automatic path produces exactly that claim.
# A dispatch from any other branch now fails at "Configure AWS credentials" —
# which is the point. Deliberately NOT `environment:production`: a job with
# `environment:` gets an environment-shaped sub instead of a ref-shaped one,
# and an environment with no deployment-branch rule would re-open every
# branch. Add it only together with a protected `production` environment
# restricted to main, and list both claims below.
#
# NOT the Terraform roles. .github/workflows/terraform.yml plans and applies
# this tree through two OTHER roles, georag-github-terraform-plan (main ref,
# read-only) and georag-github-terraform-apply (the protected `production`
# environment only). They are created by bootstrap-ci-roles.sh, not here:
# the role that runs Terraform must not be managed by the state it applies.
# They reuse the OIDC provider below, so do not remove it.

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
    # main only — see the file header for why this is no longer "any ref".
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
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:kjmaguire@79488174/GeoRag-Intelligent-V2.1@1252963201:ref:refs/heads/main"]
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
    ]
    # RegisterTaskDefinition and the read calls that inspect an arbitrary
    # revision don't take a resource ARN in their request the way
    # RunTask/UpdateService do; scoping this statement to "*" and relying on
    # PassRole below (the actual privilege boundary for what a registered
    # task definition can run as) matches AWS's own documented pattern for
    # ECS deploy roles.
    resources = ["*"]
  }

  # The two calls that START or MOVE something, scoped (audit AWS-6): only
  # this deployment's task families, only on this cluster, and only this
  # cluster's services. Same ArnLike/ecs:cluster shape the scheduler role in
  # iam.tf already uses for its RunTask grant. `georag-*` covers migrate,
  # smoke and every service family CD registers.
  statement {
    sid       = "EcsRunOwnTasksInOwnCluster"
    effect    = "Allow"
    actions   = ["ecs:RunTask"]
    resources = ["arn:aws:ecs:${var.region}:${data.aws_caller_identity.current.account_id}:task-definition/${local.name}-*"]
    condition {
      test     = "ArnLike"
      variable = "ecs:cluster"
      values   = [aws_ecs_cluster.this.arn]
    }
  }

  statement {
    sid       = "EcsRollOwnServices"
    effect    = "Allow"
    actions   = ["ecs:UpdateService"]
    resources = ["arn:aws:ecs:${var.region}:${data.aws_caller_identity.current.account_id}:service/${aws_ecs_cluster.this.name}/*"]
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
