#!/usr/bin/env bash
# Back up the production database to deploy/backups/ (and prune old dumps).
#   ./deploy/backup.sh            # from the repo root on the server
# Restore:  gunzip -c deploy/backups/<file>.sql.gz | docker compose -f deploy/docker-compose.prod.yml \
#             --env-file deploy/.env exec -T db psql -U conductor -d conductor
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
mkdir -p backups
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
docker compose -f docker-compose.prod.yml --env-file .env exec -T db \
  pg_dump -U conductor -d conductor --no-owner --clean --if-exists | gzip > "backups/conductor-$stamp.sql.gz"
find backups -name 'conductor-*.sql.gz' -mtime +14 -delete
echo "backups/conductor-$stamp.sql.gz"
