#!/usr/bin/env bash
# Weekly logical dump → /data/backups (Railway volume), keep the newest $KEEP.
# Fails loudly: a zero-byte or missing dump exits non-zero so the cron run shows failed.
set -euo pipefail

: "${DATABASE_URL:?DATABASE_URL not set}"
BACKUP_DIR="${BACKUP_DIR:-/data/backups}"
KEEP="${KEEP:-8}"

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

# Prune: keep newest $KEEP dumps.
ls -1t "$BACKUP_DIR"/growthmonk-*.sql.gz | tail -n +$((KEEP + 1)) | xargs -r rm -f
echo "backup: retained $(ls -1 "$BACKUP_DIR"/growthmonk-*.sql.gz | wc -l | tr -d ' ') dumps:"
ls -lh "$BACKUP_DIR"/growthmonk-*.sql.gz
