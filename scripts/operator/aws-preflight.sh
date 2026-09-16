#!/usr/bin/env bash
# aws-preflight.sh — verify every precondition for the ECS Fargate cutover.
#
# Read-only. Mutates nothing, in the repository or in AWS. Returns non-zero if
# any A-xx item fails.
#
# Why this exists, separately from preflight.sh: that script predates ADR-0022
# and gates the deployment model this one replaced — SOPS age keys, an SSH host
# trio per environment, a SOPS-encrypted .env.production.enc, an Alertmanager
# template. Fargate has no SSH hosts, configuration comes from Secrets Manager,
# and Prometheus/Alertmanager are defined nowhere in this repository. Running it
# before an AWS cutover verifies the wrong things and can pass while verifying
# nothing that matters here.
#
# The checks below are not a restatement of deploy/aws/README.md. Each one is a
# precondition whose absence produces a failure that is silent, delayed, or
# reads as a fault in something else:
#
#   A-05  state/tfvars committed        — plaintext secrets in git, permanently
#   A-08  a placeholder credential      — the task STARTS, then every query and
#                                         every scanned page fails at runtime
#   A-10  a missing secret key          — the task never starts; not an app error
#   A-11  no probe report               — every model wire shape is assumed
#   A-13  state kept locally            — lose the file, orphan every resource
#
# Checks that need AWS report `warn`, not `fail`, when the CLI or credentials
# are absent: an unanswerable question is not a passed one. Run this from a
# shell that can reach the target account to get a real answer.
#
# Usage:
#   bash scripts/operator/aws-preflight.sh
#   AWS_REGION=ca-central-1 bash scripts/operator/aws-preflight.sh

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

TF_DIR="deploy/aws/terraform"
README="deploy/aws/README.md"
SECRET_ID="${SECRET_ID:-georag/app}"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-}}"
PYTHON="${PYTHON:-python3}"

c_red() { printf '\033[31m%s\033[0m\n' "$*"; }
c_grn() { printf '\033[32m%s\033[0m\n' "$*"; }
c_yel() { printf '\033[33m%s\033[0m\n' "$*"; }
c_blu() { printf '\033[34m%s\033[0m\n' "$*"; }

PASS=0
FAIL=0
WARN=0

check() {
  local id="$1" desc="$2" status="$3" detail="${4:-}"
  if [ "$status" = "ok" ]; then
    c_grn "✓ ${id}  ${desc}"
    [ -n "$detail" ] && echo "       ${detail}"
    PASS=$((PASS + 1))
  elif [ "$status" = "warn" ]; then
    c_yel "⚠ ${id}  ${desc}"
    [ -n "$detail" ] && echo "       ${detail}"
    WARN=$((WARN + 1))
  else
    c_red "✗ ${id}  ${desc}"
    [ -n "$detail" ] && echo "       ${detail}"
    FAIL=$((FAIL + 1))
  fi
  return 0
}

have_aws() { command -v aws >/dev/null 2>&1; }

AWS_USABLE=0

c_blu "GeoRAG AWS go-live preflight"
c_blu "Repo:   ${REPO_ROOT}"
c_blu "Region: ${REGION:-<unset>}   Secret: ${SECRET_ID}"
echo

# ---------------------------------------------------------------------------
# A-01 — every Terraform variable without a default has a value supplied
#
# The names are read out of the .tf files rather than listed here, so a variable
# added later cannot quietly escape the gate.
# ---------------------------------------------------------------------------
NO_DEFAULT=$("$PYTHON" - "$TF_DIR" <<'PY' 2>/dev/null
import glob, os, re, sys
d = sys.argv[1]
for f in sorted(glob.glob(os.path.join(d, "*.tf"))):
    src = open(f, encoding="utf-8").read()
    for m in re.finditer(r'variable\s+"([^"]+)"\s*\{(.*?)\n\}', src, re.S):
        if not re.search(r'^\s*default\s*=', m.group(2), re.M):
            print(m.group(1))
PY
)

if [ -z "$NO_DEFAULT" ]; then
  check "A-01" "Terraform variables without defaults are supplied" warn \
    "could not parse ${TF_DIR}/*.tf — check ${PYTHON} is available"
else
  missing=""
  found=""
  for v in $NO_DEFAULT; do
    env_name="TF_VAR_${v}"
    if [ -n "${!env_name:-}" ]; then
      found="${found} ${v}(env)"
    elif ls "$TF_DIR"/*.tfvars "$TF_DIR"/*.tfvars.json >/dev/null 2>&1 \
      && grep -rqE "^[[:space:]]*\"?${v}\"?[[:space:]]*[=:]" "$TF_DIR"/*.tfvars "$TF_DIR"/*.tfvars.json 2>/dev/null; then
      found="${found} ${v}(tfvars)"
    else
      missing="${missing} ${v}"
    fi
  done
  if [ -z "$missing" ]; then
    check "A-01" "all no-default Terraform variables supplied" ok "${found# }"
  else
    check "A-01" "all no-default Terraform variables supplied" fail \
      "unset:${missing} — apply will prompt or fail; see ${README}"
  fi
fi

# ---------------------------------------------------------------------------
# A-02 — secret keys the Terraform references are documented, and vice versa
#
# Delegated to the existing checker, which is the authority and runs in CI.
# ---------------------------------------------------------------------------
if [ -f scripts/check-ecs-secret-keys.py ]; then
  if out=$("$PYTHON" scripts/check-ecs-secret-keys.py 2>&1); then
    check "A-02" "ECS secret-key wiring matches its documentation" ok
  else
    check "A-02" "ECS secret-key wiring matches its documentation" fail \
      "$(printf '%s' "$out" | tail -3 | tr '\n' ' ')"
  fi
else
  check "A-02" "ECS secret-key wiring matches its documentation" fail \
    "scripts/check-ecs-secret-keys.py missing"
fi

# ---------------------------------------------------------------------------
# A-03 — GEORAG_ENV=production
#
# main.py::_assert_production_posture is the only thing that reports a security
# control being off, and it is gated on this value.
# ---------------------------------------------------------------------------
if grep -qE '^\s*GEORAG_ENV\s*=\s*"production"' "$TF_DIR"/*.tf 2>/dev/null; then
  check "A-03" "GEORAG_ENV=production (arms _assert_production_posture)" ok
else
  check "A-03" "GEORAG_ENV=production (arms _assert_production_posture)" fail \
    "without it the production posture assertions never run"
fi

# ---------------------------------------------------------------------------
# A-04 — terraform fmt is clean
# ---------------------------------------------------------------------------
if command -v terraform >/dev/null 2>&1; then
  if unformatted=$(terraform -chdir="$TF_DIR" fmt -check -recursive 2>&1); then
    check "A-04" "terraform fmt clean" ok
  else
    check "A-04" "terraform fmt clean" fail \
      "run: terraform -chdir=${TF_DIR} fmt -recursive   [${unformatted//$'\n'/, }]"
  fi
else
  check "A-04" "terraform fmt clean" warn "terraform not installed"
fi

# ---------------------------------------------------------------------------
# A-05 — no Terraform state, tfvars or plan tracked by git
#
# State holds every value it touches in plaintext. This repository has already
# committed one live credential (scripts/phase0_acceptance.sh), so this is a
# demonstrated failure mode, not a precaution.
# ---------------------------------------------------------------------------
tracked=$(git ls-files -- '*.tfstate' '*.tfstate.*' '*.tfvars' '*.tfvars.json' '*.tfplan' 2>/dev/null \
  | grep -v '\.tfvars\.example$' || true)
if [ -z "$tracked" ]; then
  check "A-05" "no Terraform state/tfvars/plan tracked by git" ok
else
  check "A-05" "no Terraform state/tfvars/plan tracked by git" fail \
    "TRACKED: ${tracked//$'\n'/, } — these carry plaintext secrets; git rm --cached and rotate"
fi

# ---------------------------------------------------------------------------
# A-13 — Terraform state is remote, not a local file
#
# With no backend block, state lands next to whoever ran apply. Lose that
# machine and the resources keep running with nothing able to manage them: the
# next apply does not adopt them, it tries to create them and collides on names
# already taken. Applying from an ephemeral box discards the state outright.
# ---------------------------------------------------------------------------
if grep -qE '^\s*backend\s+"s3"' "$TF_DIR"/*.tf 2>/dev/null; then
  if [ -f "$TF_DIR/backend.hcl" ] || [ -n "${TF_BACKEND_CONFIG:-}" ]; then
    check "A-13" "Terraform state is remote (S3 backend configured)" ok
  else
    check "A-13" "Terraform state is remote (S3 backend configured)" fail \
      "backend.tf declares S3 but ${TF_DIR}/backend.hcl is absent — partial config needs it: cp backend.hcl.example backend.hcl, then terraform init -backend-config=backend.hcl"
  fi
else
  check "A-13" "Terraform state is remote (S3 backend configured)" fail \
    "no backend block — state would be LOCAL; losing it orphans every resource"
fi

# Local state left lying around means an apply already ran without the backend.
if ls "$TF_DIR"/*.tfstate >/dev/null 2>&1; then
  check "A-13b" "no local .tfstate in the working directory" fail \
    "a local state file exists — migrate it: terraform init -migrate-state -backend-config=backend.hcl"
else
  check "A-13b" "no local .tfstate in the working directory" ok
fi

# ---------------------------------------------------------------------------
# A-14 — the CloudFront origin is not open to every other AWS customer
# ---------------------------------------------------------------------------
# Only meaningful when CloudFront is the edge. The load balancer's security
# group admits the com.amazonaws.global.cloudfront.origin-facing prefix list,
# which is EVERY CloudFront edge rather than only this distribution: without
# the shared header, any AWS customer who learns the load balancer's hostname
# can front this application from their own distribution and their own domain.
# A warning, not a failure — an empty value is a defensible choice while there
# are no users, and edge.tf says so.
# Both values are read the way A-01 reads everything else — TF_VAR_* first,
# then *.tfvars, then the variable's own default. Reading the default alone
# would report "not applicable" for an operator who selected edge = "alb" in
# tfvars, and — the expensive direction — would report the secret as UNSET for
# one who supplied it as TF_VAR_cloudfront_origin_secret, which is the normal
# way to pass a value marked `sensitive` and the only way that keeps it out of
# a file at all. A warning that fires when nothing is wrong is a warning that
# gets ignored when something is.
tf_value() { # tf_value <variable> <file-declaring-it>
  local var="$1" file="$2" env_name="TF_VAR_$1" found
  if [ -n "${!env_name:-}" ]; then
    printf '%s' "${!env_name}"
    return 0
  fi
  found="$(grep -hE "^[[:space:]]*${var}[[:space:]]*=" "$TF_DIR"/*.tfvars 2>/dev/null |
    head -1 | sed -E 's/^[^=]*=[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/')"
  if [ -z "$found" ]; then
    found="$(sed -n "/variable \"${var}\"/,/^}/p" "$file" 2>/dev/null |
      sed -n 's/^  default *= *"\(.*\)"/\1/p' | head -1)"
  fi
  printf '%s' "$found"
}

EDGE_MODE="$(tf_value edge "$TF_DIR/edge.tf")"
ORIGIN_SECRET="$(tf_value cloudfront_origin_secret "$TF_DIR/edge.tf")"
if [ "${EDGE_MODE:-cloudfront}" != "cloudfront" ]; then
  check "A-14" "CloudFront origin secret set" ok "edge is \"${EDGE_MODE}\"; not applicable"
elif [ -n "$ORIGIN_SECRET" ]; then
  check "A-14" "CloudFront origin secret set" ok
else
  check "A-14" "CloudFront origin secret set" warn \
    "unset — the ALB accepts any CloudFront distribution, not just yours. openssl rand -hex 32, then set cloudfront_origin_secret (tfvars or TF_VAR_cloudfront_origin_secret)"
fi

# ---------------------------------------------------------------------------
# A-06 — AWS credentials usable
# ---------------------------------------------------------------------------
if ! have_aws; then
  check "A-06" "AWS credentials usable" warn \
    "aws CLI not installed — every AWS-side check below is unanswerable here"
elif [ -z "$REGION" ]; then
  check "A-06" "AWS credentials usable" warn \
    "no AWS_REGION/AWS_DEFAULT_REGION set"
elif ident=$(aws sts get-caller-identity --output text --query Arn 2>&1); then
  check "A-06" "AWS credentials usable" ok "$ident"
  AWS_USABLE=1
else
  check "A-06" "AWS credentials usable" warn \
    "$(printf '%s' "$ident" | tail -1)"
fi

# ---------------------------------------------------------------------------
# A-07 — Cohere models offered in the target region (README Step 0)
# ---------------------------------------------------------------------------
if [ "$AWS_USABLE" = "1" ]; then
  models=$(aws bedrock list-foundation-models --region "$REGION" \
    --query 'modelSummaries[?providerName==`Cohere`].modelId' --output text 2>/dev/null || true)
  if [ -n "$models" ]; then
    check "A-07" "Cohere models offered in ${REGION}" ok \
      "$(printf '%s' "$models" | tr '\t' ' ')"
  else
    check "A-07" "Cohere models offered in ${REGION}" fail \
      "none listed — ADR-0022 §11 records api.cohere.com as the escape hatch"
  fi
else
  check "A-07" "Cohere models offered in region" warn "needs AWS access"
fi

# ---------------------------------------------------------------------------
# A-08 — COHERE_API_KEY is a real value, not the bootstrap placeholder
#
# This replaced A-08/A-09, which checked that the two Bedrock Marketplace
# endpoints were InService and that their configs followed the
# `<endpoint-name>-config` convention the nightly sweep recreated them by.
# ADR-0023 removed the endpoints, so both checks now have nothing to look at.
#
# What replaced them is the failure the Cohere route actually has, and it is a
# nastier shape than A-10's. A-10 catches a MISSING key: ECS then refuses to
# start the task, which is loud. A key that is PRESENT but still holds
# config.tf's `set-these-out-of-band` placeholder starts the task cleanly, and
# then every chat query 401s and every scanned page silently falls back to
# tesseract with no tables. Nothing about the deploy looks wrong.
#
# The value is never printed or logged — only its length and whether it equals
# the placeholder.
# ---------------------------------------------------------------------------
if [ "$AWS_USABLE" = "1" ]; then
  cohere_state=$(aws secretsmanager get-secret-value --secret-id "$SECRET_ID" \
    --query SecretString --output text 2>/dev/null | "$PYTHON" -c '
import json, sys
try:
    v = json.load(sys.stdin).get("COHERE_API_KEY")
except Exception:
    print("UNREADABLE"); raise SystemExit
if v is None:
    print("ABSENT")
elif not str(v).strip():
    print("BLANK")
elif str(v).strip() == "set-these-out-of-band" or "PLACEHOLDER" in str(v).upper():
    print("PLACEHOLDER")
else:
    print("SET len=%d" % len(str(v).strip()))
' 2>/dev/null || echo "UNREADABLE")
  case "$cohere_state" in
    SET*)
      check "A-08" "COHERE_API_KEY is a real value" ok "$cohere_state"
      ;;
    ABSENT|BLANK|PLACEHOLDER)
      check "A-08" "COHERE_API_KEY is a real value" fail \
        "${cohere_state} — chat 401s and every scanned page falls back to tesseract, silently. ${README} Step 3"
      ;;
    *)
      check "A-08" "COHERE_API_KEY is a real value" warn "secret unreadable"
      ;;
  esac
else
  check "A-08" "COHERE_API_KEY is a real value" warn "needs AWS access"
fi

# ---------------------------------------------------------------------------
# A-09 — no Marketplace endpoints are left running and billing
#
# The inverse of the check this number used to be. ADR-0023 moved chat and OCR
# off Bedrock Marketplace because those endpoints bill for as long as they
# exist, with no idle state — roughly $600/month for Parse alone even under the
# nightly shutdown. Nothing in this deployment creates one any more, and the
# scheduler role no longer holds DeleteEndpoint, so an endpoint left over from
# an experiment would accrue forever with nothing to turn it off.
#
# ADR-0023 recorded that `sagemaker list-endpoints` returned 0 in all four
# candidate regions on 2026-09-15. This keeps that true.
# ---------------------------------------------------------------------------
if [ "$AWS_USABLE" = "1" ]; then
  live_eps=$(aws sagemaker list-endpoints --query 'Endpoints[].EndpointName' \
    --output text 2>/dev/null || echo "UNREADABLE")
  if [ "$live_eps" = "UNREADABLE" ]; then
    check "A-09" "no SageMaker endpoints billing" warn "list-endpoints refused"
  elif [ -z "$live_eps" ] || [ "$live_eps" = "None" ]; then
    check "A-09" "no SageMaker endpoints billing" ok "0 endpoints"
  else
    check "A-09" "no SageMaker endpoints billing" fail \
      "still running: ${live_eps} — these bill continuously and nothing in this deployment uses them (ADR-0023)"
  fi
else
  check "A-09" "no SageMaker endpoints billing" warn "needs AWS access"
fi

# ---------------------------------------------------------------------------
# A-10 — every go-live secret key exists in georag/app
#
# ECS refuses to start a task referencing a key that does not exist, and the
# failure surfaces as a task that never starts rather than an application error.
# The key list is read from the README table that check-ecs-secret-keys.py
# already treats as authoritative; APP_KEY_NEXT is excluded because the README
# marks it "Not a go-live key" and rotation.tf relies on its absence.
# ---------------------------------------------------------------------------
GOLIVE_KEYS=$("$PYTHON" - "$README" <<'PY' 2>/dev/null
import re, sys
src = open(sys.argv[1], encoding="utf-8").read()
for k in re.findall(r"^\|\s*`([A-Z0-9_]+)`\s*\|", src, re.M):
    if k != "APP_KEY_NEXT":
        print(k)
PY
)

if [ "$AWS_USABLE" = "1" ] && [ -n "$GOLIVE_KEYS" ]; then
  payload=$(aws secretsmanager get-secret-value --secret-id "$SECRET_ID" \
    --query SecretString --output text 2>/dev/null || true)
  if [ -z "$payload" ]; then
    check "A-10" "go-live secret keys present in ${SECRET_ID}" fail \
      "secret absent or unreadable — every task will fail to start"
  else
    present=$(printf '%s' "$payload" | "$PYTHON" -c \
      'import json,sys; print(" ".join(json.load(sys.stdin).keys()))' 2>/dev/null || echo "")
    absent=""
    for k in $GOLIVE_KEYS; do
      case " $present " in *" $k "*) ;; *) absent="${absent} ${k}" ;; esac
    done
    if [ -z "$absent" ]; then
      extra=""
      case " $present " in *" APP_KEY_NEXT "*) extra=" (APP_KEY_NEXT present — rotation in flight?)" ;; esac
      check "A-10" "all go-live secret keys present in ${SECRET_ID}" ok \
        "$(printf '%s' "$GOLIVE_KEYS" | wc -w | tr -d ' ') keys${extra}"
    else
      check "A-10" "all go-live secret keys present in ${SECRET_ID}" fail \
        "MISSING:${absent} — referencing tasks will never start"
    fi
  fi
else
  n=$(printf '%s' "$GOLIVE_KEYS" | wc -w | tr -d ' ')
  check "A-10" "go-live secret keys present in ${SECRET_ID}" warn \
    "needs AWS access — ${n} keys expected, per ${README} Step 3"
fi

# ---------------------------------------------------------------------------
# A-11 — a wire-contract probe report is committed
#
# Every model adapter says at the top that it was written to documentation and
# never verified. The path this replaced had three behaviours that documentation
# alone got wrong, settled only by a live call. Cohere Parse has never been
# verified on any host, and after ADR-0023 neither has Cohere chat: the probe
# still covers embeddings and reranking on Bedrock, and the chat and parse
# halves now need a run against api.cohere.com.
# ---------------------------------------------------------------------------
# TWO reports, not one. The model tier is split across two vendors' auth
# since ADR-0023 and neither probe covers the other's models: bedrock_probe
# reaches Embed v4 and Rerank 3.5, cohere_probe reaches Command A+ and Parse
# 5. A single report satisfying this check would leave half the tier assumed
# while the gate read green — which is the shape of defect this whole file
# exists for.
probe_missing=""
probe_found=""

for probe in bedrock cohere; do
  count=$(ls "ops/validation/reports/${probe}_probe_"*.json 2>/dev/null | wc -l | tr -d ' ')
  if [ "$count" != "0" ]; then
    newest=$(ls -t "ops/validation/reports/${probe}_probe_"*.json 2>/dev/null | head -1)
    probe_found="${probe_found}${newest} "
  else
    probe_missing="${probe_missing}${probe} "
  fi
done

if [ -z "$probe_missing" ]; then
  check "A-11" "wire-contract probe reports present (bedrock + cohere)" ok "$probe_found"
else
  check "A-11" "wire-contract probe reports present (bedrock + cohere)" fail \
    "MISSING:${probe_missing}— run ops/validation/bedrock_probe.sh and ops/validation/cohere_probe.sh. Until both land, that half of the model tier is ASSUMED"
fi

# ---------------------------------------------------------------------------
# A-12 — VITE_REVERB_APP_KEY repo variable (baked into the frontend at build)
# ---------------------------------------------------------------------------
if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
  if gh variable list --json name -q '.[].name' 2>/dev/null | grep -qx 'VITE_REVERB_APP_KEY'; then
    check "A-12" "VITE_REVERB_APP_KEY repo variable set" ok
  else
    check "A-12" "VITE_REVERB_APP_KEY repo variable set" fail \
      "the built frontend cannot subscribe to Reverb without it"
  fi
else
  check "A-12" "VITE_REVERB_APP_KEY repo variable set" warn "gh CLI unavailable"
fi

# ---------------------------------------------------------------------------
echo
c_blu "Not checked here — these are one-time actions with no queryable result:"
echo "  • Step 1  bootstrap.sql run as the RDS master"
echo "  • Step 2  ALTER ROLE georag_app PASSWORD"
echo "  • Step 4  scripts/init_qdrant.py (CD's post_deploy_smoke check 4 catches a miss)"
echo "  • HATCHET_CLIENT_TOKEN swapped from placeholder to the engine-minted value"
echo "  • RERANKER_SCORE_THRESHOLD_HOSTED is Rerank v4's 0.2 carried to 3.5, unvalidated"
echo "  • TRUST_FORWARDED_FOR / RATE_LIMIT_ENABLED posture decisions (${README})"
echo

if [ "$FAIL" = "0" ] && [ "$WARN" = "0" ]; then
  c_grn "═══════════════════════════════════════════════════════════════════"
  c_grn "  ALL CHECKS PASSED — preconditions verified. ${PASS} passed."
  c_grn "═══════════════════════════════════════════════════════════════════"
  exit 0
elif [ "$FAIL" = "0" ]; then
  c_yel "═══════════════════════════════════════════════════════════════════"
  c_yel "  ${PASS} passed, ${WARN} UNVERIFIED — an unanswered check is not a pass."
  c_yel "  Re-run from a shell with AWS access before cutting over."
  c_yel "═══════════════════════════════════════════════════════════════════"
  exit 0
else
  c_red "═══════════════════════════════════════════════════════════════════"
  c_red "  ${PASS} passed, ${WARN} unverified, ${FAIL} FAILED — do not cut over."
  c_red "  See ${README}."
  c_red "═══════════════════════════════════════════════════════════════════"
  exit 1
fi
