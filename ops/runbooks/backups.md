# Backups — weekly logical dump of prod Postgres

Phase A rail ("the Sieve DB loss is the lesson", docs/03-roadmap.md §rails). Gate-1
evidence is serial and irreproducible: once panel runs start, losing the DB loses the
Sep 1 verdict. This rail must be live BEFORE the first scheduled panel run.

## Design

A dedicated Railway cron service (`backup`) in the same project, built from
`ops/backup/Dockerfile` (postgres:18-alpine → `pg_dump | gzip`), with its own volume at
`/data`. Weekly on Sundays 03:00 UTC, keeps the newest 8 dumps (~2 months of weeklies).
The script fails loudly (non-zero exit → failed run visible in Railway) on empty dumps,
gzip corruption, or a dump missing `schema_migrations`. After the integrity checks it
runs the optional off-site copy and the restore-verify step (both below).

Why a separate service: the dump lives on a *different volume* than the database, and a
worker-code deploy can never break the backup path. Why logical dumps: restorable onto
any Postgres 16 (`gzip -dc dump.sql.gz | psql <fresh-db-url>`), diffable, and small at
this dataset size.

## One-time setup (Railway)

1. New service `backup` from this GitHub repo. Service variables:
   - `RAILWAY_DOCKERFILE_PATH=ops/backup/Dockerfile`
   - `DATABASE_URL` → reference `${{Postgres.DATABASE_URL}}`
   - `RESTORE_VERIFY=1` (the script's default too — set `0` only to debug a wedged run)
2. Attach a volume, mount path `/data`.
3. Service settings → Cron Schedule: `0 3 * * 0`, restart policy NEVER.
4. Trigger one manual run; the logs must show the off-site line, a
   `restore-verify: ok (...)` (or its honest skip note), and end with
   `backup: ok (... KB)` plus the retained-dumps listing.

## Off-site copy (Phase D4 — rclone hook)

After the integrity checks pass, the script copies the dump off the project:
`rclone copyto "$out" "$GM_OFFSITE_REMOTE/<basename>"`. rclone or nothing — no aws-cli,
no hand-rolled sigv4. A failed copy exits non-zero (the run shows failed; the local dump
is already safe on the volume). When unconfigured it prints
`off-site: not configured (set RCLONE_* env)` and exits 0 — absence is an honest state.

Setup (any rclone backend — B2/S3/GCS/…):

1. Add `rclone` to `ops/backup/Dockerfile` (`apk add --no-cache rclone` — the binary is
   NOT in the postgres:18-alpine base image; the hook self-reports as unconfigured until
   the image ships it AND the env below is set).
2. Configure the remote purely via env vars on the backup service — no config file:
   e.g. `RCLONE_CONFIG_OFFSITE_TYPE=b2`, `RCLONE_CONFIG_OFFSITE_ACCOUNT=...`,
   `RCLONE_CONFIG_OFFSITE_KEY=...` (rclone reads `RCLONE_CONFIG_<NAME>_<KEY>`).
3. `GM_OFFSITE_REMOTE=offsite:gm-backups` (remote name from step 2 + bucket/path).
4. Trigger a manual run; logs must show `off-site: ok`; confirm the object exists in the
   bucket's web console. Retention in the bucket is the bucket's problem (lifecycle
   rule) — the script only prunes the local volume.

## Automatic restore-verify (Phase D4 extension — every run, not just quarterly)

Beyond the D4 contract's off-site hook, `backup.sh` self-verifies every dump when
`RESTORE_VERIFY=1` (the default — in the script and in the service config above): after
the dump + integrity checks it uses `createdb`/`psql` (present in the postgres:18-alpine
image) to create a scratch db `gm_restore_verify` on the SAME server as `DATABASE_URL`,
pipes the dump in with `ON_ERROR_STOP=1`, compares `schema_migrations` and
`citation_results` counts against the live db, drops the scratch db, and prints
`restore-verify: ok (N migrations, M citation rows)` — or fails loudly on any restore
error or count mismatch. Honest guards: if the role cannot `createdb`, it prints
`restore-verify: skipped — createdb not permitted ...` and continues (the dump is still
good); a count mismatch can also mean rows landed between dump and verify — re-run the
service manually to distinguish a race from a broken dump. This does NOT replace the
quarterly drill below: same-server restore proves the dump's contents, not that you can
reach them from a fresh machine when the project itself is gone.

## Restore verification (quarterly, ~10 min — a backup that has never been restored is a hope, not a backup)

1. Spin a scratch Postgres (locally: the HANDOFF §0 pg16 recipe) — never the prod DB.
2. Copy the newest dump off the volume (Railway shell on the backup service →
   `ls -t /data/backups | head -1`, then `railway ssh`/volume download).
3. `gzip -dc growthmonk-<stamp>.sql.gz | psql postgresql://postgres@localhost:54329/restore_test`
4. `DATABASE_URL=<scratch> gm status` — org/site/run counts must match prod expectations;
   spot-check `select count(*) from citation_results` vs the live number.
5. Note the date + result in this file's log below.

## Known limits (honest)

- The off-site hook exists in backup.sh but stays inert until BOTH the rclone binary is
  added to the image and the `RCLONE_CONFIG_*`/`GM_OFFSITE_REMOTE` env is set (setup
  above). Until then, dumps live only on a Railway volume in the SAME project: that
  protects against DB corruption/loss and bad migrations, NOT against whole-project
  deletion or Railway account loss.
- Restore-verify restores onto the same server the backup came from — it proves the
  dump restores, not that the off-site copy is retrievable. The quarterly drill covers
  the fresh-machine path.
- Railway's own Postgres image ships WAL/PITR features depending on plan; this rail
  assumes none of them.

## Restore-verification log

| Date | Dump | Result |
|---|---|---|
| — | — | not yet performed (service created 2026-07-04) |
