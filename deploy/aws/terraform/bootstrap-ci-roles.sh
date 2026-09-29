#!/usr/bin/env bash
# Create (or re-assert) the two IAM roles .github/workflows/terraform.yml
# assumes. Run it ONCE, from CloudShell or any shell holding admin
# credentials for the account; re-running is safe and puts both roles back
# into exactly the state described here.
#
#   bash deploy/aws/terraform/bootstrap-ci-roles.sh
#
# It prints the two role ARNs and the GitHub settings the workflow needs.
#
# THE TWO ROLES
#
#   georag-github-terraform-plan   trusted for refs/heads/main only.
#       AWS-managed ReadOnlyAccess, plus the three things a `terraform plan`
#       needs beyond reading: write/delete of the state LOCK object (not the
#       state itself), and GetSecretValue on georag/app, because refreshing
#       aws_secretsmanager_secret_version.app_placeholder reads the value.
#       It cannot change infrastructure.
#
#   georag-github-terraform-apply  trusted for the GitHub `production`
#       ENVIRONMENT only, never for a branch ref. AdministratorAccess:
#       this Terraform creates IAM roles and policies, so a narrower policy
#       is not a real boundary (a role that can write IAM can grant itself
#       anything). The boundary is the environment instead, and it only
#       holds if the environment is configured as printed below: required
#       reviewer, and deployment branches restricted to main. An
#       unprotected `production` environment would let any branch workflow
#       that names it assume this role.
#
# WHY A SCRIPT AND NOT TERRAFORM. The same reason bootstrap-state.sh creates
# the state bucket: these roles are what runs Terraform, so they cannot be
# managed by the state they apply. A Terraform-managed apply role is one bad
# plan away from deleting or narrowing itself mid-apply, with nothing left
# that can put it back. The OIDC provider they trust IS Terraform-managed
# (ci.tf); this script refuses to run until it exists.
#
# The subject prefix is the org's ID-suffixed form
# (repo:OWNER@OWNER_ID/REPO@REPO_ID) — see ci.tf for how that was found. Set
# GITHUB_OIDC_SUB_PREFIX to override it for another repository.
set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
SUB_PREFIX="${GITHUB_OIDC_SUB_PREFIX:-repo:kjmaguire@79488174/GeoRag-Intelligent-V2.1@1252963201}"
PLAN_ROLE="${PLAN_ROLE_NAME:-georag-github-terraform-plan}"
APPLY_ROLE="${APPLY_ROLE_NAME:-georag-github-terraform-apply}"
STATE_KEY="georag/production/terraform.tfstate"   # backend.tf's `key`
APP_SECRET_NAME="georag/app"

command -v aws >/dev/null 2>&1 || { echo "missing required tool: aws" >&2; exit 1; }

ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
BUCKET="${TF_STATE_BUCKET:-georag-tfstate-${ACCOUNT}}"
PROVIDER_ARN="arn:aws:iam::${ACCOUNT}:oidc-provider/token.actions.githubusercontent.com"

echo "account ${ACCOUNT}, region ${REGION}, state bucket ${BUCKET}"

if ! aws iam get-open-id-connect-provider --open-id-connect-provider-arn "$PROVIDER_ARN" >/dev/null 2>&1; then
  echo "The GitHub OIDC provider ${PROVIDER_ARN} does not exist." >&2
  echo "It is created by deploy/aws/terraform/ci.tf; apply that once first." >&2
  exit 1
fi

if ! aws s3api head-bucket --bucket "$BUCKET" >/dev/null 2>&1; then
  echo "State bucket ${BUCKET} is not reachable. Set TF_STATE_BUCKET, or run init-backend.sh first." >&2
  exit 1
fi

trust_policy() {
  local sub="$1"
  printf '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Federated":"%s"},"Action":"sts:AssumeRoleWithWebIdentity","Condition":{"StringEquals":{"token.actions.githubusercontent.com:aud":"sts.amazonaws.com","token.actions.githubusercontent.com:sub":"%s"}}}]}' \
    "$PROVIDER_ARN" "$sub"
}

ensure_role() {
  local name="$1" trust="$2" max_session="$3" description="$4"
  if aws iam get-role --role-name "$name" >/dev/null 2>&1; then
    echo "• ${name}: exists, re-asserting trust policy"
    aws iam update-assume-role-policy --role-name "$name" --policy-document "$trust"
    aws iam update-role --role-name "$name" --max-session-duration "$max_session" --description "$description"
  else
    echo "• ${name}: creating"
    aws iam create-role --role-name "$name" \
      --assume-role-policy-document "$trust" \
      --max-session-duration "$max_session" \
      --description "$description" \
      --tags Key=managed-by,Value=bootstrap-ci-roles.sh >/dev/null
  fi
}

# ── plan ────────────────────────────────────────────────────────────────
ensure_role "$PLAN_ROLE" \
  "$(trust_policy "${SUB_PREFIX}:ref:refs/heads/main")" \
  3600 \
  "terraform plan from .github/workflows/terraform.yml on main. Read-only."
aws iam attach-role-policy --role-name "$PLAN_ROLE" \
  --policy-arn arn:aws:iam::aws:policy/ReadOnlyAccess

PLAN_INLINE=$(printf '{"Version":"2012-10-17","Statement":[{"Sid":"StateLockOnly","Effect":"Allow","Action":["s3:PutObject","s3:DeleteObject"],"Resource":"arn:aws:s3:::%s/%s.tflock"},{"Sid":"RefreshAppSecretVersion","Effect":"Allow","Action":"secretsmanager:GetSecretValue","Resource":"arn:aws:secretsmanager:%s:%s:secret:%s-*"}]}' \
  "$BUCKET" "$STATE_KEY" "$REGION" "$ACCOUNT" "$APP_SECRET_NAME")
aws iam put-role-policy --role-name "$PLAN_ROLE" --policy-name terraform-plan \
  --policy-document "$PLAN_INLINE"

# ── apply ───────────────────────────────────────────────────────────────
ensure_role "$APPLY_ROLE" \
  "$(trust_policy "${SUB_PREFIX}:environment:production")" \
  7200 \
  "terraform apply from .github/workflows/terraform.yml, production environment only."
aws iam attach-role-policy --role-name "$APPLY_ROLE" \
  --policy-arn arn:aws:iam::aws:policy/AdministratorAccess

PLAN_ARN="arn:aws:iam::${ACCOUNT}:role/${PLAN_ROLE}"
APPLY_ARN="arn:aws:iam::${ACCOUNT}:role/${APPLY_ROLE}"

cat <<EOF

Done. Now, in GitHub (repository Settings):

 1. Environments → New environment → "production":
      • Required reviewers: yourself
      • Deployment branches and tags: Selected branches → add "main"
    The apply role trusts this environment and nothing else. Without both
    rules, any branch workflow naming it could assume an admin role.

 2. Secrets and variables → Actions → Variables (not secrets; none of these
    is sensitive):
      AWS_TERRAFORM_PLAN_ROLE_ARN   = ${PLAN_ARN}
      AWS_TERRAFORM_APPLY_ROLE_ARN  = ${APPLY_ARN}
      TF_STATE_BUCKET               = ${BUCKET}

 3. Secrets and variables → Actions → Secrets → New repository secret:
      PRODUCTION_TFVARS = the full contents of deploy/aws/terraform/production.tfvars
    (it carries cloudfront_origin_secret). image_tag in it is ignored: the
    workflow uses the tag production is running, unless you pass one.

Then run Actions → Terraform → Run workflow (action: plan) on main.
EOF
