# Coordinated backup and restore drill

This runbook covers one Edgar recovery unit: PostgreSQL, source files under `data/`, and every
Qdrant collection. It deliberately requires a write-quiesced capture window. A PostgreSQL dump,
filesystem copy, and Qdrant snapshots taken at unrelated times can refer to different adventure
versions and do not make a coordinated recovery point.

## Retention and ownership

The recovery operator owns the schedule, capture alerting, encryption, independent storage,
retention deletion, restore drills, and recovery records. The application does not upload or age
out backups. Keep bundles in an encrypted storage account and administrative boundary separate
from the host that runs Edgar. Keep encryption keys in the organization’s approved key service;
never place a key, `.env`, database URL, or Qdrant API key inside a bundle.

Recommended minimum policy:

| Copy | Retention | Purpose |
| --- | --- | --- |
| Daily coordinated bundle | 35 days | Recovery from recent operator error or host loss |
| Weekly coordinated bundle | 13 weeks | Recovery after a delayed incident is discovered |
| Monthly coordinated bundle | 12 months | Longer incident investigation and rollback |

Retain the PostgreSQL event history classified as durable by EDG-061 for the lifetime of its
campaign. Diagnostic events may follow their separate reviewed expiry policy; container log
rotation is not a database retention policy. Never expire a backup while it is the only verified
copy of required campaign history or source material. Store at least one copy outside the
application host and its local Docker volumes. Use storage with access controls and deletion
protection appropriate to private campaign content.

Use a planning RPO target of at most 24 hours and a planning RTO target of at most 60 minutes.
These are proposed objectives, not measured results or guarantees. The RPO target depends on the
daily capture succeeding and being monitored. Set a reference deployment size and record actual
bundle age, capture duration, restore duration, database/source/vector sizes, and test outcome at
each drill; do not claim either objective until operators have measured it on the deployment they
run. Perform a disposable restore drill monthly and after changes to database migrations, source
publication, or the Qdrant version.

## Capture

Use a host with `pg_dump`, Edgar’s Python environment, network access to PostgreSQL and Qdrant,
and enough protected local space for one complete bundle. The supplied implementation captures
Qdrant with collection snapshots, so the documented Compose single-node Qdrant service is the
supported target. A multi-node Qdrant deployment needs its own supported cluster snapshot plan.

1. Drain player writes. Stop the API process and any ingestion, generation, or indexing worker.
   Confirm no migration or manually invoked ingestion is running and no transaction remains in
   progress. Keep PostgreSQL and Qdrant available to the capture host.
2. Inject `EDGAR_BACKUP_DATABASE_URL`, `EDGAR_BACKUP_QDRANT_URL`, and, when configured,
   `EDGAR_BACKUP_QDRANT_API_KEY` from the approved secret manager. Do not place credential values
   in shell history or command arguments. `data/` is the source-data root as mounted by the API.
3. Run the capture and checksum verification:

   ```sh
   uv run python -m tools.recovery capture \
     --source-data /srv/edgar/data \
     --output /srv/backup-staging/edgar-2026-10-16 \
     --writes-quiesced

   uv run python -m tools.recovery verify \
     --bundle /srv/backup-staging/edgar-2026-10-16
   ```

   The tool stores the PostgreSQL database label, file sizes and SHA-256 checksums, Qdrant
   collection snapshots, and active source-version receipts. It rejects an adventure whose ready
   metadata or retained manuscript checksum disagrees with that source’s active vector version.
   Source roots include adventure metadata and attempt history, private manuscripts, PDFs,
   adventure assets, reviewed markdown/extractions, and extraction QA. `.index-locks` is omitted;
   those ephemeral locks are recreated. Secret-named files and key/certificate files are omitted.
4. Encrypt the completed bundle with the approved key service, transfer it to the independent
   backup account, and verify the stored object’s checksum. Confirm the scheduled-copy alert is
   healthy before resuming API and worker traffic. Keep the unencrypted staging copy only for the
   transfer window, then remove it according to the operator’s secure deletion procedure.

The capture command only creates a local bundle. It does not encrypt or upload it, configure
retention, or stop services. The `--writes-quiesced` option records an operator assertion; it
cannot detect a writer on another host.

## Disposable restore

Restore into an isolated host or namespace with a newly created, empty PostgreSQL database named
`edgar_restore_<drill-id>`, a new empty single-node Qdrant instance, and an empty source-data
directory. Use a dedicated database role. Keep the production deployment and its volumes out of
the restore environment. The tool refuses non-loopback database/Qdrant targets, a database name
outside the disposable prefix, a database with public objects, a Qdrant service with any
collection, a nonempty source destination, and restores without the explicit disposable-target
acknowledgement.

1. Retrieve the encrypted bundle and decrypt it into a private temporary directory. Verify it
   before restoring:

   ```sh
   uv run python -m tools.recovery verify --bundle /private/drill/edgar-2026-10-16
   ```

2. Inject `EDGAR_RESTORE_DATABASE_URL` for the empty loopback disposable database,
   `EDGAR_RESTORE_QDRANT_URL` for the empty loopback Qdrant instance, and the restore API key if
   configured. Restore source files, Qdrant collections, then PostgreSQL:

   ```sh
   uv run python -m tools.recovery restore \
     --bundle /private/drill/edgar-2026-10-16 \
     --source-destination /private/drill/restored-data \
     --disposable-target
   ```

3. Start an isolated Edgar API pointed only at those restored resources. Confirm `/ready` reports
   PostgreSQL, Redis, and Qdrant ready; open a fixture campaign and session; load its saved
   narration history; resolve the exact retained manuscript with
   `python -m apps.api.services.manuscript <slug> <temporary-export-path>`; and compare its
   `index_version` with `data/adventures/<slug>.json` and the active Qdrant routing receipt. Run
   one disposable turn with a local fake provider or deterministic test harness, then confirm the
   turn can persist and history reloads. Do not use a production campaign or production credentials
   during a drill.
4. Time the exercise from restore start through readiness and reopened history/turn checks. Record
   the bundle’s capture time, data age at restore, recovered campaign/session IDs, active source
   versions, retained manuscript checksum, duration, and any defects. A provider-free CI drill
   proves storage and application contracts; it does not establish the deployment’s operational
   RTO, independent-storage health, encryption-key recovery, or production RPO.
5. Destroy the disposable PostgreSQL database, Qdrant instance, source tree, exports and decrypted
   bundle using the organization’s drill cleanup procedure. Retain only the drill report and the
   separately protected backup copy.

The repository’s automated drill uses synthetic campaign text and vectors, a disposable
PostgreSQL 16 container and Qdrant v1.16.3, and a deterministic provider stub. It uses the real
`pg_dump`/`pg_restore` binaries inside the PostgreSQL container and exercises the reopened API
readiness, campaign, history, manuscript, vector-version and turn paths. In CI, Docker and both
service containers are mandatory for this test; infrastructure absence is a failure, not a skip.
