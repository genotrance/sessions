"""SQLite-backed persistence for container metadata, cookies, storage, tabs."""
from __future__ import annotations

import base64
import json
import os
import re
import sqlite3
import threading
import time
import zlib

from . import cdp

DB_PATH = os.path.join(cdp._default_data_dir(), "context_store.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS containers (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    color        TEXT DEFAULT '#3b82f6',
    cookies_blob TEXT DEFAULT '[]',
    storage_blob TEXT DEFAULT '{}',
    idb_blob     TEXT DEFAULT '{}',
    is_active    INTEGER DEFAULT 0,
    created_at   INTEGER DEFAULT (strftime('%s', 'now')),
    last_accessed_at INTEGER DEFAULT (strftime('%s', 'now'))
);

CREATE TABLE IF NOT EXISTS container_tabs (
    container_id  TEXT NOT NULL,
    url           TEXT NOT NULL,
    title         TEXT DEFAULT '',
    last_scrolled INTEGER DEFAULT 0,
    FOREIGN KEY(container_id) REFERENCES containers(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_tabs_container ON container_tabs(container_id);
"""


# ---------------------------------------------------------------------------
# Compression helpers for large blobs (IndexedDB data can be tens of MB)
# ---------------------------------------------------------------------------

_COMPRESS_PREFIX = "Z:"
_COMPRESS_THRESHOLD = 1024  # bytes — only compress if raw JSON exceeds this


def _compress_blob(data: dict) -> str:
    """Compress a dict to a storage-efficient string.

    Returns raw JSON for small data, or 'Z:' + base85(zlib) for large data.
    """
    raw = json.dumps(data)
    if len(raw) < _COMPRESS_THRESHOLD:
        return raw
    compressed = zlib.compress(raw.encode("utf-8"), level=6)
    # Only use compression if it actually saves space after encoding
    encoded = _COMPRESS_PREFIX + base64.b85encode(compressed).decode("ascii")
    return encoded if len(encoded) < len(raw) else raw


def _decompress_blob(text: str) -> dict:
    """Decompress a string back to a dict.  Handles both old and new formats."""
    if not text:
        return {}
    if text.startswith(_COMPRESS_PREFIX):
        try:
            compressed = base64.b85decode(text[len(_COMPRESS_PREFIX):])
            raw = zlib.decompress(compressed).decode("utf-8")
            return json.loads(raw)
        except Exception:
            return {}
    return json.loads(text)


class PersistenceManager:
    """SQLite-backed store for container metadata, cookies, storage, tabs."""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._lock = threading.Lock()
        with self._conn() as c:
            c.executescript(_SCHEMA)
            # Migration: add idb_blob to existing databases that predate this column
            cols = {row[1] for row in c.execute("PRAGMA table_info(containers)")}
            if "idb_blob" not in cols:
                c.execute("ALTER TABLE containers ADD COLUMN idb_blob TEXT DEFAULT '{}'")
            if "last_accessed_at" not in cols:
                c.execute("ALTER TABLE containers ADD COLUMN last_accessed_at INTEGER DEFAULT 0")
                c.execute("UPDATE containers SET last_accessed_at = created_at WHERE last_accessed_at = 0")

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_path)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON")
        return c

    @staticmethod
    def slugify(name: str) -> str:
        s = re.sub(r"[^a-zA-Z0-9]+", "-", name.strip().lower()).strip("-")
        return s or f"ctr-{int(time.time())}"

    def create_container(self, name: str, color: str = "#3b82f6",
                         cid: str | None = None) -> dict:
        base_slug = cid or self.slugify(name)
        with self._lock, self._conn() as c:
            cid = base_slug
            n = 1
            while c.execute("SELECT 1 FROM containers WHERE id=?", (cid,)).fetchone():
                n += 1
                cid = f"{base_slug}-{n}"
            c.execute("INSERT INTO containers(id, name, color) VALUES (?,?,?)",
                      (cid, name, color))
        return self.get_container(cid)

    def list_containers(self) -> list[dict]:
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT id, name, color, is_active, created_at, last_accessed_at FROM containers "
                "ORDER BY created_at ASC").fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["tab_count"] = c.execute(
                    "SELECT COUNT(*) FROM container_tabs WHERE container_id=?",
                    (r["id"],)).fetchone()[0]
                out.append(d)
            return out

    def touch_accessed(self, cid: str) -> None:
        """Update the last_accessed_at timestamp for a container."""
        with self._lock, self._conn() as c:
            c.execute("UPDATE containers SET last_accessed_at=strftime('%s','now') WHERE id=?",
                      (cid,))

    def get_container(self, cid: str) -> dict | None:
        with self._lock, self._conn() as c:
            r = c.execute("SELECT * FROM containers WHERE id=?", (cid,)).fetchone()
            if not r:
                return None
            d = dict(r)
            d["cookies"] = json.loads(d.pop("cookies_blob") or "[]")
            d["storage"] = json.loads(d.pop("storage_blob") or "{}")
            d["idb"] = _decompress_blob(d.pop("idb_blob") or "{}")
            d["tabs"] = [dict(t) for t in c.execute(
                "SELECT url, title, last_scrolled FROM container_tabs "
                "WHERE container_id=? ORDER BY rowid", (cid,)).fetchall()]
            return d

    def save_hibernation(self, cid: str, cookies: list[dict],
                         storage: dict, tabs: list[dict],
                         keep_active: bool = False,
                         idb: dict | None = None) -> None:
        with self._lock, self._conn() as c:
            c.execute("UPDATE containers SET cookies_blob=?, storage_blob=?, "
                      "idb_blob=?, is_active=? WHERE id=?",
                      (json.dumps(cookies), json.dumps(storage),
                       _compress_blob(idb or {}),
                       1 if keep_active else 0, cid))
            c.execute("DELETE FROM container_tabs WHERE container_id=?", (cid,))
            c.executemany(
                "INSERT INTO container_tabs(container_id, url, title, last_scrolled) "
                "VALUES (?,?,?,?)",
                [(cid, t.get("url", ""), t.get("title", ""),
                  int(t.get("last_scrolled", 0))) for t in tabs])

    def reset_all_active(self) -> None:
        with self._lock, self._conn() as c:
            c.execute("UPDATE containers SET is_active=0")

    def mark_active(self, cid: str, active: bool) -> None:
        with self._lock, self._conn() as c:
            c.execute("UPDATE containers SET is_active=? WHERE id=?",
                      (1 if active else 0, cid))

    def mark_active_bulk(self, cids: list[str], active: bool) -> None:
        if not cids:
            return
        val = 1 if active else 0
        with self._lock, self._conn() as c:
            c.executemany("UPDATE containers SET is_active=? WHERE id=?",
                          [(val, cid) for cid in cids])

    def clear_tabs(self, cid: str) -> None:
        with self._lock, self._conn() as c:
            c.execute("DELETE FROM container_tabs WHERE container_id=?", (cid,))

    def clone_container(self, cid: str, new_name: str) -> dict | None:
        src = self.get_container(cid)
        if not src:
            return None
        new = self.create_container(new_name, color=src["color"])
        with self._lock, self._conn() as c:
            c.execute("UPDATE containers SET cookies_blob=?, storage_blob=?, "
                      "idb_blob=? WHERE id=?",
                      (json.dumps(src["cookies"]), json.dumps(src["storage"]),
                       _compress_blob(src.get("idb", {})), new["id"]))
            c.executemany(
                "INSERT INTO container_tabs(container_id, url, title, last_scrolled) "
                "VALUES (?,?,?,?)",
                [(new["id"], t["url"], t["title"], t.get("last_scrolled", 0))
                 for t in src["tabs"]])
        return self.get_container(new["id"])

    def rename_container(self, cid: str, new_name: str) -> None:
        with self._lock, self._conn() as c:
            c.execute("UPDATE containers SET name=? WHERE id=?", (new_name, cid))

    def clean_container(self, cid: str) -> None:
        with self._lock, self._conn() as c:
            c.execute("UPDATE containers SET cookies_blob='[]', storage_blob='{}', "
                      "idb_blob='{}' WHERE id=?", (cid,))

    def delete_tab(self, cid: str, url: str) -> bool:
        with self._lock, self._conn() as c:
            rowid = c.execute(
                "SELECT rowid FROM container_tabs WHERE container_id=? AND url=? LIMIT 1",
                (cid, url)).fetchone()
            if rowid:
                c.execute("DELETE FROM container_tabs WHERE rowid=?", (rowid[0],))
                return True
            return False

    def delete_container(self, cid: str) -> None:
        with self._lock, self._conn() as c:
            c.execute("DELETE FROM container_tabs WHERE container_id=?", (cid,))
            c.execute("DELETE FROM containers WHERE id=?", (cid,))
