"""Basemap extraction and serving, against a small synthetic PMTiles archive (no network)."""

import gzip
import json
import tarfile

import pytest
from fastapi.testclient import TestClient
from pmtiles.reader import Reader
from pmtiles.tile import Compression, TileType, tileid_to_zxy, zxy_to_tileid
from pmtiles.writer import Writer

from stezhka import api, auth, basemap
from stezhka.config import Config
from stezhka.db import connect

UZH = (22.2, 48.55, 22.4, 48.7)  # small bbox around Uzhhorod


def make_planet(path, maxzoom=8):
    """Whole-world archive z0..maxzoom; tile content encodes z/x/y; z>=6 'ocean' tiles are shared."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        w = Writer(f)
        ids = []
        for z in range(maxzoom + 1):
            ids += [zxy_to_tileid(z, x, y) for x in range(2**z) for y in range(2**z)]
        for tid in sorted(ids):
            z, x, y = tileid_to_zxy(tid)
            body = b"ocean" if z >= 6 and (x + y) % 2 else f"{z}/{x}/{y}".encode()
            w.write_tile(tid, gzip.compress(body, mtime=0))
        w.finalize(
            {
                "tile_type": TileType.MVT, "tile_compression": Compression.GZIP,
                "min_lon_e7": -1800000000, "min_lat_e7": -850000000, "max_lon_e7": 1800000000,
                "max_lat_e7": 850000000, "center_zoom": 0, "center_lon_e7": 0, "center_lat_e7": 0,
            },
            {"name": "test planet", "vector_layers": []},
        )


def test_bbox_tiles():
    ids = basemap.tile_ids_for_bbox(UZH, 0, 3)
    assert ids == sorted(ids)
    assert len(ids) == 4  # one tile per zoom for a small bbox
    # y = (1 - asinh(tan(48.62°)) / pi) / 2 * 2^14 = 5652.4
    assert basemap.lonlat_to_tile(22.29, 48.62, 14) == (9206, 5652)


def test_plan_batches_merges_close_ranges():
    b = basemap.plan_batches([(0, 10), (15, 5), (10_000_000, 4)], max_gap=100)
    assert [(o, n, len(m)) for o, n, m in b] == [(0, 20, 2), (10_000_000, 4, 1)]


def test_extract_roundtrip(tmp_path):
    planet = tmp_path / "planet.pmtiles"
    make_planet(planet)
    calls = []

    def get(off, ln):
        calls.append((off, ln))
        return basemap.file_source(planet)(off, ln)

    out = tmp_path / "region.pmtiles"
    stats = basemap.extract(get, out, UZH, maxzoom=8, extra_metadata={"x": 1})
    assert stats.tiles_found == stats.tiles_wanted == len(basemap.tile_ids_for_bbox(UZH, 0, 8))

    r = Reader(basemap.file_source(out))
    h = r.header()
    assert (h["min_zoom"], h["max_zoom"]) == (0, 8)
    assert h["clustered"] is True
    assert r.metadata()["x"] == 1 and r.metadata()["name"] == "test planet"
    for z in range(9):
        x, y = basemap.lonlat_to_tile(22.29, 48.62, z)
        expect = b"ocean" if z >= 6 and (x + y) % 2 else f"{z}/{x}/{y}".encode()
        assert gzip.decompress(r.get(z, x, y)) == expect
    assert r.get(8, 0, 0) is None  # outside the bbox
    assert len(calls) < 10  # ranges were batched, not one request per tile


def test_dry_run_writes_nothing(tmp_path):
    planet = tmp_path / "planet.pmtiles"
    make_planet(planet, maxzoom=4)
    out = tmp_path / "region.pmtiles"
    stats = basemap.extract(basemap.file_source(planet), out, UZH, maxzoom=4, dry_run=True)
    assert stats.tiles_found > 0 and not out.exists()


def test_archive_extraction_rejects_path_traversal(tmp_path):
    arc = tmp_path / "evil.tar.gz"
    payload = tmp_path / "x.pbf"
    payload.write_bytes(b"x")
    with tarfile.open(arc, "w:gz") as t:
        t.add(payload, arcname="ofm/../../escape.pbf")
    with pytest.raises(ValueError):
        basemap._extract_members(arc, "ofm/", tmp_path / "dest")


# --- serving -------------------------------------------------------------------------------------


def install_local_basemap(cfg):
    d = cfg.basemap_dir
    make_planet(d / "region.pmtiles", maxzoom=3)
    (d / "styles").mkdir(parents=True)
    style = {
        "version": 8,
        "sources": {
            "ne2_shaded": {"type": "raster", "tiles": ["https://example/{z}/{x}/{y}.png"]},
            "openmaptiles": {"type": "vector", "url": "https://__TILEJSON_DOMAIN__/planet"},
        },
        "glyphs": "https://__TILEJSON_DOMAIN__/fonts/{fontstack}/{range}.pbf",
        "sprite": "https://__TILEJSON_DOMAIN__/sprites/ofm_f384/ofm",
        "layers": [
            {"id": "relief", "type": "raster", "source": "ne2_shaded"},
            {"id": "water", "type": "fill", "source": "openmaptiles", "source-layer": "water"},
        ],
    }
    for name in basemap.STYLES.values():
        (d / "styles" / f"{name}.json").write_text(json.dumps(style))


@pytest.fixture
def make_client(tmp_path, monkeypatch):
    monkeypatch.setattr(api.time, "sleep", lambda _s: None)
    clients = []

    def make(**kw):
        cfg = Config(data_dir=tmp_path, cookie_secure=False, enrich=False, **kw)
        c = TestClient(api.create_app(cfg))
        c.__enter__()
        clients.append(c)
        auth.set_password(connect(cfg.db_path), "correct horse")
        return cfg, c

    yield make
    for c in clients:
        c.__exit__(None, None, None)


def login(c):
    assert c.post("/api/login", json={"password": "correct horse"}).status_code == 200


def test_no_basemap_and_no_remote_by_default(make_client):
    _cfg, c = make_client()
    login(c)
    assert c.get("/api/config").json()["basemap"]["mode"] == "none"
    csp = c.get("/").headers["content-security-policy"]
    assert "openfreemap" not in csp and "https:" not in csp


def test_remote_fallback_is_opt_in(make_client):
    _cfg, c = make_client(basemap_remote=True)
    login(c)
    bm = c.get("/api/config").json()["basemap"]
    assert bm["mode"] == "remote" and bm["styles"]["dark"].endswith("/styles/dark")
    assert basemap.OFM_REMOTE_ORIGIN in c.get("/").headers["content-security-policy"]


def test_local_basemap_style_and_range_requests(make_client):
    cfg, c = make_client(basemap_remote=True)  # local wins over remote
    install_local_basemap(cfg)
    assert c.get("/basemap/region.pmtiles", headers={"Range": "bytes=0-6"}).status_code == 401
    login(c)
    bm = c.get("/api/config").json()["basemap"]
    assert bm["mode"] == "local"

    style = c.get(bm["styles"]["light"]).json()
    assert list(style["sources"]) == ["openmaptiles"]  # raster relief source dropped
    assert [layer["id"] for layer in style["layers"]] == ["water"]
    assert style["sources"]["openmaptiles"]["url"] == f"pmtiles://{basemap.BASE_TOKEN}basemap/region.pmtiles"
    assert "TILEJSON_DOMAIN" not in json.dumps(style)

    r = c.get("/basemap/region.pmtiles", headers={"Range": "bytes=0-6", "Accept-Encoding": "gzip"})
    assert r.status_code == 206
    assert r.content == b"PMTiles"
    assert "content-encoding" not in r.headers  # never gzip range responses
    assert c.get("/api/basemap/style/sepia").json() == {"error": "not_found"}
