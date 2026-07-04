# Backups — weekly logical dump of prod Postgres

Phase A rail ("the Sieve DB loss is the lesson", docs/03-roadmap.md §rails). Gate-1
evidence is serial and irreproducible: once panel runs start, losing the DB loses the
Sep 1 verdict. This rail must be live BEFORE the first scheduled panel run.

## Design

A dedicated Railway cron service (`backup`) in the same project, built from
`ops/backup/Dockerfile` (postgres:16-alpine → `pg_dump | gzip`), with its own volume at
`/data`. Weekly on Sundays 03:00 UTC, keeps the newest 8 dumps (~2 months of weeklies).
The script fails loudly (non-zero exit → failed run visible in Railway) on empty dumps,
gzip corruption, or a dump missing `schema_migrations`.

Why a separate service: the dump lives on a *different volume* than the database, and a
worker-code deploy can never break the backup path. Why logical dumps: restorable onto
any Postgres 16 (`gzip -dc dump.sql.gz | psql <fresh-db-url>`), diffable, and small at
this dataset size.

## One-time setup (Railway)

1. New service `backup` from this GitHub repo. Service variables:
   - `RAILWAY_DOCKERFILE_PATH=ops/backup/Dockerfile`
   - `DATABASE_URL` → reference `${{Postgres.DATABASE_URL}}`
2. Attach a volume, mount path `/data`.
3. Service settings → Cron Schedule: `0 3 * * 0`, restart policy NEVER.
4. Trigger one manual run; the logs must end with `backup: ok (... KB)` and the
   retained-dumps listing.

## Restore verification (quarterly, ~10 min — a backup that has never been restored is a hope, not a backup)

1. Spin a scratch Postgres (locally: the HANDOFF §0 pg16 recipe) — never the prod DB.
2. Copy the newest dump off the volume (Railway shell on the backup service →
   `ls -t /data/backups | head -1`, then `railway ssh`/volume download).
3. `gzip -dc growthmonk-<stamp>.sql.gz | psql postgresql://postgres@localhost:54329/restore_test`
4. `DATABASE_URL=<scratch> gm status` — org/site/run counts must match prod expectations;
   spot-check `select count(*) from citation_results` vs the live number.
5. Note the date + result in this file's log below.

## Known limits (honest)

- Dumps live on a Railway volume in the SAME project: protects against DB
  corruption/loss and bad migrations, NOT against whole-project deletion or Railway
  account loss. Off-site copy (S3/B2/GCS) is the upgrade when credentials for an object
  store exist — add `aws s3 cp` (or rclone) as a final step in backup.sh then.
- Railway's own Postgres image ships WAL/PITR features depending on plan; this rail
  assumes none of them.

## Restore-verification log

| Date | Dump | Result |
|---|---|---|
| — | — | not yet performed (service created 2026-07-04) |
