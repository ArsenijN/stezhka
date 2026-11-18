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
python3 -m venv ~/.venvs/stezhka            # outside the repo: exFAT/NTFS (fuseblk) drives break venv symlinks
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

## Background map

The map is self-hosted: `stezhka basemap` cuts your region out of the weekly
[OpenFreeMap](https://openfreemap.org) planet build (HTTP range reads, no 90 GB download) and fetches
the OFM styles (light *positron*, *dark*), sprites and fonts into `$STEZHKA_DATA_DIR/basemap/`.
Zakarpattia at zoom 0–14 is about 130 MB of tiles + ~100 MB of fonts and takes ~20 s.

```sh
stezhka basemap --dry-run                       # what would be downloaded
stezhka basemap                                 # default bbox: Zakarpattia + border margin
stezhka basemap --bbox 21.9,47.7,25.5,49.2      # W,S,E,N - grow it when rides go further
```

Re-run it now and then to pick up OSM edits. Without a local extract the map shows a plain
background; set `STEZHKA_BASEMAP_REMOTE=1` to fall back to `tiles.openfreemap.org` instead (off by
default; when off, the Content-Security-Policy does not even allow it).

## Elevation and weather

After import, rides are enriched in the background (CLI: inline after `stezhka import`, or
`stezhka enrich [--all]`):

* **Elevation** from the Copernicus GLO-30 terrain model (AWS Open Data). Tiles of ~45 MB are
  downloaded on first use into `$STEZHKA_DATA_DIR/dem/`. It is a surface model: in forests it reads
  treetop height, and bridges dip to water level until map matching lands.
* **Weather** from [Open-Meteo](https://open-meteo.com): temperature, wind (with the head/tailwind
  component along your track), rain during and before the ride, and a road-wetness estimate.
  Only a ~10 km grid cell and the date leave the server, never the track.

`STEZHKA_ENRICH=0` disables both, and with them every outbound request except `stezhka basemap`.

## Deployment

```sh
cp deploy/deploy.env.sample deploy/deploy.env   # once: SSH host, port, key (gitignored)
./deploy/deploy.sh                              # tests -> rsync -> sudo install.sh on the server
```

`deploy/install.sh` is idempotent: it creates the `stezhka` system user, `/srv/stezhka/{app,venv,data}`,
`/etc/stezhka.env` (once, from `deploy/stezhka.env.example`), the systemd unit and the
`stezhka-admin` wrapper, then restarts the service and checks it answers. Afterwards, on the server:

```sh
sudo stezhka-admin set-password
sudo stezhka-admin basemap        # ~230 MB map download, ~20 s
```

Stezhka binds to `127.0.0.1:8470` behind your HTTPS reverse proxy (subdomain or path prefix both
work; the UI uses relative URLs). Back up with `sudo stezhka-admin backup <file>` plus a copy of
`/srv/stezhka/data/raw/` (the basemap and DEM tiles can always be re-downloaded).

The reverse proxy must:
* forward the `Range` request header and pass `206 Partial Content` responses through unchanged
  (the map reads `/basemap/region.pmtiles` in byte ranges);
* not compress `/basemap/` responses (tiles are already gzipped; compressing a 206 breaks it);
* forward the `Cookie` header (login) and `X-Forwarded-Proto`.

## AI disclosure

Large parts of this project were written with an AI coding assistant (Claude, by Anthropic) under
the maintainer's direction. Design decisions are recorded in [docs/decisions.md](docs/decisions.md);
all code is reviewed, tested, and owned by the maintainer.

## License

Copyright (C) 2026 Arsenii Nochevnyi (ArsenijN)

Stezhka is free software: you can redistribute it and/or modify it under the terms of the
[GNU Affero General Public License](LICENSE) as published by the Free Software Foundation, either
version 3 of the License, or (at your option) any later version. If you run a modified version as a
network service, you must offer its users the corresponding source code.

Bundled third-party code keeps its own license: MapLibre GL JS and PMTiles (BSD-3-Clause, see
`web/vendor/*/LICENSE.txt`). Map data © OpenStreetMap contributors (ODbL), tiles by OpenFreeMap /
OpenMapTiles, elevation © Copernicus DEM, weather by Open-Meteo (CC BY 4.0).
