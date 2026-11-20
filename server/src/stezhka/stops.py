"""Stop and pause detection.

Works on *intervals* between consecutive points, not on points, because recorders often stop
logging while stationary (OpenTracks records by distance): a 40 s red light may show up as a
single 40 s gap between two fixes with almost no displacement.

* stop  - stationary 5 s .. 3 min inside a recording segment (traffic light, crossing, junction)
* break - stationary longer than that (shop, rest, chat)
* pause - recorder segment boundary (manual or auto pause)
* slowdown - a sharp slowdown while riding (something made the rider brake: a pedestrian, a car,
  a pothole); only with the phone's own (Doppler) speed - speeds derived from positions are
  averaged over +-5 s and cannot show braking

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
    # sharp slowdown: calibrated on real OpenTracks rides (the speed series is median-smoothed, so
    # its decelerations read lower than the brakes' peak): the top ~1 % of 3 s decelerations
    slowdown_from_mps: float = 12 / 3.6
    slowdown_drop_mps: float = 8 / 3.6
    slowdown_decel_mps2: float = 0.8
    slowdown_min_s: float = 2.0
    slowdown_max_s: float = 5.0


@dataclass(frozen=True, slots=True)
class Stop:
    kind: str  # "stop" | "break" | "pause" | "slowdown"
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


def detect_slowdowns(pts: list[Point], p: StopParams = StopParams()) -> list[Stop]:
    """Sharp slowdowns: speed falls by slowdown_drop_mps or more at slowdown_decel_mps2 or more.

    From each start the longest qualifying window (up to slowdown_max_s) is taken, so one braking
    is one event; the next search starts after it. Located where the slowing ended (nearest to
    whatever caused it).
    """
    out: list[Stop] = []
    n = len(pts)
    i = 0
    while i < n - 1:
        hit = None
        if pts[i].speed >= p.slowdown_from_mps:
            for j in range(i + 1, n):
                dt = pts[j].t - pts[i].t
                if dt > p.slowdown_max_s or pts[j].segment != pts[i].segment:
                    break
                dv = pts[i].speed - pts[j].speed
                if dt >= p.slowdown_min_s and dv >= p.slowdown_drop_mps and dv / dt >= p.slowdown_decel_mps2:
                    hit = j
        if hit is None:
            i += 1
            continue
        # start where the slowing began (the peak speed just before), end where it bottomed out
        a = i
        while a + 1 < hit and pts[a + 1].speed >= pts[a].speed:
            a += 1
        b = hit
        while b + 1 < n and pts[b + 1].segment == pts[b].segment and pts[b + 1].speed < pts[b].speed \
                and pts[b + 1].t - pts[a].t <= p.slowdown_max_s:
            b += 1
        out.append(Stop("slowdown", a, b, pts[a].t, pts[b].t, pts[b].lat, pts[b].lon))
        i = b + 1
    return out
