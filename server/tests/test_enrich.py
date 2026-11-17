"""DEM sampling, weather summaries and the enrichment pipeline, all offline."""

import math

import numpy as np
import pytest
import tifffile
from fastapi.testclient import TestClient
from synth import LAT0, T0, Builder

from stezhka import api, auth, enrich, weather
from stezhka.config import Config
from stezhka.db import connect
from stezhka.dem import Dem, decode_float_predictor, smooth_by_distance, tile_name
from stezhka.ingest import import_bytes

N = 3600  # GLO-30 grid for latitudes below 50°


def ele_fn(lat, lon):
    """Synthetic terrain: 100 m at LAT0, rising 1 m per ~11 m going north (≈ 9 % grade)."""
    return 100.0 + (lat - LAT0) * 10_000.0 + (lon - 22.0) * 0.0


def write_tile(dem_dir, lat0=48, lon0=22, fn=ele_fn):
    """Uncompressed tiled GeoTIFF with GLO-30 geo tags (PixelIsPoint, pixel (0,0) at NW corner)."""
    rows = np.arange(N)[:, None]
    cols = np.arange(N)[None, :]
    lat = (lat0 + 1) - rows / N
    lon = lon0 + cols / N
    data = fn(lat, lon).astype(np.float32)
    dem_dir.mkdir(parents=True, exist_ok=True)
    tifffile.imwrite(
        dem_dir / f"{tile_name(lat0, lon0)}.tif", data, tile=(1024, 1024),
        extratags=[(33550, 12, 3, (1 / N, 1 / N, 0.0), False), (33922, 12, 6, (0, 0, 0, lon0, lat0 + 1, 0), False)],
    )


def test_tile_names():
    assert tile_name(48, 22) == "Copernicus_DSM_COG_10_N48_00_E022_00_DEM"
    assert tile_name(-3, -60) == "Copernicus_DSM_COG_10_S03_00_W060_00_DEM"


def test_float_predictor_decoding_roundtrip():
    x = (np.random.default_rng(1).random((4, 6)) * 2000).astype(np.float32)
    planes = x.astype(">f4").view(np.uint8).reshape(4, 6, 4).transpose(0, 2, 1).reshape(4, 24)
    encoded = np.diff(planes, axis=1, prepend=0).astype(np.uint8)
    assert np.array_equal(decode_float_predictor(encoded.tobytes(), 4, 6), x)


def test_bilinear_sampling_and_missing_tiles(tmp_path):
    write_tile(tmp_path)
    dem = Dem(tmp_path, network=False)
    assert dem.elevation(LAT0, 22.29) == pytest.approx(100.0, abs=0.05)
    assert dem.elevation(LAT0 + 0.001, 22.29) == pytest.approx(110.0, abs=0.05)  # between grid nodes
    assert dem.elevation(48.0005, 22.5) == pytest.approx(ele_fn(48.0005, 22.5), abs=0.05)  # southern edge
    assert dem.elevation(10.0, 10.0) is None  # no tile, network off


def test_smoothing_has_no_edge_bias():
    dist = [float(i) for i in range(100)]
    vals = [d * 0.1 for d in dist]  # perfectly straight slope must stay straight
    out = smooth_by_distance(vals, dist, 10)
    assert out == pytest.approx(vals)
    assert smooth_by_distance([None, 1.0, None], [0, 1, 2]) == [None, 1.0, None]


# --- weather -------------------------------------------------------------------------------------


def hours(start, n, **over):
    base = {"temperature_c": 12.0, "humidity_pct": 70.0, "precipitation_mm": 0.0, "rain_mm": 0.0,
            "snowfall_cm": 0.0, "weather_code": 1, "wind_speed_mps": 4.0, "wind_dir_deg": 0.0, "wind_gust_mps": 7.0}
    first = int(start // 3600 * 3600)
    return [{**base, **over, "t": float(first + i * 3600)} for i in range(n)]


def test_wetness_weights_and_classes():
    assert weather.wetness_weight(0) == 1 and weather.wetness_weight(3 * 3600) == 1
    assert weather.wetness_weight(7.5 * 3600) == pytest.approx(0.5)
    assert weather.wetness_weight(13 * 3600) == 0
    assert [weather.wetness_class(x) for x in (0, 0.5, 2, 10)] == ["dry", "damp", "wet", "soaked"]


def ride_points(heading="north"):
    b = Builder().ride(600, 5.0, heading=heading)
    return [{"t": T0 + p.t, "lat": LAT0 + p.north / 111_195, "lon": 22.3 + p.east / 73_500, "segment": 0, "moving": 1}
            for p in b.points]


def test_headwind_sign_and_rain_before_ride():
    pts = ride_points("north")
    start = pts[0]["t"]
    hs = hours(start - 13 * 3600, 16, wind_dir_deg=0.0)  # wind FROM the north, rider going north
    for h in hs:
        if start - 2 * 3600 <= h["t"] <= start:  # 2 mm/h during the 2 h before the ride
            h["precipitation_mm"] = 2.0
    w = weather.summarize(hs, pts, "archive")
    assert w.headwind_mps == pytest.approx(4.0, abs=0.1)
    # ride starts 07:30: the hours ending 06:00 and 07:00 lie in the 2 h window -> 2 x 2 mm
    assert w.precip_before_mm == pytest.approx(4.0)
    assert w.wetness == "soaked"
    assert w.precip_ride_mm == 0

    hs = hours(start - 13 * 3600, 16, wind_dir_deg=180.0)  # from the south: tailwind
    assert weather.summarize(hs, pts, "archive").headwind_mps == pytest.approx(-4.0, abs=0.1)


def test_weather_is_cached_per_cell(tmp_path, monkeypatch):
    conn = connect(tmp_path / "db.sqlite3")
    calls = []

    def fake_fetch(lat, lon, start, end, now=None):
        calls.append((lat, lon))
        return hours(start, int((end - start) // 3600) + 2), "archive"

    monkeypatch.setattr(weather, "fetch_hours", fake_fetch)
    pts = ride_points()
    weather.ride_weather(conn, 48.6249, 22.2911, pts)
    weather.ride_weather(conn, 48.6301, 22.2999, pts)  # same 0.1° cell, same day
    assert calls == [(48.6, 22.3)]  # one request, coarse coordinates only


# --- pipeline ------------------------------------------------------------------------------------


@pytest.fixture
def offline_weather(monkeypatch):
    def fake_fetch(lat, lon, start, end, now=None):
        return hours(start, int((end - start) // 3600) + 2, precipitation_mm=0.3), "forecast"

    monkeypatch.setattr(weather, "fetch_hours", fake_fetch)


def climb_ride():
    """10 minutes north at 5 m/s = 3 km = 0.02698° -> +269.8 m on the synthetic terrain."""
    return Builder().ride(600, 5.0, heading="north").gpx()


def test_enrich_ride_end_to_end(tmp_path, offline_weather):
    write_tile(tmp_path / "dem")
    conn = connect(tmp_path / "db.sqlite3")
    rid = import_bytes(conn, tmp_path / "raw", climb_ride(), "climb.gpx").ride_id
    assert enrich.pending_rides(conn) == [rid]
    assert enrich.enrich_ride(conn, Dem(tmp_path / "dem", network=False), rid) == []
    r = conn.execute("SELECT ele_source, ascent_m, descent_m, dem_version, weather_version FROM rides").fetchone()
    assert r["ele_source"] == "dem"
    assert r["ascent_m"] == pytest.approx(269.8, rel=0.03) and r["descent_m"] < 2
    w = conn.execute("SELECT * FROM ride_weather").fetchone()
    assert w["source"] == "forecast" and w["headwind_mps"] == pytest.approx(4.0, abs=0.1)
    # the synthetic ride is weeks old, so provisional forecast weather is queued for an archive refresh
    assert enrich.pending_rides(conn) == [rid]
    conn.execute("UPDATE ride_weather SET source = 'archive'")
    assert enrich.pending_rides(conn) == []


def test_enrichment_failure_is_recorded_and_backed_off(tmp_path, monkeypatch):
    def broken(*_a, **_k):
        raise OSError("network down")

    monkeypatch.setattr(weather, "fetch_hours", broken)
    conn = connect(tmp_path / "db.sqlite3")
    rid = import_bytes(conn, tmp_path / "raw", climb_ride(), None).ride_id
    errors = enrich.enrich_ride(conn, Dem(tmp_path / "dem", network=False), rid)
    assert errors == ["weather: network down"]
    assert "network down" in conn.execute("SELECT enrich_error FROM rides").fetchone()[0]
    assert enrich.pending_rides(conn) == []  # retried only after RETRY_S


def test_api_exposes_dem_profile_and_weather(tmp_path, offline_weather, monkeypatch):
    monkeypatch.setattr(api.time, "sleep", lambda _s: None)
    write_tile(tmp_path / "dem")
    cfg = Config(data_dir=tmp_path, cookie_secure=False, enrich=False)
    conn = connect(cfg.db_path)
    rid = import_bytes(conn, cfg.raw_dir, climb_ride(), None).ride_id
    enrich.enrich_ride(conn, Dem(cfg.dem_dir, network=False), rid)
    auth.set_password(conn, "correct horse")
    with TestClient(api.create_app(cfg)) as c:
        c.post("/api/login", json={"password": "correct horse"})
        ride = c.get(f"/api/rides/{rid}").json()
        assert ride["ele_source"] == "dem" and ride["weather"]["wetness"] in ("damp", "wet", "soaked")
        pts = c.get(f"/api/rides/{rid}/points").json()
        ele = [row[pts["fields"].index("ele")] for row in pts["rows"]]
        assert ele[-1] - ele[0] == pytest.approx(269.8, rel=0.03)
        assert not math.isnan(ele[0])
