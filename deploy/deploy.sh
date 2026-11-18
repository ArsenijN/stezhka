#!/usr/bin/env bash
# deploy.sh - test locally, sync the source to the server, install/upgrade and restart Stezhka.
# Same configuration style as the web server's sync_to_server.sh.
#
#   cp deploy/deploy.env.sample deploy/deploy.env    # once; fill in your values (gitignored)
#   ./deploy/deploy.sh                                # tests -> sync -> sudo install.sh
#   ./deploy/deploy.sh --skip-tests
#
# You are asked for the SSH key passphrase once (shared connection) and for sudo on the server
# once (interactive prompt over ssh -t; the password is never stored or echoed).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
ENV_FILE="$HERE/deploy.env"

if [ ! -f "$ENV_FILE" ]; then
    echo "ERROR: $ENV_FILE not found. Copy deploy/deploy.env.sample to deploy/deploy.env and fill it in."
    exit 1
fi
while IFS='=' read -r key val; do
    [[ "$key" =~ ^[[:space:]]*# ]] && continue
    [[ -z "${key// }" ]] && continue
    val="${val%%#*}"
    key="${key//[[:space:]]/}"
    val="${val#"${val%%[![:space:]]*}"}"
    val="${val%"${val##*[![:space:]]}"}"
    val="${val/\$\{HOME\}/$HOME}"
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] && export "$key=$val"
done < "$ENV_FILE"

: "${REMOTE_USER:?set REMOTE_USER in deploy.env}"
: "${REMOTE_HOST:?set REMOTE_HOST in deploy.env}"
REMOTE_PORT="${REMOTE_PORT:-22}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519}"
REMOTE_SRC="${REMOTE_SRC:-stezhka-src}"           # relative to the remote user's home
DEV_PYTHON="${DEV_PYTHON:-$HOME/.venvs/stezhka/bin/python}"

SKIP_TESTS=0
for arg in "$@"; do
    case "$arg" in
        --skip-tests) SKIP_TESTS=1 ;;
        -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
        *) echo "unknown option: $arg"; exit 1 ;;
    esac
done

if [ "$SKIP_TESTS" -eq 0 ]; then
    echo "== tests"
    (cd "$ROOT/server" && "$DEV_PYTHON" -m pytest -q -p no:warnings) || { echo "tests failed - not deploying"; exit 1; }
fi

SSH_OPTS=(-p "$REMOTE_PORT" -i "$SSH_KEY"
          -o ControlMaster=auto -o "ControlPath=/tmp/ssh_mux_stezhka_%r@%h:%p" -o ControlPersist=120)
echo "== connecting to $REMOTE_HOST"
ssh "${SSH_OPTS[@]}" -N -f "$REMOTE_USER@$REMOTE_HOST"

echo "== sync -> ~/$REMOTE_SRC"
rsync -az --delete \
    --exclude '.git/' --exclude 'data/' --exclude '.claude/' --exclude 'deploy/deploy.env' \
    --exclude '__pycache__/' --exclude '*.egg-info/' --exclude 'build/' --exclude '.pytest_cache/' \
    -e "ssh ${SSH_OPTS[*]}" "$ROOT/" "$REMOTE_USER@$REMOTE_HOST:$REMOTE_SRC/"

echo "== install on server (sudo)"
ssh -t "${SSH_OPTS[@]}" "$REMOTE_USER@$REMOTE_HOST" "sudo bash $REMOTE_SRC/deploy/install.sh"

ssh "${SSH_OPTS[@]}" -O exit "$REMOTE_USER@$REMOTE_HOST" 2>/dev/null || true
echo "deploy complete"
