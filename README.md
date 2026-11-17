# Stezhka

*стежка: a footpath, a trail.*

Self-hosted ride statistics for cyclists: record rides on your phone, keep them on your own
server, see where you are fast or slow, and (soon) learn per-road-segment speeds, predict ETA,
and get routes that mix known fast roads with paths you have never ridden.

Status: **phase 1**: GPX import, cleaning, stop detection, web map with speed-coloured track and
elevation/speed profile. See [docs/decisions.md](docs/decisions.md) for the design and roadmap.

## Layout

```
server/   Python package `stezhka` (FastAPI + SQLite): import, processing, API, CLI
web/      static UI (plain ES modules + vendored MapLibre GL, no build step)
deploy/   systemd unit + environment example
docs/     design notes and decisions
```

## Development

```sh
python3 -m venv ~/.venvs/stezhka            # outside the repo: NTFS/fuse drives break venv symlinks
~/.venvs/stezhka/bin/pip install -e 'server[dev]'
cd server && ~/.venvs/stezhka/bin/pytest

export STEZHKA_DATA_DIR=$PWD/../data STEZHKA_COOKIE_SECURE=0
~/.venvs/stezhka/bin/stezhka set-password
~/.venvs/stezhka/bin/stezhka import ~/Downloads/*.gpx
~/.venvs/stezhka/bin/stezhka serve          # http://127.0.0.1:8470
```

## Recording

Use [OpenTracks](https://opentracksapp.com/) (F-Droid). In its settings enable automatic GPX
export to a folder, and sync that folder to the server with Syncthing (server side: a
*Receive Only* folder). Point `STEZHKA_INBOX` at it; new rides are imported within a minute.
Stezhka never moves or deletes files in the inbox.

On Xiaomi/Poco (HyperOS/MIUI): Battery → *No restrictions* for OpenTracks, enable Autostart, and
lock it in Recents, or the system will kill recording with the screen off.

## Deployment

See [deploy/stezhka.service](deploy/stezhka.service). Stezhka binds to `127.0.0.1:8470` and is
meant to sit behind your HTTPS reverse proxy (subdomain or path prefix both work; the UI uses
relative URLs). Back up with `stezhka backup <file>` plus a copy of `$STEZHKA_DATA_DIR/raw/`.
