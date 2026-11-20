"""OSM nodes that explain stops: traffic signals, crossings, stop / give-way signs, level crossings.

The basemap tiles carry roads, barriers and bus stops but none of these, so they come from the
Overpass API, one 0.1 degree cell (~11 x 7 km) at a time: the request names the cell, never a
track. Cells are refetched after REFRESH_S (OSM changes slowly). Only with STEZHKA_ENRICH on.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import time
import urllib.parse
import urllib.request

from .db import tx

log = logging.getLogger(__name__)

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
USER_AGENT = "stezhka-osm/1"
CELL_DEG = 0.1
REFRESH_S = 30 * 86400
RETRY_S = 6 * 3600  # after a failed fetch
KINDS = ("traffic_signals", "crossing", "stop_sign", "level_crossing")


def cell_key(lat: float, lon: float) -> str:
    return f"{math.floor(lat / CELL_DEG)}:{math.floor(lon / CELL_DEG)}"


def cell_bbox(key: str) -> tuple[float, float, float, float]:
    """(south, west, north, east) of a cell."""
    a, b = (int(v) for v in key.split(":"))
    return (round(a * CELL_DEG, 6), round(b * CELL_DEG, 6), round((a + 1) * CELL_DEG, 6), round((b + 1) * CELL_DEG, 6))


def kind_of(tags: dict) -> str | None:
    hw, rw = tags.get("highway"), tags.get("railway")
    if hw == "traffic_signals" or tags.get("crossing") == "traffic_signals":
        return "traffic_signals"
    if rw in ("level_crossing", "crossing"):
        return "level_crossing"
    if hw in ("stop", "give_way"):
        return "stop_sign"
    if hw == "crossing" or tags.get("crossing"):
        return "crossing"
    return None


def query(bbox: tuple[float, float, float, float]) -> str:
    s, w, n, e = bbox
    b = f"({s},{w},{n},{e})"
    return (
        "[out:json][timeout:90];("
        f'node["highway"~"^(traffic_signals|crossing|stop|give_way)$"]{b};'
        f'node["railway"~"^(level_crossing|crossing)$"]{b};'
        ");out qt;"
    )


def fetch_cell(key: str, url: str = OVERPASS_URL) -> list[tuple[int, str, float, float]]:
    """Nodes of one cell as (id, kind, lat, lon)."""
    data = urllib.parse.urlencode({"data": query(cell_bbox(key))}).encode()
    req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=120) as r:
        body = json.load(r)
    out = []
    for el in body.get("elements", []):
        k = kind_of(el.get("tags") or {})
        if k and el.get("type") == "node":
            out.append((el["id"], k, el["lat"], el["lon"]))
    return out


def needed_cells(conn: sqlite3.Connection) -> set[str]:
    """Cells touched by any ride's bounding box (rides are short; a box is a few cells at most)."""
    cells: set[str] = set()
    for r in conn.execute(
        "SELECT MIN(p.lat), MIN(p.lon), MAX(p.lat), MAX(p.lon) FROM points p"
        " JOIN rides r ON r.id = p.ride_id WHERE r.trashed_at IS NULL GROUP BY p.ride_id"
    ):
        s, w, n, e = r
        for a in range(math.floor(s / CELL_DEG), math.floor(n / CELL_DEG) + 1):
            for b in range(math.floor(w / CELL_DEG), math.floor(e / CELL_DEG) + 1):
                cells.add(f"{a}:{b}")
    return cells


def refresh(conn: sqlite3.Connection, fetch=fetch_cell, now: float | None = None) -> int:
    """Fetch missing or stale cells. Returns how many cells got new data."""
    now = time.time() if now is None else now
    have = dict(conn.execute("SELECT cell, fetched_at FROM osm_cells").fetchall())
    done = 0
    for key in sorted(needed_cells(conn)):
        if key in have and now - have[key] < REFRESH_S:
            continue
        try:
            nodes = fetch(key)
        except (OSError, ValueError) as e:  # network or a bad answer: try again later, keep old data
            log.warning("OSM nodes for cell %s: %s", key, e)
            with tx(conn):  # back off: the cell counts as stale again in RETRY_S
                conn.execute(
                    "INSERT INTO osm_cells(cell, fetched_at) VALUES (?, ?) ON CONFLICT(cell) DO UPDATE SET fetched_at = excluded.fetched_at",
                    (key, now - REFRESH_S + RETRY_S),
                )
            continue
        with tx(conn):
            conn.execute("DELETE FROM osm_nodes WHERE cell = ?", (key,))
            conn.executemany(
                "INSERT OR REPLACE INTO osm_nodes(id, cell, kind, lat, lon) VALUES (?,?,?,?,?)",
                ((i, key, k, la, lo) for i, k, la, lo in nodes),
            )
            conn.execute(
                "INSERT INTO osm_cells(cell, fetched_at) VALUES (?, ?) ON CONFLICT(cell) DO UPDATE SET fetched_at = excluded.fetched_at",
                (key, now),
            )
        done += 1
        log.info("OSM nodes for cell %s: %d", key, len(nodes))
    return done
