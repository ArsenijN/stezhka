# Ride Stats — project notes (handoff to Claude Code)

Personal bike ride statistics system: record rides on an Android phone, sync to a home server, view on an interactive web map, learn per-road-segment speeds, predict ETA, and eventually route through unknown roads to grow the map.

## Context
- Owner: Arsenij, Uzhhorod, Ukraine. Rides to/from university (UzhNU) and around town.
- Recording phone: **Poco C65** (budget Android, aggressive battery management, almost certainly no barometer).
- Server: Debian 13 home server (the "cloud at home"), already has a custom Python HTTPS server with SNI routing (`server_https.py`) that can serve the web UI.
- Bikes: several (CUBE MTB, MIFA/BIRIA trekking). Stats must be filterable per bike.
- No fenders yet: wet roads/puddles at speed soak clothes and backpack. Weather is a first-class feature.
- Preference: root-cause fixes and long-term stability over finicky setups. Treat the server side as a production service.

## Goals
1. Record rides (GPS track with timestamps, speed).
2. Interactive map showing the track colored by speed; identify slow sections.
3. Elevation profile (explain uphill-to-uni vs downhill-back).
4. Per-road-segment statistics aggregated across rides.
5. Average speed / ETA for a route from known data and assumptions.
6. Understand *why* slow: crossings, traffic lights, junctions, surface, slope, weather.
7. Routing that sometimes prefers unknown segments so the map grows naturally.

## Existing tools (check before building)
- Recording: OpenTracks (Android, open source, GPX), Strava, Komoot, Ride with GPS.
- Self-hosted viewers: **Endurain**, **FitTrackee**, GPX Studio (single tracks). Verify current features; none are known to give a multi-ride "typical speed per segment" map or ETA prediction.
- Plan: use OpenTracks + Endurain/FitTrackee for 1-2 weeks to find real gaps, then build only the missing layers.

## Gaps worth building
- Speed-colored map aggregated over many rides (typical speed per road segment).
- ETA prediction for a new route from past segment data.
- Weather/wetness-aware stats.
- Exploration-aware routing.

## Architecture (proposed)
**Android app** (native Kotlin, or PWA first)
- Foreground service recording GPS; local storage (SQLite/files); sync to server over HTTPS when on Wi-Fi (or any connection, configurable).
- One-tap buttons: "puddle / bad spot", "on sidewalk" correction, pause.
- Biggest risk: Android battery optimization killing the service on the Poco C65. Prototype and test this first (long ride, screen off).

**Server** (Python, on the Debian box)
- Auth-protected API: upload rides, list rides, fetch stats.
- Storage: SQLite or PostgreSQL (+ PostGIS if segment queries get heavy). Keep raw GPX/points untouched; derived data is recomputable.
- Enrichment jobs: weather, elevation, map matching, aggregation.
- Valhalla (Meili map matching + routing) with an OSM extract for the region.

**Web UI**
- Leaflet or MapLibre, login required. Ride list, map with speed-colored track, elevation graph linked to the map, slow-section list, filters (bike, season, time of day, weather).

## Data pipeline
1. **Ingest**: raw points (lat, lon, time, speed, accuracy). Clean: drop low-accuracy points, smooth jitter, detect stops/pauses (speed ~0 for N seconds).
2. **Weather**: Open-Meteo historical API per ride. Store temperature, wind, and precipitation for the previous ~6 h. Compute a **road wetness score** with decay (rain in last 3 h full weight, 3-12 h partial weight). Store per ride.
3. **Elevation**: look up each point in a DEM (SRTM or Copernicus DEM); don't trust phone GPS altitude. Smooth, then derive per-segment slope.
4. **Map matching**: Valhalla Meili (probabilistic, whole-track) to OSM ways/edges. Sidewalk vs road is ambiguous when parallel; use speed consistency plus the manual "on sidewalk" correction.
5. **Stop explanation**: for each stop/slowdown, look for OSM nodes within ~20 m (crossings, traffic signals, junctions, stop signs). If none, label "unknown", never guess.
6. **Aggregation**: per segment: median speed, spread, sample count, stop frequency, slope; split by weather/wetness, time of day, bike.
7. **Puddle layer**: user-reported bad spots accumulate into a map layer; later usable as a routing penalty.

## ETA and routing
- Route = sequence of OSM segments; ETA = sum of segment times.
- Known segment: use own median, adjusted for weather/time of day, with confidence from sample count.
- Unknown segment: model from OSM tags (highway type, surface, slope, crossings/signals nearby), calibrated on known segments.
- Exploration: small bonus in routing cost for unknown segments, user setting "explore level 0-3" so it doesn't send him somewhere awful when late.
- Navigation v1: server computes route with Valhalla and exports it (GPX) to an existing nav app. Turn-by-turn in own app only later if still wanted.

## Build order
1. Recording app + sync to server (and test battery survival).
2. Web map: speed-colored track + elevation graph.
3. Weather + DEM enrichment.
4. Map matching + per-segment aggregation.
5. ETA prediction.
6. Routing with exploration.

## Open questions / decisions for Code
- Native Kotlin app vs PWA vs using OpenTracks for recording initially (recommended: OpenTracks first, own app after the pipeline works).
- Database choice (SQLite vs PostgreSQL/PostGIS).
- How to run Valhalla on the server (resources: i3 370M is old; check RAM and build time for a Ukraine/region extract).
- Auth method for app and web (token per device vs login session).
- Privacy: tracks reveal home/uni locations; keep everything on the home server, no third-party sync.

## First tasks suggested for Claude Code
- Scaffold repo: `server/` (API + jobs), `web/`, `docs/`, `android/` (later).
- Write a GPX importer + cleaning/stop-detection module with tests on a real OpenTracks export.
- Minimal web page: list rides, show one track colored by speed with an elevation chart.

---

## Addendum (2026-11-17): interim recording and Health Connect

Append to the existing notes. Does not replace anything above.

### Interim recording setup (before the custom app exists)
- **Poco C65**: OpenTracks, exporting GPX. Activity type: cycling.
- **iPhone**: Open GPX Tracker (better GPS, has a barometer). Settings: location "Always" + precise, highest accuracy, fitness activity type, distance filter 5-10 m, check that auto-pause doesn't create gaps at stops.
- Record some rides with **both phones at once** to measure how much the Poco's data differs and how much cleaning the pipeline needs. Name each ride with bike + weather until the app logs those itself.
- Mount the phone with a clear sky view (handlebar or outer pocket, not the backpack). Wait ~30 s for a GPS lock before riding off.

### Confirmed facts about the Poco C65
- OpenTracks sensor screen shows **no barometer, no thermometer, no heart rate sensor**. Elevation must come from a DEM lookup (SRTM/Copernicus), as planned.
- Battery: set OpenTracks to "No restrictions", allow location "all the time", enable autostart / lock in recents. Still the main reliability risk for the future app.

### OpenTracks settings in use
- Min distance between location records: **10 m** (5 m is an option; 1 m is not useful: GPS gives ~1 fix/s, and stops would produce jitter clusters that inflate distance and confuse stop detection).
- Max distance / new segment every **200 m** (only splits on GPS dropouts, fine).
- Idle threshold: **10 s**.
- Sampling interval: 0 s (as fast as GPS provides).
- GPS accuracy: currently ignores fixes worse than 50 m. **Plan: lower to ~20-25 m**; raise again if gaps appear under trees/between buildings.
- Export: **GPX** as the primary format. KMZ only if photos attached to the track are wanted (e.g. photos of puddles/bad spots). The pipeline should consume GPX.

### Heart rate and steps via Health Connect
- Galaxy Fit3 data (steps, heart rate) syncs through Samsung Health into **Health Connect** on the Poco, and was confirmed readable there. OpenTracks itself cannot read the Fit3 (it only reads standard Bluetooth sensors).
- Plan: optional enrichment step in the Android app. Request Health Connect permission to read heart rate (and steps), read records for the ride's start-end window after the ride, and merge into track points/segments by timestamp.
- Heart rate use: show effort on the map/elevation graph (e.g. the climb to uni). Steps: help separate riding from walking (pushing the bike, sidewalks).
- Limits:
  - Fit3 has **no GPS**; the phone still records the route.
  - Density is unknown. All-day tracking is probably sparse (hourly min/max visible in the UI; underlying samples unverified). For dense heart rate, start a **cycling workout on the Fit3** before the ride, then check what arrives in Health Connect.
  - Data arrives when the watch syncs, so it may lag a few minutes after the ride.
- First test (no code needed): one ride with the Fit3 cycling workout + OpenTracks running, then compare timestamps.

### Additions to the data model / pipeline
- Store the **source device** per ride (Poco/OpenTracks, iPhone/Open GPX Tracker) and per-ride metadata: bike, notes.
- Importer must handle GPX from both apps. iPhone elevation may come from the barometer; still prefer DEM-derived elevation for consistency, but keep the raw value.
- New enrichment stage: Health Connect heart rate/steps (optional, skipped if no data).

### Added tasks for Claude Code
- Importer test fixtures: one OpenTracks GPX and one Open GPX Tracker GPX from the same ride.
- Compare the two tracks (point density, jitter, gaps, speed values) to tune cleaning thresholds.
- Prototype Health Connect reader (Kotlin) that dumps heart rate samples for a time window to JSON; check sample density with and without a Fit3 workout.
