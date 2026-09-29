# ADR 0024: Qdrant storage stays on EFS for now; Qdrant leaves Fargate Spot

- **Date**: 2026-09-29
- **Status**: Proposed. The Terraform half is written; the storage decision is
  Kyle's.
- **Deciders**: Kyle Maguire (SME)
- **Relates to**: ADR-0022 (AWS as the production cloud), audit finding AWS-14

## Context

Production Qdrant (`qdrant/qdrant:v1.19.1`, one task) keeps `/qdrant/storage`
on EFS through an access point (`deploy/aws/terraform/data.tf`,
`services.tf` `efs_mount_path`). EFS is NFSv4. The collection's WAL and its
mmap'd segments live there.

Two things stop that task abruptly:

1. **Fargate Spot reclaims**, at any time of day, with two minutes' notice.
   `spot.tf` put every service on Spot by default, Qdrant included.
2. **The nightly shutdown sweep**, every day at 17:00 America/Vancouver.

The audit that raised this (AWS-14) recalled Qdrant's installation guidance as
requiring block-level, POSIX-compatible storage and ruling out network file
systems such as NFS. **That has not been verified in this session**: the
documentation host is blocked from this environment (`CONNECT 403`). Treat it
as a claim to check, not a fact. Kyle, or anyone with a browser, should read
Qdrant's current installation/storage requirements and record the exact
sentence here.

The Azure deployment had the same class of problem with Azure Files
(`data.tf` records the quota exhaustion that stalled the optimiser).

## What goes wrong if the claim is right

- Collection corruption, or a slow WAL recovery, after an abrupt stop. It
  shows up as retrieval returning nothing, or as Qdrant failing `/readyz` in
  the morning so the startup sweep's tier 1 never goes stable.
- Qdrant is the one store with **no backup of its own**. EFS automatic backups
  are off, and `upgrade-qdrant.sh` takes the only snapshots, and only when it
  runs. Rebuilding means re-embedding from `silver.document_passages`
  (`scripts/reset_embeddings_for_reencode.py`), which costs Bedrock calls and
  hours.

## Decision (tonight, 2026-09-29)

**Do not move the storage tonight.** A storage move for the only unbacked
store is not a change to make blind, at night, without a snapshot and a
rehearsal.

**Take the mitigations that need no migration:**

1. **Qdrant is pinned to on-demand Fargate** (`spot.tf`,
   `local.pinned_on_demand`). This is a local rather than the
   `on_demand_services` default, so a tfvars that sets that variable cannot
   silently undo it. It removes the unscheduled abrupt stops. The nightly
   stop remains.
2. **`stopTimeout = 120`** on Qdrant (`services.tf`,
   `local.service_stop_timeout`), up from Fargate's 30-second default. 120 s is
   Fargate's maximum. It gives Qdrant time to flush its WAL on SIGTERM.
3. **The shutdown sweep drains tiers in reverse** (`shutdown-sweep.sh`, audit
   AWS-8). Qdrant stops only after the worker and fastapi have stopped
   writing to it.
4. **EFS `transition_to_primary_storage_class = AFTER_1_ACCESS`**
   (`data.tf`, audit AWS-17). Segments that aged into IA come back on the
   first read instead of being re-billed from IA every morning.

## Cost of the mitigation

All figures come from `spot.tf`'s own table: 13.5 vCPU and 30 GB cost
$0.68/h on demand and about $0.20/h on Spot. Qdrant's task is 1 vCPU and
4 GB, so its share of the $0.48/h difference is somewhere between the vCPU
share (7%) and the memory share (13%). That is roughly $0.04–0.06/h. At the
8.5 h/day schedule it comes to about **$9–16/month extra**. This is derived
from the repo's own numbers, not from AWS pricing. Check it in Cost Explorer
after a week.

## Options for the real fix (Kyle to choose)

| Option | What it takes | Trade-off |
| --- | --- | --- |
| A. Accept EFS, with snapshots | A scheduled Qdrant snapshot to the backups bucket, e.g. a Hatchet cron before 17:00 reusing `upgrade-qdrant.sh`'s snapshot task | Keeps the topology; adds a restore path it does not have today |
| B. Task-local storage + snapshot/restore | Ephemeral storage (Fargate allows up to 200 GiB), snapshot to S3 at shutdown, restore at startup | Block-level storage; startup gets slower by the restore time; a failed snapshot at 17:00 loses the day's upserts unless re-embedded |
| C. EBS volume attached to the ECS task | ECS-managed EBS volumes for Fargate, created from a snapshot at task start | Block storage without a restore step in the app; needs its own Terraform and a snapshot lifecycle; not evaluated here |
| D. Managed Qdrant (Qdrant Cloud) | A new vendor surface, network egress, a contract | Removes the store from the power switch; bills while it exists |

Whichever is chosen, it needs a rehearsal against a copy of the collection
first. `upgrade-qdrant.sh` already records exact point counts per collection,
and that is the check to reuse.

## Consequences

- Qdrant keeps its EFS storage and inherits whatever risk the vendor's
  guidance attaches to it. The risk now covers only the nightly stop, not
  Spot reclaims at random times, and the nightly stop now has a 120 s drain.
- There is a ~$9–16/month line item, estimated above.
- This ADR stays **Proposed** until the vendor guidance is verified and an
  option from the table is chosen or explicitly declined.
