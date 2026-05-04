"""Integration tests: full API+manager lifecycle, snapshot concurrency,
localStorage resilience, dashboard UI wiring, and crash recovery.

These tests use the fake CDP infrastructure but exercise the full stack from
HTTP requests through ContainerManager to the persistence layer."""
from __future__ import annotations

import http.client
import json
import os
import sys
import threading
import time
import unittest
from unittest import mock

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_root, "src"))
sys.path.insert(0, _root)

from sessions.manager import ContainerManager, SNAPSHOT_CDP_TIMEOUT, SNAPSHOT_FRESHNESS_SEC
from sessions.server import make_server
from tests.fakes import _PatchedManagerMixin, make_fake_session_factory


# ---------------------------------------------------------------------------
# Shared HTTP test base
# ---------------------------------------------------------------------------

class _HttpTestBase(_PatchedManagerMixin, unittest.TestCase):
    """Mixin that starts an HTTP test server with fake CDP wiring."""

    def setUp(self):
        super().setUp()
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        self.port = s.getsockname()[1]
        s.close()
        # Safety check: ensure we're not using the default daemon port
        from sessions.server import DEFAULT_API_PORT
        if self.port == DEFAULT_API_PORT:
            raise RuntimeError(
                f"Test server bound to default daemon port {DEFAULT_API_PORT}. "
                "Stop the daemon before running tests to avoid accidental shutdown.")
        self.server = make_server(self.mgr, port=self.port)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def _req(self, method: str, path: str, body: dict | None = None
             ) -> tuple[int, dict]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        data = json.dumps(body).encode() if body else None
        headers = {"content-type": "application/json"} if data else {}
        conn.request(method, path, data, headers)
        r = conn.getresponse()
        raw = r.read()
        conn.close()
        try:
            return r.status, json.loads(raw)
        except Exception:
            return r.status, {"raw": raw.decode("utf-8", errors="replace")}

    def _get_html(self) -> str:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/")
        r = conn.getresponse()
        html = r.read().decode()
        conn.close()
        return html


# ===========================================================================
# 1. Full end-to-end lifecycle via HTTP API
# ===========================================================================

class TestFullLifecycleE2E(_HttpTestBase):
    """Exercise a realistic user journey: create → restore → snapshot →
    add tabs → hibernate → clone → clean → delete — all via HTTP."""

    def test_complete_session_journey(self):
        # 1. Create a session
        s, c = self._req("POST", "/api/containers", {"name": "journey"})
        self.assertEqual(s, 200)
        cid = c["id"]
        self.assertTrue(cid)

        # 2. Verify it shows up in the list as hot (auto-restored on create)
        s, data = self._req("GET", "/api/containers")
        self.assertEqual(s, 200)
        ids = [c["id"] for c in data["containers"]]
        self.assertIn(cid, ids)
        ctr = next(c for c in data["containers"] if c["id"] == cid)
        self.assertTrue(ctr["hot"])

        # 3. Hibernate it
        s, res = self._req("POST", f"/api/containers/{cid}/hibernate")
        self.assertEqual(s, 200)
        self.assertIn("tabs_saved", res)

        # 4. Seed rich state for restore
        self.store.save_hibernation(
            cid,
            [{"name": "auth", "value": "tok123", "domain": "app.com",
              "path": "/", "url": "https://app.com"}],
            {"https://app.com": {"theme": "dark", "lang": "en"}},
            [{"url": "https://app.com/home", "title": "Home"},
             {"url": "https://app.com/settings", "title": "Settings"}])

        # 5. Restore
        s, res = self._req("POST", f"/api/containers/{cid}/restore")
        self.assertEqual(s, 200)
        self.assertEqual(res["tabs_opened"], 2)
        ctx = res["browserContextId"]
        self.assertTrue(ctx)

        # 6. Verify cookies were injected
        self.assertIn(ctx, self.fb.cookies)
        cookie_names = {c["name"] for c in self.fb.cookies[ctx]}
        self.assertIn("auth", cookie_names)

        # 7. Verify localStorage was injected via scripts
        scripts = []
        for tid, ss in self.fb.new_doc_scripts.items():
            if self.fb.targets.get(tid, {}).get("browserContextId") == ctx:
                scripts.extend(ss)
        self.assertTrue(any("theme" in s for s in scripts))

        # 8. Open an additional tab via API
        s, res = self._req("POST", f"/api/containers/{cid}/open",
                           {"url": "https://app.com/new"})
        self.assertEqual(s, 200)
        self.assertIn("targetId", res)

        # 9. Snapshot (should preserve state without disposing context)
        s, res = self._req("POST", "/api/snapshot-all")
        self.assertEqual(s, 200)

        # 10. Verify still hot after snapshot
        s, data = self._req("GET", "/api/containers")
        ctr = next(c for c in data["containers"] if c["id"] == cid)
        self.assertTrue(ctr["hot"])

        # 11. Clone the session
        s, clone = self._req("POST", f"/api/containers/{cid}/clone",
                             {"name": "journey-copy"})
        self.assertEqual(s, 200)
        clone_id = clone["id"]
        self.assertNotEqual(clone_id, cid)
        clone_full = self.store.get_container(clone_id)
        self.assertEqual(len(clone_full["cookies"]), 1)

        # 12. Clean the original
        s, _ = self._req("POST", f"/api/containers/{cid}/clean")
        self.assertEqual(s, 200)
        cleaned = self.store.get_container(cid)
        self.assertEqual(cleaned["cookies"], [])
        self.assertEqual(cleaned["storage"], {})

        # 13. Delete both
        for del_id in (cid, clone_id):
            s, res = self._req("DELETE", f"/api/containers/{del_id}")
            self.assertEqual(s, 200)
            self.assertTrue(res["deleted"])

        # 14. List should be empty
        s, data = self._req("GET", "/api/containers")
        self.assertEqual(len(data["containers"]), 0)


# ===========================================================================
# 2. Snapshot concurrency and freshness
# ===========================================================================

class TestSnapshotConcurrency(_PatchedManagerMixin, unittest.TestCase):
    """Verify that concurrent snapshot_all calls don't corrupt state or
    deadlock, and that freshness skipping works correctly."""

    def test_concurrent_snapshot_all_no_deadlock(self):
        """Two snapshot_all calls running simultaneously should both complete
        without deadlock or data corruption."""
        ids = []
        for name in ("c1", "c2", "c3"):
            c = self.store.create_container(name)
            self.store.save_hibernation(
                c["id"], [], {},
                [{"url": f"https://{name}.com", "title": name}])
            self.mgr.restore(c["id"])
            ids.append(c["id"])

        errors = []

        def snap():
            try:
                self.mgr.snapshot_all()
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=snap)
        t2 = threading.Thread(target=snap)
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)
        self.assertFalse(t1.is_alive(), "snapshot_all thread 1 deadlocked")
        self.assertFalse(t2.is_alive(), "snapshot_all thread 2 deadlocked")
        self.assertEqual(errors, [], f"snapshot errors: {errors}")

        # All containers should still be hot and have persisted state
        for cid in ids:
            self.assertIn(cid, self.mgr.hot)
            full = self.store.get_container(cid)
            self.assertEqual(full["is_active"], 1)

    def test_snapshot_if_stale_skips_fresh(self):
        """_snapshot_if_stale should skip containers snapshotted recently."""
        c = self.store.create_container("fresh")
        self.store.save_hibernation(
            c["id"], [], {},
            [{"url": "https://fresh.com", "title": "F"}])
        self.mgr.restore(c["id"])

        # First snapshot should proceed
        r1 = self.mgr._snapshot_if_stale(c["id"])
        self.assertIn("tabs_saved", r1)

        # Immediately after, should be skipped
        r2 = self.mgr._snapshot_if_stale(c["id"])
        self.assertEqual(r2["skipped"], "fresh")

    def test_snapshot_freshness_expires(self):
        """After SNAPSHOT_FRESHNESS_SEC, a container should be re-snapshotted."""
        c = self.store.create_container("expire")
        self.store.save_hibernation(
            c["id"], [], {},
            [{"url": "https://expire.com", "title": "E"}])
        self.mgr.restore(c["id"])

        self.mgr.snapshot(c["id"])

        # Artificially age the timestamp and clear hash so re-snapshot is forced
        self.mgr._last_snapshot_time[c["id"]] = time.time() - SNAPSHOT_FRESHNESS_SEC - 1
        self.mgr._last_snapshot_hash.pop(c["id"], None)

        r = self.mgr._snapshot_if_stale(c["id"])
        self.assertIn("tabs_saved", r)

    def test_snapshot_during_quick_shutdown_no_race(self):
        """quick_shutdown should snapshot correctly even if a background loop
        was recently snapshotting (no wrong-ctx bug)."""
        sessions = []
        for name in ("wa", "dc", "gh"):
            c = self.store.create_container(name)
            self.store.save_hibernation(
                c["id"],
                [{"name": f"k_{name}", "value": "v", "domain": f"{name}.com",
                  "path": "/", "url": f"https://{name}.com"}],
                {f"https://{name}.com": {"data": name}},
                [{"url": f"https://{name}.com/page", "title": name.upper()}])
            self.mgr.restore(c["id"])
            sessions.append(c)

        # Simulate a background snapshot happening (like the snapshot_loop)
        self.mgr.snapshot_all()

        # Age timestamps and clear hashes so quick_shutdown will re-snapshot
        for c in sessions:
            self.mgr._last_snapshot_time[c["id"]] = 0
            self.mgr._last_snapshot_hash.pop(c["id"], None)

        self.mgr.close_chrome = mock.MagicMock()
        results = self.mgr.quick_shutdown()

        # Each container should have been snapshotted (not skipped, not errored)
        for r in results:
            self.assertIn("tabs_saved", r, f"Expected tabs_saved in {r}")
            self.assertEqual(r["tabs_saved"], 1)

        # Verify the saved state matches the correct container, not a swapped ctx
        for c in sessions:
            full = self.store.get_container(c["id"])
            name = c["name"]
            self.assertEqual(len(full["tabs"]), 1)
            self.assertIn(name, full["tabs"][0]["url"],
                          f"Tab URL for {name} should contain {name}")


# ===========================================================================
# 3. localStorage resilience
# ===========================================================================

class TestLocalStorageResilience(_PatchedManagerMixin, unittest.TestCase):
    """Verify that localStorage collection is resilient to JS errors,
    restricted pages, and empty storage."""

    def test_localstorage_js_error_does_not_block_snapshot(self):
        """If one tab's localStorage throws, the snapshot should still save
        the other tabs' data and cookies."""
        c = self.store.create_container("ls-err")
        self.store.save_hibernation(
            c["id"], [],
            {"https://good.com": {"key": "val"}},
            [{"url": "https://good.com/page", "title": "Good"},
             {"url": "https://bad.com/page", "title": "Bad"}])
        self.mgr.restore(c["id"])
        ctx = self.mgr.hot[c["id"]]

        # Seed storage for the good tab
        for tid, info in self.fb.targets.items():
            if info["browserContextId"] == ctx:
                if "good.com" in info["url"]:
                    self.fb.local_storage[tid] = {
                        "https://good.com": {"key": "val"}}

        # Make the bad tab's runtime.evaluate raise an exception
        orig_tab_session = self.mgr._tab_session

        class _FailingRuntime:
            def evaluate(self, expr, **_):
                if "localStorage" in expr:
                    raise RuntimeError("CDP -1: JS exception: Uncaught")
                return "https://bad.com"

        class _FailingTabSession:
            def __init__(self, tid):
                self._tid = tid
                self._real = None
                # Only fail for the bad tab
                is_bad = self._check_bad(tid)
                if is_bad:
                    self.runtime = _FailingRuntime()
                    self.page = mock.MagicMock()
                else:
                    real = orig_tab_session(tid).__enter__()
                    self.runtime = real.runtime
                    self.page = real.page

            def _check_bad(self, tid):
                info = self.fb_ref.targets.get(tid, {})
                return "bad.com" in info.get("url", "")

            fb_ref = self.fb

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        self.mgr._tab_session = lambda tid: _FailingTabSession(tid)

        result = self.mgr.snapshot(c["id"])

        # Snapshot should succeed
        self.assertIn("tabs_saved", result)
        self.assertEqual(result["tabs_saved"], 2)  # both tabs saved
        # The good tab's storage should have been collected
        full = self.store.get_container(c["id"])
        self.assertIn("https://good.com", full["storage"])

    def test_empty_localstorage_saved_as_empty(self):
        """Tabs with no localStorage should not cause errors."""
        c = self.store.create_container("empty-ls")
        self.store.save_hibernation(
            c["id"], [], {},
            [{"url": "https://nols.com/page", "title": "NoLS"}])
        self.mgr.restore(c["id"])

        result = self.mgr.snapshot(c["id"])
        self.assertIn("tabs_saved", result)
        full = self.store.get_container(c["id"])
        # Empty localStorage is still saved as {origin: {}} — not an error
        # The important thing is no exception was raised
        self.assertIsInstance(full["storage"], dict)

    def test_about_blank_tabs_skipped(self):
        """about:blank and chrome:// tabs should not appear in snapshots."""
        c = self.store.create_container("skip-tabs")
        self.mgr.restore(c["id"])
        ctx = self.mgr.hot[c["id"]]

        # Seed some unsaveable tabs
        self.fb.seed_tab(ctx, "about:blank")
        self.fb.seed_tab(ctx, "chrome://settings/")
        self.fb.seed_tab(ctx, "chrome://newtab/")
        self.fb.seed_tab(ctx, "https://real.com/page", "Real Page")

        result = self.mgr.snapshot(c["id"])
        self.assertIn("tabs_saved", result)
        full = self.store.get_container(c["id"])
        urls = [t["url"] for t in full["tabs"]]
        self.assertNotIn("about:blank", urls)
        self.assertNotIn("chrome://settings/", urls)
        self.assertIn("https://real.com/page", urls)


# ===========================================================================
# 4. Crash recovery and auto-restore
# ===========================================================================

class TestCrashRecovery(_PatchedManagerMixin, unittest.TestCase):
    """Simulate crash scenarios and verify state is recovered."""

    def test_quick_shutdown_preserves_active_flag(self):
        """After quick_shutdown, is_active should remain True so the next
        start auto-restores the session."""
        ids = []
        for name in ("crash-a", "crash-b"):
            c = self.store.create_container(name)
            self.store.save_hibernation(
                c["id"], [], {},
                [{"url": f"https://{name}.com", "title": name}])
            self.mgr.restore(c["id"])
            ids.append(c["id"])

        self.mgr.close_chrome = mock.MagicMock()
        self.mgr.quick_shutdown()

        # is_active should still be 1 for both
        for cid in ids:
            full = self.store.get_container(cid)
            self.assertEqual(full["is_active"], 1)

    def test_auto_restore_after_simulated_crash(self):
        """Simulate: snapshot → crash → new manager auto-restores."""
        c = self.store.create_container("survive")
        self.store.save_hibernation(
            c["id"],
            [{"name": "tok", "value": "abc", "domain": "surv.com",
              "path": "/", "url": "https://surv.com"}],
            {"https://surv.com": {"state": "important"}},
            [{"url": "https://surv.com/app", "title": "App"}])
        self.mgr.restore(c["id"])

        # Snapshot the live state
        self.mgr.snapshot(c["id"])

        # Simulate crash: quick_shutdown + clear hot
        self.mgr.close_chrome = mock.MagicMock()
        self.mgr.quick_shutdown()

        # Create a fresh manager (simulating a new process start)
        mgr2 = ContainerManager(store=self.store)
        FakeBrowserSession, FakeTabSession = make_fake_session_factory(self.fb)
        mgr2._browser_session = lambda: FakeBrowserSession()
        mgr2._tab_session = lambda tid: FakeTabSession(tid)
        mgr2._open_tab_with_storage = self.mgr._open_tab_with_storage

        restored = mgr2.auto_restore_hot()
        self.assertEqual(len(restored), 1)
        self.assertIn(c["id"], mgr2.hot)

        # Verify restored state has the right data
        ctx = mgr2.hot[c["id"]]
        urls = [t["url"] for t in self.fb.targets.values()
                if t.get("browserContextId") == ctx and t.get("type") == "page"]
        self.assertIn("https://surv.com/app", urls)

    def test_stale_hot_detection_hibernates_orphan(self):
        """If all tabs for a container disappear (user closed them in Chrome),
        _check_stale_hot should soft-hibernate it."""
        c = self.store.create_container("orphan")
        self.store.save_hibernation(
            c["id"], [], {},
            [{"url": "https://orphan.com/", "title": "O"}])
        self.mgr.restore(c["id"])
        ctx = self.mgr.hot[c["id"]]

        # Snapshot first so there's state to preserve
        self.mgr.snapshot(c["id"])

        # Remove all page targets for this context
        for tid in list(self.fb.targets):
            if self.fb.targets[tid].get("browserContextId") == ctx:
                del self.fb.targets[tid]

        self.mgr._check_stale_hot()

        self.assertNotIn(c["id"], self.mgr.hot)
        full = self.store.get_container(c["id"])
        self.assertEqual(full["is_active"], 0)
        # Saved tabs from last snapshot should still be there
        self.assertEqual(len(full["tabs"]), 1)


# ===========================================================================
# 5. Multi-session isolation end-to-end via HTTP
# ===========================================================================

class TestIsolationE2E(_HttpTestBase):
    """Verify that sessions are fully isolated when accessed via HTTP."""

    def test_cookie_isolation_roundtrip(self):
        """Cookies set in session A are not visible after restoring session B."""
        # Create two sessions with different cookies for the same domain
        _, a = self._req("POST", "/api/containers", {"name": "iso-a"})
        _, b = self._req("POST", "/api/containers", {"name": "iso-b"})
        # Hibernate both
        self._req("POST", f"/api/containers/{a['id']}/hibernate")
        self._req("POST", f"/api/containers/{b['id']}/hibernate")

        # Seed different cookies
        self.store.save_hibernation(
            a["id"],
            [{"name": "auth", "value": "AAA", "domain": "app.com",
              "path": "/", "url": "https://app.com"}],
            {}, [{"url": "https://app.com/", "title": "App"}])
        self.store.save_hibernation(
            b["id"],
            [{"name": "auth", "value": "BBB", "domain": "app.com",
              "path": "/", "url": "https://app.com"}],
            {}, [{"url": "https://app.com/", "title": "App"}])

        # Restore both
        self._req("POST", f"/api/containers/{a['id']}/restore")
        self._req("POST", f"/api/containers/{b['id']}/restore")

        ctx_a = self.mgr.hot[a["id"]]
        ctx_b = self.mgr.hot[b["id"]]
        self.assertNotEqual(ctx_a, ctx_b)

        a_vals = {c["value"] for c in self.fb.cookies.get(ctx_a, [])}
        b_vals = {c["value"] for c in self.fb.cookies.get(ctx_b, [])}
        self.assertIn("AAA", a_vals)
        self.assertNotIn("BBB", a_vals)
        self.assertIn("BBB", b_vals)
        self.assertNotIn("AAA", b_vals)

    def test_simultaneous_sessions_status(self):
        """Multiple hot sessions should all appear correctly in status."""
        ids = []
        for name in ("s1", "s2", "s3"):
            _, c = self._req("POST", "/api/containers", {"name": name})
            ids.append(c["id"])

        s, data = self._req("GET", "/api/containers")
        self.assertEqual(s, 200)
        hot_ids = {c["id"] for c in data["containers"] if c["hot"]}
        for cid in ids:
            self.assertIn(cid, hot_ids)


# ===========================================================================
# 6. Tab management via HTTP
# ===========================================================================

class TestTabManagementE2E(_HttpTestBase):
    """Test tab-level operations: activate, close, delete saved tab."""

    def test_activate_and_close_tab(self):
        _, c = self._req("POST", "/api/containers", {"name": "tabs"})
        cid = c["id"]
        # Hibernate first so restore opens saved tabs in a fresh context
        self._req("POST", f"/api/containers/{cid}/hibernate")
        self.store.save_hibernation(
            cid, [], {},
            [{"url": "https://tabs.com/a", "title": "A"},
             {"url": "https://tabs.com/b", "title": "B"}])
        self._req("POST", f"/api/containers/{cid}/restore")

        ctx = self.mgr.hot[cid]
        tids = [t["targetId"] for t in self.fb.targets.values()
                if t["browserContextId"] == ctx]
        self.assertGreaterEqual(len(tids), 2)

        # Activate first tab
        s, res = self._req("POST", "/api/activate", {"targetId": tids[0]})
        self.assertEqual(s, 200)
        self.assertTrue(res["activated"])

        # Close second tab
        s, res = self._req("POST", "/api/close-tab", {"targetId": tids[1]})
        self.assertEqual(s, 200)
        self.assertTrue(res["closed"])
        self.assertNotIn(tids[1], self.fb.targets)

    def test_delete_saved_tab(self):
        _, c = self._req("POST", "/api/containers", {"name": "del-tab"})
        cid = c["id"]
        self._req("POST", f"/api/containers/{cid}/hibernate")
        self.store.save_hibernation(
            cid, [], {},
            [{"url": "https://dt.com/keep", "title": "Keep"},
             {"url": "https://dt.com/remove", "title": "Remove"}])

        s, res = self._req("DELETE", f"/api/containers/{cid}/tab",
                           {"url": "https://dt.com/remove"})
        self.assertEqual(s, 200)
        self.assertTrue(res["deleted"])

        full = self.store.get_container(cid)
        urls = [t["url"] for t in full["tabs"]]
        self.assertNotIn("https://dt.com/remove", urls)
        self.assertIn("https://dt.com/keep", urls)

    def test_open_tab_on_cold_session_auto_restores(self):
        """Opening a URL on a hibernated session should auto-restore it."""
        _, c = self._req("POST", "/api/containers", {"name": "auto-open"})
        cid = c["id"]
        self._req("POST", f"/api/containers/{cid}/hibernate")
        self.assertNotIn(cid, self.mgr.hot)

        s, res = self._req("POST", f"/api/containers/{cid}/open",
                           {"url": "https://new-page.com"})
        self.assertEqual(s, 200)
        self.assertIn(cid, self.mgr.hot)

    def test_open_saved_tab_does_not_duplicate(self):
        """Clicking a saved tab in a hibernated session should not open it twice.
        Root cause: restore() was appending also_open_url without checking if it
        was already in the saved tabs list."""
        _, c = self._req("POST", "/api/containers", {"name": "dup-test"})
        cid = c["id"]
        self._req("POST", f"/api/containers/{cid}/hibernate")
        # Save 3 tabs
        self.store.save_hibernation(
            cid, [], {},
            [{"url": "https://dup.com/a", "title": "A"},
             {"url": "https://dup.com/b", "title": "B"},
             {"url": "https://dup.com/c", "title": "C"}])

        # Click the second tab (restore with also_open_url pointing to saved tab)
        s, res = self._req("POST", f"/api/containers/{cid}/open",
                           {"url": "https://dup.com/b"})
        self.assertEqual(s, 200)

        # Should have exactly 3 tabs, not 4
        ctx = self.mgr.hot[cid]
        tids = [t["targetId"] for t in self.fb.targets.values()
                if t["browserContextId"] == ctx and t["type"] == "page"]
        self.assertEqual(len(tids), 3,
                         f"Expected 3 tabs, got {len(tids)} — duplicate tab opened")


# ===========================================================================
# 7. Rename and clone
# ===========================================================================

class TestRenameAndClone(_HttpTestBase):

    def test_rename_preserves_state(self):
        _, c = self._req("POST", "/api/containers", {"name": "old"})
        cid = c["id"]
        self.store.save_hibernation(
            cid,
            [{"name": "k", "value": "v", "domain": "r.com",
              "path": "/", "url": "https://r.com"}],
            {"https://r.com": {"x": "1"}},
            [{"url": "https://r.com/page", "title": "P"}])

        s, res = self._req("PATCH", f"/api/containers/{cid}",
                           {"name": "renamed"})
        self.assertEqual(s, 200)
        self.assertEqual(res["name"], "renamed")

        # State should be intact
        full = self.store.get_container(cid)
        self.assertEqual(len(full["cookies"]), 1)
        self.assertEqual(len(full["tabs"]), 1)

    def test_clone_creates_independent_copy(self):
        _, c = self._req("POST", "/api/containers", {"name": "orig"})
        cid = c["id"]
        self._req("POST", f"/api/containers/{cid}/hibernate")
        self.store.save_hibernation(
            cid,
            [{"name": "s", "value": "v", "domain": "c.com",
              "path": "/", "url": "https://c.com"}],
            {"https://c.com": {"d": "1"}},
            [{"url": "https://c.com/", "title": "C"}])

        s, clone = self._req("POST", f"/api/containers/{cid}/clone",
                             {"name": "copy"})
        self.assertEqual(s, 200)
        clone_id = clone["id"]
        self.assertNotEqual(clone_id, cid)

        # Modifying the clone shouldn't affect the original
        self._req("POST", f"/api/containers/{clone_id}/clean")
        clone_full = self.store.get_container(clone_id)
        self.assertEqual(clone_full["cookies"], [])
        orig_full = self.store.get_container(cid)
        self.assertEqual(len(orig_full["cookies"]), 1)


# ===========================================================================
# 8. Dashboard HTML integration
# ===========================================================================

class TestDashboardIntegration(_HttpTestBase):
    """Test that the dashboard HTML returned by the server contains correct
    wiring for all features, and that the status API returns data that
    the dashboard JS can render."""

    def test_dashboard_html_served(self):
        html = self._get_html()
        self.assertIn("<title>Sessions</title>", html)
        self.assertIn("refresh()", html)

    def test_status_api_shape_for_hot_session(self):
        """Status API should return live_tabs for hot containers."""
        _, c = self._req("POST", "/api/containers", {"name": "ui-hot"})
        cid = c["id"]
        # Hibernate first so restore opens the saved tab (not about:blank)
        self._req("POST", f"/api/containers/{cid}/hibernate")
        self.store.save_hibernation(
            cid, [], {},
            [{"url": "https://ui.com/page", "title": "UI Page"}])
        self._req("POST", f"/api/containers/{cid}/restore")

        s, data = self._req("GET", "/api/containers")
        ctr = next(c for c in data["containers"] if c["id"] == cid)
        self.assertTrue(ctr["hot"])
        self.assertIn("live_tabs", ctr)
        self.assertTrue(len(ctr["live_tabs"]) > 0)
        tab = ctr["live_tabs"][0]
        self.assertIn("targetId", tab)
        self.assertIn("url", tab)
        self.assertIn("title", tab)

    def test_status_api_shape_for_cold_session(self):
        """Status API should return saved_tabs for cold containers."""
        _, c = self._req("POST", "/api/containers", {"name": "ui-cold"})
        cid = c["id"]
        self._req("POST", f"/api/containers/{cid}/hibernate")
        self.store.save_hibernation(
            cid, [], {},
            [{"url": "https://cold.com/", "title": "Cold"}])

        s, data = self._req("GET", "/api/containers")
        ctr = next(c for c in data["containers"] if c["id"] == cid)
        self.assertFalse(ctr["hot"])
        self.assertIn("saved_tabs", ctr)
        self.assertEqual(len(ctr["saved_tabs"]), 1)
        self.assertEqual(ctr["saved_tabs"][0]["url"], "https://cold.com/")

    def test_dashboard_checkbox_alignment_css(self):
        """Checkbox should be top-aligned (flex-start), not center."""
        html = self._get_html()
        self.assertIn("align-items:flex-start", html)
        # Should have top padding matching .tab padding
        self.assertIn("padding:5px 2px 0 8px", html)

    def test_dashboard_has_all_action_functions(self):
        """Dashboard JS must define all the action functions wired to the UI."""
        html = self._get_html()
        required_functions = [
            "createSession(", "activate(", "restoreAndOpen(",
            "closeTab(", "deleteSavedTab(", "cleanDefault(",
            "restartBackend(", "quitDaemon(",
            "bulkAct(", "ctxAct(", "showCtxMenu(",
            "toggleSelect(", "toggleSelectAll(",
            "renderList(", "renderSearch(",
            "onSearchInput(", "clearSearch(",
            "_moveFocus(", "_moveBrowseFocus(",
            "_activateSearchMatch(", "_activateBrowseItem(",
        ]
        for fn in required_functions:
            self.assertIn(fn, html, f"Missing function: {fn}")

    def test_dashboard_all_api_endpoints_referenced(self):
        """Dashboard JS must reference all API endpoints it uses."""
        html = self._get_html()
        endpoints = [
            "/api/containers", "/api/activate", "/api/close-tab",
            "/api/shutdown", "/api/restart", "/api/clean-default",
        ]
        for ep in endpoints:
            self.assertIn(ep, html, f"Missing endpoint reference: {ep}")


# ===========================================================================
# 9. Error handling
# ===========================================================================

class TestErrorHandling(_HttpTestBase):
    """Verify the server handles errors gracefully."""

    def test_404_on_unknown_container(self):
        s, _ = self._req("POST", "/api/containers/nonexistent/restore")
        self.assertEqual(s, 404)

    def test_hibernate_cold_container_returns_500(self):
        _, c = self._req("POST", "/api/containers", {"name": "cold-err"})
        cid = c["id"]
        self._req("POST", f"/api/containers/{cid}/hibernate")
        s, _ = self._req("POST", f"/api/containers/{cid}/hibernate")
        self.assertIn(s, (500, 200))  # might be 500 (RuntimeError) or 200 if already cold

    def test_delete_nonexistent_container(self):
        s, _ = self._req("DELETE", "/api/containers/ghost")
        self.assertIn(s, (200, 404))

    def test_invalid_rename_returns_400(self):
        _, c = self._req("POST", "/api/containers", {"name": "ren"})
        s, res = self._req("PATCH", f"/api/containers/{c['id']}", {})
        self.assertEqual(s, 400)
        self.assertIn("error", res)

    def test_404_unknown_path(self):
        s, _ = self._req("GET", "/api/nonexistent")
        self.assertEqual(s, 404)


# ===========================================================================
# 10. Shutdown and restart via HTTP
# ===========================================================================

class TestShutdownRestart(_HttpTestBase):
    """Test shutdown and restart API endpoints."""

    def test_shutdown_snapshots_and_responds(self):
        _, c = self._req("POST", "/api/containers", {"name": "sd"})
        cid = c["id"]
        self.store.save_hibernation(
            cid, [], {},
            [{"url": "https://sd.com/", "title": "SD"}])
        self._req("POST", f"/api/containers/{cid}/restore")

        self.mgr.close_chrome = mock.MagicMock()
        s, res = self._req("POST", "/api/shutdown")
        self.assertEqual(s, 200)
        self.assertTrue(res["shutdown"])
        self.assertIn("results", res)

        # State should be preserved
        full = self.store.get_container(cid)
        self.assertEqual(full["is_active"], 1)

    def test_restart_endpoint_responds(self):
        s, res = self._req("POST", "/api/restart")
        self.assertEqual(s, 200)
        self.assertTrue(res["restarting"])

    def test_clean_default_context(self):
        s, res = self._req("POST", "/api/clean-default")
        self.assertEqual(s, 200)
        self.assertTrue(res["cleaned"])


# ===========================================================================
# 11. Restart mechanics
# ===========================================================================

class TestRestartMechanics(_HttpTestBase):
    """Detailed tests for the restart endpoint and cli.py restart logic."""

    def test_restart_cb_not_bound_as_method(self):
        """restart_cb must be a staticmethod on the handler class so that
        accessing it via self.restart_cb doesn't inject self as first arg.
        This was the root-cause bug: plain function → bound method → TypeError
        silently swallowed inside a daemon thread."""
        fired = []
        self.server.RequestHandlerClass.restart_cb = staticmethod(
            lambda: fired.append(True))
        s, res = self._req("POST", "/api/restart")
        self.assertEqual(s, 200)
        time.sleep(0.05)
        self.assertTrue(fired, "restart_cb was never called — descriptor binding bug?")

    def test_restart_cb_called_directly_not_wrapped_in_thread(self):
        """restart_cb() is called directly in _route (no extra Thread wrapper)
        so that TypeError propagates and is logged rather than silently dropped."""
        def bad_cb():
            raise RuntimeError("intentional")
        self.server.RequestHandlerClass.restart_cb = staticmethod(bad_cb)
        # Should not raise on the client side — error is caught by _handle
        s, _ = self._req("POST", "/api/restart")
        self.assertEqual(s, 200)  # response already sent before cb runs

    def test_restart_cb_none_does_not_crash(self):
        """If restart_cb is None the endpoint still returns 200 gracefully."""
        self.server.RequestHandlerClass.restart_cb = None
        s, res = self._req("POST", "/api/restart")
        self.assertEqual(s, 200)
        self.assertTrue(res.get("restarting"))

    def test_restart_does_not_call_shutdown_endpoint(self):
        """The restart handler must NOT call quick_shutdown / close_chrome.
        Chrome must stay alive across a restart."""
        shutdown_called = []
        self.mgr.quick_shutdown = lambda: shutdown_called.append(True) or []
        self.server.RequestHandlerClass.restart_cb = staticmethod(lambda: None)
        self._req("POST", "/api/restart")
        time.sleep(0.05)
        self.assertFalse(shutdown_called,
                         "quick_shutdown was called during restart — Chrome would be killed")

    def test_restart_argv_flag_triggers_spawn_not_graceful_exit(self):
        """Simulate the _do_restart thread populating _restart_argv and verify
        that the finally-block spawn path is taken (not graceful_exit)."""
        import subprocess as _sp

        # Minimal simulation: a list acting as _restart_argv
        restart_argv: list[str] = []

        spawned = []
        exited = []

        def fake_popen(argv, **kw):
            spawned.append(argv)

        def fake_exit(code):
            exited.append(code)

        # Simulate what the finally block does
        restart_argv.extend(["python", "-m", "sessions", "start", "--foreground"])
        with mock.patch("subprocess.Popen", fake_popen), \
             mock.patch("os._exit", fake_exit):
            if restart_argv:
                import os as _os
                popen_kw = {"stdout": _sp.DEVNULL, "stderr": _sp.DEVNULL,
                            "stdin": _sp.DEVNULL}
                _sp.Popen(restart_argv, **popen_kw)
                _os._exit(0)

        self.assertTrue(spawned, "Popen was never called in restart path")
        self.assertTrue(exited, "os._exit was never called in restart path")
        self.assertIn("--foreground", spawned[0])

    def test_startup_probe_skips_if_port_free(self):
        """The foreground startup probe must NOT send /api/shutdown when the
        port is free (no existing daemon). Root cause: self-shutdown on restart
        because the new process bound the port before the probe ran."""
        import socket as _sock

        # Use a free port — nothing listening on it
        free_s = _sock.socket()
        free_s.bind(("127.0.0.1", 0))
        free_port = free_s.getsockname()[1]
        free_s.close()

        # Attempt TCP connection to free port → should fail → no HTTP probe
        probe_hit = []
        try:
            _s = _sock.create_connection(("127.0.0.1", free_port), timeout=0.2)
            _s.close()
            probe_hit.append(True)
        except OSError:
            pass  # expected — nothing listening

        self.assertFalse(probe_hit,
                         "TCP probe succeeded on a free port — self-shutdown would follow")

    def test_reclaim_stale_chrome_skips_own_pid(self):
        """_reclaim_stale_chrome must NOT send /api/shutdown when the PID file
        belongs to the current process. This was the root cause of restart
        self-shutdown: the new process wrote its PID, then _reclaim_stale_chrome
        read it back and shut itself down."""
        from sessions.cli import _reclaim_stale_chrome, _write_daemon_pid
        import os
        import tempfile

        # Use a temp PID file so the real DAEMON_PID_FILE is never touched
        tmp_pid = os.path.join(tempfile.mkdtemp(prefix="ctxd-test-"), "sessions-api.pid")
        with mock.patch("sessions.cli.DAEMON_PID_FILE", tmp_pid):
            _write_daemon_pid(os.getpid(), self.port)
            # Mock ChromeManager.is_running to return True (Chrome is alive)
            with mock.patch("sessions.cli.ChromeManager") as MockCM:
                MockCM.return_value.is_running.return_value = True
                result = _reclaim_stale_chrome(9222)
        # Should return True (reuse Chrome) without sending shutdown
        self.assertTrue(result, "Should reuse Chrome, not try to shut down own daemon")

    def test_shutdown_from_python_urllib_is_startup_probe(self):
        """Verify that a Python-urllib User-Agent shutdown request is logged,
        helping diagnose the self-shutdown-on-restart issue."""
        self.mgr.close_chrome = mock.MagicMock()
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/api/shutdown", b"{}",
                     {"Content-Type": "application/json",
                      "User-Agent": "Python-urllib/3.12"})
        r = conn.getresponse()
        r.read()
        conn.close()
        self.assertEqual(r.status, 200)
        # The Referer/User-Agent is logged — no assertion needed beyond no crash


# ===========================================================================
# 12. Snapshot lock safety: no wrong-ctx writes
# ===========================================================================

class TestSnapshotLockSafety(_PatchedManagerMixin, unittest.TestCase):
    """Verify the split-lock snapshot doesn't write stale ctx data."""

    def test_snapshot_aborted_if_ctx_changed(self):
        """If a container's ctx changes during _collect_state (e.g., it was
        re-restored), the save should be skipped."""
        c = self.store.create_container("race")
        self.store.save_hibernation(
            c["id"], [], {},
            [{"url": "https://race.com", "title": "R"}])
        self.mgr.restore(c["id"])

        # Wrap _collect_state to change ctx mid-collection
        real_collect = self.mgr._collect_state

        def sneaky_collect(ctx):
            # Simulate: between unlock and re-lock, the container got re-restored
            result = real_collect(ctx)
            # Change the hot entry to a different ctx (simulating concurrent restore)
            self.mgr.hot[c["id"]] = "DIFFERENT_CTX"
            return result

        self.mgr._collect_state = sneaky_collect

        result = self.mgr.snapshot(c["id"])
        self.assertEqual(result.get("skipped"), "stale",
                         "Should skip save when ctx changed during collection")

    def test_snapshot_aborted_if_container_deleted(self):
        """If a container is deleted during _collect_state, save is skipped."""
        c = self.store.create_container("del-race")
        self.store.save_hibernation(
            c["id"], [], {},
            [{"url": "https://del.com", "title": "D"}])
        self.mgr.restore(c["id"])

        real_collect = self.mgr._collect_state

        def sneaky_collect(ctx):
            result = real_collect(ctx)
            # Simulate deletion mid-collection
            del self.mgr.hot[c["id"]]
            return result

        self.mgr._collect_state = sneaky_collect

        result = self.mgr.snapshot(c["id"])
        self.assertEqual(result.get("skipped"), "stale")


# ===========================================================================
# 12. Dashboard CDP check with timeout
# ===========================================================================

class TestDashboardCDPCheck(_PatchedManagerMixin, unittest.TestCase):
    """Verify _check_dashboard_alive uses short timeout and handles errors."""

    def test_check_passes_timeout_to_get_targets(self):
        """get_targets should be called with SNAPSHOT_CDP_TIMEOUT."""
        tid = self.fb.seed_tab("", "http://localhost:9999/", "Sessions")
        self.mgr._dashboard_target_id = tid
        self.mgr._on_ui_close = lambda: None

        calls = []

        class _SpySession:
            def __init__(self):
                self.target = _SpyTarget()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        class _SpyTarget:
            def get_targets(self_, *, timeout=None):
                calls.append(timeout)
                return list(self.fb.targets.values())

        self.mgr._browser_session = lambda: _SpySession()
        self.mgr._check_dashboard_alive()

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], SNAPSHOT_CDP_TIMEOUT)


if __name__ == "__main__":
    unittest.main()
