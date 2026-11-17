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
from urllib.parse import unquote

from fastapi import Depends, FastAPI, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__, auth
from .config import Config
from .db import connect
from .gpx import GpxError
from .ingest import MAX_FILE_BYTES, InboxWatcher, import_bytes

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


class ApiError(Exception):
    def __init__(self, status: int, code: ErrorCode):
        self.status = status
        self.code = code


class LoginBody(BaseModel):
    password: str = Field(max_length=1024)


class RidePatch(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    notes: str | None = Field(default=None, max_length=10_000)
    bike_id: int | None = None


class BikeBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    kind: str | None = Field(default=None, max_length=50)


_RIDE_COLUMNS = (
    "r.id, r.name, r.activity, r.started_at, r.ended_at, r.elapsed_s, r.moving_s, r.distance_m,"
    " r.avg_moving_mps, r.max_mps, r.ascent_m, r.descent_m, r.point_count, r.speed_source,"
    " r.bike_id, b.name AS bike_name, r.notes,"
    " (SELECT COUNT(*) FROM stops s WHERE s.ride_id = r.id AND s.kind = 'stop') AS stop_count"
)

POINT_FIELDS = ["t", "lat", "lon", "ele", "speed", "dist", "moving", "segment"]

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; worker-src 'self' blob:; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob: https:; "
        "connect-src 'self' https:; font-src 'self' https:; frame-ancestors 'none'; base-uri 'none'"
    ),
}


def create_app(cfg: Config) -> FastAPI:
    def open_db() -> sqlite3.Connection:
        return connect(cfg.db_path)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        open_db().close()  # create/migrate the schema before serving
        watcher = None
        if cfg.inbox_dir:
            watcher = InboxWatcher(open_db, cfg.raw_dir, cfg.inbox_dir, cfg.inbox_poll_s)
            watcher.start()
            log.info("watching inbox %s every %.0fs", cfg.inbox_dir, cfg.inbox_poll_s)
        yield
        if watcher:
            watcher.stop()

    app = FastAPI(title="Stezhka", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        for k, v in _SECURITY_HEADERS.items():
            resp.headers.setdefault(k, v)
        if request.url.path.startswith("/api/"):
            resp.headers.setdefault("Cache-Control", "no-store")
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

    # --- session -------------------------------------------------------------------------------

    @app.get("/api/session")
    def session(request: Request, conn: sqlite3.Connection = Depends(db)):
        return {
            "authenticated": auth.session_valid(conn, request.cookies.get(auth.SESSION_COOKIE)),
            "password_set": auth.has_password(conn),
            "version": __version__,
        }

    @app.post("/api/login")
    def login(body: LoginBody, request: Request, response: Response, conn: sqlite3.Connection = Depends(db)):
        if not auth.has_password(conn):
            raise ApiError(503, ErrorCode.NO_PASSWORD_SET)
        if not auth.check_password(conn, body.password):
            time.sleep(1.0)  # slows down guessing; runs in a worker thread
            raise ApiError(401, ErrorCode.BAD_CREDENTIALS)
        token = auth.create_session(conn, request.headers.get("user-agent"))
        response.set_cookie(
            auth.SESSION_COOKIE,
            token,
            max_age=auth.SESSION_TTL_S,
            httponly=True,
            secure=cfg.cookie_secure,
            samesite="strict",
            path="/",
        )
        return {"ok": True}

    @app.post("/api/logout")
    def logout(request: Request, response: Response, conn: sqlite3.Connection = Depends(db)):
        auth.delete_session(conn, request.cookies.get(auth.SESSION_COOKIE))
        response.delete_cookie(auth.SESSION_COOKIE, path="/")
        return {"ok": True}

    @app.get("/api/config")
    def config(_conn: sqlite3.Connection = Depends(require_auth)):
        return {"basemap_style": cfg.basemap_style}

    # --- rides ---------------------------------------------------------------------------------

    def _ride(conn: sqlite3.Connection, ride_id: int) -> dict:
        row = conn.execute(
            f"SELECT {_RIDE_COLUMNS} FROM rides r LEFT JOIN bikes b ON b.id = r.bike_id WHERE r.id = ?",
            (ride_id,),
        ).fetchone()
        if not row:
            raise ApiError(404, ErrorCode.NOT_FOUND)
        return dict(row)

    @app.get("/api/rides")
    def list_rides(conn: sqlite3.Connection = Depends(require_auth)):
        rows = conn.execute(
            f"SELECT {_RIDE_COLUMNS} FROM rides r LEFT JOIN bikes b ON b.id = r.bike_id ORDER BY r.started_at DESC"
        ).fetchall()
        return {"rides": [dict(r) for r in rows]}

    @app.get("/api/rides/{ride_id}")
    def get_ride(ride_id: int, conn: sqlite3.Connection = Depends(require_auth)):
        ride = _ride(conn, ride_id)
        stops = conn.execute(
            "SELECT kind, start_idx, end_idx, start_t, end_t, lat, lon, reason FROM stops"
            " WHERE ride_id = ? ORDER BY start_t",
            (ride_id,),
        ).fetchall()
        ride["stops"] = [dict(s) for s in stops]
        return ride

    @app.get("/api/rides/{ride_id}/points")
    def get_points(ride_id: int, conn: sqlite3.Connection = Depends(require_auth)):
        _ride(conn, ride_id)
        rows = conn.execute(
            "SELECT t, round(lat, 6), round(lon, 6), round(COALESCE(ele_smooth, ele), 1), round(speed, 2),"
            " round(dist, 1), moving, segment FROM points WHERE ride_id = ? ORDER BY idx",
            (ride_id,),
        ).fetchall()
        return {"fields": POINT_FIELDS, "rows": [list(r) for r in rows]}

    @app.post("/api/rides", status_code=201)
    async def upload_ride(request: Request, response: Response, conn: sqlite3.Connection = Depends(require_auth)):
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
            res = await run_in_threadpool(import_bytes, conn, cfg.raw_dir, bytes(data), name[:255] if name else None)
        except GpxError:
            raise ApiError(400, ErrorCode.INVALID_GPX) from None
        if not res.created:
            response.status_code = 200
        return {"id": res.ride_id, "created": res.created}

    @app.patch("/api/rides/{ride_id}")
    def patch_ride(ride_id: int, body: RidePatch, conn: sqlite3.Connection = Depends(require_auth)):
        _ride(conn, ride_id)
        fields = {k: getattr(body, k) for k in body.model_fields_set}
        if "bike_id" in fields and fields["bike_id"] is not None:
            if not conn.execute("SELECT 1 FROM bikes WHERE id = ?", (fields["bike_id"],)).fetchone():
                raise ApiError(404, ErrorCode.NOT_FOUND)
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

    if cfg.web_dir.is_dir():
        app.mount("/", StaticFiles(directory=cfg.web_dir, html=True), name="web")
    return app
