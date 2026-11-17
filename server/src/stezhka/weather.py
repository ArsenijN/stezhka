"""Weather per ride from Open-Meteo (free, no key; data CC BY 4.0, "Weather data by Open-Meteo.com").

Privacy: requests carry only a coarse cell (lat/lon rounded to 0.1°, ~10 km) and whole UTC days,
never the track. Hourly data is cached per cell in `weather_hourly`, so rides on the same day in
the same town cost one request.

Source choice: the archive API (reanalysis) is complete from ~5 days back; for newer rides the
forecast API serves the recent past from its model analysis. Responses with gaps are rejected.

Road wetness is a heuristic, not a measurement:
  effective_mm = Σ precip_h · w(age_h),  w = 1 for rain during the ride or up to 3 h before it,
                                          linearly 1 → 0 between 3 h and 12 h before it.
Classes: dry < 0.2 mm ≤ damp < 1 ≤ wet < 4 ≤ soaked.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

WEATHER_VERSION = 1
CELL_DEG = 0.1
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_MIN_AGE_S = 5 * 86400
USER_AGENT = "stezhka-weather/1"
ATTRIBUTION = "Weather: Open-Meteo.com (CC BY 4.0)"

HOURLY = {
    # open-meteo variable -> column
    "temperature_2m": "temperature_c",
    "relative_humidity_2m": "humidity_pct",
    "precipitation": "precipitation_mm",
    "rain": "rain_mm",
    "snowfall": "snowfall_cm",
    "weather_code": "weather_code",
    "wind_speed_10m": "wind_speed_mps",
    "wind_direction_10m": "wind_dir_deg",
    "wind_gusts_10m": "wind_gust_mps",
}
LOOKBACK_S = 12 * 3600


def cell(lat: float, lon: float) -> tuple[float, float]:
    return round(round(lat / CELL_DEG) * CELL_DEG, 4), round(round(lon / CELL_DEG) * CELL_DEG, 4)


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def _hour_floor(ts: float) -> int:
    return int(ts // 3600 * 3600)


def fetch_hours(lat: float, lon: float, start_ts: float, end_ts: float, *, now: float | None = None) -> tuple[list[dict], str]:
    """Hourly rows covering [start_ts, end_ts] for one cell. Raises on incomplete data."""
    now = now or time.time()
    use_archive = now - end_ts >= ARCHIVE_MIN_AGE_S
    base, source = (ARCHIVE_URL, "archive") if use_archive else (FORECAST_URL, "forecast")
    params = {
        "latitude": f"{lat:.2f}",
        "longitude": f"{lon:.2f}",
        "start_date": _day(start_ts),
        "end_date": _day(end_ts),
        "hourly": ",".join(HOURLY),
        "wind_speed_unit": "ms",
        "timezone": "GMT",
        "timeformat": "unixtime",
    }
    url = f"{base}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.load(r)
    hourly = data.get("hourly") or {}
    times = hourly.get("time") or []
    rows = []
    for i, t in enumerate(times):
        row = {"t": float(t)}
        for var, col in HOURLY.items():
            row[col] = (hourly.get(var) or [None] * len(times))[i]
        rows.append(row)
    needed = [r for r in rows if _hour_floor(start_ts) <= r["t"] <= end_ts]
    if not needed or any(r["precipitation_mm"] is None or r["temperature_c"] is None for r in needed):
        raise ValueError(f"{source}: incomplete weather data for {params['start_date']}..{params['end_date']}")
    return rows, source


def ensure_hours(conn: sqlite3.Connection, lat: float, lon: float, start_ts: float, end_ts: float) -> str:
    """Make sure weather_hourly has every hour in [start, end] for the cell. Returns the source."""
    clat, clon = cell(lat, lon)
    first, last = _hour_floor(start_ts), _hour_floor(end_ts)
    expected = (last - first) // 3600 + 1
    have = conn.execute(
        # MAX: 'forecast' > 'archive', so any forecast-sourced hour marks the window as provisional
        "SELECT COUNT(*), MAX(source) FROM weather_hourly WHERE cell_lat = ? AND cell_lon = ? AND t BETWEEN ? AND ?",
        (clat, clon, first, last),
    ).fetchone()
    if have[0] >= expected:
        return have[1]
    rows, source = fetch_hours(clat, clon, start_ts, end_ts)
    now = time.time()
    conn.executemany(
        f"INSERT INTO weather_hourly(cell_lat, cell_lon, t, {', '.join(HOURLY.values())}, source, fetched_at)"
        f" VALUES (?, ?, ?, {', '.join('?' * len(HOURLY))}, ?, ?)"
        " ON CONFLICT(cell_lat, cell_lon, t) DO UPDATE SET "
        + ", ".join(f"{c} = excluded.{c}" for c in [*HOURLY.values(), "source", "fetched_at"]),
        [(clat, clon, r["t"], *(r[c] for c in HOURLY.values()), source, now) for r in rows],
    )
    log.info("weather: %d hours for cell %s,%s from %s", len(rows), clat, clon, source)
    return source


@dataclass(frozen=True, slots=True)
class RideWeather:
    temperature_c: float
    humidity_pct: float | None
    wind_speed_mps: float
    wind_dir_deg: float
    wind_gust_mps: float | None
    headwind_mps: float | None  # mean along-track component while moving; + = against you
    precip_ride_mm: float
    precip_before_mm: float  # 12 h before start
    wetness_mm: float
    wetness: str
    weather_code: int | None
    source: str


def wetness_weight(age_s: float) -> float:
    """age_s = seconds between the end of a precipitation hour and the ride start (≤0 = during)."""
    h = age_s / 3600
    if h <= 3:
        return 1.0
    if h >= 12:
        return 0.0
    return (12 - h) / 9


def wetness_class(mm: float) -> str:
    return "dry" if mm < 0.2 else "damp" if mm < 1 else "wet" if mm < 4 else "soaked"


def _bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return math.degrees(math.atan2(y, x)) % 360


def summarize(hours: list[sqlite3.Row | dict], points: list[sqlite3.Row | dict], source: str) -> RideWeather:
    """hours: weather_hourly rows covering start-12h .. end (open-meteo time = start of hour, values
    are for the preceding hour for precipitation). points: t, lat, lon, segment, moving."""
    start, end = points[0]["t"], points[-1]["t"]
    by_t = {int(h["t"]): h for h in hours}
    during = [h for h in hours if _hour_floor(start) <= h["t"] <= end + 3600]

    def mean(col):
        vals = [h[col] for h in during if h[col] is not None]
        return sum(vals) / len(vals) if vals else None

    # Precipitation at hour t is the sum over the hour ending at t.
    wet = 0.0
    before = 0.0
    during_mm = 0.0
    for h in hours:
        p = h["precipitation_mm"] or 0.0
        hour_end = h["t"]
        if hour_end <= start:
            age = start - hour_end
            if age <= LOOKBACK_S:
                before += p
            wet += p * wetness_weight(age)
        elif hour_end - 3600 < end:
            during_mm += p
            wet += p

    # Wind vector mean (directions can't be averaged as numbers), and headwind along the track.
    u = v = 0.0
    for h in during:
        ws, wd = h["wind_speed_mps"] or 0.0, math.radians(h["wind_dir_deg"] or 0.0)
        u += ws * math.sin(wd)
        v += ws * math.cos(wd)
    n = max(len(during), 1)
    wind_dir = math.degrees(math.atan2(u / n, v / n)) % 360

    def wind_at(t):
        h = by_t.get(_hour_floor(t)) or by_t.get(_hour_floor(t) + 3600)
        return (h["wind_speed_mps"], h["wind_dir_deg"]) if h and h["wind_speed_mps"] is not None else None

    head_sum = dt_sum = 0.0
    for a, b in zip(points, points[1:]):
        dt = b["t"] - a["t"]
        if a["segment"] != b["segment"] or not (a["moving"] and b["moving"]) or dt <= 0 or dt > 60:
            continue
        w = wind_at(a["t"])
        if not w or (a["lat"] == b["lat"] and a["lon"] == b["lon"]):
            continue
        brg = _bearing(a["lat"], a["lon"], b["lat"], b["lon"])
        head_sum += w[0] * math.cos(math.radians(w[1] - brg)) * dt  # wind FROM direction ahead = +
        dt_sum += dt

    codes = [h["weather_code"] for h in during if h["weather_code"] is not None]
    return RideWeather(
        temperature_c=mean("temperature_c"),
        humidity_pct=mean("humidity_pct"),
        wind_speed_mps=mean("wind_speed_mps") or 0.0,
        wind_dir_deg=wind_dir,
        wind_gust_mps=max((h["wind_gust_mps"] for h in during if h["wind_gust_mps"] is not None), default=None),
        headwind_mps=head_sum / dt_sum if dt_sum else None,
        precip_ride_mm=during_mm,
        precip_before_mm=before,
        wetness_mm=wet,
        wetness=wetness_class(wet),
        weather_code=max(codes) if codes else None,  # WMO codes: higher = more significant weather
        source=source,
    )


def ride_window(start: float, end: float) -> tuple[float, float]:
    return start - LOOKBACK_S - 3600, end + 3600


def ride_weather(conn: sqlite3.Connection, lat: float, lon: float, points: list) -> RideWeather:
    lo, hi = ride_window(points[0]["t"], points[-1]["t"])
    source = ensure_hours(conn, lat, lon, lo, hi)
    clat, clon = cell(lat, lon)
    hours = conn.execute(
        "SELECT * FROM weather_hourly WHERE cell_lat = ? AND cell_lon = ? AND t BETWEEN ? AND ? ORDER BY t",
        (clat, clon, _hour_floor(lo), hi),
    ).fetchall()
    return summarize(hours, points, source)


def recent_cutoff(now: float | None = None) -> float:
    """Rides that ended after this were fetched from the forecast API and should be refreshed."""
    return (now or time.time()) - ARCHIVE_MIN_AGE_S - timedelta(days=1).total_seconds()
