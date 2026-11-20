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
hourly data is cached per cell (`weather_hourly`), so a day of commutes costs one request.
Source: Open-Meteo's **Historical Forecast API** (high-resolution models, available immediately,
kept from 2022 on); ERA5 reanalysis only for older dates. Evaluated against METAR rain reports at
Nyiregyhaza (LHNY, ~80 km away, Oct 1 - Nov 13 2026, 87 rain hours): rain hours detected 74.7 % vs
75.9 %, false alarms 32.3 % vs 35.9 %, CSI 0.55 vs 0.53 - equivalent, so the earlier plan to replace
recent data with the archive after 5 days was dropped (it bought nothing). Uzhhorod's own airport
station (UKLU) publishes no reports while the airspace is closed.
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

## D11. Road network from the basemap tiles, built on demand
The planned Geofabrik/osmium extract was replaced by the self-hosted basemap's z14 vector tiles
(OpenMapTiles `transportation` layer): no second download (Ukraine's extract is > 1 GB), no new
external service, offline, refreshed with the map. The tiles keep every road and path at full
detail with class, subclass (footway, cycleway…), paved/unpaved, bridge/tunnel and names
(`transportation_name`). Missing: OSM ids and detailed tags (`smoothness`, `tracktype`,
`mtb:scale`, `incline`, `bicycle=*`); if routing needs them, a per-area Overpass fetch can add them
to the stored segments later.
Graph: lines clipped to each tile's own square (neighbours meet on the border, where nodes are
merged), split into segments at shared vertices and line ends. Measured: 27 % of street ends lie on
the other street's line without a shared vertex (tile simplification), so ends are snapped onto
lines within ~1.5 m. Result around Uzhhorod: 98 % of nodes in one connected network. Segment id =
hash of class + geometry: stable while the map does not change there; a basemap refresh re-matches
every ride.
Matching: HMM after Newson & Krumm (σ 10 m, β 10 m, candidates within 35 m, a fix every ~8 m),
routes between candidates by bounded Dijkstra; the full route is reconstructed, so short segments
between fixes count too.
v0.5.1 fixes from real rides: (1) a **sideways switch** of up to 25 m between ways the map does not
link (road <-> its sidewalk, linked only at a few crossings) - without it a ride that began on the
road stayed there for a whole street although every fix was closer to the sidewalk (prospekt
Svobody: fixes 4 m from the footway, 8 m from the road; the user rides the sidewalk there);
(2) routes that would need > 16 m/s between two fixes are rejected (a detour through the network
had produced "133 km/h" on a 36 m yard lane); (3) fixes while standing are not matched (tangles
near home). Sidewalks stay separate segments in the statistics: the user's sidewalk riding has more
slowdowns (pedestrians, puddles) than the road, so merging them would blur both.
Two-phone agreement on identical segments: 90 % and 78 %; 52 % on the pair with the chest-pocket
phone that lags ~14 m (it sits between road and sidewalk; it is not the main recording).
Marks on roads (v0.5.6) are placed when *My roads* is requested, not stored per segment: a mark
keeps its time range, and the ride's passes say which segments and offsets that time covered. So
marks follow every re-match and basemap refresh with no migration, and a mark made before its ride
was matched simply lands later. Placed on the road line, not the raw track: marks from several
rides on one street overlap instead of fanning out with GPS noise.

## Roadmap
Moved to [ROADMAP.md](../ROADMAP.md).
