# ADR 0025: Embedding moves to Cohere's own API, on Embed 5

- **Date**: 2026-10-01
- **Status**: Accepted (2026-10-04)
- **Implemented (code half)**: 2026-10-04. The adapter, the probe's `embed`
  section, the `--all` reset, the `embed_model` tag, `answer_runs.embedding_model`,
  Terraform, compose, the chart and the docs are in the tree; migration steps
  1 and 3-7 (the credentialed probe run, the Qdrant snapshot, the cutover,
  the verification and the 14-day rollback hold) remain operator actions.
  Production is NOT moved by this tree: `config.tf` reads
  `var.embedding_backend`, which defaults to `bedrock`, so a deploy or an
  unrelated `terraform apply` leaves the v4 space in place. **The cutover
  (step 4) is setting `embedding_backend = "cohere"` in the production
  tfvars and applying**, after steps 1 and 3, with the collection reset
  (`src/fastapi/scripts/reset_embeddings_for_reencode.py --all`) and the embed sweep in
  the same sitting. The code default is `cohere` (an unset value selects the
  target hosted backend), so compose and any environment that sets nothing
  already embed on Embed 5.
- **Deciders**: Kyle Maguire (SME)
- **Supersedes**: ADR-0023 "What stays the same", the bullet beginning
  "**Embeddings do not move.**" Everything else in ADR-0023 stands. That
  includes its sub-decision that **rerank stays on Bedrock at 3.5**, which
  this ADR does not touch.

## Context

Cohere released **Embed 5** on 2026-09-30 in two variants that share one
embedding space ([changelog](https://docs.cohere.com/changelog/embed-v5),
[announcement](https://cohere.com/blog/embed-5)):

- `embed-v5.0-pro`, tuned for retrieval quality (indexing);
- `embed-v5.0-fast`, tuned for latency (live queries).

The announcement claims its largest gains over Embed 4 on "visually rich
documents, financial filings, parsed PDFs, code, and multilingual
retrieval". Parsed PDFs and visually rich pages are most of our corpus:
NI 43-101 reports, scanned drill logs, plan sheets and cross-sections, which
`passage_embedder._encode_image_sync` already embeds as page images.

Three facts decide where we can get it.

**1. It is not on Bedrock.** At launch Cohere lists Embed 5 on its own API,
Model Vault, North, Microsoft Foundry and Amazon SageMaker. It is not in the
Bedrock serverless catalogue, where production gets `cohere.embed-v4:0`
today (`deploy/aws/terraform/variables.tf:288`,
`src/fastapi/app/services/embedding.py:67`).

**2. The SageMaker listing is a Marketplace endpoint.** ADR-0023 rejected
that product for chat and Parse because it bills for the instance whether
anything calls it or not. The same reasoning rules it out for embedding.

**3. Cohere's own API is already a production dependency.** Since ADR-0023,
chat (`llm_cohere.py`) and Parse (`ingest/cohere_parse_client.py`) call
`{COHERE_BASE_URL}/v2/...` with `COHERE_API_KEY`. The credential, the
Secrets Manager entry, the egress path and the probe (`cohere_probe.py`)
all exist. Moving embedding there adds an endpoint, not a vendor or a
secret.

Nothing moves on its own. Model IDs are pinned, so `cohere.embed-v4:0`
keeps serving v4 for as long as Bedrock offers it.

## Options considered

| Option | Cost (text) | Quality | Effort | Outcome |
|---|---|---|---|---|
| A. Stay on Bedrock Embed v4; wait for Embed 5 to reach Bedrock | $0.12 / 1M tokens, **draws AWS credits** | Embed 4 | None now | Rejected as the plan, kept as the fallback. Bedrock has no published date for Embed 5. |
| B. Embed 5 on an AWS Marketplace SageMaker endpoint | Instance-hours, **billed while idle** | Embed 5 | High | Rejected for the reason ADR-0023 gives against option A there. |
| C. **Embed 5 on Cohere's own API** | **Pro $0.12, Fast $0.08 / 1M tokens; images $0.40 / 1M**, cash | **Embed 5** ✅ | Medium: one adapter, a probe section, a full re-embed | **Chosen**, see Decision. |
| D. Embed 4 on Cohere's own API | $0.12 / 1M, cash | Embed 4 | Medium, plus a re-embed for no quality gain | Rejected: it pays C's costs without C's benefit. |

Option A deserves a fair statement, because it will look attractive again
the day Bedrock lists Embed 5. It keeps IAM role credentials (nothing
long-lived to rotate), keeps embedding spend on AWS credits, and keeps
chunk text inside AWS. If Bedrock adds Embed 5, switching back is a model
ID, an IAM ARN and a re-embed, and the re-embed is the only part that
costs anything. It lost because it means waiting an unknown time for the
model that matches our corpus best.

**Cost is not the deciding factor in either direction.** The two hosts
charge the same list price for text at Embed 4 / Pro level. A 50M-token
full re-embed costs about $6 on either one, and query-side embedding
costs a fraction of a cent per question. On every query, Rerank 3.5
($2.00 / 1,000 queries) costs more than embedding by orders of magnitude,
and it does not move here.

## Decision

**Dense embedding moves to Cohere's own API with a new
`EMBEDDING_BACKEND=cohere`. Ingest (documents and page images) uses
`embed-v5.0-pro` with `output_dimension: 1024`. Queries also use
`embed-v5.0-pro` at cutover; moving them to `embed-v5.0-fast` is a later,
measured step (see sub-decision).**

### What stays the same

- **`georag_chunks` keeps its shape**: dense slot `''`, 1024 dims, Cosine,
  plus SPLADE++ sparse. Embed 5 Pro supports output dimensions 2048, 1536,
  1024, 768, 512 and 256, so no collection schema migration and no
  drop-and-recreate of the collection is required (`init_qdrant.py` has no
  such mode; `src/fastapi/scripts/reset_embeddings_for_reencode.py --all` is the
  mechanism). **Every vector still has to be
  rewritten** (see Migration mechanics).
- **Rerank stays on Bedrock, Rerank 3.5.** ADR-0023's sub-decision and its
  trigger ("revisit when the credits are exhausted") are unchanged.
  `RERANKER_SCORE_THRESHOLD_HOSTED` scores rerank output, not embeddings,
  so this move neither validates nor invalidates it.
- **SPLADE++ is unchanged.** It is still the self-hosted `sparse` service.
  Sparse vectors do not depend on the dense model and are not re-encoded.
- **No new secret.** `COHERE_API_KEY` is already the 11th go-live key
  (ADR-0023). The Secrets Manager count stays at 11.
- **No new Python dependency.** The adapter uses `httpx`, like
  `llm_cohere.py` and `cohere_parse_client.py`.
- **`EMBEDDING_BACKEND=bedrock` stays selectable**, and stays the
  rollback. `local` (the compose sidecars) is unaffected.
- The asymmetric `input_type` contract is unchanged: `search_document` for
  ingest, `search_query` for retrieval, `image` for page renders.

### What changed

- `EMBEDDING_BACKEND` gains `cohere`. Both `embedding.get_embedding_model`
  (query path) and `passage_embedder.load_embedding_model` (ingest path)
  branch on it. **They must agree.** A mismatch writes one vector space and
  queries another (ADR-0021 migration step 2), and it fails silently as
  poor retrieval, not as an error.
- A new `_CohereEmbedding` class in `app/services/embedding.py`, a sibling
  of `_BedrockEmbedding`. It exposes the same surface the callers already
  use: `encode()`, `embed_query()`, `embed_image()` and
  `get_sentence_embedding_dimension()`. `main.py`'s startup dimension check
  calls the last of these. **The names are a contract enforced only by duck
  typing.** `agent/tools.py:2241` checks `hasattr(_model, "embed_query")`;
  if the method is missing or named differently, every question falls back
  to `.encode()` and is embedded as `search_document`, with no error. The
  passage embedder does the same with `embed_image` (an `AttributeError`
  logged once per batch). The adapter's tests must assert that the query
  path sends `input_type: "search_query"` through the real `tools.py` call
  site, not only through the class.
- **Every dense point records the model that produced it.** The Qdrant
  payload built in `passage_embedder.py:588` gains `embed_model`
  (for example `embed-v5.0-pro`). Today nothing records it: the payload has
  no model field, and `answer_runs.embedding_model` /
  `embedding_model_version` exist (`models/answer_run.py:158`) but nothing
  writes them. Without the tag, a collection holding both v4 and v5
  vectors (Gotcha 2) cannot be detected from the data. With it, step 6
  becomes a count rather than an inference. The query path writes the
  query model into `answer_runs`, so any answer can be traced to the
  vector space that retrieved it.
- `COHERE_EMBED_TIMEOUT_S` and a separate query-path client, mirroring
  `BEDROCK_EMBED_TIMEOUT_S` and the budgeted query-path client (VEN-6,
  `embedding.py:84`). Embedding a question sits inside the chat latency
  budget; ingest batches do not, and must not share its timeout.
- **The code default for `EMBEDDING_BACKEND` moves from `bedrock` to
  `cohere`.** This follows the rule CLAUDE.md records for the current
  default: an unset value selects the hosted backend production actually
  uses, never one that does not exist there.
- New settings: `COHERE_EMBED_MODEL` (default `embed-v5.0-pro`),
  `COHERE_EMBED_QUERY_MODEL` (default: same as `COHERE_EMBED_MODEL`) and
  `COHERE_EMBED_DIMENSION` (default `1024`, validated against
  `EMBEDDING_DIMENSION`).
- `app/services/cohere_wire.py` gains an `EMBED` contract for
  `POST /v2/embed`, beside `PARSE` and `CHAT`, with every field marked
  `ASSUMED` until the probe observes it.
- `ops/validation/cohere_probe.py` gains an embed section, and
  `fake_cohere.py` gains a fake `/v2/embed`.
- Terraform: `EMBEDDING_BACKEND=cohere` and the two model settings in
  `config.tf`. After the rollback window, `bedrock_embed_model_id` and
  its `bedrock:InvokeModel` resource in `iam.tf:111` are removed. The rerank
  grant stays.
- Docs: CLAUDE.md's "Embedding / rerank / OCR" line, the manual's maintained
  chapters, `.env.example` / `.env.production.example`, and
  `docker-compose.yml`'s hosted-backend defaults.
- Fix while in the file: `passage_embedder._encode_image_sync`'s
  `image_backend_unsupported` log still tells the operator to set
  `EMBEDDING_BACKEND=foundry`, a value retired by ADR-0022.

### Sub-decision: Fast for queries waits for a measurement

Cohere states that Pro and Fast share an embedding space, so a corpus
indexed with Pro can be queried with Fast without re-indexing. If that holds,
the query path should use Fast. It is cheaper ($0.08 vs $0.12) and
reportedly 2.4× faster on the one embed call that sits on the chat hot path.

**It is not taken at cutover**, because it is a vendor claim about the very
property (whether two vectors are comparable) that fails silently when it
is false. Cross-model drift would show up only as somewhat worse
retrieval, with every guard still passing. The probe's embed section
therefore measures it before `COHERE_EMBED_QUERY_MODEL` changes:

- the same query embedded by both models, cosine similarity between them;
- top-k overlap of Pro-query vs Fast-query retrieval against the
  re-embedded corpus.

Moving queries to Fast is then a one-variable change with no re-embed.
Rolling it back is the same.

## Migration mechanics (for future reference)

> **As run (2026-10-04).** Every step below that touches a store runs from
> `.github/workflows/embed5-cutover.yml`, one step per dispatch, as a one-off
> ECS task on the worker's task definition
> (`src/fastapi/scripts/ops/embed5_cutover.py`): `probe` (step 1, inside the
> VPC with the production key, both image shapes, the real adapter),
> `questions label=before` (step 6's baseline), `snapshot` (step 3, to the
> backups bucket), then `terraform.yml action=apply embedding_backend=cohere`
> (step 4's switch, approved on the `production` environment) followed by
> `cd.yml` so both services actually run the new revision, then `reset`
> (step 4's clear: refused unless both running services and the task itself
> say `cohere`, the snapshot prefix names objects, and the phrase is typed),
> `verify` until it exits 0 (3 = sweep still running), `questions
> label=after`. The reset walks `silver.workspaces` under RLS because the
> task runs as `georag_app`, which sees nothing unscoped.

1. **Probe first.** Run the extended `cohere_probe.py` with the production
   key. It must observe:
   - `POST /v2/embed` accepting `embed-v5.0-pro`;
   - a 1024-length float vector for each of `search_document`,
     `search_query` and `image`;
   - which image request shape is accepted (`images` vs `inputs`; v4's
     ambiguity, `embedding.py:244`);
   - the image pixel cap (v4 was 2M px, and `page_image.render_page_png`
     downscales to that);
   - the per-request input-count limit;
   - the rate limit the key actually gets.

   Commit the report. Reversible: nothing has changed yet.
2. **Land the adapter** behind `EMBEDDING_BACKEND=cohere` with unit tests
   against `fake_cohere.py`. Production stays on `bedrock`. Reversible.
3. **Snapshot `georag_chunks`** (`POST /collections/georag_chunks/snapshots`;
   Qdrant's storage is on EFS per ADR-0024). The snapshot is the rollback:
   without it, going back to Bedrock means another full re-embed.
4. **Cut over (point of no return without the snapshot).**
   - Set `EMBEDDING_BACKEND=cohere` on **both** the FastAPI service and the
     hatchet-worker in the same apply.
   - Delete every dense point.
   - Set `embedding_id = NULL` on **every** passage, text and
     `modality='image'` alike.
   - Let `embed_pending_passages` re-encode everything.

   Neither existing tool does this as written:
   - `src/fastapi/scripts/reset_embeddings_for_reencode.py` touches only rows with
     `contextualized_content IS NOT NULL`;
   - `src/fastapi/scripts/reembed_qdrant.py` skips page-image points by
     design, because it can only re-embed from payload text.

   The reset needs a `--all` mode (or a one-off statement recorded in the
   runbook) that clears every row.
5. **Accept a degraded window.** There is no Qdrant alias, so the
   collection name is fixed and retrieval returns only what has been
   re-embedded so far. Run the cutover after hours, before the nightly ECS
   stop. With the corpus at its current size the sweep finishes in one
   sitting. That stops being true as projects are added, which is the main
   reason to do this now rather than later.
6. **Verify**:
   - the count of points with a dense vector equals the count of passages
     with `embedding_id IS NOT NULL`;
   - **zero points lack `embed_model = embed-v5.0-pro`**;
   - no passage is left with `embedding_id IS NULL`;
   - page-image points are present;
   - a fixed set of known questions on Red Star and one other project
     return cited answers.

   Run the same fixed set **before** step 4 and record refusals and
   citations. `RERANKER_SCORE_THRESHOLD_HOSTED` does not change, but the
   candidates reaching the reranker do, so the Layer 1 gate's refusal rate
   can move in either direction. A before/after comparison on the same
   questions is the only signal available until the threshold is
   properly measured (ADR-0023 follow-up).
7. **Hold the rollback** (the snapshot plus `EMBEDDING_BACKEND=bedrock`) for
   14 days, then remove the Bedrock embed IAM grant and variable.

## Gotchas to expect (from the moves before this one)

1. **Query and ingest must switch together.** They are separate ECS
   services with separate environments. If one apply updates one of them
   and not the other, the result is the ADR-0021 failure mode: green
   health checks, every component working, and retrieval quietly wrong.
2. **A partial re-embed is worse than none.** A collection holding both v4
   and v5 vectors still returns results, ranked by meaningless cosines
   between two unrelated spaces. Clear first, then re-encode. Never "top
   up".
3. **"Same dimension" does not mean "same space".** 1024 matching 1024 is
   what lets the startup check pass, and it is why that check cannot catch
   this. Only the re-embed makes the vectors comparable.
4. **Image embedding has two request shapes, and which one Cohere accepts
   for v5 is unobserved.** The try-primary, fall-back-once strategy in
   `_BedrockEmbedding.embed_image` carries over until the probe settles it.
5. **Cohere API limits are per key, not per role.** Chat, Parse and now
   embedding share one key's rate limit. A large ingest that hits 429 must
   back off without starving the chat path. The adapter honours
   `Retry-After` and the sweep keeps its batch ceiling.

## Consequences

### Positive

- **The embedding model best suited to our corpus**, at release, without
  waiting on Bedrock's catalogue.
- **One Cohere host for three of four capabilities.** Chat, Parse and
  embedding share one credential, one wire module (`cohere_wire.py`) and
  one probe. Only rerank remains on Bedrock.
- **A cheaper, faster query path is available** (Fast) once it is measured,
  with no further re-embed.
- **No collection migration.** 1024 dims is a supported Embed 5 output, so
  the Qdrant schema, `EMBEDDING_DIMENSION` and every consumer of the dense
  slot are unchanged.
- The wire shape will be checked against the vendor's published API rather
  than inferred through a reseller, as ADR-0023 already found for chat and
  Parse.

### Negative

- **Embedding spend leaves the AWS credits.** ADR-0023 kept rerank on
  Bedrock for exactly this reason. For embedding the amount is small (a few
  dollars per full corpus, fractions of a cent per query), and that is why
  this ADR accepts it where ADR-0023 did not for rerank.
- **All chunk text and every page image now leave AWS on ingest**, not only
  the scanned pages Parse already sent. Under ADR-0023's position Cohere is
  inside the contracted set, so `egress_gate.py` is correctly not called.
  **This rests on the same stated assumption: no client contract requires
  Canadian or in-cloud data residency.** If one ever does, this ADR and
  ADR-0023 are revisited together, and option A (Bedrock) is the
  residency-safe fallback for embedding.
- **More vendor concentration.** A Cohere outage now stops ingest
  embedding as well as chat and OCR. A Cohere API outage also stops query
  embedding, which means retrieval itself stops. The Anthropic chat
  fallback does not help with that, because it does not embed.
  `EMBEDDING_BACKEND=bedrock` is not a hot fallback either: it is a
  different vector space and needs a re-embed (or the snapshot).
- **A one-time full re-embed with a degraded retrieval window**, and a
  second one if we ever move back without the snapshot.
- **Cohere's data-retention terms now cover the whole corpus, and they have
  never been checked.** Neither ADR-0023 nor this ADR records whether
  Cohere's API keeps request content, for how long, or whether it may be
  used for training under our account's terms. Bedrock's terms are AWS's.
  Under ADR-0023 this already applied to questions, answers and scanned
  pages. After this move it applies to every chunk of every document.
  **Confirm the account's retention and training settings before
  cutover**, and record what was found in this ADR.
- **IAM's short-lived credentials are swapped for a long-lived key on
  another hot path.** The key already exists, so the exposure is not new,
  but rotating it now interrupts three capabilities at once.
  `docs/RUNBOOK.md`'s rotation procedure must say so.

## Verification (this commit)

This ADR records a proposed decision. No code changes in this commit. What
was checked:

- The Embed v4 pin in the codebase: `variables.tf:288`, `embedding.py:67`,
  `docker-compose.yml:855`/`:1909`, `.env.example:655`,
  `.env.production.example:684`, and the single-model IAM resource at
  `iam.tf:111`.
- Image embedding shares the dense slot (`embedding.py:226-233`) and is
  produced only by `passage_embedder._encode_image_sync`, so a full
  re-embed must include image passages.
- The scope limits of both existing re-embed tools, from their source
  (`reset_embeddings_for_reencode.py:71`, `reembed_qdrant.py:233`).
- No Qdrant alias is used anywhere, so cutover happens in place.
- Embed 5 model names, supported dimensions and pricing come from Cohere's
  2026-09-30 publications cited above. **They have not been observed from
  this account**: that is migration step 1.

## Follow-ups (NOT part of this ADR; tracked separately)

- **Move queries to `embed-v5.0-fast`** once the probe's cross-model
  measurement passes. This is the sub-decision above.
- **Revisit option A if Bedrock lists Embed 5.** At that point the credits
  argument and IAM auth both favour Bedrock again, and the move back costs
  one re-embed.
- **Revisit together with rerank when the AWS credits run out**
  (ADR-0023's trigger). Moving rerank to Cohere v4 then would leave
  Bedrock with nothing to serve and remove the `bedrock_probe.py` half of
  `aws-preflight.sh` A-11.
- **On-prem has no embedding backend set.** `charts/georag/` and the
  `kubernetes/manifests/` files set no `EMBEDDING_BACKEND`, so an on-prem
  install takes the code default. That is `bedrock` today and would be
  `cohere` after this ADR, and an air-gapped site can reach neither. This
  problem exists today and is not created here, but this move would
  change which wrong backend it picks. Have the chart set `local` (the
  sidecars) explicitly, before any on-prem install.
- **Give `georag_chunks` an alias** before the corpus is large enough that
  a full re-embed cannot finish in one evening. With an alias, a future
  model change builds a new collection and swaps it in, with no degraded
  window.
