# ADR 0023: Cohere chat and parse move to Cohere's own API

- **Date**: 2026-09-15
- **Status**: Proposed
- **Deciders**: Kyle Maguire (SME)
- **Supersedes**: ADR-0022 decision 1 (Cohere route = Amazon Bedrock, option
  D). The rest of ADR-0022 stands unchanged: AWS is still the production
  cloud, RDS is still the database, ECS Fargate is still the compute, and
  SPLADE++ is still a self-hosted Fargate sidecar.

## Context

ADR-0022 chose to reach all four Cohere capabilities through Amazon Bedrock:
Embed v4 and Rerank 3.5 from the serverless catalogue, Command A+ and Parse 5
through **Bedrock Marketplace** endpoints. Its own Consequences section
flagged the load-bearing assumption:

> **The route depends on Command A+ and Parse 5 being subscribable in Bedrock
> Marketplace in the target region**, which could not be verified from the
> session that wrote this.

It could not be verified because that session had no AWS access. On
2026-09-15 the account was reachable for the first time, and the assumption
does not hold.

### What was actually found, 2026-09-15

All figures below come from the account (`046369253475`) via CloudShell, not
from documentation.

**1. Command A+ and Parse 5 are not on Bedrock at all.** They are listed on
**AWS Marketplace** as SageMaker model packages — a different product from
Bedrock Marketplace. The distinction decides the whole adapter layer: a
Bedrock Marketplace deployment is invoked through `bedrock-runtime` Converse
with the endpoint ARN as `modelId`, which is what `llm_bedrock.py` does,
while a SageMaker model package needs `sagemaker-runtime.invoke_endpoint`
with a different request and response shape. That is ADR-0022's option C,
costed there as "full rewrite ×4 + endpoint ops".

**2. The instance classes make them uneconomic.** Command A+ requires A100 or
H100 instances. Parse 5 runs about **$2.50/hour**. Marketplace endpoints have
no idle state — they bill for the instance whether or not anything calls
them. Even under the nightly shutdown (8 h/day) Parse alone is ~$600/month to
*exist*, before a single page is read; Command A+ on A100/H100 is an order of
magnitude worse. For a platform targeting junior mining companies this is not
a tuning problem, it is the wrong cost shape.

**3. The original target region cannot host the model tier either.**

| Region | Cohere models in Bedrock | Marketplace endpoints API |
|---|---|---|
| `ca-west-1` (Calgary) | **0** | `AccessDeniedException` |
| `ca-central-1` (Montreal) | 6 | available |
| `us-east-1` | 6 | available |
| `us-west-2` | 6 | available |

Calgary was the working assumption and is simply not viable: Bedrock is there
(19 foundation models) but carries no Cohere at all, and
`ListMarketplaceModelEndpoints` is refused outright. The call was made as the
account **root** user, which no IAM policy can deny, so that refusal is the
service reporting it does not exist in the region rather than a permissions
problem.

**4. Nothing was ever deployed.** `ListMarketplaceModelEndpoints` returns 0 in
every region that supports it, and `sagemaker list-endpoints` returns 0 in all
four. The Marketplace subscription completed; the endpoint deployment never
did. No idle billing has been incurred.

### Why the documented escape hatch was not simply available

ADR-0022 §11 records the fallback as "chat and parse on `api.cohere.com`,
embeddings and reranking left on Bedrock". That is the decision taken here,
but it was an intention rather than a capability: there is **no**
`api.cohere.com` client anywhere in the codebase, and `LLM_BACKEND` accepts
only `bedrock | vllm | anthropic` (`app/config.py:276`). Taking the escape
hatch means writing two adapters.

## Options considered

| Option | Cost shape | Capability | Effort | Outcome |
|---|---|---|---|---|
| A. Deploy the AWS Marketplace SageMaker packages | **A100/H100 + $2.50/h, billed idle** | Full parity | High — rewrite ×2 + endpoint ops | Rejected on cost. This is ADR-0022 option C, and the reason it was not chosen then. |
| B. Substitute AWS-native services — Textract for OCR, a Bedrock serverless model for chat | Per page / per token, **$0 idle** | Partial — Textract is not Parse (no reading-order narrative, no image description); chat is not Command A+ | Medium — one new adapter, one model-id change | Rejected: real capability loss on the OCR half. Kept on the table as the cheapest fallback if the Cohere API bill ever becomes the binding constraint. |
| C. **Chat + Parse on Cohere's own API; Embed on Bedrock** | Per token, **$0 idle** | **Full parity** ✅ | Medium — two new adapters, no new dependencies | **Chosen** — see Decision. |
| D. All four on Cohere's own API | Per token, $0 idle | Full parity, and Rerank returns to v4 | Medium+ | Partially adopted; see the open sub-decision on rerank. |

Option B deserves a fair hearing, because a future maintainer will be tempted
by it. Textract is genuinely strong at tables, is priced per page
(`$0.020`/page for Tables+Queries sync, `$0.070` for Forms+Tables+Queries
async in `us-east-1`, from the AWS Price List API, publication date
2026-09-11), charges nothing when idle, and keeps every byte inside AWS with
IAM auth. What it does not do is what `cohere_parse_client` was built around:
reading-order text, markdown output, and image description for plan sheets
and cross-sections. That gap is why it lost, not price.

## Decision

**Command A+ and Parse 5 are reached through Cohere's own API
(`api.cohere.com`) with an API key. Embed v4 stays on Bedrock serverless with
IAM auth.**

### What stays the same

- AWS is still the production cloud. RDS, ECS Fargate, EFS, ALB, S3,
  EventBridge Scheduler, CloudWatch — all of ADR-0022 outside decision 1.
- **Embeddings do not move.** `EMBEDDING_BACKEND=bedrock`,
  `cohere.embed-v4:0`, 1024 dimensions, `embedding.py:340`. They are the
  highest-volume call in the system during ingest and IAM auth is worth
  keeping for them.
- `georag_chunks` is unchanged: 1024-dim dense + SPLADE++ sparse.
- SPLADE++ still runs as its own Fargate service. It has no hosted
  equivalent anywhere and that has not changed.
- The nightly ECS stop/start stays. That is where the compute saving is.
- Every hard rule in CLAUDE.md is unaffected.

### What changed

- `LLM_BACKEND` gains a `cohere` value. `bedrock` remains valid and is still
  what embeddings use.
- Two new adapters, both on `httpx` (already a direct dependency,
  `pyproject.toml:89`) — **no new Python dependencies**, matching the
  existing house pattern.
  - Chat must stream: the query path is SSE end to end
    (`status/bind/delta/citation/completed/failed`) and Laravel re-broadcasts
    the frames on Reverb.
  - Parse replaces the Bedrock transport in `cohere_parse_client` while
    keeping its public surface, exactly as ADR-0022 did when it moved that
    file from Foundry to Bedrock.
- `COHERE_API_KEY` joins Secrets Manager: **11 go-live keys, not 10**.
- `BEDROCK_CHAT_MODEL_ID` and `BEDROCK_PARSE_MODEL_ID` are retired, and with
  them the two tfvars with no default (`bedrock_chat_endpoint_name`,
  `bedrock_parse_endpoint_name`) — **4 no-default tfvars instead of 6**.
- The Marketplace IAM goes: the `sagemaker:*Endpoint*` grants in `iam.tf`
  that exist only to invoke and cycle those endpoints.
- **The endpoint half of the nightly sweep goes.** `shutdown-sweep.sh`'s
  `delete-endpoint` and `startup-sweep.sh`'s `create-endpoint` have nothing
  left to act on, and with them goes `BEDROCK_ENDPOINT_NOT_INSERVICE`
  (`alerts.tf:86`) — the Sev 1 that ADR-0022 called the sharpest edge in the
  deployment — and the `<endpoint-name>-config` naming trap that could take
  out chat and OCR on a morning restart.

### Open sub-decision: where rerank runs

Not settled at the time of writing, recorded so it is not lost.

- **Bedrock, Rerank 3.5** (status quo, no work): in-region, IAM auth, no extra
  network hop on the query hot path. But `RERANKER_SCORE_THRESHOLD_HOSTED =
  0.2` was measured against **v4**, and it is the only retrieval-quality gate
  in the system. It stays unvalidated, and re-measuring it is blocked on SME
  chunk-level labelling that does not exist yet.
- **Cohere API, Rerank v4** (recommended): the API key is being added
  regardless, so the marginal cost is one more adapter path. The threshold
  becomes the number it was actually measured on, closing an open risk for
  free. Costs one external hop per query.

## Migration mechanics (for future reference)

1. Obtain a Cohere API key; confirm Command A+ and Parse 5 are on the plan.
2. Write the chat adapter (streaming) behind `LLM_BACKEND=cohere`. Reversible
   — `bedrock` and `anthropic` remain selectable.
3. Swap the Parse transport inside `cohere_parse_client`, keeping the public
   surface and the response adapter. Reversible.
4. Terraform: drop the two endpoint tfvars, the Marketplace IAM, the endpoint
   half of both sweeps, and the `BEDROCK_ENDPOINT_NOT_INSERVICE` alarm. Add
   `COHERE_API_KEY` to the `georag/app` secret.
5. Write `COHERE_API_KEY` into Secrets Manager **before the first apply** —
   ECS refuses to start a task referencing a key that does not exist, and the
   failure presents as a task that never starts rather than an application
   error.
6. Verify against the live API before cutover, then apply.

There is no point of no return here, because there is no data to migrate:
the stores are fresh. That is also why region choice is still cheap today and
will not be in six months.

## Gotchas hit during the migration (worth knowing for next time)

1. **"Marketplace" means two different products.** AWS Marketplace
   (SageMaker model packages, `sagemaker-runtime.invoke_endpoint`) and
   Bedrock Marketplace (Bedrock-managed endpoints, `bedrock-runtime` Converse
   with the endpoint ARN as `modelId`) are not the same thing, and both are
   called "the marketplace" in conversation. Getting it wrong does not fail at
   deploy: `terraform apply` succeeds, the tasks start, and chat and OCR fail
   at the first call.
2. **`AccessDeniedException` from a root session is a service-availability
   signal, not a permissions one.** Root cannot be denied by IAM policy, so
   `ListMarketplaceModelEndpoints` refusing in `ca-west-1` means the API is
   not offered there.
3. **A region having Bedrock does not mean it has your models.** `ca-west-1`
   serves 19 foundation models and zero Cohere.
4. **Serverless catalogue ≠ Marketplace.** `list-foundation-models` will never
   show a Marketplace model, so "6 Cohere models in this region" says nothing
   about whether Command A+ is reachable there.
5. **`aws login` cannot complete inside an agent sandbox.** The sign-in page
   renders in the operator's browser, but the authorization code is exchanged
   for credentials from the shell at `signin.aws.amazon.com`, which such
   environments refuse (403 to CONNECT), while `oidc.*.amazonaws.com` and
   every service endpoint answer normally. The failure presents as a proxy
   error with no credentials written. Run Step 0 from a workstation, or from
   CloudShell, which is pre-authenticated.

## Consequences

### Positive

- **Full capability parity.** Command A+ and Parse 5 are the models the
  pipeline was designed around, at their current versions, rather than
  substitutes.
- **No idle cost in the model tier.** Per-token and per-page billing only.
- **The Sev 1 disappears.** No Marketplace endpoints means nothing to delete
  and recreate nightly, no `BEDROCK_ENDPOINT_NOT_INSERVICE`, and no endpoint
  config naming convention that fails silently until a morning restart.
- **The largest open risk in the deployment shrinks.** Every Bedrock adapter
  carries `[UNVERIFIED]` because no live call had ever confirmed the wire
  shape on any host — Parse's had never been verified on Foundry either.
  Cohere's own API is publicly documented with a published, stable contract.
- Fewer moving parts: 4 no-default tfvars instead of 6, narrower IAM, a
  simpler scheduler.
- Cloud-agnostic again for the chat and OCR halves, which is what ADR-0022
  originally recommended (option A) before option D was chosen over it.

### Negative

- **A second credential.** `COHERE_API_KEY` is a long-lived secret to store,
  inject and rotate, against IAM's short-lived role credentials. It is also
  one more thing that can be leaked; note that this repository has already
  committed one live credential (`scripts/phase0_acceptance.sh`, still in git
  history and still to be rotated).
- **Document text and page images now leave AWS.** `page_vision_client.py`
  records avoiding exactly this — "page images of tenant geology egressing
  outside the cloud boundary" — as a reason for an earlier decision. That
  consideration is being reversed deliberately. Accepted on 2026-09-15 on the
  basis that no client contract currently requires Canadian or in-cloud
  residency. **If that changes, this ADR is the thing to revisit**, and the
  cheapest answer then is option B.
- **Two adapters to write and maintain**, including a streaming chat path,
  against an API whose wire shape is documented but not yet exercised here.
- **Vendor concentration.** Chat, parse and embeddings are all Cohere; an
  outage or pricing change hits three capabilities at once. `LLM_BACKEND`
  still offers `anthropic` as a chat fallback, which limits the blast radius
  for the query path but not for ingest.
- Rerank stays at 3.5 with an unvalidated threshold unless the open
  sub-decision above resolves to Cohere v4.

## Verification (this commit)

This ADR records a decision; the code lands separately. What is verified here:

- Region and catalogue facts reproduced from the account, not documentation:
  `list-foundation-models` Cohere counts per region, and
  `list-marketplace-model-endpoints` returning 0 or `AccessDeniedException`.
- Textract prices quoted from `aws pricing get-products --service-code
  AmazonTextract`, publication date 2026-09-11.
- The absence of any `api.cohere.com` client and the `LLM_BACKEND` value set
  confirmed by reading `app/config.py` and grepping `src/fastapi/app/`.
- `httpx>=0.28` confirmed as an existing direct dependency, so the adapters
  add none.

When the code lands, verification is: a committed
`ops/validation/reports/` probe report against the live Cohere API covering
chat (including streaming), parse, and embeddings; `[UNVERIFIED]` removed
from the adapters it covers; and `scripts/operator/aws-preflight.sh` passing
A-10 with the 11-key secret.

## Follow-ups (NOT part of this ADR; tracked separately)

- **Resolve the rerank sub-decision** — before the adapters are written, so
  the work is scoped once.
- **Re-measure `RERANKER_SCORE_THRESHOLD_HOSTED`** if rerank stays on Bedrock
  3.5. Blocked on SME chunk-level relevance labelling;
  `tests/golden_questions/seed_template.yaml` is a skeleton with none.
- **Rotate the Redis password** committed in `scripts/phase0_acceptance.sh`.
  It is in git history and predates all of this.
- **Move off the account root user** before any production deployment.
  Deployment work on 2026-09-15 was done as `arn:aws:iam::046369253475:root`.
- **Choose the final region.** `ca-west-1` is ruled out by this ADR.
  `ca-central-1` keeps data in Canada and has the Cohere serverless
  catalogue; `us-east-1` is the broadest and is the Terraform default. Cheap
  to change now, expensive once the stores hold data.
- **Revisit option B (Textract + a Bedrock chat model)** if the Cohere API
  bill becomes the binding constraint, or if a client requires that no
  document content leaves AWS.
