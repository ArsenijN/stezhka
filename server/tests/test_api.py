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
    for path in ("/api/rides", "/api/rides/1", "/api/rides/1/points", "/api/bikes", "/api/config", "/api/trash"):
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


def test_login_lockout_per_address(client, cfg):
    auth.set_password(connect(cfg.db_path), "correct horse")
    for _ in range(auth.LoginThrottle.PER_IP_MAX):
        assert client.post("/api/login", json={"password": "wrong guess!"}).status_code == 401
    r = client.post("/api/login", json={"password": "correct horse"})  # even the right one
    assert r.status_code == 429 and r.json() == {"error": "too_many_attempts"}


def test_login_throttle_window_and_reset():
    now = [0.0]
    th = auth.LoginThrottle(clock=lambda: now[0])
    for _ in range(th.PER_IP_MAX - 1):
        th.failed("1.2.3.4")
    assert not th.blocked("1.2.3.4")
    th.succeeded("1.2.3.4")  # a successful login clears that address
    for _ in range(th.PER_IP_MAX):
        th.failed("1.2.3.4")
    assert th.blocked("1.2.3.4") and not th.blocked("5.6.7.8")
    now[0] += th.WINDOW_S + 1
    assert not th.blocked("1.2.3.4")
    for i in range(th.GLOBAL_MAX):  # distributed guessing locks everyone
        th.failed(f"10.0.0.{i}")
    assert th.blocked("5.6.7.8")


def test_short_passwords_rejected(cfg):
    with pytest.raises(ValueError):
        auth.set_password(connect(cfg.db_path), "short-pass")


def test_delete_moves_to_trash_and_undo_restores(client, cfg):
    from stezhka.ingest import import_bytes

    login(client, cfg)
    data = commute().gpx()
    rid = client.post("/api/rides", content=data).json()["id"]
    raw = list(cfg.raw_dir.rglob("*.gpx"))
    assert len(raw) == 1
    assert client.delete(f"/api/rides/{rid}").json() == {"ok": True, "trash_days": 30}
    # gone from every view, but the GPX file stays while it is in the trash
    assert client.get(f"/api/rides/{rid}").status_code == 404
    assert client.get(f"/api/rides/{rid}/points").status_code == 404
    assert client.get("/api/rides").json()["rides"] == []
    assert client.patch(f"/api/rides/{rid}", json={"notes": "x"}).status_code == 404
    assert client.delete(f"/api/rides/{rid}").json() == {"error": "not_found"}
    assert raw[0].exists()
    trash = client.get("/api/trash").json()
    assert [r["id"] for r in trash["rides"]] == [rid] and trash["trash_days"] == 30
    assert trash["rides"][0]["purge_at"] > trash["rides"][0]["trashed_at"]

    # the inbox re-scanning its folder must not bring it back
    res = import_bytes(connect(cfg.db_path), cfg.raw_dir, data, "inbox.gpx")
    assert res.skipped_deleted and not res.created

    assert client.post(f"/api/trash/{rid}/restore").json() == {"ok": True}
    assert client.get(f"/api/rides/{rid}").status_code == 200
    assert client.get("/api/trash").json()["rides"] == []
    assert client.post(f"/api/trash/{rid}/restore").json() == {"error": "not_found"}


def test_upload_restores_a_trashed_ride(client, cfg):
    login(client, cfg)
    data = commute().gpx()
    rid = client.post("/api/rides", content=data).json()["id"]
    client.delete(f"/api/rides/{rid}")
    r = client.post("/api/rides", content=data)  # an explicit upload brings it back, same id
    assert r.status_code == 201 and r.json() == {"id": rid, "created": True}
    assert client.get(f"/api/rides/{rid}").status_code == 200


def test_erase_from_trash_tombstones_inbox_but_not_explicit_upload(client, cfg):
    from stezhka.ingest import import_bytes

    login(client, cfg)
    data = commute().gpx()
    rid = client.post("/api/rides", content=data).json()["id"]
    raw = list(cfg.raw_dir.rglob("*.gpx"))
    # only rides in the trash can be erased
    assert client.delete(f"/api/trash/{rid}").json() == {"error": "not_found"}
    client.delete(f"/api/rides/{rid}")
    assert client.delete(f"/api/trash/{rid}").json() == {"ok": True}
    assert not raw[0].exists()
    assert client.get("/api/trash").json()["rides"] == []

    conn = connect(cfg.db_path)
    res = import_bytes(conn, cfg.raw_dir, data, "inbox.gpx")
    assert res.skipped_deleted and not res.created
    assert conn.execute("SELECT COUNT(*) FROM points").fetchone()[0] == 0

    r = client.post("/api/rides", content=data)
    assert r.status_code == 201 and r.json()["created"] is True


def test_trash_is_purged_after_trash_days(client, cfg):
    from stezhka.ingest import TRASH_DAYS, purge_trash

    login(client, cfg)
    from dataclasses import replace

    from synth import to_gpx

    def ride_at(hour):  # separate rides an hour apart (not recordings of one trip)
        return to_gpx([replace(p, t=p.t + 3600 * hour) for p in commute().points])

    keep = client.post("/api/rides", content=ride_at(0)).json()["id"]
    old = client.post("/api/rides", content=ride_at(1)).json()["id"]
    recent = client.post("/api/rides", content=ride_at(2)).json()["id"]
    client.delete(f"/api/rides/{old}")
    client.delete(f"/api/rides/{recent}")
    conn = connect(cfg.db_path)
    conn.execute("UPDATE rides SET trashed_at = trashed_at - ? WHERE id = ?", ((TRASH_DAYS + 1) * 86400, old))
    assert purge_trash(conn, cfg.raw_dir) == 1
    assert [r["id"] for r in client.get("/api/trash").json()["rides"]] == [recent]
    assert [r["id"] for r in client.get("/api/rides").json()["rides"]] == [keep]
    assert len(list(cfg.raw_dir.rglob("*.gpx"))) == 2


def test_change_password(client, cfg):
    login(client, cfg)
    assert client.post("/api/password", json={"current": "wrong one!!", "new": "x" * 12}).json() == {"error": "bad_credentials"}
    assert client.post("/api/password", json={"current": "correct horse", "new": "short"}).json() == {"error": "password_too_short"}
    assert client.post("/api/password", json={"current": "correct horse", "new": "battery staple 42"}).json() == {"ok": True}
    assert client.get("/api/rides").status_code == 200  # this browser stays signed in
    client.post("/api/logout")
    assert client.post("/api/login", json={"password": "correct horse"}).status_code == 401
    assert client.post("/api/login", json={"password": "battery staple 42"}).status_code == 200


def test_web_ui_files_are_revalidated(client):
    # An upgrade must never mix a new app.js with a cached old module (seen in v0.4: showCompare
    # missing from a cached map.js). no-cache = the browser asks every time; unchanged files get 304.
    r = client.get("/js/map.js")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-cache" and "etag" in r.headers
    assert client.get("/js/map.js", headers={"If-None-Match": r.headers["etag"]}).status_code == 304


def test_source_link_offered_before_login(client):
    # AGPL-3.0 section 13: every user of the service must be offered the source
    assert client.get("/api/session").json()["source_url"].startswith("https://")


def test_upload_key_can_only_upload(client, cfg):
    login(client, cfg)
    created = client.post("/api/tokens", json={"name": "iPhone shortcut"}).json()
    key = created["token"]
    assert key.startswith("stz_up_")
    listed = client.get("/api/tokens").json()["tokens"]
    assert [t["name"] for t in listed] == ["iPhone shortcut"] and "token" not in listed[0]
    client.post("/api/logout")

    h = {"Authorization": f"Bearer {key}", "X-Filename": "2026-11-17T15%3A14%3A12%2B0200.gpx"}
    r = client.post("/api/rides", content=commute().gpx(), headers=h)
    assert r.status_code == 201 and r.json()["created"] is True
    assert connect(cfg.db_path).execute("SELECT source_name FROM rides").fetchone()[0] == "2026-11-17T15:14:12+0200.gpx"
    # the key opens nothing else
    for path in ("/api/rides", "/api/tokens", "/api/config", "/api/bikes"):
        assert client.get(path, headers=h).status_code == 401, path
    assert client.post("/api/rides", content=commute().gpx(), headers={"Authorization": "Bearer stz_up_wrong"}).status_code == 401

    login(client, cfg)
    assert connect(cfg.db_path).execute("SELECT last_used_at FROM upload_tokens").fetchone()[0] is not None
    assert client.delete(f"/api/tokens/{created['id']}").json() == {"ok": True}
    client.post("/api/logout")
    assert client.post("/api/rides", content=commute().gpx(), headers=h).status_code == 401  # revoked
