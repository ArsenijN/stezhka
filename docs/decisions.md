# Design decisions

Short records of what was decided and why. Original brainstorm: [notes.md](notes.md).

## D1. Record with OpenTracks + Syncthing first; own Android app later, maybe never
OpenTracks is open source, records GPX with Doppler speed and accuracy, and auto-exports finished
tracks to a folder. Syncthing moves that folder to the server without any cloud. That means no
app code, no upload auth, and no sync code in phase 1. Our own app becomes worth it only for
one-tap annotations (puddle, sidewalk) and on-device ETA. The HTTP upload endpoint already exists
for that day.

## D2. One road graph of our own instead of Valhalla
The core feature is a **custom cost per road segment** (own median speed, weather adjustment,
exploration bonus for unridden segments). Valhalla/GraphHopper are built around their own costing
models, and feeding them per-edge user weights is awkward. Plan:

* cut an OSM extract (Geofabrik Ukraine → `osmium extract` by bbox, starting with Zakarpattia),
* build our own graph with stable segment ids, **including** `highway=path|track|footway|bridleway`
  unless `bicycle=no` (forest and park dirt paths are part of the point of this project),
  keeping `surface`, `tracktype`, `smoothness`, `mtb:scale`, `incline` as features,
* match tracks to it with an HMM map matcher (Newson & Krumm 2009; `leuvenmapmatching`),
* route with Dijkstra/A* where the cost is predicted time × exploration factor.

Matching, stats and routing then share segment ids. An oblast-sized graph fits easily in RAM on
the 8 GB server. Regions are added as rides go further (Mukachevo, Antalovetska Polyana...).
Fallback if our own matcher is poor: Valhalla Meili for matching only.

## D3. SQLite, not PostgreSQL/PostGIS
One user, hundreds of rides, ~1M points: SQLite handles that trivially. It's a single file, there
are no upgrades to babysit, and online backup is built in. Spatial work happens in Python (shapely
+ STRtree) on the graph. Revisit only if a real query becomes slow.
**Never put the live DB on SMB.** SQLite locking over network shares corrupts data. The SMB
"cold storage" is for backups: nightly `stezhka backup` snapshot + `rsync` of `raw/`.

## D4. Raw is truth, everything else is derived
Original GPX bytes are stored once by SHA-256 (`data/raw/ab/abcd….gpx`); re-imports are no-ops.
Points, stops and summaries carry `processing_version`; `stezhka reprocess` rebuilds them after
algorithm changes without losing user edits (bike, notes).

## D5. Stop detection works on intervals, not points
OpenTracks may log nothing while stationary, so a red light can be one 40 s gap between two fixes.
A gap counts as stationary when displacement/time < 1 m/s. Stops shorter than 5 s or drifting more
than 20 m (pushing the bike up a path) are moving time. Over 3 min = *break*; recorder segment
boundary = *pause*. Why a stop happened (signal, crossing, junction) comes later from nearby OSM
nodes; if nothing is there, the reason stays unknown.

## D6. Elevation: phone GPS now, Copernicus GLO-30 DEM next
GPS altitude is shown with a visible "rough" note. GLO-30 is cleaner than SRTM around valleys and
towns; a few 1°×1° tiles cover Zakarpattia (~tens of MB).

## D7. Web: FastAPI + plain ES modules + vendored MapLibre, no build step
Nothing to rebuild when Node tooling changes. MapLibre is vendored (`web/vendor/maplibre-gl`, see
VERSION) so the UI loads no third-party scripts. The basemap defaults to OpenFreeMap (free, no key);
self-hosting a Protomaps `.pmtiles` extract of the region later removes that last external request.
Strict CSP, HttpOnly SameSite=Strict session cookie, scrypt password, server-side sessions.

## D8. i18n: keys, never English-as-key
`t('stats.distance')` with i18next JSON v4 locale files (`web/locales/*.json`): nested keys,
CLDR plurals (`_one/_few/_many/_other`; Ukrainian needs all of them), `{{var}}` interpolation.
The format is supported natively by Weblate (Hosted Weblate is free for libre projects, the usual
route for F-Droid apps). The server never sends UI text, only error codes (`errors.<code>`).
`server/tests/test_i18n.py` fails on non-literal keys, missing/unused keys, undeclared dynamic
prefixes, and translation variables that English does not have.

## D9. Weather (planned)
Open-Meteo: the archive API lags a few days, so recent rides use the forecast API with `past_days`
and are refilled from the archive later. Store per ride: temperature, wind, precipitation for the
previous 12 h, and a wetness score (rain in last 3 h full weight, 3–12 h decaying).

## Roadmap
1. ~~GPX import, cleaning, stops, web map + profile~~ (done)
2. Bikes UI, ride editing, deleting with tombstones (so the inbox doesn't re-import deleted rides)
3. DEM elevation + weather enrichment
4. OSM graph + map matching + per-segment aggregation ("typical speed" map layer, explored-roads layer)
5. ETA for a drawn/selected route
6. Routing with explore level 0–3, discovery loops (orienteering heuristic), GPX export for navigation
7. Licence choice + F-Droid packaging, if it goes public
