"""HTTP API and static web UI.

Errors are returned as {"error": "<code>"} with codes from ErrorCode. The server never sends
human-readable text: the web UI translates codes via its locale files (errors.<code>).
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Iterator
from contextlib import asynccontextmanager
from enum import StrEnum
from typing import Literal
from urllib.parse import unquote

from fastapi import Depends, FastAPI, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__, auth, basemap
from .config import Config
from .db import connect
from .gpx import GpxError
from .dem import ATTRIBUTION as DEM_ATTRIBUTION
from .enrich import EnrichWorker
from . import feelings, recordings, segments
from .ingest import (
    reprocess,
    MAX_FILE_BYTES, TRASH_DAYS, InboxWatcher, TrashPurger, delete_ride, import_bytes, restore_ride, trash_ride,
)
from .weather import ATTRIBUTION as WEATHER_ATTRIBUTION

log = logging.getLogger(__name__)


class ErrorCode(StrEnum):
    UNAUTHORIZED = "unauthorized"
    BAD_CREDENTIALS = "bad_credentials"
    NO_PASSWORD_SET = "no_password_set"
    NOT_FOUND = "not_found"
    INVALID_GPX = "invalid_gpx"
    FILE_TOO_LARGE = "file_too_large"
    BAD_REQUEST = "bad_request"
    BIKE_EXISTS = "bike_exists"
    TOO_MANY_ATTEMPTS = "too_many_attempts"
    PASSWORD_TOO_SHORT = "password_too_short"


class ApiError(Exception):
    def __init__(self, status: int, code: ErrorCode):
        self.status = status
        self.code = code


class LoginBody(BaseModel):
    password: str = Field(max_length=1024)


class PasswordChange(BaseModel):
    current: str = Field(max_length=1024)
    new: str = Field(max_length=1024)


class TokenBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class RidePatch(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    notes: str | None = Field(default=None, max_length=10_000)
    bike_id: int | None = None
    feeling: Literal[-1, 0, 1] | None = None
    feeling_tags: list[str] | None = Field(default=None, max_length=len(feelings.TAGS))


class RecorderBody(BaseModel):
    preferred: str | None = Field(default=None, max_length=200)


class MarkBody(BaseModel):
    start_t: float
    end_t: float
    tags: list[str] = Field(default_factory=list, max_length=len(feelings.TAGS))
    note: str | None = Field(default=None, max_length=2000)


class MarkPatch(BaseModel):
    tags: list[str] | None = Field(default=None, max_length=len(feelings.TAGS))
    note: str | None = Field(default=None, max_length=2000)


class BikeBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    kind: str | None = Field(default=None, max_length=50)


_RIDE_COLUMNS = (
    "r.id, r.name, r.activity, r.started_at, r.ended_at, r.elapsed_s, r.moving_s, r.distance_m,"
    " r.avg_moving_mps, r.max_mps, r.ascent_m, r.descent_m, r.point_count, r.speed_source,"
    " r.bike_id, b.name AS bike_name, r.notes, r.ele_source, r.enrich_error, r.creator, r.primary_id,"
    " r.feeling, r.feeling_tags,"
    " (SELECT group_concat(o.creator, ' + ') FROM rides o WHERE o.primary_id = r.id AND o.trashed_at IS NULL)"
    " AS other_creators,"
    " (SELECT COUNT(*) FROM stops s WHERE s.ride_id = r.id AND s.kind = 'stop') AS stop_count"
)

POINT_FIELDS = ["t", "lat", "lon", "ele", "speed", "dist", "moving", "segment"]

def security_headers_for(cfg: Config) -> dict[str, str]:
    # Third-party origins are allowed only when the remote basemap fallback is switched on.
    remote = f" {basemap.OFM_REMOTE_ORIGIN}" if cfg.basemap_remote else ""
    return {
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "X-Frame-Options": "DENY",
        "Content-Security-Policy": (
            "default-src 'self'; script-src 'self'; worker-src 'self' blob:; "
            f"style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:{remote}; "
            f"connect-src 'self'{remote}; font-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        ),
    }


BASEMAP_PREFIX = "/basemap/"


class _SelectiveGZip:
    """GZip everything except /basemap/: tiles are already compressed, and gzipping 206 partial
    responses would break the HTTP Range reads PMTiles relies on."""

    def __init__(self, app):
        self.app = app
        self.gzip = GZipMiddleware(app, minimum_size=1024)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith(BASEMAP_PREFIX):
            await self.app(scope, receive, send)
        else:
            await self.gzip(scope, receive, send)


class _SessionCache:
    """Short-lived cache of valid session tokens, so each tile range request does not hit SQLite.
    A logged-out session may keep map-tile access for at most TTL seconds; nothing else."""

    TTL = 60.0

    def __init__(self, open_db):
        self._open_db = open_db
        self._ok: dict[str, float] = {}

    def check(self, token: str | None) -> bool:
        if not token:
            return False
        now = time.monotonic()
        if self._ok.get(token, 0) > now:
            return True
        conn = self._open_db()
        try:
            valid = auth.session_valid(conn, token)
        finally:
            conn.close()
        if valid:
            if len(self._ok) > 1000:
                self._ok.clear()
            self._ok[token] = now + self.TTL
        else:
            self._ok.pop(token, None)
        return valid


def create_app(cfg: Config) -> FastAPI:
    def open_db() -> sqlite3.Connection:
        return connect(cfg.db_path)

    matcher = segments.MatchWorker(open_db, cfg.basemap_dir)
    worker = EnrichWorker(open_db, cfg.dem_dir, on_osm=matcher.wake) if cfg.enrich else None

    def imported() -> None:
        if worker:
            worker.wake()
        matcher.wake()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        conn = open_db()  # create/migrate the schema before serving
        try:
            done, failed = reprocess(conn, cfg.raw_dir)  # rides cleaned by an older pipeline version
            if done or failed:
                log.info("reprocessed %d ride(s) with the current cleaning; failed: %s", done, failed)
        except Exception:
            log.exception("reprocessing failed")
        try:
            linked = recordings.link_all(conn)  # rides imported before linking existed, or missed
            if linked:
                log.info("linked %d recording group(s)", linked)
            # main recordings follow the current rule (not trips the user picked by hand)
            repicked = recordings.set_preferred_recorder(conn, recordings.preferred_recorder(conn))
            if repicked:
                log.info("re-picked the main recording of %d trip(s)", repicked)
            copies = recordings.drop_all_copies(conn)  # the same recording uploaded twice
            if copies:
                log.info("moved %d duplicate export(s) to the trash", copies)
        except Exception:
            log.exception("linking recordings failed")
        finally:
            conn.close()
        purger = TrashPurger(open_db, cfg.raw_dir)
        purger.start()
        matcher.start()
        watcher = None
        if worker:
            worker.start()
        if cfg.inbox_dir:
            watcher = InboxWatcher(open_db, cfg.raw_dir, cfg.inbox_dir, cfg.inbox_poll_s, on_import=imported)
            watcher.start()
            log.info("watching inbox %s every %.0fs", cfg.inbox_dir, cfg.inbox_poll_s)
        yield
        if watcher:
            watcher.stop()
        if worker:
            worker.stop()
        purger.stop()
        matcher.stop()

    app = FastAPI(title="Stezhka", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(_SelectiveGZip)
    headers = security_headers_for(cfg)
    sessions = _SessionCache(open_db)
    throttle = auth.LoginThrottle()

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        path = request.url.path
        if path.startswith(BASEMAP_PREFIX):
            token = request.cookies.get(auth.SESSION_COOKIE)
            if not await run_in_threadpool(sessions.check, token):
                return JSONResponse({"error": ErrorCode.UNAUTHORIZED}, status_code=401, headers=headers)
        resp = await call_next(request)
        for k, v in headers.items():
            resp.headers.setdefault(k, v)
        if path.startswith("/api/"):
            resp.headers.setdefault("Cache-Control", "no-store")
        elif path.startswith(BASEMAP_PREFIX):
            resp.headers.setdefault("Cache-Control", "private, max-age=86400")
        else:
            # Web UI files are not versioned in their URLs: make the browser revalidate every time
            # (a cheap 304 via ETag) so an upgrade never mixes a new app.js with a cached old map.js.
            resp.headers.setdefault("Cache-Control", "no-cache")
        return resp

    @app.exception_handler(ApiError)
    async def api_error(_req: Request, exc: ApiError):
        return JSONResponse({"error": exc.code}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_req: Request, _exc: RequestValidationError):
        return JSONResponse({"error": ErrorCode.BAD_REQUEST}, status_code=422)

    def db() -> Iterator[sqlite3.Connection]:
        conn = open_db()
        try:
            yield conn
        finally:
            conn.close()

    def require_auth(request: Request, conn: sqlite3.Connection = Depends(db)) -> sqlite3.Connection:
        if not auth.session_valid(conn, request.cookies.get(auth.SESSION_COOKIE)):
            raise ApiError(401, ErrorCode.UNAUTHORIZED)
        return conn

    def require_upload_auth(request: Request, conn: sqlite3.Connection = Depends(db)) -> sqlite3.Connection:
        """Logged-in browser, or an upload key (Authorization: Bearer stz_up_...)."""
        if auth.session_valid(conn, request.cookies.get(auth.SESSION_COOKIE)):
            return conn
        if auth.upload_token_valid(conn, auth.bearer(request.headers.get("authorization"))):
            return conn
        raise ApiError(401, ErrorCode.UNAUTHORIZED)

    # --- session -------------------------------------------------------------------------------

    @app.get("/api/session")
    def session(request: Request, conn: sqlite3.Connection = Depends(db)):
        return {
            "authenticated": auth.session_valid(conn, request.cookies.get(auth.SESSION_COOKIE)),
            "password_set": auth.has_password(conn),
            "version": __version__,
            "source_url": cfg.source_url,  # AGPL-3.0 section 13: offered to every user
        }

    @app.post("/api/login")
    def login(body: LoginBody, request: Request, response: Response, conn: sqlite3.Connection = Depends(db)):
        if not auth.has_password(conn):
            raise ApiError(503, ErrorCode.NO_PASSWORD_SET)
        # Behind the reverse proxy, uvicorn's proxy_headers takes the client address from the
        # X-Forwarded-For header that the proxy (127.0.0.1) sets.
        ip = request.client.host if request.client else "unknown"
        with throttle.lock:  # one password check at a time: parallel guessing gains nothing
            if throttle.blocked(ip):
                raise ApiError(429, ErrorCode.TOO_MANY_ATTEMPTS)
            if not auth.check_password(conn, body.password):
                throttle.failed(ip)
                time.sleep(1.0)  # runs in a worker thread
                raise ApiError(401, ErrorCode.BAD_CREDENTIALS)
            throttle.succeeded(ip)
        set_session_cookie(response, auth.create_session(conn, request.headers.get("user-agent")))
        return {"ok": True}

    def set_session_cookie(response: Response, token: str) -> None:
        response.set_cookie(
            auth.SESSION_COOKIE,
            token,
            max_age=auth.SESSION_TTL_S,
            httponly=True,
            secure=cfg.cookie_secure,
            samesite="strict",
            path="/",
        )

    @app.post("/api/password")
    def change_password(body: PasswordChange, request: Request, response: Response, conn=Depends(require_auth)):
        ip = request.client.host if request.client else "unknown"
        with throttle.lock:  # the current password is a credential check like login
            if throttle.blocked(ip):
                raise ApiError(429, ErrorCode.TOO_MANY_ATTEMPTS)
            if not auth.check_password(conn, body.current):
                throttle.failed(ip)
                time.sleep(1.0)
                raise ApiError(401, ErrorCode.BAD_CREDENTIALS)
            throttle.succeeded(ip)
        try:
            auth.set_password(conn, body.new)  # also ends every session, including this one
        except ValueError:
            raise ApiError(400, ErrorCode.PASSWORD_TOO_SHORT) from None
        set_session_cookie(response, auth.create_session(conn, request.headers.get("user-agent")))
        return {"ok": True}

    @app.post("/api/logout")
    def logout(request: Request, response: Response, conn: sqlite3.Connection = Depends(db)):
        auth.delete_session(conn, request.cookies.get(auth.SESSION_COOKIE))
        response.delete_cookie(auth.SESSION_COOKIE, path="/")
        return {"ok": True}

    @app.get("/api/config")
    def config(_conn: sqlite3.Connection = Depends(require_auth)):
        if basemap.local_available(cfg.basemap_dir):
            bm = {
                "mode": "local",
                "styles": {theme: f"api/basemap/style/{theme}" for theme in basemap.STYLES},
                "base_token": basemap.BASE_TOKEN,
                "region": basemap.region_info(cfg.basemap_dir),
            }
        elif cfg.basemap_remote:
            bm = {
                "mode": "remote",
                "styles": {t: basemap.OFM_REMOTE_STYLE.format(style=s) for t, s in basemap.STYLES.items()},
            }
        else:
            bm = {"mode": "none", "styles": {}}
        # Data-source credits (proper names / licence notices: shown as-is, not translated).
        # Always listed: stored rides may carry DEM/weather data even if enrichment is now off.
        return {"basemap": bm, "attributions": [DEM_ATTRIBUTION, WEATHER_ATTRIBUTION]}

    @app.get("/api/basemap/style/{theme}")
    def basemap_style(theme: str, _conn: sqlite3.Connection = Depends(require_auth)):
        if theme not in basemap.STYLES or not basemap.local_available(cfg.basemap_dir):
            raise ApiError(404, ErrorCode.NOT_FOUND)
        return basemap.local_style(cfg.basemap_dir, theme)

    # --- rides ---------------------------------------------------------------------------------

    def _ride(conn: sqlite3.Connection, ride_id: int) -> dict:
        row = conn.execute(
            f"SELECT {_RIDE_COLUMNS} FROM rides r LEFT JOIN bikes b ON b.id = r.bike_id"
            " WHERE r.id = ? AND r.trashed_at IS NULL",
            (ride_id,),
        ).fetchone()
        if not row:
            raise ApiError(404, ErrorCode.NOT_FOUND)
        ride = dict(row)
        ride["feeling_tags"] = feelings.load_tags(ride["feeling_tags"])
        return ride

    @app.get("/api/rides")
    def list_rides(conn: sqlite3.Connection = Depends(require_auth)):
        rows = conn.execute(
            f"SELECT {_RIDE_COLUMNS} FROM rides r LEFT JOIN bikes b ON b.id = r.bike_id"
            " WHERE r.trashed_at IS NULL AND r.primary_id IS NULL ORDER BY r.started_at DESC"
        ).fetchall()
        rides = [dict(r) for r in rows]
        for r in rides:
            r["feeling_tags"] = feelings.load_tags(r["feeling_tags"])
        return {"rides": rides}

    @app.get("/api/rides/{ride_id}")
    def get_ride(ride_id: int, conn: sqlite3.Connection = Depends(require_auth)):
        ride = _ride(conn, ride_id)
        stops = conn.execute(
            "SELECT kind, start_idx, end_idx, start_t, end_t, lat, lon, reason FROM stops"
            " WHERE ride_id = ? ORDER BY start_t",
            (ride_id,),
        ).fetchall()
        ride["stops"] = [dict(s) for s in stops]
        w = conn.execute(
            "SELECT temperature_c, humidity_pct, wind_speed_mps, wind_dir_deg, wind_gust_mps, headwind_mps,"
            " precip_ride_mm, precip_before_mm, wetness_mm, wetness, weather_code, source"
            " FROM ride_weather WHERE ride_id = ?",
            (ride_id,),
        ).fetchone()
        ride["weather"] = dict(w) if w else None
        main = ride["primary_id"] or ride_id
        rec = conn.execute(
            "SELECT id, creator, started_at, distance_m, moving_s, avg_moving_mps, point_count, elapsed_s"
            " FROM rides WHERE (id = ? OR primary_id = ?) AND id != ? AND trashed_at IS NULL ORDER BY id",
            (main, main, ride_id),
        ).fetchall()
        ride["recordings"] = [dict(r) for r in rec]  # the trip's other recordings (other phones)
        ride["marks"] = feelings.marks(conn, ride_id)
        return ride

    @app.get("/api/rides/{ride_id}/points")
    def get_points(ride_id: int, conn: sqlite3.Connection = Depends(require_auth)):
        _ride(conn, ride_id)
        rows = conn.execute(
            "SELECT t, round(lat, 6), round(lon, 6), round(COALESCE(ele_dem, ele_smooth, ele), 1), round(speed, 2),"
            " round(dist, 1), moving, segment FROM points WHERE ride_id = ? ORDER BY idx",
            (ride_id,),
        ).fetchall()
        return {"fields": POINT_FIELDS, "rows": [list(r) for r in rows]}

    # --- the original files: one ride, or everything as a zip --------------------------------------

    def _download_name(row) -> str:
        when = time.strftime("%Y-%m-%d_%H-%M", time.localtime(row["started_at"]))
        app_name = "".join(ch for ch in (row["creator"] or "") if ch.isalnum())[:24]
        return f"stezhka_{when}_{app_name or 'ride'}_{row['id']}.gpx"

    @app.get("/api/rides/{ride_id}/usual")
    def ride_vs_usual(ride_id: int, conn: sqlite3.Connection = Depends(require_auth)):
        _ride(conn, ride_id)
        return segments.ride_vs_usual(conn, ride_id)

    @app.get("/api/rides/{ride_id}/gpx")
    def ride_gpx(ride_id: int, conn: sqlite3.Connection = Depends(require_auth)):
        row = conn.execute(
            "SELECT id, raw_path, started_at, creator FROM rides WHERE id = ? AND trashed_at IS NULL", (ride_id,)
        ).fetchone()
        path = cfg.raw_dir / row["raw_path"] if row else None
        if not row or not path.is_file():
            raise ApiError(404, ErrorCode.NOT_FOUND)
        return Response(path.read_bytes(), media_type="application/gpx+xml",
                        headers={"Content-Disposition": f'attachment; filename="{_download_name(row)}"'})

    @app.get("/api/export.zip")
    def export_zip(conn: sqlite3.Connection = Depends(require_auth)):
        import io
        import zipfile

        rows = conn.execute(
            "SELECT id, raw_path, started_at, creator FROM rides WHERE trashed_at IS NULL ORDER BY started_at"
        ).fetchall()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for r in rows:
                path = cfg.raw_dir / r["raw_path"]
                if path.is_file():
                    z.write(path, _download_name(r))
        name = time.strftime("stezhka-rides-%Y-%m-%d.zip")
        return Response(buf.getvalue(), media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.post("/api/rides", status_code=201)
    async def upload_ride(request: Request, response: Response, conn: sqlite3.Connection = Depends(require_upload_auth)):
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_FILE_BYTES:
            raise ApiError(413, ErrorCode.FILE_TOO_LARGE)
        data = bytearray()
        async for chunk in request.stream():
            data += chunk
            if len(data) > MAX_FILE_BYTES:
                raise ApiError(413, ErrorCode.FILE_TOO_LARGE)
        name = request.headers.get("x-filename")  # URL-encoded by the client (non-ASCII names)
        name = unquote(name) if name else None
        try:
            res = await run_in_threadpool(
                import_bytes, conn, cfg.raw_dir, bytes(data), name[:255] if name else None, allow_deleted=True
            )
        except GpxError:
            raise ApiError(400, ErrorCode.INVALID_GPX) from None
        if res.created:
            imported()
        else:
            response.status_code = 200
        return {"id": res.ride_id, "created": res.created}

    # --- upload keys ---------------------------------------------------------------------------

    @app.get("/api/tokens")
    def list_tokens(conn: sqlite3.Connection = Depends(require_auth)):
        rows = conn.execute("SELECT id, name, created_at, last_used_at FROM upload_tokens ORDER BY created_at").fetchall()
        return {"tokens": [dict(r) for r in rows]}

    @app.post("/api/tokens", status_code=201)
    def add_token(body: TokenBody, conn: sqlite3.Connection = Depends(require_auth)):
        token_id, token = auth.create_upload_token(conn, body.name.strip())
        return {"id": token_id, "token": token}  # the only time the key is ever shown

    @app.delete("/api/tokens/{token_id}")
    def revoke_token(token_id: int, conn: sqlite3.Connection = Depends(require_auth)):
        if conn.execute("DELETE FROM upload_tokens WHERE id = ?", (token_id,)).rowcount != 1:
            raise ApiError(404, ErrorCode.NOT_FOUND)
        return {"ok": True}

    # Deleting moves a ride to the trash (undo right away, or restore later from Settings); it is
    # erased with its GPX file after TRASH_DAYS, or at once via DELETE /api/trash/{id}.
    @app.delete("/api/rides/{ride_id}")
    def remove_ride(ride_id: int, conn: sqlite3.Connection = Depends(require_auth)):
        if not trash_ride(conn, ride_id):
            raise ApiError(404, ErrorCode.NOT_FOUND)
        return {"ok": True, "trash_days": TRASH_DAYS}

    @app.get("/api/trash")
    def list_trash(conn: sqlite3.Connection = Depends(require_auth)):
        rows = conn.execute(
            "SELECT id, name, started_at, distance_m, creator, trashed_at, trashed_at + ? AS purge_at"
            " FROM rides r WHERE trashed_at IS NOT NULL"
            # a recording trashed together with its main ride is part of that entry
            " AND NOT EXISTS (SELECT 1 FROM rides m WHERE m.id = r.primary_id AND m.trashed_at IS NOT NULL)"
            " ORDER BY trashed_at DESC",
            (TRASH_DAYS * 86400,),
        ).fetchall()
        return {"rides": [dict(r) for r in rows], "trash_days": TRASH_DAYS}

    @app.post("/api/trash/{ride_id}/restore")
    def restore_from_trash(ride_id: int, conn: sqlite3.Connection = Depends(require_auth)):
        if not restore_ride(conn, ride_id):
            raise ApiError(404, ErrorCode.NOT_FOUND)
        return {"ok": True}

    @app.delete("/api/trash/{ride_id}")
    def erase_from_trash(ride_id: int, conn: sqlite3.Connection = Depends(require_auth)):
        trashed = conn.execute("SELECT trashed_at FROM rides WHERE id = ? AND trashed_at IS NOT NULL", (ride_id,)).fetchone()
        if not trashed:
            raise ApiError(404, ErrorCode.NOT_FOUND)
        together = conn.execute(
            "SELECT id FROM rides WHERE primary_id = ? AND trashed_at = ?", (ride_id, trashed["trashed_at"])
        ).fetchall()
        for r in together:
            delete_ride(conn, cfg.raw_dir, r["id"])
        delete_ride(conn, cfg.raw_dir, ride_id)
        return {"ok": True}

    # --- marks: stretches or spots of a ride with reasons and a note -----------------------------

    def _mark_tags(tags: list[str] | None) -> list[str]:
        try:
            return feelings.clean_tags(tags)
        except ValueError:
            raise ApiError(400, ErrorCode.BAD_REQUEST) from None

    def _mark(conn: sqlite3.Connection, mark_id: int) -> sqlite3.Row:
        row = conn.execute(
            "SELECT m.id, m.ride_id FROM ride_marks m JOIN rides r ON r.id = m.ride_id"
            " WHERE m.id = ? AND r.trashed_at IS NULL",
            (mark_id,),
        ).fetchone()
        if not row:
            raise ApiError(404, ErrorCode.NOT_FOUND)
        return row

    @app.post("/api/rides/{ride_id}/marks", status_code=201)
    def add_mark(ride_id: int, body: MarkBody, conn: sqlite3.Connection = Depends(require_auth)):
        ride = _ride(conn, ride_id)
        lo, hi = sorted((body.start_t, body.end_t))
        if lo < ride["started_at"] - 1 or hi > ride["ended_at"] + 1:
            raise ApiError(400, ErrorCode.BAD_REQUEST)
        note = body.note.strip() if body.note else None
        mark_id = feelings.add_mark(conn, ride_id, lo, hi, _mark_tags(body.tags), note or None)
        return {"id": mark_id}

    @app.patch("/api/marks/{mark_id}")
    def edit_mark(mark_id: int, body: MarkPatch, conn: sqlite3.Connection = Depends(require_auth)):
        _mark(conn, mark_id)
        if "tags" in body.model_fields_set:
            conn.execute("UPDATE ride_marks SET tags = ? WHERE id = ?", (feelings.dump_tags(_mark_tags(body.tags)), mark_id))
        if "note" in body.model_fields_set:
            note = body.note.strip() if body.note else None
            conn.execute("UPDATE ride_marks SET note = ? WHERE id = ?", (note or None, mark_id))
        return {"ok": True}

    @app.delete("/api/marks/{mark_id}")
    def remove_mark(mark_id: int, conn: sqlite3.Connection = Depends(require_auth)):
        _mark(conn, mark_id)
        conn.execute("DELETE FROM ride_marks WHERE id = ?", (mark_id,))
        return {"ok": True}

    # --- recordings of one trip ----------------------------------------------------------------

    # --- roads: ridden segments with how often and how fast ----------------------------------------

    @app.get("/api/roads")
    def roads(conn: sqlite3.Connection = Depends(require_auth)):
        pending = conn.execute(
            "SELECT COUNT(*) FROM rides WHERE match_version < ? AND trashed_at IS NULL", (segments.MATCH_VERSION,)
        ).fetchone()[0]
        return {
            "segments": segments.road_stats(conn),
            "marks": segments.road_marks(conn),
            "pending": pending,
            "available": segments.basemap_file(cfg.basemap_dir).is_file(),
        }

    @app.get("/api/recorders")
    def list_recorders(conn: sqlite3.Connection = Depends(require_auth)):
        return {"preferred": recordings.preferred_recorder(conn), "recorders": recordings.recorders(conn)}

    @app.put("/api/recorders")
    def prefer_recorder(body: RecorderBody, conn: sqlite3.Connection = Depends(require_auth)):
        return {"ok": True, "changed": recordings.set_preferred_recorder(conn, body.preferred or None)}

    @app.post("/api/rides/{ride_id}/main")
    def make_main(ride_id: int, conn: sqlite3.Connection = Depends(require_auth)):
        _ride(conn, ride_id)
        recordings.set_main(conn, ride_id)
        return {"ok": True}

    @app.post("/api/rides/{ride_id}/split")
    def split_recording(ride_id: int, conn: sqlite3.Connection = Depends(require_auth)):
        _ride(conn, ride_id)
        if not recordings.split(conn, ride_id):
            raise ApiError(404, ErrorCode.NOT_FOUND)
        return {"ok": True}

    @app.patch("/api/rides/{ride_id}")
    def patch_ride(ride_id: int, body: RidePatch, conn: sqlite3.Connection = Depends(require_auth)):
        _ride(conn, ride_id)
        fields = {k: getattr(body, k) for k in body.model_fields_set}
        if "bike_id" in fields and fields["bike_id"] is not None:
            if not conn.execute("SELECT 1 FROM bikes WHERE id = ?", (fields["bike_id"],)).fetchone():
                raise ApiError(404, ErrorCode.NOT_FOUND)
        if "name" in fields:
            fields["name_edited"] = 1
        if "feeling_tags" in fields:
            try:
                fields["feeling_tags"] = feelings.dump_tags(feelings.clean_tags(fields["feeling_tags"]))
            except ValueError:
                raise ApiError(400, ErrorCode.BAD_REQUEST) from None
        if "feeling" in fields and fields["feeling"] is None:
            fields["feeling_tags"] = None  # "not rated" has no reasons
        if fields:
            sets = ", ".join(f"{k} = :{k}" for k in fields)  # keys come from the pydantic model only
            conn.execute(f"UPDATE rides SET {sets} WHERE id = :id", {**fields, "id": ride_id})
        return _ride(conn, ride_id)

    # --- bikes ---------------------------------------------------------------------------------

    @app.get("/api/bikes")
    def list_bikes(conn: sqlite3.Connection = Depends(require_auth)):
        rows = conn.execute("SELECT id, name, kind, retired FROM bikes ORDER BY retired, name").fetchall()
        return {"bikes": [dict(r) for r in rows]}

    @app.post("/api/bikes", status_code=201)
    def add_bike(body: BikeBody, conn: sqlite3.Connection = Depends(require_auth)):
        try:
            cur = conn.execute("INSERT INTO bikes(name, kind) VALUES (?, ?)", (body.name.strip(), body.kind))
        except sqlite3.IntegrityError:
            raise ApiError(409, ErrorCode.BIKE_EXISTS) from None
        return {"id": cur.lastrowid}

    @app.api_route("/api/{_rest:path}", methods=["GET", "POST", "PATCH", "PUT", "DELETE"])
    def unknown_api(_rest: str):
        raise ApiError(404, ErrorCode.NOT_FOUND)

    # Basemap files (PMTiles, fonts, sprites). Auth is enforced by the middleware above;
    # StaticFiles answers HTTP Range requests with 206, which PMTiles needs.
    app.mount(BASEMAP_PREFIX.rstrip("/"), StaticFiles(directory=cfg.basemap_dir, check_dir=False), name="basemap")
    if cfg.web_dir.is_dir():
        app.mount("/", StaticFiles(directory=cfg.web_dir, html=True), name="web")
    return app
