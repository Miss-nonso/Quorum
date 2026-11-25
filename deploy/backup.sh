#!/usr/bin/env bash
# Back up the production database to deploy/backups/ (and prune old dumps).
#   ./deploy/backup.sh            # from the repo root on the server
# Restore:  gunzip -c deploy/backups/<file>.sql.gz | docker compose -f deploy/docker-compose.prod.yml \
#             --env-file deploy/.env exec -T db psql -U quorum -d quorum
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
mkdir -p backups
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
docker compose -f docker-compose.prod.yml --env-file .env exec -T db \
  pg_dump -U quorum -d quorum --no-owner --clean --if-exists | gzip > "backups/quorum-$stamp.sql.gz"
find backups -name 'quorum-*.sql.gz' -mtime +14 -delete
echo "backups/quorum-$stamp.sql.gz"
