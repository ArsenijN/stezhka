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

## D6. Elevation: Copernicus GLO-30 DEM (phone GPS only as fallback)
GLO-30 is cleaner than SRTM around valleys and towns. Tiles (1°×1°, ~45 MB, ~6 for Zakarpattia) are
downloaded on first use from AWS Open Data; only the 1024² blocks a ride touches are decoded, in pure
numpy (the TIFF floating-point predictor is 5 lines), so no GDAL or imagecodecs. Bilinear sampling,
then a 30 m along-track moving average, then ascent with 2 m hysteresis. Verified: Runa summit
reads 1477 m (official 1479 m).
Known limits, both from GLO-30 being a *surface* model:
* forests and built-up areas read canopy/roof height (+10..25 m). Matters for forest paths.
  Candidate fix: FABDEM (bare-earth Copernicus; CC BY-NC-SA, fine for personal use).
* water is flattened, so a bridge shows a dip to river level. Fix after map matching: interpolate
  elevation linearly across OSM `bridge=*` / `tunnel=*` ways.

## D7. Web: FastAPI + plain ES modules + vendored MapLibre, no build step
Nothing to rebuild when Node tooling changes. MapLibre and pmtiles.js are vendored
(`web/vendor/*`, see each VERSION) so the UI loads no third-party scripts.
Strict CSP, HttpOnly SameSite=Strict session cookie, scrypt password, server-side sessions.

## D7a. Basemap: regional extract of the OpenFreeMap planet, served by Stezhka
OFM publishes its planet as a single PMTiles file in immutable dated folders. `stezhka basemap`
reads only the directories and tiles inside a bbox via merged HTTP range requests (Python, using
the `pmtiles` library's format code; no extra binary on the server) and writes a clustered regional
PMTiles file. Identical tiles are deduplicated by exact source offset, not by hash. Styles are OFM's
own (positron for light, dark for dark mode), rewritten to point at local tiles, sprites, and fonts;
the low-zoom raster relief layer is dropped (327 MB for zooms 0-6 is not worth it).
Considered and rejected: Planetiler/tilemaker builds (need hundreds of MB of ocean/Natural Earth
inputs and diverge from OFM styles), Protomaps basemap (different schema, so not OFM styles).
Remote OFM is an opt-in fallback (`STEZHKA_BASEMAP_REMOTE=1`); when it is off, the CSP blocks it.
`/basemap/` requires login (a 60 s session cache avoids a DB hit per range request) and is never
gzip-encoded, because compressing 206 responses breaks range reads.

## D8. i18n: keys, never English-as-key
`t('stats.distance')` with i18next JSON v4 locale files (`web/locales/*.json`): nested keys,
CLDR plurals (`_one/_few/_many/_other`; Ukrainian needs all of them), `{{var}}` interpolation.
The format is supported natively by Weblate (Hosted Weblate is free for libre projects, the usual
route for F-Droid apps). The server never sends UI text, only error codes (`errors.<code>`).
`server/tests/test_i18n.py` fails on non-literal keys, missing/unused keys, undeclared dynamic
prefixes, and translation variables that English does not have.

## D9. Weather: Open-Meteo, coarse cell, cached
Requests carry only a ~10 km cell (lat/lon rounded to 0.1°) and whole UTC days, never the track;
hourly data is cached per cell (`weather_hourly`), so a day of commutes costs one request. Rides
that ended more than 5 days ago use the archive (reanalysis); newer ones use the forecast API's recent
past and are refreshed from the archive later. Responses with gaps are rejected and retried.
Per ride (`ride_weather`): mean temperature/humidity, vector-mean wind, gusts, **headwind component
along the track** (moving time-weighted; + = against the rider; note 10 m wind is stronger than at
rider height), rain during and 12 h before, and a wetness heuristic
(Σ precipitation × weight, weight 1 up to 3 h before start, falling linearly to 0 at 12 h;
dry < 0.2 ≤ damp < 1 ≤ wet < 4 ≤ soaked mm). Wetness ignores temperature/sun drying for now.

## D10. Enrichment runs after import, never inside it
Import must work offline and fast. A background worker in the server (woken by uploads and the
inbox) enriches pending rides; the CLI does it inline after `import`. Each enrichment has a version
(`dem_version`, `weather_version`) so algorithm changes re-run it. Failures are stored in
`enrich_error` and retried after an hour; reprocessing a ride resets its enrichment.
`STEZHKA_ENRICH=0` turns all of this (and every outbound request it makes) off.

## Roadmap
1. ~~GPX import, cleaning, stops, web map + profile~~ (done)
2. ~~Self-hosted basemap~~, ~~DEM elevation + weather enrichment~~ (done)
3. Real-ride tuning: Poco C65 vs iPhone recordings side by side (accuracy, stop detection, speed)
4. Bikes UI, ride editing, deleting with tombstones (so the inbox doesn't re-import deleted rides)
5. OSM graph + map matching + per-segment aggregation ("typical speed" map layer, explored-roads
   layer, bridge/tunnel elevation fix)
6. ETA for a drawn/selected route
7. Routing with explore level 0–3, discovery loops (orienteering heuristic), GPX export for navigation
8. Licence choice + F-Droid packaging, if it goes public
