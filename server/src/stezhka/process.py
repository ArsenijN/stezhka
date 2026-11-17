"""Full per-ride processing: raw track -> cleaned points, stops and summary.

Bump PROCESSING_VERSION whenever the output of this pipeline changes, so stored rides can be
found and reprocessed from their raw files (`stezhka reprocess`).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .clean import CleanParams, CleanReport, Point, clean
from .geo import haversine_m
from .gpx import GpxError, ParsedTrack
from .stops import Stop, StopParams, detect_stops

PROCESSING_VERSION = 1


@dataclass(frozen=True, slots=True)
class Params:
    clean: CleanParams = field(default_factory=CleanParams)
    stops: StopParams = field(default_factory=StopParams)
    ele_hysteresis_m: float = 3.0


@dataclass(frozen=True, slots=True)
class Summary:
    started_at: float
    ended_at: float
    elapsed_s: float
    moving_s: float
    distance_m: float
    avg_moving_mps: float
    max_mps: float
    ascent_m: float | None
    descent_m: float | None
    point_count: int


@dataclass(slots=True)
class ProcessedRide:
    points: list[Point]
    stops: list[Stop]
    summary: Summary
    report: CleanReport


def _climb(values: list[float | None], hysteresis: float) -> tuple[float | None, float | None]:
    """Total ascent/descent, ignoring wiggles smaller than `hysteresis`."""
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return None, None
    up = down = 0.0
    ref = vals[0]
    for v in vals[1:]:
        if v - ref >= hysteresis:
            up += v - ref
            ref = v
        elif ref - v >= hysteresis:
            down += ref - v
            ref = v
    return up, down


def process(track: ParsedTrack, params: Params = Params()) -> ProcessedRide:
    pts, report = clean(track.points, params.clean)
    if len(pts) < 2:
        raise GpxError("fewer than 2 usable points after cleaning")
    stops, stationary = detect_stops(pts, params.stops)

    dist = 0.0
    moving_s = 0.0
    pts[0].dist = 0.0
    for i in range(len(pts) - 1):
        a, b = pts[i], pts[i + 1]
        if a.segment == b.segment and not stationary[i]:
            dt = b.t - a.t
            d = haversine_m(a.lat, a.lon, b.lat, b.lon)
            if dt > 0 and d / dt <= params.clean.max_speed_mps:  # unexplained jumps add nothing
                dist += d
            moving_s += dt
        b.dist = dist

    moving_speeds = [p.speed for p in pts if p.moving]
    up, down = _climb([p.ele_smooth for p in pts], params.ele_hysteresis_m)
    summary = Summary(
        started_at=pts[0].t,
        ended_at=pts[-1].t,
        elapsed_s=pts[-1].t - pts[0].t,
        moving_s=moving_s,
        distance_m=dist,
        avg_moving_mps=dist / moving_s if moving_s > 0 else 0.0,
        max_mps=max(moving_speeds, default=0.0),
        ascent_m=up,
        descent_m=down,
        point_count=len(pts),
    )
    return ProcessedRide(points=pts, stops=stops, summary=summary, report=report)
