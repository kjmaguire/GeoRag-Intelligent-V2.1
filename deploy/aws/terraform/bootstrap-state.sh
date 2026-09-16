#!/usr/bin/env bash
# bootstrap-state.sh — create the S3 bucket that holds Terraform state.
#
# Run once per account, before the first `terraform init -backend-config=...`.
#
#   bash deploy/aws/terraform/bootstrap-state.sh georag-tfstate-123456789012 us-east-1
#
# This is deliberately a script and not Terraform. The bucket cannot be managed
# by the state it stores: destroying that resource would destroy the record of
# every other resource, and creating it needs somewhere to put the state before
# the somewhere exists. A small idempotent script is the honest way out.
#
# Idempotent: safe to re-run. Each step is skipped if already in place, so it
# can also be used to verify an existing bucket still has the right settings.
#
# What it enables, and why each one matters for STATE specifically:
#   * Versioning — state is overwritten on every apply. A bad write or a
#     mistaken `state rm` is unrecoverable without it. This is the setting you
#     will be glad of.
#   * Default encryption (SSE-S3) — state holds every value Terraform touches
#     in plaintext, including the RDS master password.
#   * Public access block — all four switches. A public state bucket is a
#     complete credential disclosure.
#   * TLS-only bucket policy — refuses unencrypted transport.
#
# Locking needs no DynamoDB table: backend.tf sets use_lockfile, so the lock is
# a conditional write in this same bucket (Terraform >= 1.10).

set -euo pipefail

BUCKET="${1:-}"
REGION="${2:-us-east-1}"

if [ -z "$BUCKET" ]; then
  echo "usage: $0 <bucket-name> [region]" >&2
  echo "       bucket names are globally unique; georag-tfstate-<account-id> works" >&2
  exit 2
fi

command -v aws >/dev/null 2>&1 || { echo "aws CLI not found" >&2; exit 1; }

echo "account: $(aws sts get-caller-identity --query Arn --output text)"
echo "bucket:  ${BUCKET}  (${REGION})"
echo

if aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
  echo "• bucket exists — verifying settings"
else
  echo "• creating bucket"
  # us-east-1 is the one region that rejects an explicit LocationConstraint.
  if [ "$REGION" = "us-east-1" ]; then
    aws s3api create-bucket --bucket "$BUCKET" --region "$REGION"
  else
    aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" \
      --create-bucket-configuration "LocationConstraint=${REGION}"
  fi
fi

echo "• versioning"
aws s3api put-bucket-versioning --bucket "$BUCKET" \
  --versioning-configuration Status=Enabled

echo "• default encryption"
aws s3api put-bucket-encryption --bucket "$BUCKET" \
  --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"},"BucketKeyEnabled":true}]}'

echo "• public access block"
aws s3api put-public-access-block --bucket "$BUCKET" \
  --public-access-block-configuration \
  'BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true'

echo "• TLS-only bucket policy"
aws s3api put-bucket-policy --bucket "$BUCKET" --policy "$(cat <<JSON
{
  "Version": "2012-10-17",
  "Statement": [{
    "Sid": "DenyInsecureTransport",
    "Effect": "Deny",
    "Principal": "*",
    "Action": "s3:*",
    "Resource": ["arn:aws:s3:::${BUCKET}", "arn:aws:s3:::${BUCKET}/*"],
    "Condition": {"Bool": {"aws:SecureTransport": "false"}}
  }]
}
JSON
)"

# Old state versions accumulate forever otherwise. 90 days keeps a recovery
# window well past any rollback anyone would attempt, without unbounded growth.
echo "• lifecycle: expire noncurrent state versions after 90 days"
aws s3api put-bucket-lifecycle-configuration --bucket "$BUCKET" \
  --lifecycle-configuration \
  '{"Rules":[{"ID":"expire-noncurrent-state","Status":"Enabled","Filter":{"Prefix":""},"NoncurrentVersionExpiration":{"NoncurrentDays":90},"AbortIncompleteMultipartUpload":{"DaysAfterInitiation":7}}]}'

echo
echo "done. now:"
echo "  cd deploy/aws/terraform"
echo "  cp backend.hcl.example backend.hcl    # bucket = \"${BUCKET}\", region = \"${REGION}\""
echo "  terraform init -backend-config=backend.hcl"
