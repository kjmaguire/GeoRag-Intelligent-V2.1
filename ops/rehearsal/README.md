# Go-live rehearsal corpus

A two-tenant corpus for exercising the live AWS deployment, and the
verification that goes with it. Built during the 2026-09-18 rehearsal.

## What this is for

`database/tests/pgtap/08_silver_mvt_functions.sql` tests 73-77 already prove
the Martin tenant fence is **correct**, with both positive controls present,
and they run on every PR. Nothing here replaces that.

These files answer the two questions CI structurally cannot:

1. Does the RDS instance this platform actually runs on **carry** the fenced
   function definitions — i.e. was `2026_09_16_120000` applied here, or
   merely merged?
2. Does the fence hold for workspaces created the ordinary way, rather than
   for a fixture built by the same commit as the function it tests?

## The one design decision that matters

**The two tenants are co-located.** Their collars interleave 100 m apart
along a single line rather than sitting in separate regions.

Give tenant A collars in British Columbia and tenant B collars in
Saskatchewan and every cross-tenant assertion passes whether or not the fence
works — the bounding box already excluded the other tenant's rows. The suite
goes green and proves nothing, and it looks exactly like a suite that proves
something. Here, any tile containing one tenant's collar contains the
other's, so `workspace_id` is the only thing that can separate them.

`verify_tenant_fence.sql` section 1 asserts that co-location rather than
assuming it, so editing the seed coordinates breaks the run loudly instead of
quietly turning sections 2-4 into no-ops. That guard is load-bearing: with
the tenants separated *and* both fence guards removed, the script still
refuses to report a pass.

## Files

| file | |
|---|---|
| `seed_multitenant_corpus.sql` | Two workspaces, two projects, six interleaved collars. Idempotent, deterministic ids. |
| `verify_tenant_fence.sql` | Five sections; every check RAISEs and every section acknowledges, so the exit code AND the acknowledgement count are the verdict. |
| `teardown_multitenant_corpus.sql` | Deletes exactly the seeded ids — not a `LIKE 'rehearsal-%'` sweep — then asserts nothing is left. |
| `run_against_deployment.sh` | Runs any of the above as a one-off ECS task on `georag-fastapi`. |
| `run_step5.sh` | Step 5: `ingest` a document, poll `status`, then `query`. |
| `make_cloudshell_bundle.sh` | Emits a repo-free step-5 script — seed, ingest, status, query — built from the sources above. |
| `make_step6_bundle.sh` | Emits a repo-free seed+verify script (step 6), likewise. |
| `run_cohere_probe.sh` | Runs `ops/validation/cohere_probe.py` as a one-off task inside the VPC, using the deployed service's own `COHERE_API_KEY` and egress, and writes the report to `ops/validation/reports/` only if it verified something (aws-preflight A-11). Tested end to end without AWS or a key by `scripts/tests/run_cohere_probe_test.sh`. |

None of the three SQL files contains a psql meta-command. They are executed
over **asyncpg**, because the fastapi image is the only one in this deployment
that can reach Postgres at all: the laravel/migrate image installs `libpq-dev`
for PHP's `pdo_pgsql` but no `psql` binary, its task definition has no
`DATABASE_URL`, and its `entryPoint = ["/bin/sh","-c"]` turns a command
override into a no-op that exits 0. `psql -f` still runs all three correctly.

**Every section raises a tagged acknowledgement** (`[SEED-OK]`,
`[CHECK-OK 0]`…`[CHECK-OK 4]`, `[TEARDOWN-OK]`) and the runner requires all of
them before reporting a pass. A driver that executes nothing scores 0/5 and
fails, rather than exiting 0 and being read as a clean run — which is what the
previous `georag-migrate` version would have done, and is the same
absence-as-success shape this rehearsal produced three times elsewhere.

The SQL travels to the task gzip+base64: ECS `RunTask` caps the whole
`overrides` structure at 8192 characters, and `verify_tenant_fence.sql`
serialised to 8316 uncompressed. The runner checks the payload size itself and
refuses with the number rather than letting the AWS API reject it.

`src/fastapi/scripts/ops/step5_answer_path.py` is the step-5 assertion
itself. It lives under `src/fastapi/` rather than here because cd.yml builds
the fastapi image with `context: ./src` and `COPY fastapi/ .` — anything
under `ops/` is outside the build context and would be silently absent from
the image.

```bash
bash ops/rehearsal/run_against_deployment.sh seed
bash ops/rehearsal/run_against_deployment.sh verify
bash ops/rehearsal/run_against_deployment.sh teardown
```

Or, with no checkout at all:

```bash
bash ops/rehearsal/make_step6_bundle.sh > /tmp/step6.sh
# upload /tmp/step6.sh via CloudShell Actions -> Upload file
bash step6.sh
```

## What the verification asserts

0. The deployed `silver.pg_collars_by_project` is the **post-fence**
   definition, not a pre-fix one that would answer happily without a
   `workspace_id`.
1. The two tenants are co-located (above).
2. Positive controls — each tenant can see its own tile. Catches a fence that
   over-filters, which is just as broken and much quieter.
3. Cross-tenant denial in **both** directions. pgTAP test 74 covers one.
4. Missing and malformed `workspace_id` RAISE rather than returning a blank
   tile, which on a map is indistinguishable from "no data here".

Section 3 separates two outcomes the fence can produce, because they are not
equally bad and reporting them identically would misinform:

* **FENCE BYPASS** — a non-NULL but zero-byte tile. The `silver.projects`
  guard is gone; the collar-level predicate still withheld every feature, so
  nothing escaped.
* **DISCLOSURE** — a tile with bytes in it. Another tenant's features
  actually crossed the boundary.

## Verified how

Against a real PostgreSQL 16 + PostGIS instance, loading the actual function
body extracted from the migration, with a mutation matrix:

| mutation | result |
|---|---|
| none (healthy) | exit 0, VERIFIED |
| `p.workspace_id` guard removed | FENCE BYPASS, 0 bytes |
| both guards removed | DISCLOSURE, 445 bytes of the other tenant's features |
| pre-fence definition deployed | section 0 refuses to proceed |
| tenants separated + both guards removed | section 1 refuses to pass |
| SQL replaced with a no-op that exits 0 | **0/5 acknowledged — fails** |
| SQL truncated after section 1 | **2/5 acknowledged — fails** |

The last three rows are the point of the whole design. A broken fence must not
pass because the tenants happen to be far apart; and a runner that executed
nothing must not pass because nothing complained.

Re-run on 2026-09-18 after the runner moved to asyncpg, through the generated
CloudShell bundle's own embedded drivers rather than a hand-written copy, so
what was tested is what ships: seed on an empty database, verify, idempotent
re-seed, teardown, plus every mutation above.

## Scope

Rehearsal tooling, not part of the deploy path. Nothing in CD calls it. It
writes to a live database and `teardown_multitenant_corpus.sql` is the way
back out — read it before pointing this at anything holding real tenant data.


## Step 5 — the answer path

```bash
bash ops/rehearsal/run_step5.sh ingest   # upload the fixture PDF + trigger Hatchet
bash ops/rehearsal/run_step5.sh status   # poll until it prints READY
bash ops/rehearsal/run_step5.sh query    # the assertion
```

`ingest` dispatches and returns — Hatchet then parses, chunks, embeds and
indexes for some minutes. The phases are separate commands precisely so
nobody has to guess a sleep; a guessed sleep produces a red run that only
means "not finished yet".

**What `query` asserts, and why each one is not the obvious version:**

* **streamed** — at least *two* delta frames. A non-streaming implementation
  that emitted the whole answer in one frame would satisfy `>= 1`.
* **terminated** — `completed` is the *last* frame. A stream that emits
  `completed` and then keeps talking, or simply stops, both show up in the
  chat UI as a hung message.
* **cited** — a citation frame carrying a non-null `source_chunk_id`.
* **citation-resolves** — that id names a chunk that really exists. This is
  the one that matters. Hard rule 4 says every claim carries a
  `source_chunk_id` or is rejected; a gate that checks only for the
  *presence* of a citation cannot tell a real provenance chain from a
  well-formed fabrication, which is the exact failure §04i exists to prevent.

Resolution delegates to `validate_chunk_provenance` — the platform's own
§04i Layer 5 check — rather than a lookup written here. The first draft did
write its own, against `silver.document_passages.passage_id`, and would have
been wrong in the worst direction, reporting genuine citations as
fabricated: ids resolve in **Qdrant**, the collection depends on
`RETRIEVAL_USE_DOCUMENT_PASSAGES`, a `source_chunk_id` may be a bare UUID or
a compound `georag_reports:<id>:section=<n>:chunk=<uuid>` trace string, and
`corpus='public_geo'` citations are not Qdrant points at all.

A refusal with no citation is *correct behaviour* on an unindexed corpus —
and still fails this gate, which is right: step 5 asks for a cited answer,
not for the absence of a crash.


## When CloudShell has no checkout

CloudShell's home directory does not survive a session recycle, which
happened three times during the 2026-09-18 rehearsal, each time taking the
repo with it. `make_cloudshell_bundle.sh` (step 5, seed+ingest) and
`make_step6_bundle.sh` (step 6, seed+verify) emit standalone scripts that need
no checkout:

```bash
bash ops/rehearsal/make_cloudshell_bundle.sh > /tmp/step5.sh   # step 5
bash ops/rehearsal/make_step6_bundle.sh      > /tmp/step6.sh   # step 6
# upload both via CloudShell Actions -> Upload file, then:
bash step5.sh ingest    # seed the corpus + upload the PDF + dispatch ingest_pdf
bash step5.sh status    # poll until the passages carry embedding_id
bash step5.sh query     # the step-5 assertion
bash step6.sh           # seed + verify the tenant fence
```

`ingest` carries the fixture PDF **inside the generated script**, base64'd, so
it is one self-contained upload. `PDF=/path/to/other.pdf` overrides it. The PDF
is deliberately not taken from the container image:
`docker/fastapi.Dockerfile.dockerignore` excludes `**/tests/fixtures`, so the
fixture never reaches the build context, and the first version uploaded it from
inside the task on the stated grounds that it "ships in the image" — the live
run on 2026-09-18 died with `FileNotFoundError`. The second version required
the PDF beside the script, and the rehearsal then ran it twice with the file
missing or the script stale, because "upload two files and put them next to
each other" is a step that can half-happen.

The step-5 bundle covers **all four phases**. An earlier version emitted
seed+ingest only and left `status` and `query` to `run_step5.sh`, which needs
the checkout the bundle exists because you do not have — so step 5 was not
actually runnable end to end in the one environment it was built for. Its seed
also travelled as a `sed`-extracted `BEGIN;`…`COMMIT;`, which dropped
everything after `COMMIT` — now the `[SEED-OK]` assertion — so a half-landed
corpus passed here while failing through `run_against_deployment.sh`. Both
bundles now use the same gzip+base64 asyncpg driver.

It is a generator rather than a committed standalone script on purpose. The
obvious version — paste the SQL into a second file and commit it — creates a
copy of `seed_multitenant_corpus.sql` that drifts the first time someone
edits one and not the other. Generating means the bundle cannot disagree
with the source it came from.
