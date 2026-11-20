# Stezhka roadmap

What is done, what comes next, and what is known to be wrong. The *why* behind each choice lives in
[docs/decisions.md](docs/decisions.md); the original brainstorm in [docs/notes.md](docs/notes.md).
Versions follow SemVer `0.y.z`: each milestone below bumps `y`; fixes and smaller additions bump
`z`. Every release is an annotated git tag `vX.Y.Z`.

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
  click the profile to pin a point and centre the map on it,
  source-code link for every user (AGPL §13, `STEZHKA_SOURCE_URL`), CI running the tests on GitHub.

- **v0.4.1** Upload keys + iOS share-sheet Shortcut, Syncthing inbox setup, nightly backup and
  monthly map refresh timers.
- Phone layout: rides drawer, compact stats with an expand toggle, tap the track to pin a point,
  finger scrubbing on the profile.
- Trash: deleting is one tap with **Undo**; *Settings → Recently deleted* restores or erases;
  erased automatically after 30 days.
- **v0.4.3** Two phones on one trip are linked into one ride automatically (≥ 60 % time overlap and
  ≤ 60 m median distance; the denser recording is main), with one-tap compare, *Make main* and
  *Split off*. Totals (week / month / all, last 14 days), counting each trip once.
- **v0.4.4** How a ride felt: 👍 / 😐 / 👎 (tap again = not rated) with reason chips (problems and
  good things), and any number of marked stretches or spots per ride, each with chips and a note,
  drawn on the map and the profile. Stored by time + position so v0.5 can attach them to road
  segments; routing (v0.7) will weigh them, puddles together with the ride's wetness.


## UI ideas (when the need shows up)

- **Quicker comparing**: on phones the compare picker sits in the expanded stats. If comparing
  several rides in a row becomes common, move it to a chip on the map (pick, look, pick the next,
  without opening and closing the stats each time).

## v0.5 - roads and segments

Done in **v0.5.0**: road network from the basemap tiles (D11), HMM map matching of every ride in
the background (`stezhka match` by hand), ridden segments with passes, typical (median) and best
speed; *My roads* map layer (colour = typical speed, width = how often).

Since then:
- v0.5.1: sidewalks reachable without a mapped link, impossible speeds rejected, standing fixes
  ignored, duplicate exports of one recording trashed automatically. Sidewalks stay separate.
- v0.5.2: going into a side path and back out no longer counts as riding it (it doubled the
  distance: "84 m path at 36 km/h"); routes between fixes limited to 1.5x the straight line + 30 m;
  short pieces at junctions get a speed measured over the ride around them.
- v0.5.3: GPS-dropout cleaning (settling after gaps, acceleration gate); original GPX download and
  zip export. v0.5.4: the main recording of a trip is the better-quality one (coverage, GPS jumps);
  the preferred app only breaks ties - iOS throttled Open GPX Tracker on some rides.
- v0.5.5: a phone started mid-ride is linked but never main (quality measured over the whole trip);
  the logo clears the ride and shows only *My roads*.
- v0.5.6: marks of every ride placed on the matched road segments and shown on *My roads* (red =
  problems, green = good things, blue = notes); tap one for the reasons, note and ride; the road
  popup counts the marks on it. Rides not matched yet show the mark on the recorded track.
- v0.5.7: *vs usual* - each matched piece of a ride against the segment's median speed on the
  other rides (needs 2+ passes there): map toggle (orange = slower, blue = faster), tap for both
  speeds and the seconds gained or lost, and the total in the stats ("42 s faster than usual on
  3.1 km"). A red-light wait counts as time lost on that piece: that is where the time went.

Next:
- Statistics split by wetness, time of day, bike; spread and sample count in the popup.
- Junction costs: time lost at each junction / crossing (stops already detected).
- Stop reasons from OSM within ~20 m: traffic signals, crossings, junctions; otherwise "unknown".
- Elevation across `bridge=*` / `tunnel=*` ways interpolated instead of the DEM's river dip.

## v0.6 - ETA

The question is not "which way is shortest" but "which way is fastest *for me, today*". A short
cut through narrow household streets, steep downhills and broken surface can lose to a slightly
longer smooth road that lets you hold speed (the taxi-navigation failure in Uzhhorod: a
generic router picks the short way no local takes). So the time model is built from what you
actually ride, not from road class defaults:

- Segment time = length / *your* sustained speed on it (median of matched passes), plus the time
  you typically lose there to stops and slowdowns (signals, crossings, give-way junctions).
- **Junction and turn costs** learned from the data: time lost entering, crossing or turning at a
  node, so a route with many interruptions is penalised even if every piece is short.
- **Conditions**: speeds split by road wetness (dirt paths and broken asphalt slow down a lot after
  rain, good asphalt barely), by time of day (traffic, pedestrians), and by bike.
- Unknown segments: estimated from OSM tags (surface, smoothness, class, crossings) + DEM slope,
  calibrated on the segments you do know; low confidence until ridden.
- ETA for a picked or drawn route with a confidence band from sample counts.
- **Backtesting**: predict each past ride from the data before it, compare with the real time, and
  report the error. This is how we know the model beats "shortest path".

## v0.7 - routing and exploration

- Routing on the own graph with the v0.6 time model; "explore level" 0-3 adds a bonus for unknown
  segments, so the map grows, but never sends you somewhere awful when level 0 ("I'm late").
- Alternatives: fastest, short, and "the smooth way" side by side with ETAs, so a slightly longer
  but faster road is visible as such.
- Weather-aware: the same trip can route differently after rain (avoid paths that turn to mud).
- Discovery loops: "about N minutes, as many new streets as possible" (orienteering heuristic).
- Export route as GPX for navigation apps.
- Puddle / bad-spot layer as a routing penalty.

## Recording

- Recommended now: iPhone + Open GPX Tracker (1 s fixes, low noise, keeps the small swerves
  around obstacles). Poco + OpenTracks works on the bar holder; avoid pockets (measured: ~4.5 s
  late, ~14 m off).
- OpenTracks minimum distance between records: **5 m** rather than 10 m. Measured on paired rides,
  10 m made Poco tracks 3-7 % shorter (corners cut). The worry about jitter clusters at stops does
  not apply here: Stezhka adds no distance while stationary. Accuracy filter 20-25 m is fine (Poco
  reported 2-3 m typical, 8 m worst on our rides).
- Getting rides onto the server: [docs/syncthing.md](docs/syncthing.md) (Android: Syncthing-Fork;
  iPhone: browser upload or Synctrain/Möbius Sync).
- iPhone in one tap: upload keys (Settings) + share-sheet Shortcut, [docs/ios-shortcut.md](docs/ios-shortcut.md).
- Own Android app only for what recorders cannot do: one-tap annotations (puddle, bad surface,
  "on sidewalk"), live ETA, and **Health Connect** heart rate and steps (Galaxy Fit3 via Samsung
  Health), merged into the ride by timestamp: effort on the profile (e.g. the climb to uni), steps
  to tell riding from pushing the bike. First: a Kotlin prototype that dumps heart-rate samples for a
  time window to JSON, to check density with and without a Fit3 cycling workout. Prototype battery
  survival on the Poco C65 first.

## Operations

- Syncthing inbox: `deploy/setup-inbox.sh` + [docs/syncthing.md](docs/syncthing.md); the watcher
  ignores Syncthing's own `.stfolder` / `.stversions`.
- Nightly backup to cold storage: `stezhka-backup.timer`, see [docs/backup.md](docs/backup.md)
  (needs `STEZHKA_BACKUP_DIR` + the SMB mount).
- Monthly basemap refresh: `stezhka-basemap.timer`.
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
