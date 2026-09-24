#!/usr/bin/env bash
# Dry rehearsal of the HATCHET_CLIENT_TOKEN and CloudFront origin-secret
# rotations against a fake `aws` (and a fake `curl` for the edge). No
# credentials, no network.
#
# Like ../run.sh for APP_KEY, this is the only rehearsal these get: the one
# AWS account is production. Each case is a way the rotation either breaks
# the platform or reports success it did not have:
#
#   token_*   a token for the wrong tenant stored; a failed mint that still
#             writes the secret; the minted token left sitting in CloudWatch;
#             "OK" reported while the worker picks up nothing; a rollback
#             that restores a whole stale JSON and undoes an unrelated key.
#   edge_*    the header rules narrowed before the distribution sends the new
#             value (every request 403s for the length of a CloudFront
#             deploy); a Values+Config condition the real API rejects; the
#             new value on a command line; a rotation that "succeeds" while
#             the distribution still sends the old value.
#
# Usage: bash deploy/aws/rotation/tests/ops/run.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROT="$(cd "$HERE/../.." && pwd)"
TOKEN="${ROT}/rotate-hatchet-token.sh"
EDGE="${ROT}/rotate-cloudfront-origin-secret.sh"

for t in jq base64 openssl; do
  command -v "$t" >/dev/null 2>&1 || { echo "SKIP: $t is not installed" >&2; exit 0; }
done

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/bin"
cp "$HERE/fake-aws" "$WORK/bin/aws"
cp "$HERE/fake-curl" "$WORK/bin/curl"
chmod +x "$WORK/bin/aws" "$WORK/bin/curl"

PASS=0
FAIL=0
CURRENT=""
OUT=""
RC=0
ST=""

ok()  { PASS=$((PASS + 1)); }
bad() { printf '  FAIL  %s: %s\n' "$CURRENT" "$1"; FAIL=$((FAIL + 1)); }
show() { printf '        --- output ---\n'; sed 's/^/        /' <<<"$OUT" | tail -25; }
check() { if eval "$2"; then ok; else bad "$1"; show; fi; }

jwt() {
  local p
  p=$(printf '{"sub":"%s","token_id":"%s","exp":1797000000}' "$1" "$2" | base64 -w0 | tr '+/' '-_' | tr -d '=')
  echo "eyJhbGciOiJub25lIn0.$p.c2ln"
}
OLD_TOKEN=$(jwt t-1 old-id)

new_case() {
  CURRENT="$1"
  ST="$WORK/$(printf '%s' "$1" | tr -c 'A-Za-z0-9' '_')"
  mkdir -p "$ST/home"
  : > "$ST/calls"; : > "$ST/stdin"
  jq -n --arg t "$OLD_TOKEN" '{APP_KEY: "base64:aaa", HATCHET_CLIENT_TOKEN: $t, REDIS_PASSWORD: "r"}' > "$ST/secret.json"
  jq -n --arg t "$OLD_TOKEN" '{APP_KEY: "base64:aaa", HATCHET_CLIENT_TOKEN: $t}' > "$ST/secret.prev.json"
  cat > "$ST/rules.json" <<'JSON'
[
 {"RuleArn":"arn:rule/reverb","Priority":"10","Conditions":[
   {"Field":"path-pattern","Values":["/app/*","/apps/*"],"PathPatternConfig":{"Values":["/app/*","/apps/*"]}},
   {"Field":"http-header","HttpHeaderConfig":{"HttpHeaderName":"X-Origin-Verify","Values":["OLDVALUE-0123456789abcdef"]}}]},
 {"RuleArn":"arn:rule/verified","Priority":"20","Conditions":[
   {"Field":"http-header","HttpHeaderConfig":{"HttpHeaderName":"X-Origin-Verify","Values":["OLDVALUE-0123456789abcdef"]}}]},
 {"RuleArn":"arn:rule/default","Priority":"default","Conditions":[]}
]
JSON
  cat > "$ST/dist.json" <<'JSON'
{"ETag":"E1","DistributionConfig":{"Comment":"georag public edge","Origins":{"Quantity":1,"Items":[
  {"Id":"alb","DomainName":"georag-123.elb.amazonaws.com",
   "CustomHeaders":{"Quantity":1,"Items":[{"HeaderName":"X-Origin-Verify","HeaderValue":"OLDVALUE-0123456789abcdef"}]}}]}}}
JSON
}

run() {   # run SCRIPT ARGS... with the fakes first on PATH
  OUT=$(cd "$ST" && env -i PATH="$WORK/bin:$PATH" HOME="$ST/home" FAKE_STATE="$ST" \
          FAKE_NOMINT="${FAKE_NOMINT:-0}" FAKE_MINT_TENANT="${FAKE_MINT_TENANT:-t-1}" \
          FAKE_WORKER_DEAD="${FAKE_WORKER_DEAD:-0}" FAKE_DIST_STICKY="${FAKE_DIST_STICKY:-0}" \
          FAKE_SITE_DOWN="${FAKE_SITE_DOWN:-0}" WORKER_POLL_SECONDS=0 \
          bash "$@" 2>&1)
  RC=$?
}

token_of() { jq -r .HATCHET_CLIENT_TOKEN "$ST/secret.json"; }
claim() { cut -d. -f2 <<<"$1" | tr '_-' '/+' | { read -r p; while [ $(( ${#p} % 4 )) -ne 0 ]; do p="$p="; done; printf '%s' "$p"; } | base64 -d | jq -r ".$2"; }

echo "HATCHET_CLIENT_TOKEN rotation"
# ─────────────────────────────────────────────────────────────────────────────
new_case "token_preview_changes_nothing"
run "$TOKEN"
check "exit 0" '[ "$RC" -eq 0 ]'
check "names the current token id" 'grep -q "token_id old-id" <<<"$OUT"'
check "never prints a token" '! grep -q "eyJ" <<<"$OUT"'
check "no write" '! grep -q put-secret-value "$ST/calls"'

new_case "token_apply_happy_path"
run "$TOKEN" --apply
check "exit 0" '[ "$RC" -eq 0 ]'
check "stores the new token" '[ "$(claim "$(token_of)" token_id)" = "new-id" ]'
check "keeps every other key" '[ "$(jq -r .REDIS_PASSWORD "$ST/secret.json")" = "r" ]'
check "old value stays as AWSPREVIOUS" '[ "$(jq -r .HATCHET_CLIENT_TOKEN "$ST/secret.prev.json")" = "$OLD_TOKEN" ]'
check "deletes the mint log stream" 'grep -q "^hatchet/hatchet/" "$ST/deleted_streams"'
check "restarts the discovered consumers, not the engine" \
  'grep -qx fastapi "$ST/restarted" && grep -qx laravel-octane "$ST/restarted" && ! grep -qx hatchet "$ST/restarted"'
check "records the old token id for later" '[ "$(jq -r .old_token_id "$ST/home/.hatchet-token-rotation")" = "old-id" ]'
check "never prints a token" '! grep -q "eyJ" <<<"$OUT"'
check "never puts the token on a command line" '! grep -q "$(token_of)" "$ST/calls"'
check "says the old token is not revoked, and when it expires" 'grep -q "is not revoked; it expires 2026-12-11" <<<"$OUT"'

new_case "token_apply_no_mint_changes_nothing"
FAKE_NOMINT=1 run "$TOKEN" --apply
check "exit 1" '[ "$RC" -eq 1 ]'
check "secret untouched" '[ "$(token_of)" = "$OLD_TOKEN" ]'
check "still deletes the stream" '[ -s "$ST/deleted_streams" ]'

new_case "token_apply_wrong_tenant_is_not_stored"
FAKE_MINT_TENANT=t-other run "$TOKEN" --apply
check "exit 1" '[ "$RC" -eq 1 ]'
check "secret untouched" '[ "$(token_of)" = "$OLD_TOKEN" ]'
check "says why" 'grep -q "different tenant" <<<"$OUT"'

new_case "token_apply_dead_worker_is_not_ok"
FAKE_WORKER_DEAD=1 run "$TOKEN" --apply
check "exit 1" '[ "$RC" -eq 1 ]'
check "does not claim success" '! grep -q "TOKEN ROTATED" <<<"$OUT"'
check "points at the rollback" 'grep -q -- "--rollback" <<<"$OUT"'

new_case "token_rollback_restores_only_the_token"
run "$TOKEN" --apply
# Something else writes the secret after the rotation: a new key.
jq '.NEW_KEY = "added-later"' "$ST/secret.json" > "$ST/x" && cp "$ST/secret.json" "$ST/secret.prev.json" && mv "$ST/x" "$ST/secret.json"
run "$TOKEN" --rollback
check "refuses: AWSPREVIOUS holds the same token" '[ "$RC" -eq 1 ] && grep -q "SAME token" <<<"$OUT"'
check "the later key survives" '[ "$(jq -r .NEW_KEY "$ST/secret.json")" = "added-later" ]'

new_case "token_rollback_keeps_later_keys"
run "$TOKEN" --apply
jq '.NEW_KEY = "added-later"' "$ST/secret.json" > "$ST/x" && mv "$ST/x" "$ST/secret.json"
run "$TOKEN" --rollback
check "exit 0" '[ "$RC" -eq 0 ]'
check "token is the old one again" '[ "$(token_of)" = "$OLD_TOKEN" ]'
check "a key written after the rotation is not undone" '[ "$(jq -r .NEW_KEY "$ST/secret.json")" = "added-later" ]'

echo "CloudFront origin secret rotation"
# ─────────────────────────────────────────────────────────────────────────────
dist_value() { jq -r '.DistributionConfig.Origins.Items[0].CustomHeaders.Items[0].HeaderValue' "$ST/dist.json"; }
rule_values() { jq -c --arg a "$1" '.[] | select(.RuleArn == $a) | .Conditions[] | select(.Field == "http-header") | .HttpHeaderConfig.Values' "$ST/rules.json"; }

new_case "edge_preview_changes_nothing"
run "$EDGE"
check "exit 0" '[ "$RC" -eq 0 ]'
check "finds both header rules" 'grep -q "rules checking X-Origin-Verify: 2" <<<"$OUT"'
check "never prints the value" '! grep -q OLDVALUE <<<"$OUT"'
check "no change" '! grep -qE "modify-rule|update-distribution|put-secret|create-secret" "$ST/calls"'

new_case "edge_apply_happy_path"
run "$EDGE" --apply
NEW=$(cat "$ST/store" 2>/dev/null)
check "exit 0" '[ "$RC" -eq 0 ]'
check "a 64-hex value is stored" '[[ "$NEW" =~ ^[0-9a-f]{64}$ ]]'
check "the distribution sends it" '[ "$(dist_value)" = "$NEW" ]'
check "both rules accept only it" \
  '[ "$(rule_values arn:rule/verified)" = "[\"$NEW\"]" ] && [ "$(rule_values arn:rule/reverb)" = "[\"$NEW\"]" ]'
check "the path condition on the WebSocket rule survives" \
  'jq -e ".[] | select(.RuleArn == \"arn:rule/reverb\") | .Conditions[] | select(.Field == \"path-pattern\") | .PathPatternConfig.Values == [\"/app/*\",\"/apps/*\"]" "$ST/rules.json" >/dev/null'
check "never prints either value" '! grep -q OLDVALUE <<<"$OUT" && ! grep -q "$NEW" <<<"$OUT"'
check "neither value is ever on a command line" '! grep -q "$NEW" "$ST/calls" && ! grep -q OLDVALUE "$ST/calls"'
# The ordering property. Widen before the distribution changes; narrow after.
first_widen=$(grep -n "^elbv2 modify-rule" "$ST/stdin" | head -1 | cut -d: -f1)
dist_line=$(grep -n "^cloudfront update-distribution" "$ST/stdin" | head -1 | cut -d: -f1)
last_narrow=$(grep -n "^elbv2 modify-rule" "$ST/stdin" | tail -1 | cut -d: -f1)
check "rules widened BEFORE the distribution changes" '[ -n "$first_widen" ] && [ "$first_widen" -lt "$dist_line" ]'
check "the widening carried both values" \
  'sed -n "${first_widen}p" "$ST/stdin" | grep -q OLDVALUE && sed -n "${first_widen}p" "$ST/stdin" | grep -q "$NEW"'
check "rules narrowed only AFTER" '[ "$last_narrow" -gt "$dist_line" ]'
check "tells the operator to update Terraform's input" 'grep -q "TF_VAR_cloudfront_origin_secret" <<<"$OUT"'

new_case "edge_sticky_distribution_is_not_ok"
FAKE_DIST_STICKY=1 run "$EDGE" --apply
check "exit 1" '[ "$RC" -eq 1 ]'
check "rules still accept the old value" 'rule_values arn:rule/verified | grep -q OLDVALUE'
check "no success claimed" '! grep -q "ROTATED" <<<"$OUT"'

new_case "edge_site_down_before_changes_nothing"
FAKE_SITE_DOWN=1 run "$EDGE" --apply
check "exit 1" '[ "$RC" -eq 1 ]'
check "no change" '! grep -qE "modify-rule|update-distribution" "$ST/calls"'

new_case "edge_not_in_use_is_refused"
jq '(.[].Conditions) |= map(select(.Field != "http-header"))' "$ST/rules.json" > "$ST/x" && mv "$ST/x" "$ST/rules.json"
jq '.DistributionConfig.Origins.Items[0].CustomHeaders = {"Quantity":0}' "$ST/dist.json" > "$ST/x" && mv "$ST/x" "$ST/dist.json"
run "$EDGE" --apply
check "exit 1" '[ "$RC" -eq 1 ]'
check "sends the operator to Terraform" 'grep -q "through Terraform" <<<"$OUT"'

new_case "edge_finish_narrows_to_what_is_sent"
jq '(.[].Conditions[] | select(.Field == "http-header") | .HttpHeaderConfig.Values) |= . + ["SECOND-VALUE"]' "$ST/rules.json" > "$ST/x" && mv "$ST/x" "$ST/rules.json"
run "$EDGE" --finish
check "exit 0" '[ "$RC" -eq 0 ]'
check "only the sent value is left" '[ "$(rule_values arn:rule/verified)" = "[\"OLDVALUE-0123456789abcdef\"]" ]'

echo
if [ "$FAIL" -eq 0 ]; then
  echo "$PASS checks passed"
else
  echo "$FAIL FAILED, $PASS passed"
  exit 1
fi
