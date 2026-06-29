# PR plan — retrieval-era uncommitted WIP (2026-06-24)

Drafted by Claude for Kyle's review. **Nothing here has been committed.** This
plan + the companion script `scripts/create_retrieval_era_prs.sh` slice the
uncommitted working-tree changes into reviewable themed PRs, mirroring the
proven `scripts/create_session_prs.sh` pattern (which already sliced the
2026-06-03 audit/recommendations work into `pr/01…pr/09`).

## What this is (and isn't)

The working tree currently carries **~70 uncommitted files** (44 modified + 26
untracked, excluding `.claude/worktrees/`). These are NOT the audit work — that
already shipped on `pr/01-audit-invariants-ci-gates … pr/09-docs-pr-plan`. This
is the **2026-06-01 → 06-03 retrieval-era** output that never got committed:
the retrieval-quality overhaul, the ChatGPT gap import, the Qdrant schema fix,
the Cameco recovery, the Qwen ecosystem swap, plus assorted tenancy / evidence
/ monitoring / Foundry work (see `OVERNIGHT_2026_06_02.md` and the memory
index for the narrative).

These need to land before `pr/14-version-audit-updates` can cleanly cascade.

## Two decisions baked in (change if you disagree)

1. **Base = `pr/14-version-audit-updates`** (revised from `v2.1-baseline`). The
   WIP was authored on the `pr/11→14` lineage, so files touched by both those
   commits AND the WIP cannot be replayed onto `v2.1-baseline` — `git checkout
   v2.1-baseline` aborts on the conflict (confirmed on the handover docs). So
   the themed branches are cut from `pr/14`: conflict-free and faithful to where
   the work was written. Trade-off: the WIP PRs are **stacked** on `pr/14`
   (can't merge until the `pr/11…pr/14` chain merges) rather than an independent
   fan.
2. **~11 themed PRs**, file-level grouping (table below). The script stages
   whole files, so a change that spans features can't be split by hunk — see
   Caveats.

## Proposed branches

| Branch | Theme | Files |
|---|---|---|
| `pr/w01-retrieval-quality-overhaul` | Multi-query expansion, multi-project decomposition, citation-first salvage, sentence grounding, map-reduce summarizer, eval validator recal | `multi_query_expansion.py`, `multi_project_decomposition.py`, `atomic_claim_extractor.py`, `sentence_grounding.py`, `corpus_summarizer.py`, `query_classifier.py`, `models/rag.py`, `services/eval/validators.py`, `agent/llm_calls.py`, `agent/deps.py`, 3 tests, `OVERNIGHT_2026_06_02.md` |
| `pr/w02-chatgpt-gap-import` | 1500 gap questions → eval.golden_questions | `GapImportCsvSeeder.php`, `…extend_golden_questions_set_check.php`, 2 CSVs |
| `pr/w03-qdrant-chunks-schema` | georag_chunks sparse-slot fix + payload audit | `init_qdrant.py`, `qdrant_payload_audit.py` |
| `pr/w04-cameco-ingest-throttle` | upload-path throttle + `--keys-file` reingest | `ReingestProject.php`, `HatchetDispatchThrottle.php`, `UploadController.php`, `DrillUploadController.php`, 3 Ingestion tests |
| `pr/w05-qwen-ecosystem-swap` | reranker swap + embedding runbook + drop-ollama check | `reranker.py`, `embedding-swap-qwen3.md`, `…drop_ollama…check.php` |
| `pr/w06-evidence-citation` | EvidenceController + QueryPersistFailure event + inspector UI | `EvidenceController.php`, `QueryPersistFailure.php`, `EvidenceInspector.tsx`, `CitationFeedbackController.php`, `channels.php`, channel-auth test |
| `pr/w07-rls-tenancy` | GUC RLS policy fixes + tenancy regression tests | 2 migrations, `NoLegacyGucSetConfigInPhpTest.php`, `WorkspaceRlsCoverageTest.php` |
| `pr/w08-shadow-observability` | shadow-trigger + answer-quality scoring observability | `shadow_trigger.py`, `score_answer_quality.py`, observability test |
| `pr/w09-monitoring-alerts` | vLLM/watchdog Prometheus rules + alertmanager + buckets | `vllm-alerts.yml`, `watchdog.yml`, `alertmanager.yml`, `create-buckets.sh` |
| `pr/w10-foundry-frontend` | NewProject UI + Foundry/PublicApi/Trust controllers | `NewProject.tsx`, 4 Foundry controllers, `InterpretationWorkspaceController.php`, `PublicApiController.php`, `TrustController.php` |
| `pr/w11-backend-housekeeping` | lifespan wiring + assorted backend/docs leftovers | `main.py`, `worker.py`, `tiff_normalize.py`, `figure_extractor.py`, `public_geoscience_tool.py`, `store_reconciliation.py`, `escalation_routing.py`, `dagster/resources.py`, `bench_500q_overnight.py`, `CgiVocabSeeder.php`, 2 READMEs/COVERAGE, 4 handover docs, `create_session_prs.sh`, this plan |

The script emits a **leftovers report** at the end — any uncommitted file not
assigned to a branch is listed so nothing is silently dropped.

## Caveats — read before running

1. **Cross-cutting files can't be hunk-split by a file-level script.** The most
   likely offenders: `src/fastapi/app/main.py` (+146 lines — lifespan wiring
   for several features at once) and `services/eval/validators.py`. They are
   assigned to a single branch (`w11` and `w01` respectively), but their diffs
   carry hunks for more than one feature. **Review those two PRs' diffs by
   hand**, or split them with `git add -p` before running the rest.
2. **Wiring vs. service coupling.** `w01` ships the retrieval services; their
   lifespan registration lives in `main.py` (assigned to `w11`). If you merge
   `w01` without `w11`, the services may be present but unwired. Either move the
   relevant `main.py` hunk into `w01`, or merge `w11` alongside `w01`.
3. **Merge-with-pr/14 conflicts later.** A few files here also evolved on the
   `pr/14` lineage (`eval/validators.py`, `shadow_trigger.py`,
   `figure_extractor.py`). Expect merge conflicts when these branches and the
   `pr/11…pr/14` stack converge — resolve at integration time.
4. **`.claude/worktrees/` is excluded** (local agent state). It should be in
   `.gitignore` if it isn't already.
5. **Migrations need test-DB siblings.** Per the test-DB-parity convention, any
   raw-SQL migration here may need a `*_provision_*_for_test_db.php` sibling.
   The four migrations in `w02/w05/w07` were authored in earlier sessions —
   confirm they migrate cleanly under SQLite (`php artisan test`) before
   merging.

## How to run (you, under your identity)

```bash
git config user.name 'Kyle Maguire'                    # the script refuses the agent identity
git checkout pr/14-version-audit-updates               # WIP carries over uncommitted
bash scripts/create_retrieval_era_prs.sh --dry-run     # sanity: file→branch mapping
bash scripts/create_retrieval_era_prs.sh               # commit locally
git log --oneline pr/w01-retrieval-quality-overhaul    # review one
bash scripts/create_retrieval_era_prs.sh --push --open-prs   # ship (needs gh auth)
```

Idempotent: existing branches are skipped, so re-run safely after fixing a
group.
