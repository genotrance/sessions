"""Shared fake browser and test fixtures for ContextDaemon tests."""
from __future__ import annotations

import json
import os
import sys
import tempfile

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_root, "src"))
sys.path.insert(0, _root)

from sessions.persistence import PersistenceManager
from sessions.manager import ContainerManager


# ---------------------------------------------------------------------------
# A fake "browser" that records CDP state so we can assert the daemon's
# interactions with the Target / Storage / Page / Runtime domains.
# ---------------------------------------------------------------------------

class FakeBrowser:
    """In-memory model of Chrome for unit testing."""

    def __init__(self):
        self.contexts: set[str] = set()
        # targetId -> {url, title, browserContextId, type}
        self.targets: dict[str, dict] = {}
        # browserContextId -> list[cookie]
        self.cookies: dict[str, list[dict]] = {}
        # targetId -> {origin -> dict}
        self.local_storage: dict[str, dict] = {}
        # targetId -> {origin -> {dbName -> {storeName -> {rows, keys, ...}}}}
        self.idb_storage: dict[str, dict] = {}
        # targetId -> list[script sources injected via addScriptToEvaluateOnNewDocument]
        self.new_doc_scripts: dict[str, list[str]] = {}
        self.disposed: list[str] = []
        self._ctx_counter = 0
        self._target_counter = 0

    # -- helpers used by tests to seed "live" pages ---------------------------

    def seed_tab(self, ctx: str, url: str, title: str = "",
                 origin: str | None = None,
                 storage: dict | None = None,
                 idb: dict | None = None) -> str:
        self._target_counter += 1
        tid = f"T{self._target_counter}"
        self.targets[tid] = {"targetId": tid, "url": url, "title": title,
                             "browserContextId": ctx, "type": "page"}
        if storage is not None:
            self.local_storage[tid] = {(origin or url): storage}
        if idb is not None:
            self.idb_storage[tid] = {(origin or url): idb}
        return tid


def make_fake_session_factory(fb: FakeBrowser):
    """Build a CDPSession stub whose .target/.storage/.runtime/.page mimic CDP."""

    class _Ctx:
        def __init__(self, sess):
            self.s = sess

        def __enter__(self):
            return self.s

        def __exit__(self, *a):
            pass

    class FakeBrowserSession:
        """Stand-in for a CDPSession bound to the /browser endpoint."""

        def __init__(self):
            self.target = _FakeTargetDomain(fb)
            self.storage = _FakeStorageDomain(fb)
            self.browser = _FakeBrowserDomain(fb)

        def close(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

    class FakeTabSession:
        def __init__(self, target_id: str):
            self.target_id = target_id
            self.runtime = _FakeRuntimeDomain(fb, target_id)
            self.page = _FakePageDomain(fb, target_id)

        def send(self, method: str, params: dict | None = None, **_):
            if method == "DOMStorage.getDOMStorageItems":
                sid = (params or {}).get("storageId", {})
                origin = sid.get("securityOrigin", "")
                store = fb.local_storage.get(self.target_id, {})
                data = store.get(origin, {})
                return {"entries": [[k, v] for k, v in data.items()]}
            return {}

        def close(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

    return FakeBrowserSession, FakeTabSession


class _FakeTargetDomain:
    def __init__(self, fb: FakeBrowser):
        self.fb = fb

    def create_browser_context(self, **_):
        self.fb._ctx_counter += 1
        cid = f"CTX{self.fb._ctx_counter}"
        self.fb.contexts.add(cid)
        self.fb.cookies[cid] = []
        return cid

    def dispose_browser_context(self, browser_context_id: str):
        self.fb.contexts.discard(browser_context_id)
        self.fb.disposed.append(browser_context_id)
        for tid in [t for t, info in self.fb.targets.items()
                    if info["browserContextId"] == browser_context_id]:
            self.fb.targets.pop(tid, None)
            self.fb.local_storage.pop(tid, None)
        self.fb.cookies.pop(browser_context_id, None)
        return {}

    def get_targets(self, *, timeout=None):
        return list(self.fb.targets.values())

    def create_target(self, url="about:blank", browser_context_id=None, **_):
        self.fb._target_counter += 1
        tid = f"T{self.fb._target_counter}"
        self.fb.targets[tid] = {"targetId": tid, "url": url, "title": "",
                                "browserContextId": browser_context_id,
                                "type": "page"}
        return tid

    def close_target(self, target_id: str):
        self.fb.targets.pop(target_id, None)
        self.fb.local_storage.pop(target_id, None)
        return True

    def activate_target(self, target_id: str):
        return {}

    def get_browser_contexts(self):
        return list(self.fb.contexts)


class _FakeBrowserDomain:
    def __init__(self, fb: FakeBrowser):
        self.fb = fb
        self.closed = False

    def close(self):
        self.closed = True
        return {}

    def get_window_for_target(self, target_id: str):
        return {"windowId": 1, "bounds": {}}

    def set_window_bounds(self, window_id: int, bounds: dict):
        return {}


class _FakeStorageDomain:
    def __init__(self, fb: FakeBrowser):
        self.fb = fb

    def get_cookies(self, browser_context_id=None, timeout=30):
        return list(self.fb.cookies.get(browser_context_id, []))

    def set_cookies(self, cookies, browser_context_id=None):
        self.fb.cookies.setdefault(browser_context_id, []).extend(cookies)
        return {}

    def clear_cookies(self, browser_context_id=None):
        if browser_context_id:
            self.fb.cookies.pop(browser_context_id, None)
        else:
            self.fb.cookies.clear()
        return {}

    def clear_data_for_origin(self, origin, storage_types):
        return {}


class _FakeRuntimeDomain:
    def __init__(self, fb: FakeBrowser, target_id: str):
        self.fb = fb
        self.tid = target_id

    def evaluate(self, expression: str, **_):
        # Hand-rolled responses for the queries the daemon issues.
        if "window.location.origin" in expression:
            url = self.fb.targets.get(self.tid, {}).get("url", "")
            if not url or url == "about:blank":
                return "null"
            from urllib.parse import urlparse
            p = urlparse(url)
            return f"{p.scheme}://{p.netloc}" if p.scheme else "null"
        if "localStorage" in expression:
            store = self.fb.local_storage.get(self.tid, {})
            if store:
                only = next(iter(store.values()))
                return json.dumps(only)
            return json.dumps({})
        # IDB_LIST_JS — returns [{n: name, v: version}, ...]
        if "indexedDB.databases" in expression and "n:d.name" in expression:
            idb = self.fb.idb_storage.get(self.tid, {})
            if idb:
                origin_data = next(iter(idb.values()), {})
                db_list = []
                for db_name, db_data in origin_data.items():
                    ver = 1
                    meta = db_data.get("_meta") if isinstance(db_data, dict) else None
                    if meta:
                        ver = meta.get("version", 1)
                    db_list.append({"n": db_name, "v": ver})
                return json.dumps(db_list)
            return json.dumps([])
        # Per-database dump: build_single_db_dump_js(name) contains
        # 'var name="<dbName>"' — extract the DB name and return its data.
        if "indexedDB.open(name)" in expression and "var name=" in expression:
            import re as _re
            m = _re.search(r'var name="([^"]+)"', expression)
            if m:
                db_name = m.group(1)
                idb = self.fb.idb_storage.get(self.tid, {})
                if idb:
                    origin_data = next(iter(idb.values()), {})
                    db_data = origin_data.get(db_name, {})
                    return json.dumps(db_data)
            return json.dumps({})
        # Monolithic IDB dump fallback
        if "indexedDB" in expression:
            idb = self.fb.idb_storage.get(self.tid, {})
            if idb:
                only = next(iter(idb.values()))
                return json.dumps(only)
            return json.dumps({})
        return None


class _FakePageDomain:
    def __init__(self, fb: FakeBrowser, target_id: str):
        self.fb = fb
        self.tid = target_id

    def add_script_to_evaluate_on_new_document(self, source):
        self.fb.new_doc_scripts.setdefault(self.tid, []).append(source)
        return {"identifier": str(len(self.fb.new_doc_scripts[self.tid]))}

    def navigate(self, url, wait_for_load=False):
        if self.tid in self.fb.targets:
            self.fb.targets[self.tid]["url"] = url
        return {}


# ---------------------------------------------------------------------------
# Test fixture that patches ContainerManager's CDP access points.
# ---------------------------------------------------------------------------

class _PatchedManagerMixin:
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ctxd-test-")
        self.db = os.path.join(self.tmp, "context_store.db")
        self.fb = FakeBrowser()
        FakeBrowserSession, FakeTabSession = make_fake_session_factory(self.fb)
        self._fake_browser_cls = FakeBrowserSession
        self._fake_tab_cls = FakeTabSession

        self.store = PersistenceManager(self.db)
        self.mgr = ContainerManager(store=self.store)

        # Replace _browser_session / _tab_session / _new_browser_session
        # with our fakes.
        self.mgr._browser_session = lambda: FakeBrowserSession()
        self.mgr._new_browser_session = lambda: FakeBrowserSession()
        self.mgr._tab_session = lambda target_id: FakeTabSession(target_id)

        # _open_tab_with_storage uses the real CDPSession + HTTP /json/list.
        # Replace it with a fake that just creates a target and records injection.
        fb = self.fb

        def fake_open(ctx, url, storage_by_origin, idb_by_origin=None, **_kwargs):
            from urllib.parse import urlparse
            fb._target_counter += 1
            tid = f"T{fb._target_counter}"
            p = urlparse(url)
            origin = f"{p.scheme}://{p.netloc}" if p.scheme and p.netloc else None
            fb.targets[tid] = {"targetId": tid, "url": url, "title": "",
                               "browserContextId": ctx, "type": "page"}
            kv = storage_by_origin.get(origin) if origin else None
            if kv:
                fb.new_doc_scripts.setdefault(tid, []).append(json.dumps(kv))
                fb.local_storage[tid] = {origin: dict(kv)}
            return tid

        self.mgr._open_tab_with_storage = fake_open

        # Patch raw HTTP helpers that bypass _browser_session so tests never
        # contact the real Chrome on port 9222 or the real daemon on port 9999.
        self.mgr._get_targets_cached = lambda max_age=2.0, _fb=self.fb: [
            dict(t, id=t["targetId"]) for t in _fb.targets.values()
        ]
        self.mgr._chrome_http_reachable = lambda: False

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)
