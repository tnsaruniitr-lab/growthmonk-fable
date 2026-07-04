# RLS role split — staged cutover to a non-owner worker role

Closes the HANDOFF §2 debt: on Railway the worker and api connect as the database
OWNER, and Postgres exempts table owners from row-level security unless the table has
`FORCE ROW LEVEL SECURITY`. Today the `org_isolation` policies exist but do not bind
the processes that matter. `ops/scripts/create_worker_role.sql` fixes that: it creates
a non-owner `gm_worker` role with plain CRUD grants and forces RLS on every org-scoped
table (explicit list in the script).

**This runbook is a deliberate operator action. Nothing here runs automatically — the
script is NOT a migration, and no deploy performs the cutover.** Execute it before any
second operator (or any second org) touches prod.

Design notes:

- Migrations KEEP the owner URL forever: they create tables and policies, which
  `gm_worker` must never be able to do. Only the worker and api move to the new role.
- `orgs`, `jobs`, `schedules`, `cost_events`, `quota_ledgers`, `schema_migrations` and
  the `gsc_*` tables carry no org_isolation policy and stay plainly readable — the
  worker needs them org-less (job claim loop, cost ledger, ingest bookkeeping).
- FORCE RLS also binds the OWNER on those tables. Owner sessions doing tenant work
  must set `app.org_id` like everyone else (they already do — `gm.db.set_org`,
  ADR-14). Superusers still bypass RLS by definition; Railway's default role is not
  superuser.
- `alter default privileges` in the script covers tables created by FUTURE owner-run
  migrations. New migrations that add org-scoped tables must ALSO add their own
  `force row level security` line to the script's list (grep `org_isolation` under
  `ops/migrations/` to regenerate it).

## Stage 1 — scratch rehearsal (no prod contact)

1. Restore the newest backup onto a scratch pg16 (HANDOFF §0 recipe +
   `ops/runbooks/backups.md` restore steps) — never the prod DB.
2. Apply the script as the scratch owner:
   `psql "$SCRATCH_OWNER_URL" -f ops/scripts/create_worker_role.sql`
3. Set a throwaway password: `alter role gm_worker password 'scratch-only';`
4. Run the verification queries in the script's §4 **as gm_worker** — the forced-RLS
   catalog check, the cross-org probe, and the no-context probe (both probes MUST
   return 0 rows).
5. Run the FULL suite as gm_worker:
   `DATABASE_URL=postgresql://gm_worker:scratch-only@localhost:54329/<scratch> \
    .venv/bin/pytest platform/tests -q`
   Green means the app's `set_org` discipline is complete; any failure here is a code
   path doing tenant work without org context — fix it BEFORE touching prod.

## Stage 2 — create the role on prod (reversible, read-only impact)

1. `psql "$PROD_OWNER_URL" -f ops/scripts/create_worker_role.sql` (idempotent).
2. Set the real password interactively (`\password gm_worker`) — never in a file, never
   in a Railway variable description. Record it only in the secrets store
   (ops/runbooks/secrets.md discipline).
3. Verify grants and forced RLS read-only: script §4a as owner, §4b/§4c as gm_worker.
   Prod keeps running as owner throughout — nothing observable changes for the app.

## Stage 3 — cutover (one env edit per service)

1. Build the worker/api URL: same host/port/db as the owner URL, user `gm_worker` +
   the stage-2 password.
2. In Railway: set `DATABASE_URL` on the **worker** and **api** services to the
   gm_worker URL. Do NOT touch the migration/deploy step's URL — migrations keep the
   owner URL (they run `create table`/`create policy`).
3. Redeploy both services.

## Stage 4 — verify live

1. `GET /healthz` on the api → 200.
2. Watch one full job cycle: enqueue or wait for a scheduled job; it must claim, run,
   and finish (`gm jobs` / `/admin/jobs/recent`). A permission-denied or empty-select
   surprise here means a missed grant or a code path without `set_org` — roll back.
3. Spot-check a tenant read through the api (e.g. a report share URL) still renders.

## Rollback (zero schema changes)

Point the worker + api `DATABASE_URL` back to the owner URL and redeploy — one env
edit per service. The role, grants, and forced RLS stay in place harmlessly (the owner
already operates with org context set); retry the cutover after fixing whatever broke.
