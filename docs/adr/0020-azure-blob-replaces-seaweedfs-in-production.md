# ADR 0020: Azure Blob Storage replaces SeaweedFS as the production object store

- **Date**: 2026-09-06 (record). The decision was implemented 2026-07-30 as
  part of the Azure lift and has run in production since; this ADR is the
  retroactive record the 2026-09-06 architecture reconciliation (#213) found
  missing.
- **Status**: Accepted
- **Deciders**: Kyle Maguire (SME)
- **Supersedes**: the production scope of ADR-0001 (SeaweedFS replaces MinIO).
  ADR-0001 remains in force for the compose / on-premise stack. Amends
  `georag-architecture.html` §02 (object storage row) and §07-azure, both
  already updated in v1.52.
- **Related**: `src/georag_object_storage/` (`factory.py`, `azure_config.py`,
  `azure_sync_client.py`, `azure_async_client.py`), `config/filesystems.php`,
  `app/Providers/AppServiceProvider.php` (`Storage::extend('azure', …)`),
  `deploy/azure/README.md`, `deploy/azure/alerts/create-alerts.sh` §5b.

## Context

GeoRAG's Bronze layer is an immutable object archive: every upload lands
under a category prefix (`reports/`, `tabular/`, `spatial/`, `tiff/`,
`figures/`), is registered in `bronze.ingest_manifest`, and is the only
copy the ingest workflows replay from. ADR-0001 chose SeaweedFS
(`chrislusf/seaweedfs`, Apache 2.0) for that role, running as a single
all-in-one container in `docker-compose.yml` under the service name `minio`.

Production moved to **Azure Container Apps** on 2026-07-30 ("the Azure
lift"). Three facts made SeaweedFS the wrong production substrate:

1. **Container Apps has no persistent local disk.** A SeaweedFS container
   would have to keep its volume, filer and index on an Azure Files (SMB)
   share. Qdrant runs on exactly that arrangement today and produces enough
   optimizer-retry thrash that a dedicated alert exists for it
   (`georagblobcc-transaction-storm`, `create-alerts.sh` §5b). Putting the
   one irreplaceable copy of the corpus behind the same failure mode was not
   acceptable.
2. **Single-process, single-replica.** SeaweedFS all-in-one is a dev-stack
   convenience: one process, one replica, no managed durability. Azure Blob
   gives at least LRS (three replicas in one datacentre) with no operator
   work, and GRS is a flag away.
3. **Managed identity.** Container Apps can authenticate to Blob with a
   system-assigned identity, removing a long-lived S3 access key from every
   app's environment. SeaweedFS has no equivalent.

The storage-abstraction plan had already isolated object access behind
`georag_object_storage` (Python) and Laravel's `Storage` facade, so adding a
second backend behind one switch was cheaper than any hosted-SeaweedFS
arrangement.

## Options considered

| Option | Durability | Effort | Outcome |
|---|---|---|---|
| A. SeaweedFS as a Container App on an Azure Files share | Share-level only; single replica | Medium | Rejected — same SMB thrash Qdrant exhibits; single process holding the only copy. |
| B. Keep an S3 API by fronting Blob with a gateway (MinIO gateway mode) | Blob | Medium | Rejected — MinIO is AGPL and archived (ADR-0001); Azure has no native S3 endpoint. |
| C. External S3 (AWS) from Azure | S3 | Low | Rejected — second cloud vendor, cross-cloud egress on every ingest and download, no managed-identity path. |
| D. **Azure Blob Storage natively, behind `STORAGE_BACKEND`** | **LRS (GRS optional)** ✅ | Medium | Chosen — see Decision. |

## Decision

Production object storage is **Azure Blob Storage** (account `georagblobcc`),
selected by `STORAGE_BACKEND=azure_blob`. The same variable, with the same
two values, switches both layers: `georag_object_storage.factory` in Python
and every `s3*` disk in `config/filesystems.php`, whose `driver` resolves
to `azure` when the value is `azure_blob` and to `s3` otherwise.
`s3_compatible` stays the code default so the compose and air-gapped stacks
keep using SeaweedFS under ADR-0001.

### What stays the same

- Bucket / container names and the prefix layout: `bronze`, `bronze-raster`,
  `exports`, `georag-backups`; `reports/{project_id}/…`, `figures/{report_id}/…`.
  `AzureBlobConfig` maps each `Bucket` to `AZURE_STORAGE_CONTAINER_*` with the
  same defaults.
- Every caller. `UploadController`, `S3BronzeStore`, `passage_embedder`,
  `ingest_zip_archive`, the export job and the figure resolver all go
  through the abstraction and did not change.
- The compose service is still named `minio` and the env vars keep their
  `MINIO_*` / `AWS_*` names for the `s3_compatible` path (ADR-0001's
  compatibility decision stands).
- `bronze.ingest_manifest` keys are backend-agnostic object keys, so a
  manifest written under one backend resolves under the other.

### What changed

- `src/georag_object_storage/`: `azure_config.py` (`AzureBlobConfig`, one of
  `AZURE_STORAGE_CONNECTION_STRING` or `AZURE_STORAGE_ACCOUNT_URL`),
  `azure_sync_client.py`, `azure_async_client.py`; `factory.py` dispatches
  on `STORAGE_BACKEND` (`_KNOWN_BACKENDS = ("s3_compatible", "azure_blob")`).
- `composer.json`: `league/flysystem-azure-blob-storage` pinned `3.0.5`.
- `config/filesystems.php`: `s3`, `s3-bronze` and `exports` disks switch
  driver on `STORAGE_BACKEND`; Azure-only keys `connection_string`,
  `container`, `auth_mode`, `account_name`.
- `app/Providers/AppServiceProvider.php`: `Storage::extend('azure', …)` with
  an opt-in managed-identity mode (`AZURE_STORAGE_AUTH_MODE=managed_identity`,
  token from IMDS via `ManagedIdentityTokenProvider`).
- Per-store `backup_*` Hatchet workflows deleted 2026-08-23: they wrote to a
  SeaweedFS substrate that does not exist on Azure and had failed every
  night since the lift. Durability posture is now Blob LRS + Azure Postgres
  PITR; recorded in `worker.py` and `georag-architecture.html` §06.

## Migration mechanics (for a compose stack moving to Azure)

1. Create the storage account and the four containers (`bronze`,
   `bronze-raster`, `exports`, `georag-backups`). Reversible.
2. Copy existing Bronze objects with `azcopy` preserving keys. Reversible;
   the SeaweedFS volume is untouched.
3. Set `STORAGE_BACKEND=azure_blob` and the `AZURE_STORAGE_*` variables on
   **every** app that touches storage: `laravel-octane-cc`,
   `laravel-horizon-cc`, `fastapi-cc`, `hatchet-worker-cc`. A partial switch
   is the failure mode: an upload lands in Blob and the worker looks for it
   in SeaweedFS. Reversible by flipping the variable back.
4. Restart; confirm the startup line `Bronze store backed by object storage
   (STORAGE_BACKEND=azure_blob)` on `fastapi-cc` and `hatchet-worker-cc`.
5. Upload one PDF; confirm the `bronze.ingest_manifest` row, the Hatchet
   run and a presigned figure URL that resolves. Point of no return is
   decommissioning the SeaweedFS volume, which the compose stack never does.

## Gotchas hit (worth knowing for next time)

1. **`allowSharedKeyAccess` cannot be disabled yet.** Laravel's
   `temporaryUrl()` signs export and figure download URLs with the account
   key, and `microsoft/azure-storage-blob ^1.1` has no user-delegation-key
   SAS. In managed-identity mode the key is used only for local SAS
   signing; blob traffic itself is identity-authenticated. If the
   connection string is missing alongside `managed_identity`,
   `temporaryUrl()` throws rather than minting a broken URL.
2. **`presign_get` / `presign_put` raise `NotImplementedError` under
   managed identity** on the Python side for the same reason.
3. **Storage network `defaultAction` is still Allow.** Restricting it needs
   stable egress IPs, which needs VNet integration the environment does not
   have (`vnetConfiguration` is null).
4. **The same account also hosts Qdrant's SMB file share.** Transaction
   spikes on `georagblobcc` are usually Qdrant's optimizer, not Bronze
   traffic; the alert description says so.
5. **No per-workspace key prefix.** `services/seaweedfs_keys.py` specified
   one and only its own test imported it. Tenant isolation for objects rests
   on `workspace_id` in `bronze.ingest_manifest` and RLS, not on key layout.
   *Closed 2026-09-07: deleted.* Every writer lays keys out as
   `category/project_id/...` (Laravel uploads), `export_id/...` (exports) or
   `pending|final/<id>/page_N.png` (page images); the Ingestion Runs fallback
   scan and the zip-archive re-upload depend on the category being the first
   path component. Adopting the workspace prefix would have meant re-keying
   every live object in `georagblobcc` and every manifest row, for a second
   isolation layer the platform never used. Revisit only if one storage
   account ever has to serve several workspaces with direct listing access.
6. **`.env.production.example` read `STORAGE_BACKEND=s3_compatible`** until
   2026-09-06, with the Azure block marked "only fill in when switching",
   while the live apps set `azure_blob` by hand (nothing in `deploy/azure/`
   templates app env). Fixed the same day: the template now sets
   `azure_blob` with the Azure block primary and the S3 block marked
   compose / on-premise only.

## Consequences

### Positive

- Managed durability (LRS now, GRS available) for the one irreplaceable
  copy, with no backup workflow to operate.
- Managed-identity path removes a long-lived storage key from app
  environments once the SAS limitation is resolved upstream.
- One switch, two layers: the compose stack, CI and air-gapped installs
  keep SeaweedFS unchanged.

### Negative

- Two backends to keep in parity (`azure_*` clients mirror the S3 clients;
  `test_metadata_keys.py` and `test_factory.py` pin the shared contract).
- LRS covers hardware failure, not deletion. There is no soft-delete or
  versioning policy recorded; a bad `azcopy` or a mistaken container delete
  is unrecoverable today.
- Presigned URLs still depend on the account key.

## Verification

- `src/georag_object_storage/tests/`: `test_azure_sync_client.py`,
  `test_azure_async_get_file.py`, `test_factory.py`, `test_metadata_keys.py`.
- Laravel: `tests/Unit/Services/AzureBlobDiskTest.php`,
  `tests/Unit/Services/Azure/ManagedIdentityTokenProviderTest.php`,
  `tests/Feature/Azure/AzureBlobDiskLifetimeTest.php`.
- Runtime: the `STORAGE_BACKEND` value is logged at FastAPI startup;
  `georagblobcc-transaction-storm` in Azure Monitor watches the account.

## Follow-ups (not part of this ADR)

- ~~Flip `.env.production.example` to `STORAGE_BACKEND=azure_blob` and make
  the Azure block primary~~ — done 2026-09-06.
- Decide on Blob soft-delete / versioning (or GRS) — before any customer
  data that cannot be re-uploaded lands.
- ~~Wire or delete `services/seaweedfs_keys.py` (open decision from #194)~~
  — deleted 2026-09-07; see limitation 5.
- Revisit `allowSharedKeyAccess` when `azure-storage-blob` gains
  user-delegation SAS.
- Mark ADR-0001 as compose-scoped (done in this change).
