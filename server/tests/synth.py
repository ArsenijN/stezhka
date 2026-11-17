"""Synthetic GPX tracks for tests, shaped like OpenTracks exports (GPX 1.1 + extensions)."""

from __future__ import annotations

import random
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

from stezhka.geo import offset

LAT0, LON0 = 48.6208, 22.2879  # Uzhhorod
T0 = datetime(2026, 9, 1, 7, 30, tzinfo=timezone.utc).timestamp()


@dataclass
class P:
    t: float  # seconds from start
    north: float  # metres from origin
    east: float
    speed: float | None = None
    hacc: float | None = 5.0
    ele: float | None = 120.0
    seg: int = 0


@dataclass
class Builder:
    """Moves a virtual rider east/north at given speeds, one fix per `dt` seconds."""

    points: list[P] = field(default_factory=list)
    t: float = 0.0
    north: float = 0.0
    east: float = 0.0
    seg: int = 0
    rng: random.Random = field(default_factory=lambda: random.Random(42))

    def ride(self, seconds: float, mps: float, *, dt: float = 1.0, heading: str = "east", device_speed=True):
        for _ in range(int(seconds / dt)):
            self.t += dt
            if heading == "east":
                self.east += mps * dt
            else:
                self.north += mps * dt
            self.points.append(P(self.t, self.north, self.east, mps if device_speed else None, seg=self.seg))
        return self

    def stand(self, seconds: float, *, jitter_m: float = 2.0, dt: float = 1.0, device_speed=True):
        for _ in range(int(seconds / dt)):
            self.t += dt
            n = self.north + self.rng.uniform(-jitter_m, jitter_m)
            e = self.east + self.rng.uniform(-jitter_m, jitter_m)
            self.points.append(P(self.t, n, e, 0.1 if device_speed else None, seg=self.seg))
        return self

    def silent(self, seconds: float):
        """Recorder logs nothing (OpenTracks does this while stationary)."""
        self.t += seconds
        return self

    def new_segment(self):
        self.seg += 1
        return self

    def teleport(self, north: float = 0.0, east: float = 0.0):
        self.north += north
        self.east += east
        return self

    def mutate(self, index: int, **changes):
        self.points[index] = replace(self.points[index], **changes)
        return self

    def gpx(self) -> bytes:
        return to_gpx(self.points)


def _iso(t: float) -> str:
    return datetime.fromtimestamp(T0 + t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def to_gpx(points: list[P], name: str = "Test ride") -> bytes:
    segs: dict[int, list[P]] = {}
    for p in points:
        segs.setdefault(p.seg, []).append(p)
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="OpenTracks" xmlns="http://www.topografix.com/GPX/1/1"'
        ' xmlns:opentracks="http://opentracksapp.com/xmlschemas/v1"'
        ' xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v2">',
        f"<trk><name>{name}</name><type>cycling</type>",
    ]
    for seg in sorted(segs):
        out.append("<trkseg>")
        for p in segs[seg]:
            lat, lon = offset(LAT0, LON0, p.north, p.east)
            ext = []
            if p.speed is not None:
                ext.append(f"<gpxtpx:TrackPointExtension><gpxtpx:speed>{p.speed}</gpxtpx:speed></gpxtpx:TrackPointExtension>")
            if p.hacc is not None:
                ext.append(f"<opentracks:accuracy_horizontal>{p.hacc}</opentracks:accuracy_horizontal>")
            ele = f"<ele>{p.ele}</ele>" if p.ele is not None else ""
            ext_xml = f"<extensions>{''.join(ext)}</extensions>" if ext else ""
            out.append(f'<trkpt lat="{lat:.7f}" lon="{lon:.7f}">{ele}<time>{_iso(p.t)}</time>{ext_xml}</trkpt>')
        out.append("</trkseg>")
    out.append("</trk></gpx>")
    return "\n".join(out).encode()


def commute() -> Builder:
    """1 min at 18 km/h, 30 s red light, 1 min at 18 km/h."""
    return Builder().ride(60, 5.0).stand(30).ride(60, 5.0)
