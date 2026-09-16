# deploy/aws

Production infrastructure for GeoRAG on AWS (ADR-0022, 2026-09-08).

The difference from `deploy/azure/` that matters most: **this deploys
itself.** The Azure tree was hand-applied resource definitions — its README
opened by saying nothing in it was touched by CI or CD — and there was no
Bicep, Terraform or ARM template for the container apps at all, so ~55
environment variables per app were set in the portal and drifted freely
from `.env.production.example`. Nothing running could be diffed against
anything in the repository. Starting from a blank cloud is the one chance
not to repeat that.

## Check the preconditions first

```bash
AWS_REGION=<region> bash scripts/operator/aws-preflight.sh
```

Read-only, and it exits non-zero rather than letting a cutover start on a
missed step. It reports **unverified** rather than passing for anything it
cannot reach the account to answer, so run it from a shell with AWS access —
a check nobody could answer is not a check that passed.

It covers the steps below that have a queryable answer: the variables with no
default, the secret keys in `georag/app`, whether `COHERE_API_KEY` holds a
real value rather than the bootstrap placeholder — the one that starts the
tasks cleanly and then fails every query — and whether any SageMaker endpoint
is still running and billing. The one-time actions (Steps 1, 2 and 4) have no
state to query and are listed by the script as still yours.

Note that `scripts/operator/preflight.sh` is a **different** script and does
not gate this deployment: it predates ADR-0022 and checks SSH hosts and SOPS
for the compose model, which Fargate does not use.

## Before the first apply: remote state

Until 2026-09-15 this tree had **no backend block**, so state was a local file
next to whoever ran `terraform apply`. For the one record of what exists in the
account, that is the failure the rest of this tree was written to avoid: lose
the machine and the resources keep running with nothing able to manage them —
the next apply does not adopt them, it tries to *create* them and collides on
names already taken, and the way out is importing every resource by hand. It
also means no locking, and an apply from any ephemeral box (a CI runner, a
cloud dev environment) discards the state when the box is reclaimed.

Create the bucket once per account, then init against it:

```bash
bash deploy/aws/terraform/bootstrap-state.sh georag-tfstate-<account-id> <region>

cd deploy/aws/terraform
cp backend.hcl.example backend.hcl        # fill in the bucket
terraform init -backend-config=backend.hcl
```

The bucket is created by a script rather than by Terraform on purpose: it
cannot be managed by the state it holds. The script is idempotent, so re-running
it also serves as a check that versioning, encryption, the public-access block
and the TLS-only policy are still in place. Versioning is the one to care about
— state is overwritten on every apply.

Locking needs no DynamoDB table: `backend.tf` sets `use_lockfile`, so the lock
is a conditional write in the same bucket. That needs Terraform >= 1.10, which
is why `required_version` says so.

`backend.hcl` is gitignored, along with `*.tfstate` and `*.tfvars`. Nothing in
`backend.hcl` is secret, but it sits where something secret would get pasted.

## The public edge, and why you need no domain

Azure Container Apps handed every app a free `*.azurecontainerapps.io`
hostname WITH a managed certificate. **AWS has no equivalent**, and Route 53
sells domains rather than giving them away — there is no free tier for
registration at any TLD. This tree used to assume that gap away: `app_domain`
and a certificate were required inputs, so nothing could be applied at all
until a domain had been bought.

`edge` closes it, and defaults to the path that costs nothing:

| `edge` | public hostname | needs |
| --- | --- | --- |
| **`cloudfront`** (default) | the distribution's own `*.cloudfront.net` | nothing |
| `alb` | `app_domain` | a domain, and a certificate |

Every CloudFront distribution is served under AWS's own certificate for
`*.cloudfront.net`, because AWS owns that domain and has already validated it.
Put the distribution in front of the load balancer and the platform answers on
real, browser-trusted HTTPS with no domain, no ACM certificate and no DNS.
`app_domain` is unused and unset in this mode, which is why it is no longer in
the A-01 list.

**What it costs.** CloudFront bills per request and per GB out. There is a
free tier; read its current terms in the console rather than trusting a figure
written here. `cloudfront_price_class` defaults to `PriceClass_100` — North
America and Europe only, the cheapest tier.

**What you give up.** A `*.cloudfront.net` address is unbrandable and
obviously a placeholder, which is fine before launch and wrong in front of a
customer. It is also on the Public Suffix List, so a cookie can never be
scoped wider than the exact host; nothing here needs that (`SESSION_DOMAIN` is
unset, making the session cookie host-only) but a future subdomain split would.

**Two things make this safe rather than merely working:**

- The load balancer's listener is plain HTTP in this mode, so its security
  group admits ONLY the `com.amazonaws.global.cloudfront.origin-facing` prefix
  list. Left open to the internet it would serve the whole application
  unencrypted to anyone who resolved its hostname, with CloudFront's
  certificate supplying a false sense of having TLS at all.
- That prefix list is every CloudFront edge, not just yours, so any AWS
  customer who learns the load balancer's hostname could front this
  application from their own distribution and their own domain —
  authentication still applies, so it is not a breach, but it is a ready-made
  phishing page wearing the real login form. `cloudfront_origin_secret` closes
  it: `openssl rand -hex 32`, set it, and the listener's default action
  becomes a 403 for anything not carrying the header. Preflight A-14 reports
  it as a warning rather than a failure, because empty is a defensible choice
  with no users yet.

**The application had to change too.** TLS terminates at CloudFront, and the
load balancer behind it truthfully reports `http` in `X-Forwarded-Proto` —
which `ProxyTrust` honours. Laravel would then generate `http://` URLs into a
page the browser loaded over `https://`, and the browser would block every one
as mixed content. `AppServiceProvider` now calls `URL::forceScheme('https')`
whenever `APP_URL` is an https address, which is true in both edge modes and
false in local development.

**Switching to a real domain later** is `-var edge=alb` plus `app_domain`, at
which point dns.tf takes over and the section below applies. A precondition on
the HTTPS listener fails the plan if `edge = "alb"` arrives without a domain,
since `app_domain` now has a default and A-01 no longer covers it. The
distribution carries the mirror image: setting `app_domain` while `edge` is
still `cloudfront` fails the plan too, because in that mode it is inert —
no certificate, no DNS record, and a hostname you did not choose. The pairing
has to be deliberate in both directions, and an `edge = "alb"` deployment that
re-applies without setting `edge` would otherwise be migrated off its own
domain silently.

**Two things this mode gives up quietly**, beyond the unbrandable hostname:
TLS 1.0 and 1.1 stay negotiable, because CloudFront pins the security policy
to `TLSv1` for its own `*.cloudfront.net` certificate and offers no knob (the
`alb` edge serves `ELBSecurityPolicy-TLS13-1-2-2021-06`); and the
CloudFront-to-load-balancer hop is plain HTTP **across the public internet**,
not inside the VPC — the security group narrows who may open that connection,
it does not encrypt it. Both are pre-launch-acceptable and neither is
acceptable in front of a customer.

## The domain (edge = "alb")

With `edge = "alb"` the load balancer is the public edge and `app_domain` is a
hard prerequisite: `aws_lb_listener.https` cannot be created without a
certificate, and a certificate cannot be issued for a name you do not own.

Since 2026-09-16 the rest is declarative. `dns.tf` issues the ACM
certificate, writes its own DNS validation records, waits for ACM to report
ISSUED, and aliases `app_domain` at the load balancer. Register the domain,
set `app_domain`, apply.

**Register through Route 53** and the hosted zone is created for you, with its
nameservers already written into the registration. That is the only reason the
chain can be fully automatic: Terraform needs write access to the zone that
the registrar actually points at.

Note that `dns.tf` *looks the zone up* and never creates one. A second hosted
zone for a name that already has one is legal, gets a different set of
nameservers, and resolves for nobody — while Terraform reports success and the
records look right in the console.

Deploying to a subdomain of a zone you already own works too: set
`app_domain = "georag.example.com"` and `hosted_zone_name = "example.com"`.
The zone cannot be derived by chopping a label off the domain, because where
the cut falls is not a function of the string — `example.co.uk` is
registrable and `co.uk` is not.

**If the domain is hosted somewhere Terraform cannot write** — Cloudflare,
Namecheap — set `manage_dns = false`. Nothing in `dns.tf` is created,
`acm_certificate_arn` becomes required again, and the validation CNAMEs and
the record pointing at the ALB are pasted in by hand at the registrar
(`terraform output alb_dns_name` is what that record targets). A
precondition on the listener fails the plan if `manage_dns = false` arrives
without a certificate, because `acm_certificate_arn` having a default now
means `aws-preflight.sh` A-01 no longer catches it.

The certificate is **not** gated on the power switch, though the alias record
is. Public ACM certificates are free, and re-validating one on every power-on
would add minutes to a fifteen-minute restart for nothing. The alias has to go
with the ALB, or it dangles at a load balancer that no longer exists.

## The budget this runs under

AWS promotional credit: **$100/month for six months**, plus a one-off $100 for
the account-setup activities. That is the whole budget -- the Activate Founders
application was rejected on 2026-09-15, so there is no larger pool behind it and
no cash line to fall back on.

Two consequences shape the Terraform:

- **The always-on floor is ~$108/month** -- NAT, ALB, RDS storage and backups,
  EFS, logs. That alone exceeds the monthly credit *before a single container
  starts*. The power switch below is what makes the credit sufficient rather
  than a partial subsidy.
- **`db_instance_class` is `db.t4g.small`**, not `db.m7g.large`: $0.032/hour
  instead of $0.18. The variable's own documentation records what the smaller
  class costs -- ~225 max_connections against a measured peak of 99, with no
  PgBouncer in front, and burstable CPU that a long ingestion run can throttle.
  `db.t4g.medium` is the middle step if either bites.
- **One forgotten month at full size burns 5.5 months of credit.**
  `terraform/budget.tf` alerts at 50% and 80% of actual spend and at 100% of
  *forecast*. The forecast one is the useful one: it fires days before the
  credit is gone, while switching off still helps.

A budget **alerts, it does not cap**. AWS has no hard spending limit for
ordinary accounts. Acting on the alert is a human running
`terraform apply -var power=off`.

With the power switch and Spot, $622 of credit buys roughly **1,266 task-hours
-- about 7 hours a day, every day, for six months**, at the real production
sizing. The shipped schedule is 8.5h/day, which is over that ceiling rather
than merely close to it: see the table under "Turning the whole thing off".

## Fargate Spot

`fargate_capacity` defaults to **`spot`**, which is ~70% off and the largest
lever on the running cost:

| | Fargate $/hr | Total $/hr | hrs/day on credit |
| --- | --- | --- | --- |
| On demand | 0.68 | 0.97 | 3.0 |
| **All Spot (default)** | **0.20** | **0.49** | **5.9** |
| Spot, `laravel-octane` on demand | 0.27 | 0.56 | 5.2 |

**What Spot costs you:** a task can be reclaimed with two minutes' notice, and
a replacement only starts when Spot capacity exists. An interruption is a
restart, not a graceful drain -- fine for `hatchet-worker` (Hatchet retries the
step), visible for `laravel-reverb` (websockets drop and reconnect).

**Before a demo**, take the user-facing tier off Spot without giving up the
discount on the nine services nobody sees:

```bash
terraform apply -var 'on_demand_services=["laravel-octane"]'
```

That costs about **$0.07/hour extra -- $12 over 180 hours**. A name that
matches no service fails the plan rather than warning, because a typo there
means believing a tier is protected when it is not.

`fargate_capacity=on_demand` switches everything back. Both capacity providers
are always attached to the cluster, so that switch never fails for want of one.

## Turning the whole thing off

What it costs depends far more on HOURS than on sizing. Against the current
defaults (Fargate Spot, `db.t4g.small`):

| shape | $/month |
| --- | --- |
| powered off | ~$13 |
| 4h/day, weekdays only (a demo stack) | ~$39 |
| **8.5h/day (what the nightly sweeps give you)** | **~$93** |
| 8h/day (the sweeps before `startup_cron` moved to 08:30) | ~$88 |
| 17h/day (the pre-2026-09-16 default) | ~$173 |
| 24/7 | ~$240 |

$555 was the figure before Fargate Spot and `db.t4g.small` became the
defaults; it is what on-demand at the original sizing would cost. The model
these come from reads the sizing out of `main.tf` rather than estimating --
every rate is a named constant, so swapping one moves every number with it.

The **8.5h row is interpolated, not re-modelled**: the rows above sit on a
line of ~$9.50 per daily-hour per month plus ~$12 of always-on (ALB, NAT,
EFS, S3), which reproduces every other figure in the table to the dollar.
Nothing here has been billed on a real account yet, so treat the whole
column as a sizing estimate and check Cost Explorer after the first week.

The switch is a Terraform variable:

```bash
terraform apply -var power=on  -var db_deletion_protection=false   # clear the seatbelt
terraform apply -var power=off -var db_deletion_protection=false   # tear it down
terraform apply -var power=on                                      # ~15 min back up
```

**Powering off takes two applies, and that is not an oversight.** RDS
`deletion_protection` is what stands between a mistyped `terraform destroy` and
the only copy of the database, so it is on by default. Terraform does not clear
it on the way to deleting, and `count = 0` applies no attribute change to a
resource that is going away -- so the flag has to come off in an *earlier*
apply. The first command above is an in-place change with no downtime. Skip it
and the plan fails before anything is destroyed, thanks to a precondition on
`aws_db_subnet_group`; without that precondition the apply would get as far as
the RDS destroy and die there, with the ALB, the NAT gateway and every ECS
service already gone.

**Powering off destroys resources; it does not stop them.** That is deliberate
and `terraform/power.tf` carries the full reasoning. The short version is that
"stopped" does not exist for most of this:

- An **ALB** and a **NAT gateway** have no stopped state at all. They bill for
  as long as they exist -- $63/month between them, with nobody connected.
- **RDS force-starts a stopped instance after 7 days.** The nightly sweeps hide
  that, because they re-stop it the same night. Leave it "stopped" for a month
  and it wakes up, bills a day of compute, and sleeps again, repeatedly, with
  nothing reporting it.

The nightly sweeps are still the right tool for a fifteen-and-a-half-hour
window. They are not an off switch for a week.

**Your data survives.** Nothing holding state is gated: S3 (the corpus), EFS
(the Qdrant index and Redis AOF), ECR, Secrets Manager and the CloudWatch log
groups all stay. The database is destroyed, but `skip_final_snapshot = false`
means RDS writes `georag-pg-final` on the way out. To come back up on that data
rather than on an empty database:

```bash
terraform apply -var power=on -var restore_from_snapshot=georag-pg-final
```

Leave `restore_from_snapshot` unset and you get a fresh, empty instance --
which is what you want for the first ever apply, when no snapshot exists.

**Cycling more than once?** Snapshot identifiers are unique per account, so a
fixed name works exactly once: the second power-off collides with the snapshot
the first one wrote. For a repeating cadence -- a demo stack that comes up for
a few hours on weekdays, say -- give each teardown its own name:

```bash
terraform apply -var power=off -var db_deletion_protection=false \
  -var db_final_snapshot_suffix=$(date +%Y%m%d)
```

Terraform does not clean these up; they are manual snapshots and outlive
`db_backup_retention_days` on purpose. Delete the ones you no longer want by
hand.

> **Do not use a bare `terraform destroy` instead.** Three things stop it, and
> one of them locks you out for a month: `aws_secretsmanager_secret.app` has
> `recovery_window_in_days = 30`, so destroy schedules it for deletion and the
> next apply fails with *"a secret with this name is already scheduled for
> deletion"* until someone runs `aws secretsmanager restore-secret`. The
> versioned S3 buckets (no `force_destroy`) and `deletion_protection` on RDS
> each block it as well. `power = "off"` sidesteps the first two by leaving
> those resources alone, and turns the third into the deliberate two-apply
> sequence above.

`scripts/check-aws-power-flag.py` runs in CI and fails if a new hourly-billed
resource is added without the gate, if a stateful one is added with it, or if a
gated resource carries something that would block its own destruction -- which
is how both of the problems above were found, on 2026-09-16, before the first
power-off ever ran.

> **Already made a budget in the console?** `terraform/budget.tf` creates one
> named `georag-monthly`, and AWS budget names are unique per account. If yours
> has that name the first apply fails on a duplicate; adopt it instead:
> `terraform import aws_budgets_budget.monthly <account-id>:georag-monthly`.
> If it has a different name you get two budgets and duplicate emails -- not
> billed (two are free) but duplicate alerts get muted, and a muted spend alert
> is the thing that file exists to prevent. Deleting the console one before the
> first apply is equally fine: nothing bills until `power=on`.

## Layout

| Path | What it is |
| --- | --- |
| `terraform/` | Everything: VPC, ALB, ECS, RDS, EFS, S3, ECR, IAM, Secrets Manager, EventBridge Scheduler, CloudWatch |
| `terraform/power.tf` | The `power = on\|off` switch, and why "off" destroys rather than stops |
| `terraform/budget.tf` | The spend guard: alerts at 50/80% actual and 100% forecast |
| `terraform/spot.tf` | Fargate Spot, and the per-service on-demand escape hatch |
| `scheduler/` | The two nightly sweep scripts, embedded into task definitions by `terraform/scheduler.tf` |
| `scheduler/tests/` | Behavioural tests for the sweeps, run against a fake `aws` CLI |
| `rotation/` | The `APP_KEY` rotation, and the script it runs inside a one-off task (`terraform/rotation.tf`) |
| `rotation/tests/` | Behavioural tests for the rotation, against a fake `aws` CLI and a fake `php artisan` |

```bash
cd deploy/aws/terraform
terraform init
terraform plan -var-file=production.tfvars
```

`production.tfvars` is not in the repository. The variables with no default
are the three a deployment must decide: `reverb_app_key`, `alert_email` and
`image_tag`.

`app_domain` used to be a fourth. It now defaults to `""`, and on the default
`edge = "cloudfront"` path `edge.tf` carries a precondition that it **must**
stay empty — so an operator following the older sentence and setting it got a
plan-time failure. Loud rather than silent, but a trip hazard on deploy night.
`scripts/operator/aws-preflight.sh` derives this list from the `.tf` files
rather than from this paragraph, so A-01 is the authority if the two ever
disagree again.

`image_tag` is new on 2026-09-16 and replaces a hardcoded `:latest` that could
never have worked. Every ECR repository here sets
`image_tag_mutability = "IMMUTABLE"`, so a tag is written once and never
moved — and nothing pushed `latest` in any case, because cd.yml pushes
`:<short-sha>` and only that. The tag came across from docker-compose.yml,
where `georag/laravel:latest` is just what the last local build left behind.

Pass the short SHA of an image cd.yml has pushed. Two consequences worth
knowing before the first apply:

* **On a brand-new account no image exists yet**, so there is nothing valid to
  pass. Use `bootstrap` and expect every task to fail
  `CannotPullContainerError` until the first deploy — the services recover on
  their own when cd.yml re-registers each task definition with a real SHA.
  Terraform does not wait for steady state, so that first apply reports
  success over a stack that is not running. That is expected here; it is not
  expected later.
* **Re-apply with a real SHA once CD has run.** `georag-app-key-rotation` is
  the reason. Nothing re-registers it — `rotate-app-key.sh` overrides
  `command`, and ECS RunTask cannot override an image at all — so it keeps
  whatever tag the last apply gave it. Leave it on `bootstrap` and the APP_KEY
  rotation fails on an unpullable image partway through the runbook, with the
  platform already down.

`scripts/check-ecs-image-tags.py` fails CI if a literal tag comes back.

It was six until 2026-09-15. `bedrock_chat_endpoint_name` and
`bedrock_parse_endpoint_name` went with ADR-0023, which moved chat and OCR
onto Cohere's own API — the models were AWS Marketplace SageMaker packages
on A100/H100 and ~$2.50/hour, billing whether or not anything called them.
The Cohere equivalents (`cohere_chat_model`, `cohere_parse_model`,
`cohere_base_url`) all have working defaults, and the credential is a secret,
not a tfvar: a tfvar would put it in Terraform state.

`app_domain` is the bare public hostname — no scheme, no path. It drives
`APP_URL`, and through it Sanctum's stateful-domain list, and it is the
Reverb WebSocket origin allowlist. It is also the name the certificate is
issued for and the Route 53 zone the records are written into.

`acm_certificate_arn` was a fifth until 2026-09-16, when `dns.tf` took over
issuing the certificate and managing DNS. See **The domain** below.

`reverb_app_key` needs saying once, because it is one value that has to be
set identically in two unrelated places:

| Where | How it gets there |
| --- | --- |
| The Reverb server and the two publishers | `reverb_app_key` in this tfvars |
| The browser bundle | the `VITE_REVERB_APP_KEY` **repository variable**, baked in by CD |

Its secret half, `REVERB_APP_SECRET`, goes into Secrets Manager out of band
with the other application secrets. The key is public by design
(`config/reverb.php`) — the browser receives it — so it is a repository
variable rather than a secret; the secret is the half that authorises
publishing. If the bundle's key and the server's key disagree the chat
stream simply never connects, so CD fails the build when the variable is
unset rather than shipping a bundle with `key: undefined`.

## Step 0, before anything else

**Confirm that your Cohere API key covers Command A+ and Parse 5, and that
Embed v4 and Rerank 3.5 are enabled serverless in your Bedrock region.** The
model tier is split across two vendors' auth since ADR-0023 and rests on
both halves.

> **What changed on 2026-09-15, and why this section is much shorter than it
> was.** ADR-0022 routed all four Cohere capabilities through Bedrock, and
> this step used to be about deploying two Bedrock Marketplace endpoints and
> naming their configs correctly. When the account was reachable for the
> first time, that turned out not to hold: Command A+ and Parse 5 are **AWS
> Marketplace** SageMaker model packages, not Bedrock models. The two look
> alike and are both called "the marketplace", but only one works with the
> Bedrock adapter — a *Bedrock* Marketplace deployment is invoked through
> `bedrock-runtime` Converse with the endpoint ARN as `modelId`, while an
> *AWS Marketplace* package needs `sagemaker-runtime.invoke_endpoint` and a
> different request and response shape. Getting it wrong does not fail at
> deploy: `terraform apply` succeeds, the tasks start, and chat and OCR fail
> at the first call.
>
> On top of that, Command A+ wanted A100 or H100 instances and Parse ran
> about $2.50/hour, and a Marketplace endpoint has **no idle state** — it
> bills for as long as it exists. ADR-0023 moved both to Cohere's own API.
> Nothing was ever deployed, so no idle cost was incurred, and the endpoint
> naming trap, the nightly recreate and the Sev 1 alarm that watched it are
> all gone with them.
>
> Embeddings and reranking did **not** move. They are serverless Bedrock
> models with IAM auth, they cost nothing at rest, and they are where the
> account's AWS credits get spent.

### The Cohere half

There is no endpoint to stand up and nothing to name. What has to be true is
that the key works and its plan covers both models — a key entitled to chat
but not Parse deploys cleanly and then sends every scanned page to tesseract,
which extracts no tables and raises nothing.

Write it into Secrets Manager **before the first apply**. ECS refuses to
start a task that references a secret key which does not exist, and the
failure presents as a task that never starts rather than as an application
error — see Step 3.

### The Bedrock half

Embed v4 and Rerank 3.5 come from the serverless catalogue rather than from
anything you deployed, so a region that does not carry them has nowhere to
run embeddings or reranking at all:

```bash
aws bedrock list-foundation-models --region "$BEDROCK_REGION" \
  --query 'modelSummaries[?providerName==`Cohere`].[modelId,modelName]' --output table
```

Take the exact `modelId` values from that output rather than assuming them.
As of 2026-09-15 `ca-west-1` (Calgary) carried **zero** Cohere models despite
serving 19 foundation models; `ca-central-1`, `us-east-1` and `us-west-2`
each carried six. `bedrock_region` is a separate variable from `region` for
exactly this reason.

### Then the probe, and commit its report

Every model adapter in this repository says `[UNVERIFIED]` at the top,
because none has been confirmed against a live call from this codebase.
Cohere Parse's shape has **never** been verified on any host — Foundry,
Bedrock or Cohere's own API — and the chat path's three confirmed Foundry
behaviours (JSON `response_format`, reasoning in a sibling field, and the
`<|START_TEXT|>`/`<|END_TEXT|>` sentinel wrapping) do not carry over by
assumption to a new host. Documentation got all three wrong on Foundry; only
a real request settled it.

`aws-preflight.sh` A-11 fails until a report is committed.

### Step 0 end to end, from a workstation

Run this on a machine with a browser. It will **not** complete in a cloud
development container: `aws login` exchanges its authorization code at
`signin.aws.amazon.com`, and an agent sandbox typically refuses that host
(verified 2026-09-15 — `403 to CONNECT`, while `oidc.*.amazonaws.com` and
every service endpoint were reachable). The sign-in page renders, because
that runs in your browser; the token exchange runs from the shell and does
not, so no credentials are ever written and the failure reads as a proxy
error. An IAM Identity Center account can use `aws sso login
--use-device-code --no-browser` from such a container instead.

Needs `uv` on PATH — `bedrock_probe.sh` runs `uv run --no-sync` inside
`src/fastapi`.

```bash
# Every value below is a placeholder with a plausible default. Read each one
# before running the block; none of them is guessed correctly for you.
export REGION=us-east-1          # the stack's region; ca-west-1 has no Cohere
export PROFILE=georag

# 1. Authenticate. Credentials last 12h, renewable 90 days without the browser.
aws configure set region "$REGION" --profile "$PROFILE"
aws login --region "$REGION" --profile "$PROFILE"
aws sts get-caller-identity --profile "$PROFILE"

# 2. Nothing should be running on SageMaker. ADR-0023 removed the Marketplace
#    endpoints; anything listed here bills continuously and no part of this
#    deployment uses it. aws-preflight.sh A-09 checks the same thing.
aws sagemaker list-endpoints --profile "$PROFILE" --region "$REGION" \
  --query 'Endpoints[].[EndpointName,EndpointStatus]' --output table

# 3. What the serverless catalogue actually offers. Embed v4 and Rerank 3.5
#    are catalogue models — take the exact modelIds from this output.
export BEDROCK_REGION="$REGION"
aws bedrock list-foundation-models --profile "$PROFILE" --region "$BEDROCK_REGION" \
  --query 'modelSummaries[?providerName==`Cohere`].[modelId,modelName]' --output table

# 4. The probe. It takes NO --region flag: it reads the environment.
export AWS_PROFILE="$PROFILE"
export AWS_REGION="$REGION"
export BEDROCK_EMBED_MODEL_ID=cohere.embed-v4:0        # confirm against step 3
export BEDROCK_RERANK_MODEL_ID=cohere.rerank-v3-5:0    # confirm against step 3
bash ops/validation/bedrock_probe.sh

# 5. The OTHER probe — chat and OCR, which do not touch AWS at all. It needs
#    no AWS credentials and everything above is irrelevant to it; what it
#    needs is a key whose plan covers BOTH models. Read the HEADLINES block
#    it prints: "role:'system' honoured: False" is a 200 OK with the
#    grounding rules silently dropped, and nothing else in the system can
#    see that.
export COHERE_API_KEY=...   # the key you wrote into Secrets Manager
bash ops/validation/cohere_probe.sh

# 6. COMMIT BOTH REPORTS. The adapters carry [UNVERIFIED] until they exist,
#    and aws-preflight.sh A-11 fails until BOTH land here. The Cohere report
#    never contains the key — only its length.
git add ops/validation/reports/bedrock_probe_*.json ops/validation/reports/cohere_probe_*.json
git commit -m "chore(validation): commit the wire-contract probe reports"

# 7. Everything the preflight could not answer without an account.
AWS_REGION="$REGION" AWS_PROFILE="$PROFILE" bash scripts/operator/aws-preflight.sh
```

> **Two probes, and `aws-preflight.sh` A-11 wants a report from each.**
> Neither covers the other's models: `bedrock_probe` reaches Embed v4 and
> Rerank 3.5, `cohere_probe` reaches Command A+ and Parse 5. One report
> satisfying the gate would leave half the model tier assumed while the
> check read green.

Read the report before trusting any adapter. If Parse's shape differs from
what `_page_from_payload` expects, the adapter says so at runtime: since
2026-09-15 an unrecognised body fails soft to tesseract behind the
`COHERE_PARSE_UNRECOGNISED_RESPONSE` marker and its alarm, rather than
returning a silently blank page that no metric could see. A refused call has
its own marker, `COHERE_PARSE_REJECTED`, and that one is now the *entire*
signal: CloudWatch cannot see a request that never went to AWS.

## Step 1: bootstrap the database by hand, once

`docker/postgresql/init/*.sql` runs from `/docker-entrypoint-initdb.d/` on a
fresh compose volume and nowhere else. Ch 02 §1.2 records that those scripts
"never run on Azure"; they will not run on RDS either. Everything they create
that neither `migrate` nor `db:apply-raw` creates has to be applied once, as
the master user:

It creates three login roles, so it needs three passwords. Pass them as
psql variables — **the file will not run without them**, and under
`\set ON_ERROR_STOP on` it aborts at the first one rather than half-applying:

```bash
SECRET=$(aws secretsmanager get-secret-value --secret-id georag/app \
           --query SecretString --output text)

psql -h "$(terraform -chdir=deploy/aws/terraform output -raw db_endpoint)" \
     -U georag -d georag \
     -v georag_app_password="$(jq -r .GEORAG_APP_PASSWORD <<<"$SECRET")" \
     -v martin_password="$(jq -r .MARTIN_DATABASE_URL <<<"$SECRET" \
                           | sed -E 's|.*://[^:]+:([^@]+)@.*|\1|')" \
     -v hatchet_password="$(jq -r .HATCHET_DATABASE_URL <<<"$SECRET" \
                            | sed -E 's|.*://[^:]+:([^@]+)@.*|\1|')" \
     -f deploy/aws/bootstrap.sql
```

So Step 3 comes first in practice: the secret has to hold these values before
this runs. Two of the three passwords are extracted from **inside** a
connection string rather than read from a key of their own, because those
strings are what the tasks actually present: `martin_password` out of
`MARTIN_DATABASE_URL`, `hatchet_password` out of `HATCHET_DATABASE_URL`. If a
role's password and the one embedded in its URL differ, the role exists, can
log in, and still refuses the only client that uses it.

There is deliberately no `HATCHET_DB_PASSWORD` key. That name is compose-only
(`docker-compose.yml`, `docker/postgresql/init/20-hatchet-database.sql`), and
`jq -r` on a key that is absent prints the string `null` — which would set the
role's password to the literal four characters `null` and fail authentication
later, from a command that looked like it worked.

Run it again whenever you are unsure. It is idempotent by construction and
re-running it is the intended way to verify it: the last thing it prints is
`rolname, rolcanlogin` for all six roles. **`georag_app`, `hatchet` and
`martin_readonly` must all read `t`.** The three `georag_read/write/audit`
grant-holders must read `f` — nothing connects as those.

Getting this wrong does not fail loudly. The extensions are the visible half.
The other half is the Hatchet engine's own role and database — Hatchet runs
with `SERVER_MSGQUEUE_KIND=postgres`, so that database is both its schema
store and its message queue, and without it the engine starts, fails to
migrate, and the worker registers nothing: 51 workflows and 29 crons quietly
do not exist while every container reports healthy.

`bootstrap.sql` says what it deliberately leaves to something else, and why.

## Step 2: nothing — Step 1 does this now

This step used to say: run `ALTER ROLE georag_app PASSWORD ...`, because
`database/raw/phase1/10-georag-app-role.sql` creates the role with a
placeholder password committed to this repository.

Every sentence of that was wrong on AWS, and the three errors compounded into
a deployment that could not start:

1. **That file has never run here.** `db:apply-raw` walks
   `database/raw/manifest.json`, which is deliberately an explicit list rather
   than a glob, and lists five `phase0/*` files. `phase1/10` is not one of
   them.
2. **It could not have won anyway.** `services.tf` runs
   `migrate && db:apply-raw` — migrations first — and migration
   `2026_04_09_173750` creates `georag_app` **NOLOGIN** on any pgsql
   connection. `phase1/10`'s own `CREATE ROLE` is behind `IF NOT EXISTS`, so
   it would have skipped.
3. **`ALTER ROLE ... PASSWORD` does not grant LOGIN.** Verified against
   PostgreSQL rather than assumed: afterwards `rolcanlogin` is still false. A
   password on a role that may not connect is not a login.

So every application container — all five that talk to Postgres — would have
come up and died on `FATAL: role "georag_app" is not permitted to log in`.
Nothing caught it: compose creates the role differently, and CI applies only
`phase0/*.sql` and migrates as the owner.

`bootstrap.sql` now creates `georag_app` itself, with LOGIN, before the
migration chain runs, so `2026_04_09_173750`'s `IF NOT EXISTS` finds it and
leaves it alone. `scripts/check-bootstrap-roles.py` fails CI if that ever
regresses, for any of the three login roles.

Every application container connects as `georag_app`, never as `georag`.
`georag` is the RDS master and owns every table, and `ENABLE ROW LEVEL
SECURITY` does not apply to a table's owner — only `FORCE` does. Connecting
the app as the owner would make every tenancy guarantee in the platform
depend on `FORCE` having reached every table without exception.

## Step 3: write the secrets, by exact key

Terraform creates `georag/app` in Secrets Manager and deliberately does not
populate it: values in `terraform apply` are values in Terraform state. The
single secret holds a JSON object, and each container is handed individual
keys out of it by the execution role.

These are the keys, and the list is exhaustive — `scripts/check-ecs-secret-keys.py`
fails CI if the Terraform references a key that is not documented here, or if
this table names one nothing reads.

| Key | Read by | What it is |
| --- | --- | --- |
| `APP_KEY` | the three Laravel services | Laravel encryption key, `base64:`-prefixed. Rotation has its own runbook below. |
| `GEORAG_APP_PASSWORD` | every application service | `georag_app`'s password, as set in Step 2. Injected twice, as `DB_PASSWORD` for Laravel and `POSTGRES_PASSWORD` for the Python services. |
| `FASTAPI_SERVICE_KEY` | every application service | The `X-Service-Key` both sides check on every internal hop. |
| `QDRANT_API_KEY` | every application service, and qdrant | Injected into qdrant under the name *it* reads, `QDRANT__SERVICE__API_KEY`. |
| `HATCHET_CLIENT_TOKEN` | every application service | Worker/client auth to the engine. Generated by the engine on first boot. |
| `REDIS_PASSWORD` | every application service, and redis | The server sets `requirepass` from it; the clients authenticate with it. Both halves or neither. |
| `MARTIN_DATABASE_URL` | martin | Full connection string, as `martin_readonly`. |
| `HATCHET_DATABASE_URL` | hatchet | Full connection string for the `hatchet` role and database that `bootstrap.sql` creates. |
| `FLOW_JWT_SECRET` | fastapi, hatchet-worker | HS256 signing key for per-flow integration JWTs. |
| `REVERB_APP_SECRET` | the three Laravel services | Signs requests to the Pusher events API. The paired `REVERB_APP_KEY` is public and is a tfvar, not a secret. |
| `COHERE_API_KEY` | fastapi, hatchet-worker | One key, two capabilities: Command A+ chat (`LLM_BACKEND=cohere`) and Parse 5 OCR (`OCR_ENGINE=cohere_parse`), per ADR-0023. The worker needs its own copy — the parser runs there, not behind a call to fastapi. Confirm the key's plan covers **both** models; a key entitled to chat but not Parse starts everything cleanly and then sends every scanned page to tesseract. |
| `APP_KEY_NEXT` | the rotation task only | **Not a go-live key.** It exists only while an `APP_KEY` rotation is in flight; the rotation script writes it, the task reads it, the script removes it. `terraform/rotation.tf` explains why it is a secret rather than a task override, and relies on its absence to make the rotation task unrunnable at any other time. Do not create it now. |

### The one ordering constraint

ECS will not start a task that references a secret key which does not exist —
it fails before the container does, so the failure shows up as a task that
never starts rather than an application error. Write every go-live key above
before the first apply.

`HATCHET_CLIENT_TOKEN` is the awkward one, because the Hatchet engine mints
it and the engine is not up yet. So it gets a placeholder — but **not any
placeholder**.

`hatchet_sdk`'s `ClientConfig` rejects a token that does not start `ey` and
does not decode as three dot-separated JWT parts carrying `sub`, `server_url`
and `grpc_broadcast_address`. `Hatchet()` is constructed at **module import**
in `app/hatchet_workflows/__init__.py`, and `app/routers/shadow_trigger.py`
imports that package — so this is on the FastAPI boot path too, not only the
worker's. A literal string like `set-these-out-of-band` does not "let the
tasks start at all"; it crash-loops **both** Python services with a
`ValidationError` before either serves a request.

Use a syntactically valid unsigned JWT. This exact value is already in
`.github/workflows/ci.yml` and is not a secret — it carries no signature and
no real tenant, and the engine will reject it the moment it is used. It exists
only to get past the SDK's constructor:

```
eyJhbGciOiAibm9uZSIsICJ0eXAiOiAiSldUIn0.eyJzdWIiOiAiMDAwMDAwMDAtMDAwMC0wMDAwLTAwMDAtMDAwMDAwMDAwMDAwIiwgInNlcnZlcl91cmwiOiAibG9jYWxob3N0OjcwNzAiLCAiZ3JwY19icm9hZGNhc3RfYWRkcmVzcyI6ICJsb2NhbGhvc3Q6NzA3MCJ9.
```

**Minting the real one.** The obvious command is `aws ecs execute-command`,
and it does not work here: ECS Exec is not enabled on this cluster — there is
no `enable_execute_command` on any service and no `ssmmessages` grant in
`iam.tf` — and `rotation.tf` records that as a deliberate posture rather than
an oversight. The engine also has no load balancer and no exposed UI port, so
there is no second route in.

Use a one-off `run-task` with a command override instead, the same shape the
`migrate` task already uses. It borrows no standing capability: the task runs,
prints, and exits.

```bash
CLUSTER=georag
SUBNETS=$(terraform -chdir=deploy/aws/terraform output -raw private_subnet_ids)
SG=$(terraform -chdir=deploy/aws/terraform output -raw task_security_group_id)

TASK_ARN=$(aws ecs run-task --cluster "$CLUSTER" \
  --task-definition georag-hatchet \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[$SUBNETS],securityGroups=[$SG],assignPublicIp=DISABLED}" \
  --overrides '{"containerOverrides":[{"name":"hatchet","command":["/hatchet-admin","token","create","--name","ecs","--tenant-id","<tenant>"]}]}' \
  --query 'tasks[0].taskArn' --output text)

aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$TASK_ARN"
# the token is the last line the container printed:
aws logs tail /ecs/georag --since 5m --filter-pattern hatchet
```

The token reaches you through CloudWatch Logs, so treat that log stream as
carrying a credential: read it, write the value into `georag/app`, and do not
leave the stream more widely readable than the secret it just carried.

Then restart the five services that hold the token. **The ECS service names
are bare**, not prefixed — `services.tf` sets `name = each.key`, which is what
`deploy/aws/scheduler/startup-sweep.sh` and `cd.yml` both use:

```bash
for svc in fastapi hatchet-worker laravel-octane laravel-horizon laravel-reverb; do
  aws ecs update-service --cluster georag --service "$svc" --force-new-deployment
done
```

Until that swap the engine is healthy and every worker and client is not,
which reads like a Hatchet fault and is not one. `terraform apply` does not
wait for steady state, so it will report success while this is still true.

## Step 4: create the Qdrant collections, once

`scripts/init_qdrant.py` is the only thing in this repository that creates
`georag_chunks`, and nothing in the runtime path calls it on a cold start. The
drift self-heal in `embed_pending_passages` looks like it would cover this, but
it will not: it fires only when Qdrant is empty AND at least 50 passages are
already marked embedded in Postgres, which is a wipe, not a fresh install.

So on a brand-new Qdrant the collection simply is not there. Run it as a
one-off task against the deployed FastAPI image:

```bash
aws ecs run-task --cluster georag --task-definition georag-fastapi \
  --launch-type FARGATE --network-configuration "$PRIVATE_SUBNET_CONFIG" \
  --overrides '{"containerOverrides":[{"name":"fastapi",
    "command":["python3","/app/scripts/init_qdrant.py"]}]}'
```

It creates both collections with the named dense `""` slot **and** the named
sparse `"text"` slot. The sparse slot is not optional: SPLADE++ has no hosted
equivalent anywhere, so it runs as its own service here, and a collection
bootstrapped without that slot silently loses the sparse leg of hybrid
retrieval. A 2026-06-01 incident was exactly this.

You do not have to remember this one. CD runs `scripts/ops/post_deploy_smoke.py`
as a one-off task after every deploy (`cd.yml`), and its check 4 fails when
Qdrant is up but the collections the query path needs are absent. If you skip
this step the deploy gate tells you.

The collection is sized from `BEDROCK_EMBED_DIMENSION`, the same variable the
writer uses, so it cannot drift from what Cohere Embed v4 is asked to return.

## One posture decision left open: X-Forwarded-For

`TRUSTED_PROXIES` is set for you (`config.tf`, the VPC CIDR) because without
it Laravel honours no `X-Forwarded-*` header at all and every request behind
the TLS-terminating ALB looks like plain HTTP.

`TRUST_FORWARDED_FOR` is **not** set, and that is deliberate.
`App\Support\ProxyTrust` strips `X-Forwarded-For` in production unless it is,
because Azure Container Apps' ingress passed a client-supplied chain straight
through — so `$request->ip()` was whatever the caller typed. Measured
2026-08-20.

An AWS ALB does not do that: it appends the real peer to the chain, and
`drop_invalid_header_fields = true` is set on the listener. So the condition
ProxyTrust names for turning it on — "an ingress that APPENDS to the chain" —
is met here, and with `TRUSTED_PROXIES` scoped to the VPC, Symfony can walk
the chain and discard trusted hops to get the true client IP.

It is left off because that file also says to verify before believing it, and
that cannot be done from a repository. Left off, `$request->ip()` is the
ALB's address: coarse, but unforgeable, and the two limiters that matter
degrade gracefully — the login limiter also keys on the submitted email and
the query limiter keys on the authenticated user id. What you lose is real
client IPs in audit records and in the guest-facing tile limiter.

To turn it on, run ProxyTrust's own three requests against the deployed ALB
and confirm the middle one does **not** change the answer:

```bash
curl -s -o /dev/null -w '%{http_code}\n' "https://$APP_DOMAIN/metrics"
curl -s -o /dev/null -w '%{http_code}\n' -H 'X-Forwarded-For: 10.0.0.1' "https://$APP_DOMAIN/metrics"
curl -s -o /dev/null -w '%{http_code}\n' -H 'X-Forwarded-For: 8.8.8.8'  "https://$APP_DOMAIN/metrics"
```

If a spoofed header still moves the result, leave it off and say so in the
runbook. If all three agree, set `TRUST_FORWARDED_FOR=true`.

### And one more: RATE_LIMIT_ENABLED

`main.py::_assert_production_posture` logs CRITICAL on a `GEORAG_ENV=production`
process when `RATE_LIMIT_ENABLED` is off, saying "no request throttling of any
kind is installed". On this deployment that line will appear on every FastAPI
boot, and it overstates the case.

FastAPI is not in an ALB target group. Nothing outside the VPC can reach it,
and every request it serves arrives from an Octane task over Cloud Map. The
per-client throttling is on the Laravel side, where the clients actually are:
`auth-login`, `queries`, `public-geoscience-tiles` and
`bridge:report-progress` limiters in `AppServiceProvider`, plus inline
`throttle:` on the sensitive routes.

Turning the FastAPI limiter on would not add a per-client control, because
`slowapi` is wired with `key_func=get_remote_address` and every remote address
it sees is an Octane task. At `RATE_LIMIT_DEFAULT=60/minute` that is a global
cap of roughly 60 requests per minute per Octane task across all users — a
crude ceiling that would start returning 429s under ordinary load.

So it is left off, and the CRITICAL line is noise rather than a finding. Worth
fixing properly at some point — either by keying the limiter on a
workspace/user header Laravel already forwards, or by narrowing the posture
check to deployments where FastAPI is internet-facing. Neither belongs in a
cutover.

No CloudWatch alarm watches for CRITICAL log lines, so this does not page.

## The reasoning carried over from Azure

The Azure tree is being deleted, and several of its files were the only
place a hard-won operational fact was written down. Those facts moved here
rather than going with it.

**The nightly sweeps are not `set -e`, and they log to stderr.** One failed
action must not strand the others — on the startup side that would leave
the platform down for the working day — and stdout is block-buffered until
the process exits, so a killed sweep's stdout lines all carry the timestamp
of its death. That is how a truncated sweep once printed "shutdown sweep
complete". Full reasoning in `scheduler/shutdown-sweep.sh`'s header.

**Results are judged by reading state, never by an exit code.** The AWS CLI
has the same property the Azure one did: a stop fails both when it failed
and when the thing was already stopped. The converse matters more — exit 0
with the instance still `available` is a failure, and only a state read
catches it.

**The scheduler role is narrow from the start.** The two Azure scheduler
jobs held Contributor over the whole resource group until 2026-08-23, and
two cron jobs deleted the database with it.

**The DST double-fire mechanism is gone.** Container Apps Jobs schedule in
UTC only, so each sweep fired at both candidate hours with an in-script
guard exiting 0 on the wrong one — and that guard was subtly wrong for two
days a year until 2026-08-21. EventBridge Scheduler takes a timezone.

**The alert suppression window is derived, not written out again.** It
comes from the two cron expressions (`local.maintenance_window_minutes`, in
minutes since 2026-09-16 — an hour-granular version silently floors a window
whose sweeps do not both fire on the hour, and the shortfall lands where the
platform is still down). `maintenance_window_hours` surfaces it as a
Terraform output, fractional, so it can be checked.

## What changed, on purpose

Three defects the Azure deployment carried are fixed here rather than
reproduced. A fresh deployment is the one moment when that costs nothing.

- **Redis gets persistence.** `redis-cc` ran with AOF off and no volume, so
  every restart and every nightly scale-to-zero dropped all sessions and
  any queued Horizon job. Now EFS plus `--appendonly yes`.
- **Qdrant loses two failure classes.** It *did* have persistent storage on
  Azure — an Azure Files share — but that share was mounted with the
  storage account key (rotating it broke the mount on the next restart) and
  had a fixed quota whose exhaustion stalled the optimiser and generated
  10.8M storage transactions in a day. EFS is elastic and IAM-authorised.
- **Bronze gets a backup posture.** It was the one irreplaceable copy: no
  backup workflow, no restore procedure, and Azure PITR covers Postgres
  only. Versioning plus a 90-day non-current retention is the fix, and on
  S3 it is configuration.

Two further things the move buys:

- **The Hatchet worker actually stops overnight.** `--min-replicas 0` is a
  floor, not an off switch, and the worker's 29 crons meant it was never
  idle — so the largest single line item, 4 vCPU / 8 GiB, ran 24/7 through
  every "shutdown". ECS `desired-count 0` stops it.
- **Both ALB-reachable services run two tasks.** At one, every deploy and
  task replacement is a user-visible outage. For Octane the Azure cost
  objection was already answered on evidence (`max_connections` 429
  against a 24h peak of 99; Octane opens PDO connections lazily per
  worker; session, cache and queue are all Redis) — that reasoning is in
  ADR-0022 §3. For Reverb the outage is every open WebSocket, which on
  this platform is every in-flight answer stream.

  Reverb's second task is only correct because `REVERB_SCALING_ENABLED`
  is on. Each instance holds only the subscribers connected to it, and
  Cloud Map hands a publisher one task at random, so without the Redis
  pub/sub backplane roughly half of every query's frames would be
  published to a task with none of that query's subscribers and dropped
  silently. **The desired count and that flag move together.** The count
  also lives in two places — `local.services` in
  `terraform/main.tf` and the `DESIRED` table in
  `scheduler/startup-sweep.sh`, which overwrites Terraform's value every
  morning — so changing one alone reverts overnight. The sweep test
  harness asserts both services come back at 2.

## What this is NOT

Two AZs and an ALB is not high availability. Only the two ALB-reachable
services run more than one task, RDS is Single-AZ, Qdrant and Redis are
single tasks holding EFS mounts, and the platform is stopped nightly on
purpose. Reverb's second task also makes Redis a dependency of WebSocket
fan-out where before it was a dependency of nothing — Redis being a single
task on EFS is unchanged, but it now has one more thing resting on it. The second AZ exists because an ALB requires two subnets. Making
RDS Multi-AZ also means giving up the nightly stop, which is the largest
cost lever this deployment has.

## The Bedrock endpoints are the sharp edge

**Resolved 2026-09-15 (ADR-0023). Left here because the shape is worth
remembering, not because it is still live.**

Command A+ and Parse 5 are not in Bedrock's serverless catalogue. They ran
on SageMaker-managed endpoints that bill for as long as they exist — no idle
state, so the only way not to pay was to delete them nightly and recreate
them each morning from retained configs.

A failed recreate was not a degraded path. It was **no chat and no OCR**,
with no invocation metric to alarm on because there were no invocations to
fail, so the startup sweep waited for `InService` and emitted
`BEDROCK_ENDPOINT_NOT_INSERVICE` at Sev 1. That was the one operational cost
the Bedrock route added with no Azure precedent.

Both models now run on Cohere's own API, billed per token and per page.
There is no endpoint, so there is nothing to delete, nothing to recreate,
nothing to wait for and no alarm — the sweeps only touch ECS and RDS. The
cost that replaced it is a long-lived API key to hold and rotate, and a
refused call that no AWS metric can see: `COHERE_PARSE_REJECTED` and
`COHERE_PARSE_UNRECOGNISED_RESPONSE` are the whole signal for OCR.

## What is still not measured

Unchanged by the move, and recorded so it stays visible rather than being
rediscovered: nothing in production measures answer quality except
`answer_quality_watch` reading `silver.answer_runs`; the two `/metrics`
endpoints that exist are scraped by nothing in either environment; and
Laravel Pulse collects data nobody can view in production. See Ch 12.

Also unchanged: `RERANKER_SCORE_THRESHOLD_HOSTED` is 0.2, measured against
Cohere Rerank **v4**, and Bedrock serves **3.5**. It is the only
retrieval-quality gate in the system, and it is carried over unvalidated.

This document used to say "re-measure it on the golden set". That was wrong
and is worth correcting rather than deleting: calibrating a relevance floor
needs (query, chunk, relevant?) triples, and
`tests/golden_questions/seed_template.yaml` has none — its own header reads
"Status: SKELETON", and all 38 entries carry empty `expected_citations` and
`expected_numeric_values` marked "SME fills". So the re-measurement is
blocked on SME labelling that has not started, on top of needing a corpus
and in-region credentials.

Once the deployment carries traffic there is a route that needs no labels:
`answer_runs.reranker_version` records `cohere-bedrock:cohere.rerank-v3-5:0`,
so 3.5-scored runs are separable from v4-scored ones after the fact and the
floor can be picked from the observed score distribution against refusal
outcomes. Until then, treat 0.2 as unverified and watch the refusal rate —
`ops/runbooks/refusal-rate-spike.md` names this as the first thing to check.

## Testing the sweeps and the rotation

```bash
bash deploy/aws/scheduler/tests/run.sh
bash deploy/aws/rotation/tests/run.sh
```

No credentials, no network, no mutation — a fake `aws` CLI driven by
environment variables, plus a fake `php artisan` for the rotation. There is
still no staging environment to rehearse either on, so these harnesses
remain the only thing between a scheduler edit and finding out at 06:00, or
between a rotation edit and finding out that the audit ledger is
unreadable. Both run in CI.

## Rotating APP_KEY

```bash
export ROTATE_SUBNETS="$(terraform -chdir=terraform output -raw private_subnet_ids)"
export ROTATE_SECURITY_GROUP="$(terraform -chdir=terraform output -raw task_security_group_id)"

bash rotation/rotate-app-key.sh            # preflight + plan, mutates nothing
bash rotation/rotate-app-key.sh --apply    # do it
```

`ops/runbooks/secret-rotation.md` §2 is the procedure and the reasoning.
Two things to know before running it:

- **It takes the platform down.** `laravel-octane` and `laravel-horizon`
  are scaled to zero while `query_audit_log` is re-encrypted, because both
  write `encrypted` columns and `APP_MAINTENANCE_DRIVER` is `file` — a
  maintenance page cannot be made to cover two Octane tasks.
- **It must run inside the maintenance window**, after `startup-sweep.sh`
  has brought RDS up. It refuses to start against a stopped database.
