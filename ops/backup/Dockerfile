# Weekly logical backup of the prod Postgres (Phase A rail — "the Sieve DB loss is the lesson").
# Runs as a Railway cron service (see ops/runbooks/backups.md); RAILWAY_DOCKERFILE_PATH=ops/backup/Dockerfile.
# postgres:18 to match the prod DB server (pg_dump must be >= server major).
FROM postgres:18-alpine
RUN apk add --no-cache bash gzip coreutils
COPY ops/backup/backup.sh /backup.sh
RUN chmod +x /backup.sh
CMD ["/backup.sh"]
