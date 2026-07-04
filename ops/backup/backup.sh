#!/usr/bin/env bash
# Weekly logical dump → /data/backups (Railway volume), keep the newest $KEEP.
# Fails loudly: a zero-byte or missing dump exits non-zero so the cron run shows failed.
# After the integrity checks: optional off-site copy (rclone) and a self-verifying
# restore into a scratch db on the same server (RESTORE_VERIFY, default on).
set -euo pipefail

: "${DATABASE_URL:?DATABASE_URL not set}"
BACKUP_DIR="${BACKUP_DIR:-/data/backups}"
KEEP="${KEEP:-8}"
RESTORE_VERIFY="${RESTORE_VERIFY:-1}"

mkdir -p "$BACKUP_DIR"
stamp="$(date -u +%Y-%m-%dT%H%M%SZ)"
out="$BACKUP_DIR/growthmonk-$stamp.sql.gz"

echo "backup: dumping to $out"
pg_dump --no-owner --no-privileges "$DATABASE_URL" | gzip > "$out"

size=$(stat -c%s "$out" 2>/dev/null || stat -f%z "$out")
if [ "$size" -lt 10240 ]; then
  echo "backup: FAILED — dump is only ${size} bytes (expected >10KB for a live schema)" >&2
  exit 1
fi

# Integrity: gzip must decompress end-to-end and contain a schema_migrations row.
# grep -c consumes the whole stream (grep -q would exit early and feed gzip a
# SIGPIPE, which pipefail then reports as failure on a perfectly good dump).
gzip -t "$out"
matches=$(gzip -dc "$out" | grep -c "schema_migrations" || true)
if [ "${matches:-0}" -eq 0 ]; then
  echo "backup: FAILED — dump does not contain schema_migrations" >&2
  exit 1
fi

echo "backup: ok ($((size / 1024)) KB)"

# Off-site copy (Phase D4): rclone or nothing — no aws-cli, no hand-rolled sigv4.
# Runs BEFORE restore-verify so the copy leaves the project even when a later
# step fails loudly. A failed copy exits non-zero; the local dump is already safe.
if env | grep -c '^RCLONE_CONFIG_' >/dev/null && [ -n "${GM_OFFSITE_REMOTE:-}" ]; then
  echo "off-site: copying to $GM_OFFSITE_REMOTE/$(basename "$out")"
  rclone copyto "$out" "$GM_OFFSITE_REMOTE/$(basename "$out")"
  echo "off-site: ok"
else
  echo "off-site: not configured (set RCLONE_* env)"
fi

# Restore-verify (Phase D4 extension, see ops/runbooks/backups.md): pipe the dump
# into a scratch db on the SAME server and compare row counts against live. A
# backup that has never been restored is a hope, not a backup — this makes every
# weekly dump prove it restores. Skips honestly when the role cannot createdb.
if [ "$RESTORE_VERIFY" = "1" ]; then
  scratch_db="gm_restore_verify"
  # Derive the scratch-db URL: swap the path component, keep any query string.
  qs=""
  case "$DATABASE_URL" in *\?*) qs="?${DATABASE_URL#*\?}" ;; esac
  base="${DATABASE_URL%%\?*}"
  case "$base" in
    postgres://*/*|postgresql://*/*)
      scratch_url="${base%/*}/${scratch_db}${qs}"
      dropdb --if-exists --maintenance-db="$DATABASE_URL" "$scratch_db" >/dev/null 2>&1 || true
      if createdb --maintenance-db="$DATABASE_URL" "$scratch_db" 2>/tmp/createdb.err; then
        gzip -dc "$out" | psql -q -v ON_ERROR_STOP=1 "$scratch_url" >/dev/null
        live_mig=$(psql -tA "$DATABASE_URL" -c "select count(*) from schema_migrations")
        scratch_mig=$(psql -tA "$scratch_url" -c "select count(*) from schema_migrations")
        live_cit=$(psql -tA "$DATABASE_URL" -c "select count(*) from citation_results")
        scratch_cit=$(psql -tA "$scratch_url" -c "select count(*) from citation_results")
        dropdb --maintenance-db="$DATABASE_URL" "$scratch_db"
        if [ "$live_mig" != "$scratch_mig" ] || [ "$live_cit" != "$scratch_cit" ]; then
          echo "restore-verify: FAILED — live ${live_mig} migrations/${live_cit} citation rows" \
               "vs restored ${scratch_mig}/${scratch_cit}" >&2
          exit 1
        fi
        echo "restore-verify: ok (${scratch_mig} migrations, ${scratch_cit} citation rows)"
      else
        echo "restore-verify: skipped — createdb not permitted for this role" \
             "($(head -1 /tmp/createdb.err 2>/dev/null || echo 'no error detail'))"
      fi
      ;;
    *)
      echo "restore-verify: skipped — DATABASE_URL has no db path to swap (${base%%://*}://…)"
      ;;
  esac
else
  echo "restore-verify: disabled (RESTORE_VERIFY=$RESTORE_VERIFY)"
fi

# Prune: keep newest $KEEP dumps.
ls -1t "$BACKUP_DIR"/growthmonk-*.sql.gz | tail -n +$((KEEP + 1)) | xargs -r rm -f
echo "backup: retained $(ls -1 "$BACKUP_DIR"/growthmonk-*.sql.gz | wc -l | tr -d ' ') dumps:"
ls -lh "$BACKUP_DIR"/growthmonk-*.sql.gz
