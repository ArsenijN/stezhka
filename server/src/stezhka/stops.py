"""Stop and pause detection.

Works on *intervals* between consecutive points, not on points, because recorders often stop
logging while stationary (OpenTracks records by distance): a 40 s red light may show up as a
single 40 s gap between two fixes with almost no displacement.

* stop  - stationary 5 s .. 3 min inside a recording segment (traffic light, crossing, junction)
* break - stationary longer than that (shop, rest, chat)
* pause - recorder segment boundary (manual or auto pause)

Why the bike stopped is decided later (OSM features nearby). If nothing explains it the reason
stays unknown; we never guess.
"""

from __future__ import annotations

from dataclasses import dataclass

from .clean import Point
from .geo import haversine_m


@dataclass(frozen=True, slots=True)
class StopParams:
    stop_speed_mps: float = 1.0  # 3.6 km/h
    min_stop_s: float = 5.0
    max_stop_radius_m: float = 20.0  # larger drift = slow crawl / pushing, not a stop
    gap_s: float = 5.0  # longer intervals are judged by displacement over the gap
    break_s: float = 180.0


@dataclass(frozen=True, slots=True)
class Stop:
    kind: str  # "stop" | "break" | "pause"
    start_idx: int
    end_idx: int
    start_t: float
    end_t: float
    lat: float
    lon: float

    @property
    def duration_s(self) -> float:
        return self.end_t - self.start_t


def _interval_stationary(a: Point, b: Point, p: StopParams) -> bool:
    dt = b.t - a.t
    if dt > p.gap_s:
        return haversine_m(a.lat, a.lon, b.lat, b.lon) / dt < p.stop_speed_mps
    return a.speed < p.stop_speed_mps and b.speed < p.stop_speed_mps


def detect_stops(pts: list[Point], p: StopParams = StopParams()) -> tuple[list[Stop], list[bool]]:
    """Return (stops sorted by time, stationary flag per interval i = (pts[i], pts[i+1]))."""
    n = len(pts)
    stationary = [
        pts[i].segment == pts[i + 1].segment and _interval_stationary(pts[i], pts[i + 1], p)
        for i in range(n - 1)
    ]
    stops: list[Stop] = []

    i = 0
    while i < n - 1:
        if not stationary[i]:
            i += 1
            continue
        j = i
        while j + 1 < n - 1 and stationary[j + 1]:
            j += 1
        a, b = i, j + 1  # stop spans points a..b
        duration = pts[b].t - pts[a].t
        clat = sum(q.lat for q in pts[a : b + 1]) / (b - a + 1)
        clon = sum(q.lon for q in pts[a : b + 1]) / (b - a + 1)
        radius = max(haversine_m(clat, clon, q.lat, q.lon) for q in pts[a : b + 1])
        if duration >= p.min_stop_s and radius <= p.max_stop_radius_m:
            kind = "break" if duration >= p.break_s else "stop"
            stops.append(Stop(kind, a, b, pts[a].t, pts[b].t, clat, clon))
        else:
            for k in range(i, j + 1):  # brief slowdown or slow crawl: counts as moving
                stationary[k] = False
        i = j + 1

    for k in range(n - 1):
        if pts[k].segment != pts[k + 1].segment:
            q = pts[k]
            stops.append(Stop("pause", k, k + 1, q.t, pts[k + 1].t, q.lat, q.lon))

    for k, q in enumerate(pts):
        q.moving = not ((k > 0 and stationary[k - 1]) or (k < n - 1 and stationary[k]))

    stops.sort(key=lambda s: s.start_t)
    return stops, stationary
