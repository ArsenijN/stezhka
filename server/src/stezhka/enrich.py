"""Enrichment of stored rides: DEM elevation and weather.

Separate from import on purpose: network trouble must never fail or delay an import. The server
runs an EnrichWorker thread that picks up pending rides; the CLI enriches inline after `import`.
Failed rides are retried after RETRY_S. Weather fetched from the forecast API is replaced by the
archive (reanalysis) once that is available.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
import urllib.error
from collections.abc import Callable
from pathlib import Path

from . import weather
from .db import tx
from .dem import DEM_VERSION, Dem, smooth_by_distance
from .process import climb

log = logging.getLogger(__name__)

RETRY_S = 3600
ELE_HYSTERESIS_M = 2.0  # DEM is smooth; 2 m keeps real rollers, drops grid noise
_ERRORS = (OSError, ValueError, urllib.error.URLError, sqlite3.OperationalError)


def enrich_elevation(conn: sqlite3.Connection, dem: Dem, ride_id: int) -> bool:
    pts = conn.execute("SELECT idx, lat, lon, dist FROM points WHERE ride_id = ? ORDER BY idx", (ride_id,)).fetchall()
    raw = dem.profile([p["lat"] for p in pts], [p["lon"] for p in pts])
    covered = any(v is not None for v in raw)
    smooth = smooth_by_distance(raw, [p["dist"] for p in pts]) if covered else raw
    up, down = climb(smooth, ELE_HYSTERESIS_M) if covered else (None, None)
    with tx(conn):
        conn.executemany(
            "UPDATE points SET ele_dem = ? WHERE ride_id = ? AND idx = ?",
            ((None if v is None else round(v, 2), ride_id, p["idx"]) for v, p in zip(smooth, pts)),
        )
        if covered:
            conn.execute(
                "UPDATE rides SET ascent_m = ?, descent_m = ?, ele_source = 'dem', dem_version = ? WHERE id = ?",
                (up, down, DEM_VERSION, ride_id),
            )
        else:  # outside DEM coverage: keep GPS elevation, don't retry
            conn.execute("UPDATE rides SET dem_version = ? WHERE id = ?", (DEM_VERSION, ride_id))
    return covered


def enrich_weather(conn: sqlite3.Connection, ride_id: int, *, refresh: bool = False) -> str:
    pts = [
        dict(r)
        for r in conn.execute(
            "SELECT t, lat, lon, segment, moving FROM points WHERE ride_id = ? ORDER BY idx", (ride_id,)
        ).fetchall()
    ]
    lat = sum(p["lat"] for p in pts) / len(pts)
    lon = sum(p["lon"] for p in pts) / len(pts)
    if refresh:  # drop provisional forecast hours so they are re-fetched from the archive
        lo, hi = weather.ride_window(pts[0]["t"], pts[-1]["t"])
        clat, clon = weather.cell(lat, lon)
        conn.execute(
            "DELETE FROM weather_hourly WHERE cell_lat = ? AND cell_lon = ? AND t BETWEEN ? AND ? AND source = 'forecast'",
            (clat, clon, lo - 3600, hi),
        )
    w = weather.ride_weather(conn, lat, lon, pts)
    cols = [
        "temperature_c", "humidity_pct", "wind_speed_mps", "wind_dir_deg", "wind_gust_mps", "headwind_mps",
        "precip_ride_mm", "precip_before_mm", "wetness_mm", "wetness", "weather_code", "source",
    ]
    with tx(conn):
        conn.execute(
            f"INSERT INTO ride_weather(ride_id, {', '.join(cols)}) VALUES (?, {', '.join('?' * len(cols))})"
            f" ON CONFLICT(ride_id) DO UPDATE SET {', '.join(f'{c} = excluded.{c}' for c in cols)}",
            (ride_id, *(getattr(w, c) for c in cols)),
        )
        conn.execute("UPDATE rides SET weather_version = ? WHERE id = ?", (weather.WEATHER_VERSION, ride_id))
    return w.source


def enrich_ride(conn: sqlite3.Connection, dem: Dem, ride_id: int) -> list[str]:
    """Run every pending enrichment for one ride. Returns error messages (empty = all good)."""
    row = conn.execute(
        "SELECT r.dem_version, r.weather_version, r.ended_at, w.source AS wsource"
        " FROM rides r LEFT JOIN ride_weather w ON w.ride_id = r.id WHERE r.id = ?",
        (ride_id,),
    ).fetchone()
    errors = []
    if row["dem_version"] < DEM_VERSION:
        try:
            enrich_elevation(conn, dem, ride_id)
        except _ERRORS as e:
            errors.append(f"elevation: {e}")
    stale_forecast = row["wsource"] == "forecast" and row["ended_at"] < weather.recent_cutoff()
    if row["weather_version"] < weather.WEATHER_VERSION or stale_forecast:
        try:
            enrich_weather(conn, ride_id, refresh=stale_forecast)
        except _ERRORS as e:
            errors.append(f"weather: {e}")
    conn.execute(
        "UPDATE rides SET enrich_error = ?, enrich_attempt_at = ? WHERE id = ?",
        ("; ".join(errors) or None, time.time(), ride_id),
    )
    for e in errors:
        log.warning("ride %s enrichment: %s", ride_id, e)
    return errors


def pending_rides(conn: sqlite3.Connection, limit: int = 20) -> list[int]:
    now = time.time()
    rows = conn.execute(
        """
        SELECT r.id FROM rides r LEFT JOIN ride_weather w ON w.ride_id = r.id
        WHERE (r.dem_version < :dem OR r.weather_version < :wx
               OR (w.source = 'forecast' AND r.ended_at < :cutoff))
          AND (r.enrich_error IS NULL OR r.enrich_attempt_at < :retry)
        ORDER BY r.started_at DESC LIMIT :limit
        """,
        {
            "dem": DEM_VERSION, "wx": weather.WEATHER_VERSION, "cutoff": weather.recent_cutoff(now),
            "retry": now - RETRY_S, "limit": limit,
        },
    ).fetchall()
    return [r["id"] for r in rows]


def enrich_pending(conn: sqlite3.Connection, dem: Dem, limit: int = 20) -> int:
    """Enrich up to `limit` pending rides; returns how many were attempted."""
    ids = pending_rides(conn, limit)
    for rid in ids:
        enrich_ride(conn, dem, rid)
    return len(ids)


def enrich_all_pending(conn: sqlite3.Connection, dem: Dem, should_stop: Callable[[], bool] = lambda: False) -> int:
    """Drain the queue. Each ride is attempted at most once per call (no hot loop on bad data)."""
    seen: set[int] = set()
    while not should_stop():
        ids = [i for i in pending_rides(conn, 20) if i not in seen]
        if not ids:
            break
        for rid in ids:
            enrich_ride(conn, dem, rid)
            seen.add(rid)
    return len(seen)


class EnrichWorker:
    """Background thread: enrich pending rides every `poll_s`, or right after wake()."""

    def __init__(self, connect: Callable[[], sqlite3.Connection], dem_dir: Path, poll_s: float = 300.0):
        self._connect = connect
        self._dem = Dem(dem_dir)
        self._poll_s = poll_s
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def wake(self) -> None:
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                conn = self._connect()
                try:
                    enrich_all_pending(conn, self._dem, self._stop.is_set)
                finally:
                    conn.close()
            except Exception:  # keep the worker alive; logged with traceback
                log.exception("enrichment worker failed")
            self._wake.wait(self._poll_s)
            self._wake.clear()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="stezhka-enrich", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=10)
