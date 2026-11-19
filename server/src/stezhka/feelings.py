"""How a ride felt: like / neutral / dislike with reasons, and marks on stretches of the route.

Marks are stored by time (start_t, end_t) and position, not by point index: indices change when a
ride is reprocessed, times and coordinates do not. v0.5 maps them onto road segments, and routing
(v0.7) weighs them, together with the ride's road wetness (a "puddles" mark on a wet day says little
about the same road when it is dry).
"""

from __future__ import annotations

import json
import sqlite3
import time

NEGATIVE_TAGS = ("surface", "puddles", "traffic", "pedestrians", "turns", "steep", "unsafe", "other")
POSITIVE_TAGS = ("smooth", "quiet", "scenic", "green")
TAGS = NEGATIVE_TAGS + POSITIVE_TAGS
FEELINGS = (-1, 0, 1)  # dislike, neutral ("it was OK"), like; NULL = not rated


def clean_tags(tags: list[str] | None) -> list[str]:
    """Known tags only, each once, in canonical order. Raises ValueError on an unknown tag."""
    tags = tags or []
    unknown = set(tags) - set(TAGS)
    if unknown:
        raise ValueError(f"unknown tags: {sorted(unknown)}")
    return [t for t in TAGS if t in tags]


def dump_tags(tags: list[str]) -> str | None:
    return json.dumps(tags) if tags else None


def load_tags(raw: str | None) -> list[str]:
    return json.loads(raw) if raw else []


def _position(conn: sqlite3.Connection, ride_id: int, t: float) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT lat, lon FROM points WHERE ride_id = ? ORDER BY abs(t - ?) LIMIT 1", (ride_id, t)
    ).fetchone()


def add_mark(conn: sqlite3.Connection, ride_id: int, start_t: float, end_t: float, tags: list[str], note: str | None) -> int:
    start_t, end_t = sorted((start_t, end_t))
    a = _position(conn, ride_id, start_t)
    b = _position(conn, ride_id, end_t)
    if a is None or b is None:
        raise ValueError("ride has no points")
    cur = conn.execute(
        "INSERT INTO ride_marks(ride_id, start_t, end_t, start_lat, start_lon, end_lat, end_lon, tags, note, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (ride_id, start_t, end_t, a["lat"], a["lon"], b["lat"], b["lon"], dump_tags(tags), note, time.time()),
    )
    return cur.lastrowid


def marks(conn: sqlite3.Connection, ride_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT id, start_t, end_t, tags, note FROM ride_marks WHERE ride_id = ? ORDER BY start_t", (ride_id,)
    ).fetchall()
    return [{**dict(r), "tags": load_tags(r["tags"])} for r in rows]
