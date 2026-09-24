#!/usr/bin/env bash
# Rotate the CloudFront -> load balancer origin secret (X-Origin-Verify) with
# no window in which real traffic is refused.
#
#   bash rotate-cloudfront-origin-secret.sh            # preview: what exists, change nothing
#   bash rotate-cloudfront-origin-secret.sh --apply    # rotate
#   bash rotate-cloudfront-origin-secret.sh --finish   # only drop the old value from the
#                                                      # listener rules (after an --apply
#                                                      # that stopped part-way)
#
# WHAT THE SECRET DOES. See `cloudfront_origin_secret` in
# deploy/aws/terraform/edge.tf: the load balancer's security group admits
# every CloudFront edge, not only ours, so the listener refuses any request
# that lacks the header our distribution adds. Every listener rule that
# forwards carries the same header condition (services.tf).
#
# THE ORDER IS THE WHOLE POINT. CloudFront takes minutes to push a config
# change to every edge, and the listener rules change at once. So:
#   1. every rule accepts BOTH values        (old edges and new edges both pass)
#   2. the distribution sends the NEW value  (then wait until it is Deployed)
#   3. check a request through CloudFront gets through
#   4. every rule accepts only the NEW value
# Stop anywhere and nothing is refused: before 2 finishes the rules still
# accept the old value; after it, they already accept the new one.
#
# WHERE THE NEW VALUE GOES. It is Terraform input (a sensitive tfvar), not
# something Terraform generates, and the next `terraform apply` with the OLD
# value in production.tfvars would put the old one back. So the script stores
# the new value in its own Secrets Manager secret, $STORE_ID (created on
# first use; not georag/app, which only holds values tasks read), and prints
# the one line to update production.tfvars with. The value is never printed.
#
# It is also the only copy when the platform is powered off: power = "off"
# destroys the distribution and the load balancer, and power = "on" rebuilds
# them from production.tfvars.
set -euo pipefail

NAME="${GEORAG_NAME:-georag}"
HEADER="X-Origin-Verify"
STORE_ID="${STORE_ID:-$NAME/cloudfront-origin-secret}"
MODE="${1:-preview}"

for tool in aws jq openssl curl; do
  command -v "$tool" >/dev/null 2>&1 || { echo "missing required tool: $tool" >&2; exit 1; }
done

# ── find the load balancer, its port-80 listener, and the distribution ─────
LB=$(aws elbv2 describe-load-balancers --names "$NAME" \
       --query 'LoadBalancers[0].{arn:LoadBalancerArn,dns:DNSName}' --output json 2>/dev/null || true)
[ -n "$LB" ] || LB='{}'
LB_ARN=$(jq -r '.arn // empty' <<<"$LB")
LB_DNS=$(jq -r '.dns // empty' <<<"$LB")
if [ -z "$LB_ARN" ]; then
  echo "no load balancer named '$NAME' (is the platform powered off?)" >&2
  exit 1
fi
LISTENER=$(aws elbv2 describe-listeners --load-balancer-arn "$LB_ARN" \
             --query 'Listeners[?Port==`80`].ListenerArn | [0]' --output text)
if [ -z "$LISTENER" ] || [ "$LISTENER" = "None" ]; then
  echo "the load balancer has no port-80 listener; this is not the CloudFront edge mode" >&2
  exit 1
fi
DIST_ID=$(aws cloudfront list-distributions \
            --query "DistributionList.Items[?Origins.Items[?DomainName=='$LB_DNS']].Id | [0]" --output text)
if [ -z "$DIST_ID" ] || [ "$DIST_ID" = "None" ]; then
  echo "no CloudFront distribution has $LB_DNS as its origin" >&2
  exit 1
fi
DIST_DOMAIN=$(aws cloudfront get-distribution --id "$DIST_ID" --query 'Distribution.DomainName' --output text)

rules_json () {
  aws elbv2 describe-rules --listener-arn "$LISTENER" --output json \
    | jq --arg h "$HEADER" '[.Rules[]
        | select(any(.Conditions[]?; .Field == "http-header" and .HttpHeaderConfig.HttpHeaderName == $h))]'
}

# The distribution's current value, or empty. Read, never printed.
dist_value () {
  aws cloudfront get-distribution-config --id "$DIST_ID" --output json \
    | jq -r --arg h "$HEADER" '[.DistributionConfig.Origins.Items[]
        | (.CustomHeaders.Items // [])[] | select(.HeaderName == $h) | .HeaderValue] | first // empty'
}

# Point every header-checking rule at exactly the values in $1 (a JSON array).
# Every other condition on the rule is kept. Secret values reach jq through
# the environment and the AWS CLI through stdin, never on a command line,
# where any process on the machine could read them.
set_rule_values () {
  local arn body
  while read -r arn; do
    body=$(rules_json | V="$1" jq -c --arg arn "$arn" --arg h "$HEADER" '
      .[] | select(.RuleArn == $arn)
      | {RuleArn: .RuleArn,
         Conditions: [.Conditions[] | del(.Values)
           | if .Field == "http-header" and .HttpHeaderConfig.HttpHeaderName == $h
             then .HttpHeaderConfig.Values = (env.V | fromjson) else . end]}')
    printf '%s' "$body" | aws elbv2 modify-rule --cli-input-json file:///dev/stdin \
      --query 'Rules[0].Priority' --output text >/dev/null
    unset body
  done < <(rules_json | jq -r '.[].RuleArn')
}

rule_value_count () { rules_json | jq '[.[].Conditions[] | select(.Field == "http-header") | .HttpHeaderConfig.Values | length] | max // 0'; }

# HTTP status of the health route, through the distribution. Only 403 means
# refused: the listener's fixed response. A 502/503 still proves the request
# matched a forwarding rule (the app is scaled down, not the edge refusing).
# curl prints 000 itself on a connection failure; `|| echo` would double it.
through_cloudfront () {
  local c
  c=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "https://$DIST_DOMAIN/up" 2>/dev/null) || true
  printf '%s' "${c:-000}"
}

n_rules=$(rules_json | jq length)
cur=$(dist_value)
echo "load balancer:    $NAME ($LB_DNS)"
echo "distribution:     $DIST_ID ($DIST_DOMAIN)"
echo "rules checking $HEADER: $n_rules (values per rule now: $(rule_value_count))"
echo "distribution sends it: $([ -n "$cur" ] && echo "yes (${#cur} chars)" || echo no)"

if [ "$n_rules" = "0" ] || [ -z "$cur" ]; then
  echo
  echo "The origin secret is not in use (cloudfront_origin_secret is empty in Terraform)." >&2
  echo "Turning it ON changes the listener's default action; do that through Terraform," >&2
  echo "not this script: set cloudfront_origin_secret in production.tfvars and apply." >&2
  exit 1
fi

case "$MODE" in
preview)
  echo
  echo "Nothing changed. Next: bash $0 --apply"
  ;;

--finish)
  echo
  echo "=== narrowing every rule to the value the distribution sends now"
  if [ "$(aws cloudfront get-distribution --id "$DIST_ID" --query 'Distribution.Status' --output text)" != "Deployed" ]; then
    echo "the distribution is still deploying; wait, then re-run --finish" >&2
    exit 1
  fi
  set_rule_values "$(A="$cur" jq -cn '[env.A]')"
  code=$(through_cloudfront)
  echo "through CloudFront: HTTP $code"
  [ "$code" != "403" ] || { echo "REFUSED after narrowing - re-run --apply" >&2; exit 1; }
  echo "FINISHED: the rules accept only the current value."
  ;;

--apply)
  code=$(through_cloudfront)
  echo "through CloudFront before: HTTP $code"
  if [ "$code" = "403" ] || [ "$code" = "000" ]; then
    echo "the site is not answering through CloudFront before anything changed - stopping" >&2
    exit 1
  fi

  new=$(openssl rand -hex 32)
  [ "${#new}" -eq 64 ] || { echo "could not generate a value" >&2; exit 1; }

  echo
  echo "=== 1. store the new value in Secrets Manager ($STORE_ID)"
  if aws secretsmanager describe-secret --secret-id "$STORE_ID" >/dev/null 2>&1; then
    printf '%s' "$new" | aws secretsmanager put-secret-value --secret-id "$STORE_ID" \
      --secret-string file:///dev/stdin --query VersionId --output text >/dev/null
  else
    printf '%s' "$new" | aws secretsmanager create-secret --name "$STORE_ID" \
      --description "CloudFront -> ALB X-Origin-Verify value; Terraform input cloudfront_origin_secret" \
      --secret-string file:///dev/stdin --query ARN --output text >/dev/null
  fi
  stored=$(aws secretsmanager get-secret-value --secret-id "$STORE_ID" --query SecretString --output text)
  [ "$stored" = "$new" ] || { echo "read-back of $STORE_ID did not match - stopping, nothing else changed" >&2; exit 1; }
  unset stored

  echo "=== 2. every rule accepts the old AND the new value"
  set_rule_values "$(A="$cur" B="$new" jq -cn '[env.A, env.B]')"

  echo "=== 3. the distribution sends the new value"
  cfg=$(aws cloudfront get-distribution-config --id "$DIST_ID" --output json)
  etag=$(jq -r .ETag <<<"$cfg")
  V="$new" jq -c --arg id "$DIST_ID" --arg etag "$etag" --arg h "$HEADER" '
      {Id: $id, IfMatch: $etag,
       DistributionConfig: (.DistributionConfig
         | .Origins.Items |= map(
             if any((.CustomHeaders.Items // [])[]; .HeaderName == $h)
             then .CustomHeaders.Items |= map(if .HeaderName == $h then .HeaderValue = env.V else . end)
             else . end))}' <<<"$cfg" \
    | aws cloudfront update-distribution --cli-input-json file:///dev/stdin \
        --query 'Distribution.Status' --output text >/dev/null
  unset cfg
  echo "    waiting for every edge to have it (usually 3-10 minutes)..."
  aws cloudfront wait distribution-deployed --id "$DIST_ID"

  if [ "$(dist_value)" != "$new" ]; then
    echo "the distribution does not report the new value after deploying - the rules still" >&2
    echo "accept both, so nothing is refused. Investigate, then re-run --apply." >&2
    exit 1
  fi
  code=$(through_cloudfront)
  echo "=== 4. through CloudFront with the new value: HTTP $code"
  if [ "$code" = "403" ] || [ "$code" = "000" ]; then
    echo "not answering - the rules still accept both values, so the old path is intact." >&2
    exit 1
  fi

  echo "=== 5. every rule accepts only the new value"
  set_rule_values "$(B="$new" jq -cn '[env.B]')"
  unset new cur
  code=$(through_cloudfront)
  echo "    through CloudFront: HTTP $code"
  [ "$code" != "403" ] || { echo "REFUSED after narrowing - run: bash $0 --finish" >&2; exit 1; }

  echo
  echo "ORIGIN SECRET ROTATED. The old value is refused from now on."
  echo
  echo "BEFORE THE NEXT terraform apply, put the new value where Terraform reads it,"
  echo "or the apply puts the old one back. Either set cloudfront_origin_secret in"
  echo "production.tfvars from:"
  echo "  aws secretsmanager get-secret-value --secret-id $STORE_ID --query SecretString --output text"
  echo "or export it for the apply instead of keeping it in the file:"
  echo "  export TF_VAR_cloudfront_origin_secret=\$(aws secretsmanager get-secret-value --secret-id $STORE_ID --query SecretString --output text)"
  ;;

*)
  echo "usage: bash $0 [--apply | --finish]" >&2
  exit 2
  ;;
esac
