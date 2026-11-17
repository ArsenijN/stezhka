import pytest
from fastapi.testclient import TestClient
from synth import commute

from stezhka import api, auth
from stezhka.config import Config
from stezhka.db import connect


@pytest.fixture
def cfg(tmp_path):
    return Config(data_dir=tmp_path, cookie_secure=False, enrich=False)


@pytest.fixture
def client(cfg, monkeypatch):
    monkeypatch.setattr(api.time, "sleep", lambda _s: None)
    with TestClient(api.create_app(cfg)) as c:
        yield c


def login(client, cfg, password="correct horse"):
    auth.set_password(connect(cfg.db_path), password)
    r = client.post("/api/login", json={"password": password})
    assert r.status_code == 200


def test_everything_requires_login(client):
    for path in ("/api/rides", "/api/rides/1", "/api/rides/1/points", "/api/bikes", "/api/config"):
        r = client.get(path)
        assert r.status_code == 401 and r.json() == {"error": "unauthorized"}, path
    assert client.post("/api/rides", content=commute().gpx()).status_code == 401


def test_login_flow(client, cfg):
    assert client.post("/api/login", json={"password": "whatever1"}).json() == {"error": "no_password_set"}
    auth.set_password(connect(cfg.db_path), "correct horse")
    r = client.post("/api/login", json={"password": "wrong pass"})
    assert r.status_code == 401 and r.json()["error"] == "bad_credentials"
    r = client.post("/api/login", json={"password": "correct horse"})
    assert r.status_code == 200
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert client.get("/api/session").json()["authenticated"] is True
    client.post("/api/logout")
    assert client.get("/api/rides").status_code == 401


def test_upload_list_detail_points_patch(client, cfg):
    login(client, cfg)
    r = client.post("/api/rides", content=commute().gpx(), headers={"x-filename": "morning.gpx"})
    assert r.status_code == 201
    rid = r.json()["id"]
    r = client.post("/api/rides", content=commute().gpx())
    assert r.status_code == 200 and r.json() == {"id": rid, "created": False}

    rides = client.get("/api/rides").json()["rides"]
    assert len(rides) == 1 and rides[0]["stop_count"] == 1

    detail = client.get(f"/api/rides/{rid}").json()
    assert [s["kind"] for s in detail["stops"]] == ["stop"]

    pts = client.get(f"/api/rides/{rid}/points").json()
    assert pts["fields"] == api.POINT_FIELDS
    assert len(pts["rows"]) == detail["point_count"]

    bike = client.post("/api/bikes", json={"name": "CUBE", "kind": "mtb"}).json()["id"]
    assert client.post("/api/bikes", json={"name": "CUBE"}).json() == {"error": "bike_exists"}
    r = client.patch(f"/api/rides/{rid}", json={"bike_id": bike, "notes": "puddles"})
    assert r.json()["bike_name"] == "CUBE" and r.json()["notes"] == "puddles"
    assert client.patch(f"/api/rides/{rid}", json={"bike_id": 999}).json() == {"error": "not_found"}


def test_bad_input_returns_codes(client, cfg):
    login(client, cfg)
    assert client.post("/api/rides", content=b"<nope/>").json() == {"error": "invalid_gpx"}
    assert client.get("/api/rides/42").json() == {"error": "not_found"}
    assert client.get("/api/nothing-here").json() == {"error": "not_found"}
    assert client.patch("/api/rides/1", json={"name": 5}).json() == {"error": "bad_request"}


def test_security_headers_and_static(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"
