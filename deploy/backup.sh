#!/usr/bin/env bash
# stezhka-backup - nightly backup of Stezhka to cold storage (e.g. an SMB share).
# Installed to /usr/local/sbin by install.sh, run by stezhka-backup.timer. Settings in /etc/stezhka.env:
#
#   STEZHKA_BACKUP_DIR=/mnt/cold-stezhka    where backups go (unset = backups off)
#   STEZHKA_BACKUP_KEEP=14                  database snapshots to keep
#
# 1. consistent database snapshot (SQLite online backup, safe while the service runs), as the
#    service user into data/backups/, keeping the newest STEZHKA_BACKUP_KEEP;
# 2. mirror data/backups/ and data/raw/ (the original GPX files) to STEZHKA_BACKUP_DIR.
# The basemap and DEM tiles are not backed up: they can always be downloaded again.
set -euo pipefail

ENV_FILE="${STEZHKA_ENV_FILE:-/etc/stezhka.env}"          # overridable for testing
ADMIN="${STEZHKA_ADMIN:-/usr/local/sbin/stezhka-admin}"
set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a
DATA="${STEZHKA_DATA_DIR:-/srv/stezhka/data}"
DEST="${STEZHKA_BACKUP_DIR:-}"
KEEP="${STEZHKA_BACKUP_KEEP:-14}"

if [ -z "$DEST" ]; then
    echo "STEZHKA_BACKUP_DIR is not set in /etc/stezhka.env: backups are off"
    exit 0
fi
if [ ! -d "$DEST" ]; then
    echo "ERROR: $DEST does not exist (cold storage not mounted?)"
    exit 1
fi
# A share that is not mounted leaves its mount point as an empty folder on the local disk:
# refuse to "back up" onto the same filesystem as the data.
if [ "$(stat -c %d "$DEST")" = "$(stat -c %d "$DATA")" ]; then
    echo "ERROR: $DEST is on the same filesystem as $DATA (cold storage not mounted?)"
    exit 1
fi

snap="$DATA/backups/stezhka-$(date +%Y-%m-%d).sqlite3"
"$ADMIN" backup "$snap"
# keep the newest $KEEP snapshots
mapfile -t old < <(ls -1t "$DATA"/backups/stezhka-*.sqlite3 2>/dev/null | tail -n +"$((KEEP + 1))")
for f in "${old[@]}"; do rm -f -- "$f"; done

mkdir -p "$DEST/db" "$DEST/raw"
rsync -rt --delete "$DATA/backups/" "$DEST/db/"
rsync -rt --delete "$DATA/raw/" "$DEST/raw/"
echo "backup ok: $(ls -1 "$DEST/db" | wc -l) snapshots, $(find "$DEST/raw" -name '*.gpx' | wc -l) GPX files in $DEST"
