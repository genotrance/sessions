"""Sessions - Browser isolation and session manager for Chromium."""
from __future__ import annotations

from .cli import (
    DAEMON_PID_FILE,
    SNAPSHOT_INTERVAL_SEC,
    cmd_start,
    cmd_status,
    cmd_stop,
    setup_logging,
)
from .dashboard import DASHBOARD_HTML
from .manager import ContainerManager, DEFAULT_BROWSER_PORT
from .persistence import DB_PATH, PersistenceManager, _SCHEMA
from .server import DEFAULT_API_PORT, make_server
from .utils import (
    clean_cookie as _clean_cookie,
    domain_of as _domain_of,
    normalize_url as _normalize_url,  # noqa: F401
    origin_of as _origin_of,
    origins_from_cookies as _origins_from_cookies,
)

__all__ = [
    "PersistenceManager",
    "DB_PATH",
    "_SCHEMA",
    "ContainerManager",
    "_clean_cookie",
    "_origins_from_cookies",
    "_domain_of",
    "_origin_of",
    "DEFAULT_BROWSER_PORT",
    "DASHBOARD_HTML",
    "make_server",
    "DEFAULT_API_PORT",
    "setup_logging",
    "cmd_start",
    "cmd_stop",
    "cmd_status",
    "SNAPSHOT_INTERVAL_SEC",
    "DAEMON_PID_FILE",
]

__version__ = "0.1.2"
