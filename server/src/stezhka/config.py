"""Runtime configuration from environment variables (systemd `Environment=` / `EnvironmentFile=`).

STEZHKA_DATA_DIR      where the database and raw track files live      (default: ./data)
STEZHKA_INBOX         folder to watch for new GPX files, e.g. Syncthing (default: off)
STEZHKA_INBOX_POLL_S  inbox poll interval in seconds                   (default: 30)
STEZHKA_HOST          bind address                                     (default: 127.0.0.1)
STEZHKA_PORT          bind port                                        (default: 8470)
STEZHKA_COOKIE_SECURE send session cookie only over HTTPS: 1/0          (default: 1)
STEZHKA_BASEMAP_STYLE MapLibre style URL for the background map
STEZHKA_WEB_DIR       static web UI folder                             (default: <repo>/web)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_BASEMAP = "https://tiles.openfreemap.org/styles/positron"
_REPO_WEB = Path(__file__).resolve().parents[3] / "web"


def _bool(v: str | None, default: bool) -> bool:
    if v is None or v.strip() == "":
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class Config:
    data_dir: Path
    inbox_dir: Path | None = None
    inbox_poll_s: float = 30.0
    web_dir: Path = _REPO_WEB
    host: str = "127.0.0.1"
    port: int = 8470
    cookie_secure: bool = True
    basemap_style: str = DEFAULT_BASEMAP

    @property
    def db_path(self) -> Path:
        return self.data_dir / "stezhka.sqlite3"

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @classmethod
    def from_env(cls) -> Config:
        e = os.environ
        inbox = e.get("STEZHKA_INBOX")
        return cls(
            data_dir=Path(e.get("STEZHKA_DATA_DIR", "data")).resolve(),
            inbox_dir=Path(inbox).resolve() if inbox else None,
            inbox_poll_s=float(e.get("STEZHKA_INBOX_POLL_S", "30")),
            web_dir=Path(e.get("STEZHKA_WEB_DIR", str(_REPO_WEB))).resolve(),
            host=e.get("STEZHKA_HOST", "127.0.0.1"),
            port=int(e.get("STEZHKA_PORT", "8470")),
            cookie_secure=_bool(e.get("STEZHKA_COOKIE_SECURE"), True),
            basemap_style=e.get("STEZHKA_BASEMAP_STYLE", DEFAULT_BASEMAP),
        )
