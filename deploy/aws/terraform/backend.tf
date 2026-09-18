# Remote state (added 2026-09-15).
#
# Until this file existed there was no backend block at all, so state was a
# LOCAL file next to whoever ran `terraform apply`. For infrastructure that is
# meant to be the one source of truth, that is the failure the rest of this
# tree exists to prevent:
#
#   * Lose the machine, lose the state. The resources keep running and nothing
#     can manage them any more. The next apply does not adopt them, it tries to
#     CREATE them and fails on names already taken — and the way out is
#     importing every resource by hand.
#   * No locking. Two applies at once interleave writes and corrupt state.
#   * Applying from an ephemeral environment (a CI runner, a cloud dev box)
#     silently discards the state when the box is reclaimed. The ADR-0022
#     cutover was very nearly run from one.
#
# S3 keeps the state, and `use_lockfile` keeps the lock in the same bucket via
# conditional writes — no DynamoDB table to provision or pay for. That needs
# Terraform >= 1.10, which is why required_version moved up in main.tf.
#
# PARTIAL CONFIGURATION. The bucket is account-specific and S3 bucket names are
# globally unique, so it is not hardcoded here. Supply it at init.
#
# Recommended: one command, resolves your account ID and does everything below
# (create/verify the bucket, write backend.hcl, run init):
#
#   bash deploy/aws/terraform/init-backend.sh
#
# Manual alternative — use this if you want to name the bucket yourself rather
# than accept the auto-generated georag-tfstate-<account-id>:
#
#   bash deploy/aws/terraform/bootstrap-state.sh <bucket-name> <region>
#   cp backend.hcl.example backend.hcl     # fill in your bucket
#   terraform init -backend-config=backend.hcl
#
# backend.hcl is gitignored. The values in it are not secret, but the file sits
# exactly where someone would paste something that is.

terraform {
  backend "s3" {
    key          = "georag/production/terraform.tfstate"
    encrypt      = true
    use_lockfile = true
  }
}
