#!/usr/bin/env bash
# install.sh - install or upgrade Stezhka on the server. Runs as root, from the synced source tree:
#
#   sudo bash ~/stezhka-src/deploy/install.sh        (deploy.sh does this for you)
#
# Idempotent: re-run it for every upgrade. It never overwrites /etc/stezhka.env or anything in
# /srv/stezhka/data once they exist.
#
# Layout:
#   /srv/stezhka/app     code + web UI (root-owned, read-only for the service)
#   /srv/stezhka/venv    Python virtualenv
#   /srv/stezhka/data    database, raw GPX, basemap, DEM tiles (owned by user "stezhka")
#   /etc/stezhka.env     configuration (created once from deploy/stezhka.env.example)
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
PREFIX=/srv/stezhka
APP="$PREFIX/app"
VENV="$PREFIX/venv"
DATA="$PREFIX/data"
ENV_FILE=/etc/stezhka.env
UNIT=/etc/systemd/system/stezhka.service

[ "$(id -u)" -eq 0 ] || { echo "install.sh must run as root (sudo)"; exit 1; }
command -v rsync >/dev/null || { echo "rsync is required: apt install rsync"; exit 1; }

echo "== user and directories"
id stezhka &>/dev/null || useradd --system --home-dir "$PREFIX" --shell /usr/sbin/nologin stezhka
install -d -o root -g root -m 755 "$PREFIX" "$APP"
install -d -o stezhka -g stezhka -m 750 "$DATA"

echo "== code"
rsync -a --delete \
    --exclude '__pycache__/' --exclude '*.egg-info/' --exclude 'build/' --exclude '.pytest_cache/' \
    "$SRC/server" "$SRC/web" "$SRC/deploy" "$SRC/LICENSE" "$SRC/README.md" "$APP/"

echo "== python environment"
if [ ! -x "$VENV/bin/python" ]; then
    python3 -m venv "$VENV" || { echo "python3 -m venv failed: apt install python3-venv"; exit 1; }
fi
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet --upgrade "$APP/server"
"$VENV/bin/stezhka" --help >/dev/null

echo "== configuration"
if [ ! -f "$ENV_FILE" ]; then
    install -m 640 -o root -g stezhka "$APP/deploy/stezhka.env.example" "$ENV_FILE"
    echo "   created $ENV_FILE (edit it to enable the Syncthing inbox etc.)"
else
    echo "   keeping existing $ENV_FILE"
fi
install -m 755 "$APP/deploy/stezhka-admin" /usr/local/sbin/stezhka-admin

echo "== systemd"
if ! cmp -s "$APP/deploy/stezhka.service" "$UNIT"; then
    install -m 644 "$APP/deploy/stezhka.service" "$UNIT"
    systemctl daemon-reload
fi
systemctl enable --quiet stezhka
systemctl restart stezhka

echo "== health check"
for _ in $(seq 1 30); do
    if session=$("$VENV/bin/python" -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8470/api/session', timeout=2).read().decode())" 2>/dev/null); then
        echo "   up: $session"
        case "$session" in
            *'"password_set":false'*) echo; echo "NEXT: set the login password:   sudo stezhka-admin set-password" ;;
        esac
        [ -f "$DATA/basemap/region.pmtiles" ] || echo "NEXT: download the map (~230 MB): sudo stezhka-admin basemap"
        exit 0
    fi
    sleep 1
done
echo "Stezhka did not answer on 127.0.0.1:8470. Recent log:"
journalctl -u stezhka -n 40 --no-pager
exit 1
