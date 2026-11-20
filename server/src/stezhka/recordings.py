"""Several recordings of one trip (e.g. two phones on the same ride) shown as one ride.

Every recording stays a ride of its own (raw file, points, stats). One of them is the *main* ride;
the others point to it with `rides.primary_id` and are hidden from the list and the totals, but stay
one tap away for comparing. Linking is automatic on import; the user can pick another main recording
or split one off (`rides.unlinked`, never linked automatically again).
"""

from __future__ import annotations

import bisect
import logging
import math
import re
import sqlite3
import time

from .db import get_setting, set_setting, tx

log = logging.getLogger(__name__)

# Measured on real two-phone rides: 99-100 % time overlap, median distance 8-11 m between the tracks
# (28 m with a phone in a chest pocket lagging ~4.5 s). Separate trips are far outside both limits.
MIN_OVERLAP = 0.6  # of the shorter recording's elapsed time
MAX_MEDIAN_M = 60.0
MAX_GAP_S = 30.0  # a point is compared only with a fix of the other track at most this far in time


def _track(conn: sqlite3.Connection, ride_id: int) -> list[tuple[float, float, float]]:
    return [tuple(r) for r in conn.execute("SELECT t, lat, lon FROM points WHERE ride_id = ? ORDER BY idx", (ride_id,))]


def median_distance_m(a: list[tuple[float, float, float]], b: list[tuple[float, float, float]]) -> float | None:
    """Median distance between each point of `a` and the time-nearest point of `b` (None: no pairs)."""
    if not a or not b:
        return None
    tb = [p[0] for p in b]
    ds = []
    for t, lat, lon in a:
        j = bisect.bisect_left(tb, t)
        best = None
        for k in (j - 1, j):
            if 0 <= k < len(b) and abs(tb[k] - t) <= MAX_GAP_S and (best is None or abs(tb[k] - t) < abs(tb[best] - t)):
                best = k
        if best is None:
            continue
        kx = math.cos(math.radians(lat))
        ds.append(math.hypot((b[best][2] - lon) * kx, b[best][1] - lat) * 111_320)
    if not ds:
        return None
    ds.sort()
    return ds[len(ds) // 2]


def same_trip(conn: sqlite3.Connection, a: sqlite3.Row, b: sqlite3.Row) -> bool:
    overlap = min(a["ended_at"], b["ended_at"]) - max(a["started_at"], b["started_at"])
    shorter = min(a["elapsed_s"], b["elapsed_s"])
    if shorter <= 0 or overlap < MIN_OVERLAP * shorter:
        return False
    med = median_distance_m(_track(conn, a["id"]), _track(conn, b["id"]))
    return med is not None and med <= MAX_MEDIAN_M


def _members(conn: sqlite3.Connection, main_id: int) -> list[int]:
    return [main_id] + [r[0] for r in conn.execute("SELECT id FROM rides WHERE primary_id = ? ORDER BY id", (main_id,))]


def _make_main(conn: sqlite3.Connection, group: list[int], main_id: int) -> None:
    conn.execute("UPDATE rides SET primary_id = NULL WHERE id = ?", (main_id,))
    for rid in group:
        if rid != main_id:
            conn.execute("UPDATE rides SET primary_id = ? WHERE id = ?", (main_id, rid))
            # marks are by time and position, so they stay right on any recording of the trip
            conn.execute("UPDATE ride_marks SET ride_id = ? WHERE ride_id = ?", (main_id, rid))


PREFERRED_KEY = "main_recorder"  # settings: the recording app whose recording is main, "" = automatic


def preferred_recorder(conn: sqlite3.Connection) -> str | None:
    return get_setting(conn, PREFERRED_KEY) or None


QUALITY_TIE = 0.1  # recordings this close in quality count as equal: the preferred app decides
WILD_MPS = 13.0  # a 1-second step faster than ~47 km/h is GPS jitter, not riding
WILD_WEIGHT = 10.0


def quality(conn: sqlite3.Connection, ride_id: int) -> float:
    """How well a recording covers its ride: share of the time with a fix at least every 3 s,
    minus a penalty for wild 1-second jumps. Measured on real two-phone trips: either phone can be
    the better one (iOS throttled Open GPX Tracker to a fix every 6-12 s on one ride, with 46-79
    km/h jitter in between, while OpenTracks recorded it fully)."""
    pts = _track(conn, ride_id)
    if len(pts) < 2 or pts[-1][0] <= pts[0][0]:
        return 0.0
    covered = wild = ones = 0
    for (t0, la0, lo0), (t1, la1, lo1) in zip(pts, pts[1:]):
        dt = t1 - t0
        if dt <= 3:
            covered += dt
        if 0 < dt <= 1.5:
            ones += 1
            kx = math.cos(math.radians(la0))
            if math.hypot((lo1 - lo0) * kx, la1 - la0) * 111_320 / dt > WILD_MPS:
                wild += 1
    return covered / (pts[-1][0] - pts[0][0]) - WILD_WEIGHT * wild / max(ones, 1)


def _best(conn: sqlite3.Connection, group: list[int]) -> int:
    """Default main recording: the best quality; among recordings of about equal quality the
    preferred app's (Settings), then the better one."""
    preferred = preferred_recorder(conn)
    rows = conn.execute(
        f"SELECT id, creator, point_count * 1.0 / MAX(elapsed_s, 1) AS rate FROM rides"
        f" WHERE id IN ({','.join('?' * len(group))})", group
    ).fetchall()
    scored = [(quality(conn, r["id"]), r["creator"] == preferred and preferred is not None, r["rate"], -r["id"], r["id"])
              for r in rows]
    top = max(q for q, *_ in scored)
    close = [s for s in scored if s[0] >= top - QUALITY_TIE]
    return max(close, key=lambda s: (s[1], s[2], s[0], s[3]))[4]  # preferred app, then denser


# Recorders name tracks by their start time (OpenTracks: "2026-11-18T20:03+02"); not a user's name.
_TIMESTAMP_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{1,2}[:_]\d{2}")


def _merge_user_fields(conn: sqlite3.Connection, group: list[int], main_id: int) -> None:
    """The trip's name, notes and bike live on the main recording: fill what it lacks from the others."""
    rows = {r["id"]: r for r in conn.execute(
        f"SELECT id, name, name_edited, notes, bike_id, feeling, feeling_tags FROM rides WHERE id IN ({','.join('?' * len(group))})", group)}
    main = rows[main_id]
    others = [rows[i] for i in group if i != main_id]
    rated = next((r for r in [main, *others] if r["feeling"] is not None), None)
    feeling = rated["feeling"] if rated else None
    feeling_tags = rated["feeling_tags"] if rated else None
    real_name = lambda n: bool(n) and not _TIMESTAMP_NAME.match(n)  # noqa: E731
    # a name the user typed wins, then a real (non-timestamp) name from a GPX file, main's first
    ranked = sorted([main, *others], key=lambda r: (not r["name_edited"], not real_name(r["name"])))
    name = ranked[0]["name"] if real_name(ranked[0]["name"]) or ranked[0]["name_edited"] else main["name"]
    edited = int(any(r["name_edited"] for r in rows.values()))
    notes = main["notes"] or next((o["notes"] for o in others if o["notes"]), None)
    bike = main["bike_id"] if main["bike_id"] is not None else next((o["bike_id"] for o in others if o["bike_id"] is not None), None)
    conn.execute(
        "UPDATE rides SET name = ?, name_edited = ?, notes = ?, bike_id = ?, feeling = ?, feeling_tags = ? WHERE id = ?",
        (name, edited, notes, bike, feeling, feeling_tags, main_id),
    )


_RIDE = "SELECT id, primary_id, started_at, ended_at, elapsed_s FROM rides"


def link_new(conn: sqlite3.Connection, ride_id: int) -> int | None:
    """Link a ride to a recording of the same trip, if there is one. Returns the main ride id."""
    me = conn.execute(f"{_RIDE} WHERE id = ? AND unlinked = 0 AND trashed_at IS NULL", (ride_id,)).fetchone()
    if not me or me["primary_id"] is not None:
        return None
    candidates = conn.execute(
        f"{_RIDE} WHERE id != ? AND unlinked = 0 AND trashed_at IS NULL AND started_at < ? AND ended_at > ?"
        " ORDER BY started_at",
        (ride_id, me["ended_at"], me["started_at"]),
    ).fetchall()
    for other in candidates:
        if other["primary_id"] == ride_id or not same_trip(conn, me, other):
            continue
        main = other["primary_id"] or other["id"]
        group = sorted(set(_members(conn, main) + _members(conn, ride_id)))
        best = _best(conn, group)
        with tx(conn):
            _merge_user_fields(conn, group, best)
            _make_main(conn, group, best)
        log.info("rides %s are recordings of one trip; main %s", group, best)
        drop_copies(conn, best)
        return main_of(conn, best)
    return None


def link_all(conn: sqlite3.Connection) -> int:
    """Link every unlinked ride (oldest first). Idempotent. Returns how many rides got linked."""
    n = 0
    for (rid,) in conn.execute("SELECT id FROM rides WHERE primary_id IS NULL ORDER BY started_at").fetchall():
        still_main = conn.execute("SELECT primary_id IS NULL FROM rides WHERE id = ?", (rid,)).fetchone()
        if still_main and still_main[0] and link_new(conn, rid) is not None:
            n += 1
    return n


def main_of(conn: sqlite3.Connection, ride_id: int) -> int | None:
    row = conn.execute("SELECT primary_id FROM rides WHERE id = ?", (ride_id,)).fetchone()
    return None if row is None else (row[0] or ride_id)


def set_main(conn: sqlite3.Connection, ride_id: int, *, chosen: bool = True) -> bool:
    """Make this recording the main ride of its group (chosen: by the user, so the preferred-app
    setting will not override it). False if the ride does not exist."""
    main = main_of(conn, ride_id)
    if main is None:
        return False
    with tx(conn):
        conn.execute("UPDATE rides SET main_chosen = 0 WHERE id = ?", (main,))
        conn.execute("UPDATE rides SET main_chosen = ? WHERE id = ?", (int(chosen), ride_id))
        # the user's name, notes and bike belong to the trip: move them to the new main recording
        if main != ride_id:
            conn.execute(
                "UPDATE rides SET (name, name_edited, notes, bike_id, feeling, feeling_tags) ="
                " (SELECT name, name_edited, notes, bike_id, feeling, feeling_tags FROM rides WHERE id = :old)"
                " WHERE id = :new",
                {"old": main, "new": ride_id},
            )
        _make_main(conn, _members(conn, main), ride_id)
    return True


def split(conn: sqlite3.Connection, ride_id: int) -> bool:
    """Take a recording out of its group for good. False if it is not in a group."""
    row = conn.execute("SELECT primary_id FROM rides WHERE id = ?", (ride_id,)).fetchone()
    if row is None:
        return False
    group = _members(conn, row[0] or ride_id)
    if len(group) < 2:
        return False
    rest = [r for r in group if r != ride_id]
    with tx(conn):
        conn.execute("UPDATE rides SET primary_id = NULL, unlinked = 1 WHERE id = ?", (ride_id,))
        if row[0] is None:  # it was the main one: the best of the rest takes over
            _make_main(conn, rest, _best(conn, rest))
    return True


def recorders(conn: sqlite3.Connection) -> list[str]:
    """Recording apps seen in rides that are part of a group (the only ones the setting matters for)."""
    rows = conn.execute(
        "SELECT DISTINCT creator FROM rides r WHERE creator IS NOT NULL AND trashed_at IS NULL"
        " AND (primary_id IS NOT NULL OR EXISTS (SELECT 1 FROM rides o WHERE o.primary_id = r.id))"
        " ORDER BY creator"
    ).fetchall()
    return [r[0] for r in rows]


def set_preferred_recorder(conn: sqlite3.Connection, creator: str | None) -> int:
    """Store the preference and re-pick the main recording of every trip the user has not picked
    by hand. Returns how many trips changed."""
    set_setting(conn, PREFERRED_KEY, creator or "")
    changed = 0
    mains = conn.execute(
        "SELECT r.id FROM rides r WHERE r.primary_id IS NULL AND r.main_chosen = 0"
        " AND EXISTS (SELECT 1 FROM rides o WHERE o.primary_id = r.id)"
    ).fetchall()
    for (main,) in mains:
        best = _best(conn, _members(conn, main))
        if best != main:
            set_main(conn, best, chosen=False)
            changed += 1
    return changed


COPY_SLACK_S = 3.0  # time slack when checking that one recording lies within another
COPY_MAX_M = 3.0  # median distance: a copy of the same recording, not a second phone


def drop_copies(conn: sqlite3.Connection, main_id: int) -> list[int]:
    """Trash recordings that are copies of another in the same trip: same app, time range inside
    the other's, track on top of it (one recording exported twice, e.g. by hand mid-ride and by the
    automatic export at the end). Lossless: the kept one has every fix. A copy the user named,
    annotated or marked is left alone. Restorable from the trash like any deleted ride."""
    group = _members(conn, main_id)
    if len(group) < 2:
        return []
    rows = {r["id"]: r for r in conn.execute(
        f"SELECT id, creator, started_at, ended_at, point_count, name_edited, notes, feeling, primary_id"
        f" FROM rides WHERE id IN ({','.join('?' * len(group))}) AND trashed_at IS NULL", group)}
    dropped = []
    for a in rows.values():
        if a["id"] in dropped or a["name_edited"] or a["notes"] or a["feeling"] is not None:
            continue
        if conn.execute("SELECT 1 FROM ride_marks WHERE ride_id = ?", (a["id"],)).fetchone():
            continue
        for b in rows.values():
            if b["id"] == a["id"] or b["id"] in dropped or b["creator"] != a["creator"]:
                continue
            inside = b["started_at"] - COPY_SLACK_S <= a["started_at"] and a["ended_at"] <= b["ended_at"] + COPY_SLACK_S
            bigger = (b["point_count"], -b["id"]) > (a["point_count"], -a["id"])  # keep the fuller one
            if not (inside and bigger):
                continue
            med = median_distance_m(_track(conn, a["id"]), _track(conn, b["id"]))
            if med is None or med > COPY_MAX_M:
                continue
            dropped.append(a["id"])
            break
    if dropped:
        with tx(conn):
            for rid in dropped:
                conn.execute("UPDATE rides SET trashed_at = ? WHERE id = ?", (time.time(), rid))
            main = conn.execute("SELECT primary_id IS NULL AND trashed_at IS NULL FROM rides WHERE id = ?", (main_id,)).fetchone()[0]
            if not main:  # the main one was the copy: the best of the rest takes over
                rest = [r for r in group if r not in dropped]
                _make_main(conn, rest, _best(conn, rest))
        log.info("trashed %s: the same recording exported twice", dropped)
    return dropped


def drop_all_copies(conn: sqlite3.Connection) -> int:
    """drop_copies() for every trip with several recordings (startup pass for existing data)."""
    n = 0
    for (main,) in conn.execute(
        "SELECT r.id FROM rides r WHERE r.primary_id IS NULL AND r.trashed_at IS NULL"
        " AND EXISTS (SELECT 1 FROM rides o WHERE o.primary_id = r.id AND o.trashed_at IS NULL)"
    ).fetchall():
        n += len(drop_copies(conn, main))
    return n
