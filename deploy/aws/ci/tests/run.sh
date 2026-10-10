#!/usr/bin/env bash
# shellcheck disable=SC2016  # backticks in grep patterns are literal Markdown
# Tests for the Terraform-from-GitHub pieces, run against tests/fake-aws.
# No credentials, no network.
#
#   bootstrap_*   deploy/aws/terraform/bootstrap-ci-roles.sh: the two trust
#                 policies are the whole security model, so their exact
#                 subjects are pinned — plan on the main ref, apply on the
#                 production ENVIRONMENT, and never the other way round.
#   summary_*     deploy/aws/ci/summarise-plan.sh: a destroy or replace of a
#                 stateful resource must surface as a warning.
#   workflow_*    .github/workflows/terraform.yml: static checks on the
#                 lines that decide which role a job can assume.
#   cd_*          .github/workflows/cd.yml and terraform.yml's shell, pulled
#                 out of the YAML and run: which commit a dispatch may deploy,
#                 what the sweep gate refuses, and that a refused Qdrant does
#                 not stop the other vendor services rolling. Plus the two
#                 constants that must agree across files (the concurrency
#                 group, the deploy role's session ceiling).
#
# Usage: bash deploy/aws/ci/tests/run.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${HERE}/../../../.." && pwd)"
BOOTSTRAP="${ROOT}/deploy/aws/terraform/bootstrap-ci-roles.sh"
SUMMARISE="${ROOT}/deploy/aws/ci/summarise-plan.sh"
WORKFLOW="${ROOT}/.github/workflows/terraform.yml"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "${WORK}/bin"
cp "${HERE}/fake-aws" "${WORK}/bin/aws"
chmod +x "${WORK}/bin/aws"

PASS=0
FAIL=0
CURRENT=""
OUT=""
RC=0

fail_case() { printf '  FAIL  %s: %s\n' "$CURRENT" "$1"; FAIL=$((FAIL + 1)); }
begin() { CURRENT="$1"; : > "${WORK}/aws.log"; rm -rf "${WORK}/aws.log.docs"; }
done_case() { [ "$FAIL" -eq "$1" ] && { printf '  ok    %s\n' "$CURRENT"; PASS=$((PASS + 1)); }; }

run_bootstrap() {
  OUT=$(PATH="${WORK}/bin:${PATH}" FAKE_AWS_LOG="${WORK}/aws.log" bash "$BOOTSTRAP" 2>&1)
  RC=$?
}

json_get() { jq -r "$2" "${WORK}/aws.log.docs/$1"; }

SUB='repo:kjmaguire@79488174/GeoRag-Intelligent-V2.1@1252963201'

# ── bootstrap ──────────────────────────────────────────────────────────
begin bootstrap_creates_both_roles; f=$FAIL
run_bootstrap
[ "$RC" -eq 0 ] || fail_case "exit ${RC}: ${OUT}"
[ "$(grep -c '^iam create-role' "${WORK}/aws.log")" -eq 2 ] || fail_case "expected two create-role calls"
[ "$(json_get georag-github-terraform-plan.trust.json '.Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"]')" = "${SUB}:ref:refs/heads/main" ] \
  || fail_case "plan role must trust the main ref"
[ "$(json_get georag-github-terraform-apply.trust.json '.Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"]')" = "${SUB}:environment:production" ] \
  || fail_case "apply role must trust the production environment, not a ref"
[ "$(json_get georag-github-terraform-apply.trust.json '.Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:aud"]')" = "sts.amazonaws.com" ] \
  || fail_case "audience must be pinned"
[ "$(json_get georag-github-terraform-apply.trust.json '.Statement[0].Principal.Federated')" = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com" ] \
  || fail_case "must trust the GitHub OIDC provider"
grep -qx 'arn:aws:iam::aws:policy/ReadOnlyAccess' "${WORK}/aws.log.docs/georag-github-terraform-plan.managed" \
  || fail_case "plan role needs ReadOnlyAccess"
if grep -q AdministratorAccess "${WORK}/aws.log.docs/georag-github-terraform-plan.managed"; then
  fail_case "plan role must NOT be an admin"
fi
grep -qx 'arn:aws:iam::aws:policy/AdministratorAccess' "${WORK}/aws.log.docs/georag-github-terraform-apply.managed" \
  || fail_case "apply role needs AdministratorAccess"
[ "$(json_get georag-github-terraform-plan.inline.json '[.Statement[] | select(.Sid=="StateLockOnly") | .Resource] | .[0]')" \
    = "arn:aws:s3:::georag-tfstate-123456789012/georag/production/terraform.tfstate.tflock" ] \
  || fail_case "plan role may write the lock object only"
[ "$(json_get georag-github-terraform-plan.inline.json '[.Statement[] | .Action] | flatten | map(select(startswith("s3:Put") or startswith("s3:Delete"))) | length')" -eq 2 ] \
  || fail_case "unexpected S3 write actions on the plan role"
printf '%s' "$OUT" | grep -q 'AWS_TERRAFORM_PLAN_ROLE_ARN   = arn:aws:iam::123456789012:role/georag-github-terraform-plan' \
  || fail_case "must print the plan role ARN to configure"
printf '%s' "$OUT" | grep -q 'Deployment branches' || fail_case "must print the environment protection steps"
done_case "$f"

begin bootstrap_is_idempotent_on_existing_roles; f=$FAIL
FAKE_AWS_ROLES_EXIST=1 run_bootstrap
[ "$RC" -eq 0 ] || fail_case "exit ${RC}: ${OUT}"
grep -q '^iam create-role' "${WORK}/aws.log" && fail_case "must not create roles that exist"
[ "$(grep -c '^iam update-assume-role-policy' "${WORK}/aws.log")" -eq 2 ] || fail_case "must re-assert both trust policies"
[ "$(json_get georag-github-terraform-apply.trust.json '.Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"]')" = "${SUB}:environment:production" ] \
  || fail_case "re-asserted apply trust must stay environment-scoped"
done_case "$f"

begin bootstrap_refuses_without_oidc_provider; f=$FAIL
FAKE_AWS_NO_PROVIDER=1 run_bootstrap
[ "$RC" -ne 0 ] || fail_case "must fail without the OIDC provider"
grep -q '^iam create-role\|^iam attach-role-policy' "${WORK}/aws.log" && fail_case "must change nothing"
done_case "$f"

begin bootstrap_refuses_without_state_bucket; f=$FAIL
FAKE_AWS_NO_BUCKET=1 run_bootstrap
[ "$RC" -ne 0 ] || fail_case "must fail without the state bucket"
grep -q '^iam create-role' "${WORK}/aws.log" && fail_case "must change nothing"
done_case "$f"

# ── summary ────────────────────────────────────────────────────────────
printf 'plan text\n' > "${WORK}/plan.txt"

begin summary_no_changes; f=$FAIL
echo '[]' > "${WORK}/changes.json"
OUT=$(bash "$SUMMARISE" "${WORK}/changes.json" "${WORK}/plan.txt")
printf '%s' "$OUT" | grep -q 'No changes' || fail_case "empty plan must say so"
done_case "$f"

begin summary_counts_and_no_false_alarm; f=$FAIL
cat > "${WORK}/changes.json" <<'EOF'
[{"address":"aws_cloudwatch_metric_alarm.x","actions":["create"]},
 {"address":"aws_ecs_task_definition.this[\"fastapi\"]","actions":["delete","create"]},
 {"address":"aws_iam_role_policy.y","actions":["update"]}]
EOF
OUT=$(bash "$SUMMARISE" "${WORK}/changes.json" "${WORK}/plan.txt")
printf '%s' "$OUT" | grep -q '1 to create, 1 to update, 1 to replace, 0 to destroy' || fail_case "wrong counts: ${OUT}"
printf '%s' "$OUT" | grep -q 'WARNING' && fail_case "a task definition replace is routine, not a warning"
done_case "$f"

begin summary_warns_on_stateful_replace; f=$FAIL
cat > "${WORK}/changes.json" <<'EOF'
[{"address":"aws_db_instance.this","actions":["delete","create"]},
 {"address":"aws_ecs_service.this[\"qdrant\"]","actions":["create","delete"]},
 {"address":"aws_efs_file_system.this","actions":["update"]}]
EOF
OUT=$(bash "$SUMMARISE" "${WORK}/changes.json" "${WORK}/plan.txt")
printf '%s' "$OUT" | grep -q 'WARNING' || fail_case "must warn"
printf '%s' "$OUT" | grep -q '`aws_db_instance.this`: delete then create' || fail_case "must name the database"
printf '%s' "$OUT" | grep -q 'aws_ecs_service.this\["qdrant"\]`: create then delete' || fail_case "must name the qdrant service"
printf '%s' "$OUT" | grep -q '`aws_efs_file_system.this`: update' && fail_case "an in-place update is not a warning"
done_case "$f"

# ── workflow ───────────────────────────────────────────────────────────
job_block() {  # print the body of one job under `jobs:`
  awk -v job="  $1:" '
    $0 == job { on = 1; next }
    on && /^  [a-z_-]+:$/ { exit }
    on { print }' "$WORKFLOW"
}

begin workflow_role_per_job; f=$FAIL
PLAN_JOB=$(job_block plan)
APPLY_JOB=$(job_block apply)
{ [ -n "$PLAN_JOB" ] && [ -n "$APPLY_JOB" ]; } || fail_case "plan and apply jobs must exist"
printf '%s' "$APPLY_JOB" | grep -q '^    environment: production$' || fail_case "apply must run in the production environment"
printf '%s' "$PLAN_JOB" | grep -q 'environment:' && fail_case "plan must not name an environment (its role trusts the ref)"
printf '%s' "$APPLY_JOB" | grep -q 'AWS_TERRAFORM_PLAN_ROLE_ARN' && fail_case "apply must not use the plan role"
printf '%s' "$PLAN_JOB" | grep -q 'AWS_TERRAFORM_APPLY_ROLE_ARN' && fail_case "plan must not use the apply role"
printf '%s' "$APPLY_JOB" | grep -q "github.ref == 'refs/heads/main'" || fail_case "apply must be main-only"
printf '%s' "$PLAN_JOB" | grep -q "github.ref == 'refs/heads/main'" || fail_case "plan must be main-only"
done_case "$f"

begin workflow_triggers_and_artifacts; f=$FAIL
awk '/^on:/{on=1;next} on && /^[a-z]/{exit} on{print}' "$WORKFLOW" | grep -Eq '^  (push|pull_request|pull_request_target|schedule|workflow_run):' \
  && fail_case "must be dispatch-only"
grep -q '^  contents: read$' "$WORKFLOW" || fail_case "default token permissions must be read-only"
grep -Eq 'path: .*tfplan' "$WORKFLOW" && fail_case "the binary plan (it embeds state and secrets) must never be an artifact"
grep -q 'diff -u approved/changes.json changes.json' "$WORKFLOW" || fail_case "apply must compare against the approved change list"
done_case "$f"

# ── workflow shell, run ────────────────────────────────────────────────
CD="${ROOT}/.github/workflows/cd.yml"
CI_TF="${ROOT}/deploy/aws/terraform/ci.tf"

step_script() {  # print the dedented `run: |` body of the step named $2 in workflow $1
  awk -v name="$2" '
    function indent(s) { match(s, /^ */); return RLENGTH }
    { line = $0; sub(/^ */, "", line) }
    !on && line == "- name: " name { on = 1; next }
    on && !inrun && line ~ /^run: \|$/ { inrun = 1; base = indent($0) + 2; next }
    on && !inrun && line ~ /^- (name|uses):/ { exit }
    inrun {
      if ($0 ~ /^ *$/) { print ""; next }
      if (indent($0) < base) exit
      print substr($0, base + 1)
    }' "$1"
}

# --- which commit may a dispatch deploy -----------------------------------
# The shape check (7-40 hex) said nothing about WHICH commit: one from an
# unreviewed branch passed it, was built, pushed and rolled onto production.
# And a seven-character SHA passed it and then failed actions/checkout.
# A real repository stands in for the checkout: main is A then B; F lives only
# on a feature branch, as a commit fetched with `fetch-depth: 0` would.
RESOLVE="${WORK}/resolve.sh"
step_script "$CD" "Resolve deploy SHA" > "$RESOLVE"
SRC="${WORK}/src"; CLONE="${WORK}/clone"
rm -rf "$SRC" "$CLONE"; mkdir -p "$SRC"
git -C "$SRC" init -q -b main
git -C "$SRC" config user.email test@example.invalid
git -C "$SRC" config user.name test
git -C "$SRC" commit -q --allow-empty -m A
git -C "$SRC" commit -q --allow-empty -m B
git -C "$SRC" checkout -q -b feature
git -C "$SRC" commit -q --allow-empty -m F
git -C "$SRC" checkout -q main
git clone -q "$SRC" "$CLONE"
SHA_A=$(git -C "$CLONE" rev-parse origin/main~1)
SHA_B=$(git -C "$CLONE" rev-parse origin/main)
SHA_F=$(git -C "$CLONE" rev-parse origin/feature)

# run_resolve <event> <input sha> <workflow_run head sha> <github.sha>
# GIT_DIR points git at the stand-in checkout; GITHUB_OUTPUT is where the
# step's outputs land.
run_resolve() {
  : > "${WORK}/gh_output"
  OUT=$(env GIT_DIR="${CLONE}/.git" EVENT="$1" INPUT_SHA="$2" RUN_SHA="$3" EVENT_SHA="$4" \
        GITHUB_OUTPUT="${WORK}/gh_output" bash "$RESOLVE" 2>&1)
  RC=$?
}
out_of() { sed -n "s/^$1=//p" "${WORK}/gh_output"; }

begin cd_resolve_step_was_extracted; f=$FAIL
[ -s "$RESOLVE" ] || fail_case "could not extract the 'Resolve deploy SHA' step from cd.yml"
done_case "$f"

begin cd_dispatch_of_a_commit_on_main_is_deployed_in_full; f=$FAIL
run_resolve workflow_dispatch "$SHA_B" "" "$SHA_B"
[ "$RC" -eq 0 ] || fail_case "exit ${RC}: ${OUT}"
[ "$(out_of sha)" = "$SHA_B" ] || fail_case "sha output is '$(out_of sha)', expected the full ${SHA_B}"
[ "$(out_of short_sha)" = "${SHA_B:0:7}" ] || fail_case "short_sha is '$(out_of short_sha)'"
done_case "$f"

begin cd_dispatch_of_a_short_sha_is_resolved_to_the_full_one; f=$FAIL
run_resolve workflow_dispatch "${SHA_A:0:7}" "" "$SHA_B"
[ "$RC" -eq 0 ] || fail_case "exit ${RC}: ${OUT}"
[ "$(out_of sha)" = "$SHA_A" ] || fail_case "sha output is '$(out_of sha)', expected the full ${SHA_A} (actions/checkout cannot fetch an abbreviation)"
[ "$(out_of short_sha)" = "${SHA_A:0:7}" ] || fail_case "short_sha is '$(out_of short_sha)'"
done_case "$f"

begin cd_dispatch_of_an_unreviewed_commit_is_refused; f=$FAIL
run_resolve workflow_dispatch "$SHA_F" "" "$SHA_B"
[ "$RC" -ne 0 ] || fail_case "a commit that is on a branch but not on main was accepted"
printf '%s' "$OUT" | grep -q 'not an ancestor of origin/main' || fail_case "must say why: ${OUT}"
[ -z "$(out_of sha)" ] || fail_case "must not emit a sha for a refused commit"
run_resolve workflow_dispatch "${SHA_F:0:7}" "" "$SHA_B"
[ "$RC" -ne 0 ] || fail_case "the abbreviated form of an unreviewed commit was accepted"
done_case "$f"

begin cd_dispatch_of_an_unknown_commit_is_refused; f=$FAIL
run_resolve workflow_dispatch "$(printf 'a%.0s' $(seq 1 40))" "" "$SHA_B"
[ "$RC" -ne 0 ] || fail_case "a SHA that exists nowhere was accepted"
printf '%s' "$OUT" | grep -q 'not a commit in this repository' || fail_case "must say why: ${OUT}"
done_case "$f"

begin cd_dispatch_that_is_not_a_sha_is_refused; f=$FAIL
run_resolve workflow_dispatch "main" "" "$SHA_B"
[ "$RC" -ne 0 ] || fail_case "a branch name was accepted"
printf '%s' "$OUT" | grep -q 'is not a commit SHA' || fail_case "must say why: ${OUT}"
done_case "$f"

# Empty input means "latest main" -- github.sha, i.e. the tip of whatever ref
# the dispatch was started from. From a feature branch that is not main.
begin cd_dispatch_with_no_sha_from_main_deploys_main; f=$FAIL
run_resolve workflow_dispatch "" "" "$SHA_B"
[ "$RC" -eq 0 ] || fail_case "exit ${RC}: ${OUT}"
[ "$(out_of sha)" = "$SHA_B" ] || fail_case "sha output is '$(out_of sha)'"
done_case "$f"

begin cd_dispatch_with_no_sha_from_a_feature_branch_is_refused; f=$FAIL
run_resolve workflow_dispatch "" "" "$SHA_F"
[ "$RC" -ne 0 ] || fail_case "a dispatch from a feature branch deployed that branch's tip"
done_case "$f"

# The CI-triggered path is a push to main by its trigger and already carries 40
# characters: unchanged, and it needs no git at all (GIT_DIR points nowhere).
begin cd_workflow_run_path_is_unchanged; f=$FAIL
: > "${WORK}/gh_output"
OUT=$(env GIT_DIR="${WORK}/no-such-repo" EVENT=workflow_run INPUT_SHA="" RUN_SHA="$SHA_B" EVENT_SHA="$SHA_F" \
      GITHUB_OUTPUT="${WORK}/gh_output" bash "$RESOLVE" 2>&1); RC=$?
[ "$RC" -eq 0 ] || fail_case "exit ${RC}: ${OUT}"
[ "$(out_of sha)" = "$SHA_B" ] || fail_case "sha output is '$(out_of sha)', expected the run's head_sha"
[ "$(out_of short_sha)" = "${SHA_B:0:7}" ] || fail_case "short_sha is '$(out_of short_sha)'"
done_case "$f"

echo
echo "${PASS} passed, ${FAIL} failed"
[ "$FAIL" -eq 0 ]
