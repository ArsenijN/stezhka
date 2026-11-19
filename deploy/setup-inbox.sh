#!/usr/bin/env bash
# setup-inbox.sh - prepare the Syncthing inbox folder for Stezhka on the server and switch it on.
#
#   sudo bash ~/stezhka-src/deploy/setup-inbox.sh [syncthing-user]     (default user: $SUDO_USER)
#
# Creates /srv/syncthing/stezhka-inbox, owned by the user Syncthing runs as, readable by the
# "stezhka" service through a default ACL (so every file Syncthing writes later is readable too,
# whatever permissions it arrives with), sets STEZHKA_INBOX in /etc/stezhka.env and restarts.
# Idempotent. See docs/syncthing.md for the Syncthing side.
set -euo pipefail

INBOX=/srv/syncthing/stezhka-inbox
ENV_FILE=/etc/stezhka.env
SYNC_USER="${1:-${SUDO_USER:-}}"

[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)"; exit 1; }
[ -n "$SYNC_USER" ] && id "$SYNC_USER" &>/dev/null || { echo "usage: setup-inbox.sh <user Syncthing runs as>"; exit 1; }
id stezhka &>/dev/null || { echo "user 'stezhka' missing: deploy Stezhka first (deploy/deploy.sh)"; exit 1; }
[ -f "$ENV_FILE" ] || { echo "$ENV_FILE missing: deploy Stezhka first"; exit 1; }
command -v setfacl >/dev/null || { echo "setfacl missing: apt install acl"; exit 1; }

install -d -o "$SYNC_USER" -g "$SYNC_USER" -m 755 /srv/syncthing
install -d -o "$SYNC_USER" -g stezhka -m 2750 "$INBOX"
setfacl -R -m g:stezhka:rX "$INBOX"
setfacl -R -d -m g:stezhka:rX "$INBOX"
echo "inbox: $INBOX (owner $SYNC_USER, readable by stezhka)"

if grep -q '^STEZHKA_INBOX=' "$ENV_FILE"; then
    sed -i "s|^STEZHKA_INBOX=.*|STEZHKA_INBOX=$INBOX|" "$ENV_FILE"
elif grep -q '^#STEZHKA_INBOX=' "$ENV_FILE"; then
    sed -i "s|^#STEZHKA_INBOX=.*|STEZHKA_INBOX=$INBOX|" "$ENV_FILE"
else
    printf '\nSTEZHKA_INBOX=%s\n' "$INBOX" >> "$ENV_FILE"
fi
echo "enabled in $ENV_FILE"

systemctl restart stezhka
sleep 2
journalctl -u stezhka -n 20 --no-pager | grep -i 'inbox' || true
echo "done: point the Syncthing folder at $INBOX (folder type: Receive Only)"
