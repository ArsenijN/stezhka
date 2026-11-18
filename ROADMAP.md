# Stezhka roadmap

What is done, what comes next, and what is known to be wrong. The *why* behind each choice lives in
[docs/decisions.md](docs/decisions.md); the original brainstorm in [docs/notes.md](docs/notes.md).
Versions follow SemVer `0.y.z`: each milestone below bumps `y`.

## Done

- **v0.1** GPX import (OpenTracks, Open GPX Tracker, any GPX 1.0/1.1), cleaning, stop/break/pause
  detection, web UI: ride list, speed-coloured map, elevation + speed profile, key-based i18n (en, uk).
- **v0.2** Self-hosted basemap: Zakarpattia extract of the OpenFreeMap planet, light + dark styles.
- **v0.3** DEM elevation (Copernicus GLO-30), weather per ride (Open-Meteo Historical Forecast,
  validated against METAR rain reports), background enrichment, AGPL-3.0.
- Deployment: `deploy/deploy.sh` → idempotent `install.sh`, `stezhka-admin`, login brute-force
  protection, live at `stezhka.arseniusgen.dev` behind `server_https.py`.
- **v0.4** Managing rides: bikes (settings dialog, per-ride bike, list filter), edit name/notes,
  delete with tombstone (the inbox never re-imports a deleted ride; an explicit upload restores it),
  change password in the UI, compare overlay of a second ride (nearest in time listed first),
  source-code link for every user (AGPL §13, `STEZHKA_SOURCE_URL`), CI running the tests on GitHub.

## Next: v0.5 - roads and segments

- OSM extract for the region (Geofabrik Ukraine → `osmium extract` by bbox) → own road graph with
  stable segment ids, **including** paths/tracks/footways unless `bicycle=no`; keep `surface`,
  `tracktype`, `smoothness`, `mtb:scale`, `incline`.
- HMM map matching of every ride to the graph (sidewalk vs road ambiguity: speed consistency +
  manual correction later).
- Per-segment statistics: median speed, spread, sample count, stop frequency; split by weather
  wetness, time of day, bike.
- Map layers: *typical speed* per segment, *explored* (ridden) vs *unexplored* roads.
- Stop reasons from OSM within ~20 m: traffic signals, crossings, junctions; otherwise "unknown".
- Elevation across `bridge=*` / `tunnel=*` ways interpolated instead of the DEM's river dip.

## v0.6 - ETA

- Pick or draw a route; ETA = Σ segment times from own medians (adjusted for wetness, time of day,
  bike), with a confidence band from sample counts.
- Unknown segments: model from OSM tags + slope, calibrated on known segments.

## v0.7 - routing and exploration

- Routing on the own graph; cost = predicted time × exploration factor; "explore level" 0-3.
- Discovery loops: "about N minutes, as many new streets as possible" (orienteering heuristic).
- Export route as GPX for navigation apps.
- Puddle / bad-spot layer as a routing penalty.

## Recording

- Recommended now: iPhone + Open GPX Tracker (1 s fixes, low noise). Poco + OpenTracks works on
  the bar holder; set its recording distance interval to the minimum; avoid pockets.
- Own Android app only for what recorders cannot do: one-tap annotations (puddle, bad surface,
  "on sidewalk"), live ETA. Prototype battery survival on the Poco C65 first.

## Operations

- Syncthing receive-only inbox on the server → `STEZHKA_INBOX` (then rides arrive automatically).
- Nightly backup to the SMB cold storage: `stezhka-admin backup` snapshot + `rsync` of `raw/`
  (systemd timer).
- Monthly basemap refresh (`stezhka-admin basemap`), systemd timer.
- Translations through Weblate once the repository is public.
- F-Droid packaging once there is an Android app.

## Known issues

- GLO-30 is a surface model: forests read treetop height (+10..25 m). Candidate: FABDEM.
- Bridges dip to river level until v0.5 (see above).
- Phone in a jacket pocket: fixes ~4.5 s late and ~14 m off (measured 2026-11-18); stops can land
  ~20 m from where they happened. Map matching will absorb most of it.
- Wetness ignores drying by sun/wind/temperature.
- Basemap line widths are in screen pixels (not metres), so roads are not drawn at their real
  width; zoom is capped at 19. Sidewalks/paths are drawn as dashed lines (OFM styles hide them).
- Rides imported before v0.3.x on the server show weather source `forecast`; same data as
  `historical_forecast`, label only.
