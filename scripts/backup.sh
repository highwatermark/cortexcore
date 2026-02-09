#!/usr/bin/env bash
# Daily database backup script
# Copies momentum.db to data/backups/ with date stamp
# Retains 30 days of backups
#
# Add to crontab: 30 21 * * 1-5 /home/ubuntu/momentum-agent-v2/scripts/backup.sh
# (4:30 PM ET = 21:30 UTC during EST, 20:30 UTC during EDT)

set -euo pipefail

PROJECT_DIR="/home/ubuntu/momentum-agent-v2"
DB_PATH="${PROJECT_DIR}/data/momentum.db"
BACKUP_DIR="${PROJECT_DIR}/data/backups"
RETENTION_DAYS=30

# Create backup dir if needed
mkdir -p "${BACKUP_DIR}"

# Check DB exists
if [ ! -f "${DB_PATH}" ]; then
    echo "$(date -Iseconds) ERROR: Database not found at ${DB_PATH}"
    exit 1
fi

# Create backup
DATE=$(date +%Y%m%d)
BACKUP_PATH="${BACKUP_DIR}/momentum_${DATE}.db"
cp "${DB_PATH}" "${BACKUP_PATH}"
echo "$(date -Iseconds) Backup created: ${BACKUP_PATH} ($(du -h "${BACKUP_PATH}" | cut -f1))"

# Clean old backups
find "${BACKUP_DIR}" -name "momentum_*.db" -mtime +${RETENTION_DAYS} -delete
echo "$(date -Iseconds) Cleaned backups older than ${RETENTION_DAYS} days"
