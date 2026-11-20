"""Elevation from the Copernicus GLO-30 digital surface model (AWS Open Data, free).

Tiles are 1°x1° Cloud-Optimized GeoTIFFs (float32 metres, DEFLATE + floating-point predictor,
1024x1024 internal blocks, PixelIsPoint: pixel (0,0) is centred exactly on the tile's NW corner).
They are downloaded once per tile on demand (~45 MB each; Zakarpattia needs ~6) into
data/dem/. Only the 1024x1024 blocks a ride touches are decoded, in pure numpy, so neither GDAL
nor the imagecodecs C package is needed.

Caveat: GLO-30 is a *surface* model. In forests and between buildings it reports the canopy/roof
height (often +10..25 m). Ride profiles through woods are therefore lumpier than reality; smoothing
over ~30 m along the track removes the worst of it. A bare-earth model (e.g. FABDEM) can be added
later behind the same interface.

Attribution required: "Copernicus DEM © DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH
2014-2018, provided under COPERNICUS by the European Union and ESA".
"""

from __future__ import annotations

import logging
import math
import threading
import urllib.error
import urllib.request
import zlib
from collections import OrderedDict
from pathlib import Path

import numpy as np
import tifffile

log = logging.getLogger(__name__)

DEM_VERSION = 2  # 2: bridges and tunnels bridged over (v0.5.8); bump when sampling/smoothing changes, to re-enrich stored rides
BASE_URL = "https://copernicus-dem-30m.s3.amazonaws.com"
USER_AGENT = "stezhka-dem/1"
ATTRIBUTION = "Copernicus DEM © DLR e.V., Airbus DS (EU/ESA)"


def tile_name(lat0: int, lon0: int) -> str:
    ns = f"{'N' if lat0 >= 0 else 'S'}{abs(lat0):02d}"
    ew = f"{'E' if lon0 >= 0 else 'W'}{abs(lon0):03d}"
    return f"Copernicus_DSM_COG_10_{ns}_00_{ew}_00_DEM"


def decode_float_predictor(raw: bytes, rows: int, cols: int) -> np.ndarray:
    """Undo TIFF predictor 3 (floating point) for one block of float32 samples.

    Each row is stored as byte planes (most significant byte first) with horizontal byte-wise
    differencing applied across the whole row.
    """
    a = np.frombuffer(raw, dtype=np.uint8).reshape(rows, cols * 4)
    a = np.cumsum(a, axis=1, dtype=np.uint8)  # wraps modulo 256, like the encoder
    a = np.ascontiguousarray(a.reshape(rows, 4, cols).transpose(0, 2, 1))
    return a.view(">f4").reshape(rows, cols).astype(np.float32)


class _Tile:
    """Lazy block reader for one GLO-30 GeoTIFF."""

    def __init__(self, path: Path):
        self.path = path
        with tifffile.TiffFile(path) as tf:
            page = tf.pages[0]
            self.height, self.width = page.shape
            self.block_h, self.block_w = page.tilelength, page.tilewidth
            self.offsets = list(page.dataoffsets)
            self.counts = list(page.databytecounts)
            self.predictor = int(page.predictor)
            self.compression = int(page.compression)
            scale = page.tags["ModelPixelScaleTag"].value
            tie = page.tags["ModelTiepointTag"].value
        if self.compression not in (1, 8, 32946):
            raise ValueError(f"{path.name}: unsupported TIFF compression {self.compression}")
        self.dx, self.dy = scale[0], scale[1]
        self.lon0, self.lat0 = tie[3], tie[4]  # centre of pixel (0,0)
        self.blocks_across = math.ceil(self.width / self.block_w)
        self._cache: OrderedDict[int, np.ndarray] = OrderedDict()
        self._lock = threading.Lock()

    def _block(self, idx: int) -> np.ndarray:
        with self._lock:
            if idx in self._cache:
                self._cache.move_to_end(idx)
                return self._cache[idx]
        with open(self.path, "rb") as f:
            f.seek(self.offsets[idx])
            raw = f.read(self.counts[idx])
        if self.compression != 1:
            raw = zlib.decompress(raw)
        if self.predictor == 3:
            arr = decode_float_predictor(raw, self.block_h, self.block_w)
        elif self.predictor == 1:
            arr = np.frombuffer(raw, dtype="<f4").reshape(self.block_h, self.block_w)
        else:
            raise ValueError(f"{self.path.name}: unsupported predictor {self.predictor}")
        with self._lock:
            self._cache[idx] = arr
            while len(self._cache) > 8:  # 8 x 4 MB per tile
                self._cache.popitem(last=False)
        return arr

    def pixel(self, row: int, col: int) -> float:
        row = min(max(row, 0), self.height - 1)
        col = min(max(col, 0), self.width - 1)
        b = (row // self.block_h) * self.blocks_across + col // self.block_w
        return float(self._block(b)[row % self.block_h, col % self.block_w])


class Dem:
    """Bilinear elevation lookup across tiles, downloading missing tiles on first use."""

    def __init__(self, dem_dir: Path, *, network: bool = True, base_url: str = BASE_URL):
        self.dir = dem_dir
        self.network = network
        self.base_url = base_url
        self._tiles: dict[tuple[int, int], _Tile | None] = {}
        self._lock = threading.Lock()

    def _download(self, name: str, dest: Path) -> bool:
        url = f"{self.base_url}/{name}/{name}.tif"
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                data = r.read()
        except urllib.error.HTTPError as e:
            if e.code in (403, 404):  # no tile: open sea / outside coverage
                (self.dir / f"{name}.missing").touch()
                return False
            raise
        tmp = dest.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(dest)
        log.info("downloaded DEM tile %s (%.1f MB)", name, len(data) / 1e6)
        return True

    def _tile(self, lat0: int, lon0: int) -> _Tile | None:
        key = (lat0, lon0)
        with self._lock:
            if key in self._tiles:
                return self._tiles[key]
            name = tile_name(lat0, lon0)
            path = self.dir / f"{name}.tif"
            tile = None
            if not path.exists() and not (self.dir / f"{name}.missing").exists() and self.network:
                self.dir.mkdir(parents=True, exist_ok=True)
                self._download(name, path)
            if path.exists():
                tile = _Tile(path)
            self._tiles[key] = tile
            return tile

    def _node(self, lat: float, lon: float) -> float | None:
        """Value at the grid node nearest to (lat, lon), from whichever tile holds it."""
        lat0, lon0 = math.floor(lat + 1e-9), math.floor(lon + 1e-9)
        tile = self._tile(lat0, lon0)
        if tile is None:
            return None
        row = round((tile.lat0 - lat) / tile.dy)
        col = round((lon - tile.lon0) / tile.dx)
        return tile.pixel(row, col)

    def elevation(self, lat: float, lon: float) -> float | None:
        lat0, lon0 = math.floor(lat), math.floor(lon)
        tile = self._tile(lat0, lon0)
        if tile is None:
            return None
        fr = (tile.lat0 - lat) / tile.dy
        fc = (lon - tile.lon0) / tile.dx
        r0, c0 = math.floor(fr), math.floor(fc)
        tr, tc = fr - r0, fc - c0
        vals = []
        for dr, dc in ((0, 0), (0, 1), (1, 0), (1, 1)):
            node_lat = tile.lat0 - (r0 + dr) * tile.dy
            node_lon = tile.lon0 + (c0 + dc) * tile.dx
            v = tile.pixel(r0 + dr, c0 + dc) if (r0 + dr < tile.height and c0 + dc < tile.width) else self._node(node_lat, node_lon)
            if v is None:
                return None
            vals.append(v)
        v00, v01, v10, v11 = vals
        return (v00 * (1 - tc) + v01 * tc) * (1 - tr) + (v10 * (1 - tc) + v11 * tc) * tr

    def profile(self, lats, lons) -> list[float | None]:
        return [self.elevation(a, b) for a, b in zip(lats, lons)]


def smooth_by_distance(values: list[float | None], dist: list[float], half_window_m: float = 30.0) -> list[float | None]:
    """Centred moving average over distance along the track.

    The half-window shrinks symmetrically near both ends (no one-sided bias). Both window edges
    max(d-H, d0, 2d-dN) and min(d+H, 2d-d0, dN) are non-decreasing in d, so one sliding pass is exact.
    """
    n = len(values)
    out: list[float | None] = [None] * n
    if not n:
        return out
    d0, dn = dist[0], dist[-1]
    lo = hi = 0
    total = 0.0
    count = 0
    for i in range(n):
        if values[i] is None:
            continue
        h = min(half_window_m, dist[i] - d0, dn - dist[i])
        while hi < n and dist[hi] <= dist[i] + h:
            if values[hi] is not None:
                total += values[hi]
                count += 1
            hi += 1
        while lo < hi and dist[lo] < dist[i] - h:
            if values[lo] is not None:
                total -= values[lo]
                count -= 1
            lo += 1
        out[i] = total / count if count else values[i]
    return out
