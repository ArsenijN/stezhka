"""Self-hosted basemap: a regional extract of the OpenFreeMap planet + OFM styles, sprites, fonts.

OpenFreeMap publishes its weekly planet build as one PMTiles file (~90 GB). PMTiles is designed for
HTTP range reads, so a region is extracted by reading only the directories and tiles it needs:

  1. read the header and root directory,
  2. walk leaf directories that can contain wanted tile ids (one level at a time, batched),
  3. download the wanted tiles' byte ranges, merged into few large requests,
  4. write a new, clustered PMTiles file with the same tile format and metadata.

The result (`data/basemap/region.pmtiles`, tens of MB for an oblast) is served by Stezhka itself, so
viewing the map makes no third-party requests. Planet versions live in immutable, dated folders, so a
half-finished run can never mix two builds.
"""

from __future__ import annotations

import bisect
import gzip
import json
import logging
import math
import shutil
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from pmtiles.tile import Compression, Entry, deserialize_directory, deserialize_header, zxy_to_tileid
from pmtiles.writer import finalize_header

log = logging.getLogger(__name__)

OFM_TILES = "https://btrfs.openfreemap.com"
OFM_ASSETS = "https://assets.openfreemap.com"
OFM_REMOTE_ORIGIN = "https://tiles.openfreemap.org"
OFM_REMOTE_STYLE = OFM_REMOTE_ORIGIN + "/styles/{style}"
USER_AGENT = "stezhka-basemap/1 (self-hosted map extract)"

# Zakarpattia oblast with a margin over the SK/HU/RO/PL borders. West, south, east, north.
DEFAULT_BBOX = (21.9, 47.7, 24.8, 49.2)
DEFAULT_MAXZOOM = 14  # OFM planet max zoom; MapLibre over-zooms beyond it
STYLES = {"light": "positron", "dark": "dark"}
ATTRIBUTION = (
    '<a href="https://openfreemap.org" target="_blank">OpenFreeMap</a> '
    '<a href="https://www.openmaptiles.org/" target="_blank">&copy; OpenMapTiles</a> '
    'Data from <a href="https://www.openstreetmap.org/copyright" target="_blank">OpenStreetMap</a>'
)

MAX_GAP = 512 * 1024  # merge ranges closer than this into one request (cheap over-fetch)
MAX_BATCH = 16 * 1024 * 1024
WORKERS = 4

GetBytes = Callable[[int, int], bytes]


# --- sources -------------------------------------------------------------------------------------


class HttpRangeSource:
    """get_bytes(offset, length) over HTTP Range requests, with retries."""

    def __init__(self, url: str, retries: int = 4, timeout: float = 120):
        self.url = url
        self.retries = retries
        self.timeout = timeout

    def __call__(self, offset: int, length: int) -> bytes:
        headers = {"Range": f"bytes={offset}-{offset + length - 1}", "User-Agent": USER_AGENT}
        for attempt in range(self.retries + 1):
            try:
                req = urllib.request.Request(self.url, headers=headers)
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    if r.status != 206:
                        raise OSError(f"server ignored Range request (HTTP {r.status})")
                    data = r.read()
                if len(data) != length:
                    raise OSError(f"short read: {len(data)} of {length} bytes")
                return data
            except (OSError, urllib.error.URLError) as e:
                if attempt == self.retries:
                    raise
                wait = 2**attempt
                log.warning("range %d+%d failed (%s), retrying in %ds", offset, length, e, wait)
                time.sleep(wait)
        raise AssertionError("unreachable")


def file_source(path: Path) -> GetBytes:
    def get(offset: int, length: int) -> bytes:
        with open(path, "rb") as f:
            f.seek(offset)
            return f.read(length)

    return get


def _http_get(url: str, timeout: float = 120) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def latest_planet_version() -> str:
    return _http_get(f"{OFM_ASSETS}/deployed_versions/planet.txt").decode().strip()


def planet_url(version: str) -> str:
    if not version.replace("_", "").isalnum():
        raise ValueError(f"unexpected planet version {version!r}")
    return f"{OFM_TILES}/areas/planet/{version}/tiles.pmtiles"


# --- tile math -----------------------------------------------------------------------------------


def lonlat_to_tile(lon: float, lat: float, z: int) -> tuple[int, int]:
    n = 2**z
    lat = max(min(lat, 85.0511), -85.0511)
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)
    return min(max(x, 0), n - 1), min(max(y, 0), n - 1)


def tile_ids_for_bbox(bbox: tuple[float, float, float, float], minzoom: int, maxzoom: int) -> list[int]:
    west, south, east, north = bbox
    ids = []
    for z in range(minzoom, maxzoom + 1):
        x0, y0 = lonlat_to_tile(west, north, z)
        x1, y1 = lonlat_to_tile(east, south, z)
        ids.extend(zxy_to_tileid(z, x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1))
    ids.sort()
    return ids


# --- range batching ------------------------------------------------------------------------------


def plan_batches(ranges: Iterable[tuple[int, int]], max_gap: int = MAX_GAP, max_batch: int = MAX_BATCH):
    """Group (offset, length) ranges into [(batch_offset, batch_length, [ranges...])]."""
    batches: list[tuple[int, int, list[tuple[int, int]]]] = []
    for off, ln in sorted(set(ranges)):
        if batches:
            b_off, b_len, members = batches[-1]
            end = off + ln
            if off - (b_off + b_len) <= max_gap and end - b_off <= max_batch:
                members.append((off, ln))
                batches[-1] = (b_off, max(b_len, end - b_off), members)
                continue
        batches.append((off, ln, [(off, ln)]))
    return batches


def fetch_ranges(get: GetBytes, ranges, on_range: Callable[[int, int, bytes], None], workers: int = WORKERS) -> int:
    """Fetch many ranges with few requests; calls on_range(offset, length, data) in offset order.
    Returns the number of requests made."""
    batches = plan_batches(ranges)

    def load(batch):
        return batch, get(batch[0], batch[1])

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for (b_off, _b_len, members), data in pool.map(load, batches):
            for off, ln in members:
                on_range(off, ln, data[off - b_off : off - b_off + ln])
    return len(batches)


# --- extraction ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExtractStats:
    tiles_wanted: int
    tiles_found: int
    unique_tiles: int
    tile_bytes: int
    output_bytes: int


def _read_header(get: GetBytes) -> tuple[dict, bytes]:
    head = get(0, 16384)  # spec: header + root directory always fit in the first 16 KiB
    return deserialize_header(head[:127]), head


def _collect_entries(get: GetBytes, header: dict, head: bytes, wanted: list[int]) -> dict[int, tuple[int, int]]:
    """tile_id -> (absolute offset, length) for every wanted tile present in the archive."""

    def any_wanted(lo: int, hi: int) -> bool:
        i = bisect.bisect_left(wanted, lo)
        return i < len(wanted) and wanted[i] < hi

    found: dict[int, tuple[int, int]] = {}
    root = head[header["root_offset"] : header["root_offset"] + header["root_length"]]
    if len(root) < header["root_length"]:
        root = get(header["root_offset"], header["root_length"])
    level: list[tuple[bytes, int]] = [(root, 2**64)]  # (directory bytes, exclusive upper tile id)
    for depth in range(4):  # spec allows at most 3 levels of leaves
        next_ranges: dict[tuple[int, int], int] = {}
        for blob, upper in level:
            entries = deserialize_directory(blob)
            for k, e in enumerate(entries):
                lo = e.tile_id
                if e.run_length > 0:
                    hi = lo + e.run_length
                    i = bisect.bisect_left(wanted, lo)
                    while i < len(wanted) and wanted[i] < hi:
                        found[wanted[i]] = (header["tile_data_offset"] + e.offset, e.length)
                        i += 1
                else:
                    hi = entries[k + 1].tile_id if k + 1 < len(entries) else upper
                    if any_wanted(lo, hi):
                        next_ranges[(header["leaf_directory_offset"] + e.offset, e.length)] = hi
        if not next_ranges:
            break
        fetched: list[tuple[bytes, int]] = []
        fetch_ranges(get, next_ranges, lambda off, ln, data: fetched.append((data, next_ranges[(off, ln)])))
        level = fetched
        log.info("directory level %d: %d leaf directories", depth + 1, len(fetched))
    return found


def extract(
    get: GetBytes,
    out_path: Path,
    bbox: tuple[float, float, float, float] = DEFAULT_BBOX,
    maxzoom: int = DEFAULT_MAXZOOM,
    *,
    extra_metadata: dict | None = None,
    dry_run: bool = False,
) -> ExtractStats:
    header, head = _read_header(get)
    maxzoom = min(maxzoom, header["max_zoom"])
    wanted = tile_ids_for_bbox(bbox, header["min_zoom"], maxzoom)
    found = _collect_entries(get, header, head, wanted)
    unique = sorted(set(found.values()))
    tile_bytes = sum(ln for _, ln in unique)
    log.info("%d of %d tiles present, %d unique, %.1f MB", len(found), len(wanted), len(unique), tile_bytes / 1e6)
    if dry_run or not found:
        return ExtractStats(len(wanted), len(found), len(unique), tile_bytes, 0)

    meta_raw = get(header["metadata_offset"], header["metadata_length"]) if header["metadata_length"] else b""
    if meta_raw and header["internal_compression"] == Compression.GZIP:
        meta_raw = gzip.decompress(meta_raw)
    metadata = json.loads(meta_raw) if meta_raw.strip() else {}
    metadata.update(extra_metadata or {})

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_out = out_path.with_suffix(".tmp")
    with tempfile.TemporaryFile(dir=out_path.parent) as spool, tempfile.TemporaryFile(dir=out_path.parent) as data_f:
        # 1. download tile bytes into a spool file (source-offset order)
        spool_at: dict[int, int] = {}  # source offset -> spool offset

        def keep(off: int, _ln: int, data: bytes) -> None:
            spool_at[off] = spool.tell()
            spool.write(data)

        fetch_ranges(get, unique, keep)

        # 2. tile data in tile-id order; identical source tiles (forests, fields) are stored once.
        #    Dedupe is exact (same source offset), not hash-based.
        entries: list[Entry] = []
        out_offset_of: dict[int, int] = {}
        written = 0
        for tid in sorted(found):
            src_off, ln = found[tid]
            if src_off not in out_offset_of:
                spool.seek(spool_at[src_off])
                data_f.write(spool.read(ln))
                out_offset_of[src_off] = written
                written += ln
            o = out_offset_of[src_off]
            last = entries[-1] if entries else None
            if last and last.offset == o and last.tile_id + last.run_length == tid:
                last.run_length += 1
            else:
                entries.append(Entry(tid, o, ln, 1))

        west, south, east, north = bbox
        out_header = dict(header)
        out_header.update(
            min_lon_e7=int(west * 1e7), min_lat_e7=int(south * 1e7),
            max_lon_e7=int(east * 1e7), max_lat_e7=int(north * 1e7),
            center_lon_e7=int((west + east) / 2 * 1e7), center_lat_e7=int((south + north) / 2 * 1e7),
            center_zoom=min(10, maxzoom),
        )
        hdr, root, meta_c, leaves = finalize_header(
            out_header, len(found), entries, len(out_offset_of), metadata, True, written
        )
        with open(tmp_out, "wb") as f:
            for part in (hdr, root, meta_c, leaves):
                f.write(part)
            data_f.seek(0)
            shutil.copyfileobj(data_f, f)
    tmp_out.replace(out_path)
    return ExtractStats(len(wanted), len(found), len(unique), tile_bytes, out_path.stat().st_size)


def fetch_region(
    basemap_dir: Path,
    bbox: tuple[float, float, float, float] = DEFAULT_BBOX,
    maxzoom: int = DEFAULT_MAXZOOM,
    version: str | None = None,
    dry_run: bool = False,
) -> ExtractStats:
    version = version or latest_planet_version()
    url = planet_url(version)
    log.info("extracting bbox %s z0-%d from OpenFreeMap planet %s", bbox, maxzoom, version)
    stats = extract(
        HttpRangeSource(url), basemap_dir / "region.pmtiles", bbox, maxzoom,
        extra_metadata={"stezhka_extract": {"source": url, "bbox": list(bbox), "maxzoom": maxzoom}},
        dry_run=dry_run,
    )
    if not dry_run and stats.tiles_found:
        info = {
            "ofm_version": version, "bbox": list(bbox), "maxzoom": maxzoom,
            "created_at": time.time(), "tiles": stats.tiles_found, "bytes": stats.output_bytes,
        }
        (basemap_dir / "region.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    return stats


# --- styles, sprites, fonts ----------------------------------------------------------------------


def _extract_members(archive: Path, prefix: str, dest: Path, keep: Callable[[str], bool] = lambda _n: True) -> int:
    """Extract regular files under `prefix` into dest (prefix stripped). Rejects path tricks."""
    count = 0
    dest_resolved = dest.resolve()
    with tarfile.open(archive) as tar:
        for m in tar.getmembers():
            if not m.isfile() or not m.name.startswith(prefix):
                continue
            rel = m.name[len(prefix) :]
            if not rel or not keep(rel):
                continue
            target = (dest / rel).resolve()
            if dest_resolved not in target.parents:
                raise ValueError(f"unsafe path in archive: {m.name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with tar.extractfile(m) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
            count += 1
    return count


def fetch_assets(basemap_dir: Path) -> None:
    """Download OFM styles (light+dark), sprites and fonts; swap them in atomically."""
    basemap_dir.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".assets-", dir=basemap_dir))
    try:
        wanted_styles = {f"{s}.json" for s in STYLES.values()}
        for url, prefix, sub, keep in (
            (f"{OFM_ASSETS}/styles/ofm.tar.gz", "ofm/", "styles", lambda n: n in wanted_styles),
            (f"{OFM_ASSETS}/sprites/ofm_f384.tar.gz", "ofm_f384/", "sprites", lambda _n: True),
            (f"{OFM_ASSETS}/fonts/ofm.tar.gz", "ofm/", "fonts", lambda n: n.endswith(".pbf")),
        ):
            log.info("downloading %s", url)
            archive = staging / "download.tar.gz"
            archive.write_bytes(_http_get(url, timeout=600))
            n = _extract_members(archive, prefix, staging / sub, keep)
            archive.unlink()
            if not n:
                raise ValueError(f"{url}: archive layout changed, nothing extracted")
            log.info("  %s: %d files", sub, n)
        for sub in ("styles", "sprites", "fonts"):
            final = basemap_dir / sub
            old = basemap_dir / f".{sub}.old"
            if final.exists():
                final.replace(old)
            (staging / sub).replace(final)
            if old.exists():
                shutil.rmtree(old)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


# --- serving -------------------------------------------------------------------------------------

BASE_TOKEN = "__STEZHKA_BASE__"  # replaced by the browser with the app's base URL


def local_available(basemap_dir: Path) -> bool:
    return (basemap_dir / "region.pmtiles").is_file() and all(
        (basemap_dir / "styles" / f"{s}.json").is_file() for s in STYLES.values()
    )


def local_style(basemap_dir: Path, theme: str) -> dict:
    """OFM style rewritten to use the local extract, sprites and fonts.

    URLs contain BASE_TOKEN because MapLibre needs absolute URLs and only the browser knows the
    public base URL (subdomain or path prefix behind the reverse proxy).
    """
    style = json.loads((basemap_dir / "styles" / f"{STYLES[theme]}.json").read_text(encoding="utf-8"))
    raster = {k for k, v in style["sources"].items() if v.get("type") == "raster"}  # low-zoom relief
    style["sources"] = {k: v for k, v in style["sources"].items() if k not in raster}
    style["sources"]["openmaptiles"] = {
        "type": "vector",
        "url": f"pmtiles://{BASE_TOKEN}basemap/region.pmtiles",
        "attribution": ATTRIBUTION,
    }
    style["layers"] = [layer for layer in style["layers"] if layer.get("source") not in raster]
    style["glyphs"] = f"{BASE_TOKEN}basemap/fonts/{{fontstack}}/{{range}}.pbf"
    style["sprite"] = f"{BASE_TOKEN}basemap/sprites/ofm"
    return style


def region_info(basemap_dir: Path) -> dict | None:
    p = basemap_dir / "region.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None
