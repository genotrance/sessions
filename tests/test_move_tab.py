"""Comprehensive tests for the move-tab (cut/paste) feature.

Covers:
  - PersistenceManager.move_tab (DB-level)
  - ContainerManager.move_tab (cold→cold, hot→cold, cold→hot, hot→hot)
  - HTTP API endpoint POST /api/move-tab
  - Dashboard HTML UI elements for cut/paste
"""
from __future__ import annotations

import http.client
import json
import os
import sys
import threading
import unittest

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_root, "src"))
sys.path.insert(0, _root)

from sessions.persistence import PersistenceManager
from sessions.server import make_server
from tests.fakes import _PatchedManagerMixin


# ---------------------------------------------------------------------------
# PersistenceManager.move_tab tests
# ---------------------------------------------------------------------------

class TestPersistenceMoveTab(unittest.TestCase):
    """Test the DB-level move_tab method on PersistenceManager."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="ctxd-move-")
        self.db = os.path.join(self.tmp, "ctx.db")
        self.store = PersistenceManager(self.db)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_move_tab_basic(self):
        """Tab row moves from source to destination."""
        src = self.store.create_container("Src")
        dst = self.store.create_container("Dst")
        self.store.save_hibernation(
            src["id"], [], {},
            [{"url": "https://a.com/1", "title": "A"},
             {"url": "https://b.com/2", "title": "B"}])
        ok = self.store.move_tab(src["id"], dst["id"], "https://a.com/1")
        self.assertTrue(ok)
        src_full = self.store.get_container(src["id"])
        dst_full = self.store.get_container(dst["id"])
        self.assertEqual(len(src_full["tabs"]), 1)
        self.assertEqual(src_full["tabs"][0]["url"], "https://b.com/2")
        self.assertEqual(len(dst_full["tabs"]), 1)
        self.assertEqual(dst_full["tabs"][0]["url"], "https://a.com/1")
        self.assertEqual(dst_full["tabs"][0]["title"], "A")

    def test_move_tab_not_found(self):
        """Returns False when the tab URL doesn't exist in source."""
        src = self.store.create_container("Src")
        dst = self.store.create_container("Dst")
        ok = self.store.move_tab(src["id"], dst["id"], "https://nope.com")
        self.assertFalse(ok)

    def test_move_tab_copies_cookies_for_origin(self):
        """Cookies matching the tab's origin are copied to destination."""
        src = self.store.create_container("Src")
        dst = self.store.create_container("Dst")
        cookies = [
            {"name": "s", "value": "1", "domain": "a.com", "path": "/"},
            {"name": "other", "value": "2", "domain": "unrelated.com", "path": "/"},
        ]
        self.store.save_hibernation(
            src["id"], cookies, {},
            [{"url": "https://a.com/page", "title": "A"}])
        self.store.move_tab(src["id"], dst["id"], "https://a.com/page")
        dst_full = self.store.get_container(dst["id"])
        dst_cookies = dst_full["cookies"]
        self.assertEqual(len(dst_cookies), 1)
        self.assertEqual(dst_cookies[0]["name"], "s")
        self.assertEqual(dst_cookies[0]["domain"], "a.com")

    def test_move_tab_copies_localstorage(self):
        """localStorage for the tab's origin is copied to destination."""
        src = self.store.create_container("Src")
        dst = self.store.create_container("Dst")
        storage = {"https://a.com": {"tok": "abc"}, "https://other.com": {"x": "1"}}
        self.store.save_hibernation(
            src["id"], [], storage,
            [{"url": "https://a.com/p", "title": "A"}])
        self.store.move_tab(src["id"], dst["id"], "https://a.com/p")
        dst_full = self.store.get_container(dst["id"])
        self.assertIn("https://a.com", dst_full["storage"])
        self.assertEqual(dst_full["storage"]["https://a.com"]["tok"], "abc")

    def test_move_tab_copies_idb(self):
        """IndexedDB data for the tab's origin is copied to destination."""
        src = self.store.create_container("Src")
        dst = self.store.create_container("Dst")
        idb = {"https://a.com": {"mydb": {"store1": {"rows": [{"k": 1}]}}}}
        self.store.save_hibernation(
            src["id"], [], {},
            [{"url": "https://a.com/p", "title": "A"}],
            idb=idb)
        self.store.move_tab(src["id"], dst["id"], "https://a.com/p")
        dst_full = self.store.get_container(dst["id"])
        self.assertIn("https://a.com", dst_full["idb"])
        self.assertEqual(dst_full["idb"]["https://a.com"]["mydb"]["store1"]["rows"],
                         [{"k": 1}])

    def test_move_tab_no_duplicate_cookies(self):
        """If destination already has a cookie with the same key, skip it."""
        src = self.store.create_container("Src")
        dst = self.store.create_container("Dst")
        cookie = {"name": "s", "value": "1", "domain": "a.com", "path": "/"}
        self.store.save_hibernation(
            src["id"], [cookie], {},
            [{"url": "https://a.com/p", "title": "A"}])
        self.store.save_hibernation(
            dst["id"],
            [{"name": "s", "value": "old", "domain": "a.com", "path": "/"}],
            {}, [])
        self.store.move_tab(src["id"], dst["id"], "https://a.com/p")
        dst_full = self.store.get_container(dst["id"])
        # Should still be just 1 cookie (no duplicate added)
        a_cookies = [c for c in dst_full["cookies"] if c["domain"] == "a.com"]
        self.assertEqual(len(a_cookies), 1)

    def test_move_only_first_matching_tab(self):
        """If two tabs have the same URL, only the first one moves."""
        src = self.store.create_container("Src")
        dst = self.store.create_container("Dst")
        self.store.save_hibernation(
            src["id"], [], {},
            [{"url": "https://dup.com", "title": "D1"},
             {"url": "https://dup.com", "title": "D2"}])
        self.store.move_tab(src["id"], dst["id"], "https://dup.com")
        src_full = self.store.get_container(src["id"])
        dst_full = self.store.get_container(dst["id"])
        self.assertEqual(len(src_full["tabs"]), 1)
        self.assertEqual(len(dst_full["tabs"]), 1)

    def test_move_preserves_destination_existing_tabs(self):
        """Existing tabs in destination are not lost."""
        src = self.store.create_container("Src")
        dst = self.store.create_container("Dst")
        self.store.save_hibernation(
            src["id"], [], {},
            [{"url": "https://a.com/1", "title": "A"}])
        self.store.save_hibernation(
            dst["id"], [], {},
            [{"url": "https://existing.com", "title": "E"}])
        self.store.move_tab(src["id"], dst["id"], "https://a.com/1")
        dst_full = self.store.get_container(dst["id"])
        urls = [t["url"] for t in dst_full["tabs"]]
        self.assertIn("https://existing.com", urls)
        self.assertIn("https://a.com/1", urls)
        self.assertEqual(len(urls), 2)


# ---------------------------------------------------------------------------
# ContainerManager.move_tab tests (cold/hot combinations)
# ---------------------------------------------------------------------------

class TestManagerMoveTabColdCold(_PatchedManagerMixin, unittest.TestCase):
    """Move tab between two cold (hibernated) sessions."""

    def test_cold_to_cold_basic(self):
        s1 = self.store.create_container("S1")
        s2 = self.store.create_container("S2")
        self.store.save_hibernation(
            s1["id"],
            [{"name": "c", "value": "v", "domain": "ex.com", "path": "/"}],
            {"https://ex.com": {"k": "val"}},
            [{"url": "https://ex.com/p", "title": "P"}],
            idb={"https://ex.com": {"db1": {"st": {"rows": [1]}}}})
        res = self.mgr.move_tab(s1["id"], s2["id"], url="https://ex.com/p")
        self.assertTrue(res.get("moved"))
        self.assertEqual(res["src"], s1["id"])
        self.assertEqual(res["dest"], s2["id"])
        # Source should be empty
        src = self.store.get_container(s1["id"])
        self.assertEqual(len(src["tabs"]), 0)
        # Destination should have the tab
        dst = self.store.get_container(s2["id"])
        self.assertEqual(len(dst["tabs"]), 1)
        self.assertEqual(dst["tabs"][0]["url"], "https://ex.com/p")
        # Storage copied
        self.assertIn("https://ex.com", dst["storage"])
        self.assertEqual(dst["storage"]["https://ex.com"]["k"], "val")

    def test_cold_to_cold_tab_not_found(self):
        s1 = self.store.create_container("S1")
        s2 = self.store.create_container("S2")
        res = self.mgr.move_tab(s1["id"], s2["id"], url="https://no.com")
        self.assertIn("error", res)

    def test_same_session_returns_error(self):
        s1 = self.store.create_container("S1")
        self.store.save_hibernation(
            s1["id"], [], {},
            [{"url": "https://a.com", "title": "A"}])
        res = self.mgr.move_tab(s1["id"], s1["id"], url="https://a.com")
        self.assertIn("error", res)

    def test_no_url_returns_error(self):
        s1 = self.store.create_container("S1")
        s2 = self.store.create_container("S2")
        res = self.mgr.move_tab(s1["id"], s2["id"])
        self.assertIn("error", res)


class TestManagerMoveTabHotCold(_PatchedManagerMixin, unittest.TestCase):
    """Move a live (hot) tab to a cold (hibernated) session."""

    def test_hot_to_cold(self):
        s1 = self.store.create_container("Hot")
        s2 = self.store.create_container("Cold")
        # Seed and restore s1 to make it hot
        self.store.save_hibernation(
            s1["id"],
            [{"name": "c1", "value": "v1", "domain": "hot.com",
              "path": "/", "url": "https://hot.com"}],
            {"https://hot.com": {"tok": "123"}},
            [{"url": "https://hot.com/page", "title": "Hot Page"}])
        self.mgr.restore(s1["id"])
        self.assertIn(s1["id"], self.mgr.hot)
        ctx = self.mgr.hot[s1["id"]]
        # Find the target for the tab
        tid = None
        for t in self.fb.targets.values():
            if t["browserContextId"] == ctx and t["url"] == "https://hot.com/page":
                tid = t["targetId"]
                # Seed localStorage in fake browser
                self.fb.local_storage[tid] = {"https://hot.com": {"tok": "123"}}
                break
        self.assertIsNotNone(tid)

        res = self.mgr.move_tab(s1["id"], s2["id"],
                                url="https://hot.com/page", target_id=tid)
        self.assertTrue(res.get("moved"))
        # Tab should be closed in source (removed from fake browser targets)
        self.assertNotIn(tid, self.fb.targets)
        # Destination (cold) should have the tab in DB
        dst = self.store.get_container(s2["id"])
        self.assertEqual(len(dst["tabs"]), 1)
        self.assertEqual(dst["tabs"][0]["url"], "https://hot.com/page")


class TestManagerMoveTabColdHot(_PatchedManagerMixin, unittest.TestCase):
    """Move a saved (cold) tab to a live (hot) session."""

    def test_cold_to_hot(self):
        s1 = self.store.create_container("Cold")
        s2 = self.store.create_container("Hot")
        self.store.save_hibernation(
            s1["id"], [], {"https://cold.com": {"x": "1"}},
            [{"url": "https://cold.com/page", "title": "Cold Page"}])
        # Restore s2 to make it hot
        self.mgr.restore(s2["id"])
        self.assertIn(s2["id"], self.mgr.hot)
        dest_ctx = self.mgr.hot[s2["id"]]

        res = self.mgr.move_tab(s1["id"], s2["id"],
                                url="https://cold.com/page")
        self.assertTrue(res.get("moved"))
        # Source should have no tabs
        src = self.store.get_container(s1["id"])
        self.assertEqual(len(src["tabs"]), 0)
        # A new tab should have been created in the dest browser context
        dest_tabs = [t for t in self.fb.targets.values()
                     if t["browserContextId"] == dest_ctx
                     and t["url"] == "https://cold.com/page"]
        self.assertEqual(len(dest_tabs), 1)


class TestManagerMoveTabColdHotSourceCleared(_PatchedManagerMixin, unittest.TestCase):
    """Regression: cold→hot must fully remove tab from source DB."""

    def test_cold_to_hot_source_cleared_after_restore(self):
        """Tab must be gone from source even after restoring the source session."""
        s1 = self.store.create_container("ColdSrc")
        s2 = self.store.create_container("HotDst")
        self.store.save_hibernation(
            s1["id"],
            [{"name": "c", "value": "v", "domain": "ex.com",
              "path": "/", "url": "https://ex.com"}],
            {"https://ex.com": {"t": "1"}},
            [{"url": "https://ex.com/p", "title": "P"},
             {"url": "https://other.com/q", "title": "Q"}])
        self.mgr.restore(s2["id"])
        res = self.mgr.move_tab(s1["id"], s2["id"], url="https://ex.com/p")
        self.assertTrue(res.get("moved"))
        # Source DB should only have the other tab
        src = self.store.get_container(s1["id"])
        self.assertEqual(len(src["tabs"]), 1)
        self.assertEqual(src["tabs"][0]["url"], "https://other.com/q")
        # Restoring the source should NOT bring back the moved tab
        self.mgr.restore(s1["id"])
        src_ctx = self.mgr.hot[s1["id"]]
        src_live = [t for t in self.fb.targets.values()
                    if t["browserContextId"] == src_ctx and t["type"] == "page"]
        src_urls = [t["url"] for t in src_live]
        self.assertNotIn("https://ex.com/p", src_urls)
        self.assertIn("https://other.com/q", src_urls)

    def test_cold_to_hot_single_tab_leaves_empty(self):
        """Moving the only tab from a cold session leaves it with zero tabs."""
        s1 = self.store.create_container("Single")
        s2 = self.store.create_container("Dest")
        self.store.save_hibernation(
            s1["id"], [], {},
            [{"url": "https://only.com/tab", "title": "Only"}])
        self.mgr.restore(s2["id"])
        res = self.mgr.move_tab(s1["id"], s2["id"], url="https://only.com/tab")
        self.assertTrue(res.get("moved"))
        src = self.store.get_container(s1["id"])
        self.assertEqual(len(src["tabs"]), 0)


class TestManagerMoveTabHotHot(_PatchedManagerMixin, unittest.TestCase):
    """Move a live tab between two hot sessions."""

    def test_hot_to_hot(self):
        s1 = self.store.create_container("Hot1")
        s2 = self.store.create_container("Hot2")
        self.store.save_hibernation(
            s1["id"],
            [{"name": "c1", "value": "v1", "domain": "h1.com",
              "path": "/", "url": "https://h1.com"}],
            {"https://h1.com": {"k": "v"}},
            [{"url": "https://h1.com/page", "title": "H1"}])
        self.mgr.restore(s1["id"])
        self.mgr.restore(s2["id"])
        self.assertIn(s1["id"], self.mgr.hot)
        self.assertIn(s2["id"], self.mgr.hot)
        src_ctx = self.mgr.hot[s1["id"]]
        dest_ctx = self.mgr.hot[s2["id"]]

        # Find the target
        tid = None
        for t in self.fb.targets.values():
            if t["browserContextId"] == src_ctx and t["url"] == "https://h1.com/page":
                tid = t["targetId"]
                self.fb.local_storage[tid] = {"https://h1.com": {"k": "v"}}
                break
        self.assertIsNotNone(tid)

        res = self.mgr.move_tab(s1["id"], s2["id"],
                                url="https://h1.com/page", target_id=tid)
        self.assertTrue(res.get("moved"))
        # Tab closed in source
        self.assertNotIn(tid, self.fb.targets)
        # New tab opened in dest context
        dest_tabs = [t for t in self.fb.targets.values()
                     if t["browserContextId"] == dest_ctx
                     and t["url"] == "https://h1.com/page"]
        self.assertEqual(len(dest_tabs), 1)


class TestManagerMoveTabResolveUrl(_PatchedManagerMixin, unittest.TestCase):
    """Test URL resolution from targetId when URL is not provided."""

    def test_resolve_url_from_target_id(self):
        """When URL is empty but targetId is given, the URL is resolved from CDP."""
        s1 = self.store.create_container("Src")
        s2 = self.store.create_container("Dst")
        self.store.save_hibernation(
            s1["id"], [],
            {"https://resolve.com": {"r": "1"}},
            [{"url": "https://resolve.com/tab", "title": "R"}])
        self.mgr.restore(s1["id"])
        ctx = self.mgr.hot[s1["id"]]
        tid = None
        for t in self.fb.targets.values():
            if t["browserContextId"] == ctx and t["url"] == "https://resolve.com/tab":
                tid = t["targetId"]
                self.fb.local_storage[tid] = {"https://resolve.com": {"r": "1"}}
                break
        self.assertIsNotNone(tid)
        # Call with url="" — should resolve from targetId
        res = self.mgr.move_tab(s1["id"], s2["id"], url="", target_id=tid)
        self.assertTrue(res.get("moved"))
        self.assertEqual(res["url"], "https://resolve.com/tab")


# ---------------------------------------------------------------------------
# HTTP API tests for /api/move-tab
# ---------------------------------------------------------------------------

class TestApiMoveTab(_PatchedManagerMixin, unittest.TestCase):
    """Test the POST /api/move-tab endpoint."""

    def setUp(self):
        super().setUp()
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        self.port = s.getsockname()[1]
        s.close()
        self.server = make_server(self.mgr, port=self.port)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def _req(self, method, path, body=None):
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

    def test_move_tab_cold_to_cold_via_api(self):
        _, s1 = self._req("POST", "/api/containers", {"name": "Src"})
        _, s2 = self._req("POST", "/api/containers", {"name": "Dst"})
        # Hibernate both (create auto-restores them)
        self._req("POST", f"/api/containers/{s1['id']}/hibernate")
        self._req("POST", f"/api/containers/{s2['id']}/hibernate")
        self.store.save_hibernation(
            s1["id"], [], {},
            [{"url": "https://api.com/page", "title": "API"}])
        s, res = self._req("POST", "/api/move-tab",
                           {"src": s1["id"], "dest": s2["id"],
                            "url": "https://api.com/page"})
        self.assertEqual(s, 200)
        self.assertTrue(res.get("moved"))
        # Verify via GET
        _, lst = self._req("GET", "/api/containers")
        for c in lst["containers"]:
            if c["id"] == s1["id"]:
                self.assertEqual(c["tab_count"], 0)
            if c["id"] == s2["id"]:
                self.assertEqual(c["tab_count"], 1)

    def test_move_tab_missing_url(self):
        _, s1 = self._req("POST", "/api/containers", {"name": "Src"})
        _, s2 = self._req("POST", "/api/containers", {"name": "Dst"})
        self._req("POST", f"/api/containers/{s1['id']}/hibernate")
        self._req("POST", f"/api/containers/{s2['id']}/hibernate")
        s, res = self._req("POST", "/api/move-tab",
                           {"src": s1["id"], "dest": s2["id"]})
        self.assertEqual(s, 200)
        self.assertIn("error", res)

    def test_move_tab_same_session(self):
        _, s1 = self._req("POST", "/api/containers", {"name": "Src"})
        self._req("POST", f"/api/containers/{s1['id']}/hibernate")
        self.store.save_hibernation(
            s1["id"], [], {},
            [{"url": "https://x.com", "title": "X"}])
        s, res = self._req("POST", "/api/move-tab",
                           {"src": s1["id"], "dest": s1["id"],
                            "url": "https://x.com"})
        self.assertEqual(s, 200)
        self.assertIn("error", res)

    def test_move_tab_cold_to_hot_via_api(self):
        """Move from cold session to a hot session via API."""
        _, s1 = self._req("POST", "/api/containers", {"name": "ColdSrc"})
        _, s2 = self._req("POST", "/api/containers", {"name": "HotDst"})
        self._req("POST", f"/api/containers/{s1['id']}/hibernate")
        self.store.save_hibernation(
            s1["id"], [], {},
            [{"url": "https://cold-api.com/p", "title": "ColdAPI"}])
        # s2 is auto-restored (hot)
        self.assertIn(s2["id"], self.mgr.hot)
        s, res = self._req("POST", "/api/move-tab",
                           {"src": s1["id"], "dest": s2["id"],
                            "url": "https://cold-api.com/p"})
        self.assertEqual(s, 200)
        self.assertTrue(res.get("moved"))


# ---------------------------------------------------------------------------
# Dashboard HTML tests for cut/paste UI
# ---------------------------------------------------------------------------

class TestDashboardCutPaste(unittest.TestCase):
    """Verify dashboard HTML contains cut/paste UI elements."""

    @classmethod
    def setUpClass(cls):
        from sessions.dashboard import DASHBOARD_HTML
        cls.html = DASHBOARD_HTML

    def test_has_cut_icon(self):
        self.assertIn('_svgCut', self.html)
        self.assertIn('tab-cut', self.html)

    def test_has_paste_icon(self):
        self.assertIn('_svgPaste', self.html)
        self.assertIn('paste-btn', self.html)

    def test_has_cut_tab_function(self):
        self.assertIn('function cutTab(', self.html)

    def test_has_cancel_cut_function(self):
        self.assertIn('function cancelCut(', self.html)

    def test_has_paste_tab_function(self):
        self.assertIn('async function pasteTab(', self.html)

    def test_cut_state_variable(self):
        self.assertIn('_cutTab', self.html)

    def test_escape_cancels_cut(self):
        self.assertIn('cancelCut()', self.html)

    def test_cut_active_css_class(self):
        self.assertIn('cut-active', self.html)

    def test_paste_target_css_class(self):
        self.assertIn('paste-target', self.html)

    def test_paste_calls_move_api(self):
        self.assertIn('/api/move-tab', self.html)

    def test_cut_icon_title(self):
        self.assertIn('Cut — move to another session', self.html)

    def test_paste_icon_title(self):
        self.assertIn('title="Paste tab here"', self.html)

    def test_cut_toast_message(self):
        self.assertIn('Tab cut', self.html)

    def test_move_toast_message(self):
        self.assertIn('Tab moved', self.html)

    def test_paste_pulse_animation(self):
        self.assertIn('pastePulse', self.html)

    def test_action_separator(self):
        self.assertIn('action-sep', self.html)

    def test_paste_mode_tab_click_intercept(self):
        """Clicking any tab in a paste-target session triggers paste."""
        self.assertIn("_cutTab&&_cutTab.cid!==", self.html)


class TestDashboardTwoColumnLayout(unittest.TestCase):
    """Verify dashboard uses a two-column hot/cold layout."""

    @classmethod
    def setUpClass(cls):
        from sessions.dashboard import DASHBOARD_HTML
        cls.html = DASHBOARD_HTML

    def test_grid_layout(self):
        self.assertIn('grid-template-columns:1fr 1fr', self.html)

    def test_col_classes(self):
        self.assertIn('col-hot', self.html)
        self.assertIn('col-cold', self.html)

    def test_col_headers(self):
        self.assertIn('col-header', self.html)
        self.assertIn('Active', self.html)
        self.assertIn('Hibernated', self.html)

    def test_col_empty_messages(self):
        self.assertIn('col-empty', self.html)
        self.assertIn('No active sessions', self.html)
        self.assertIn('No hibernated sessions', self.html)

    def test_build_row_function(self):
        self.assertIn('function _buildRow(', self.html)

    def test_build_search_row_function(self):
        self.assertIn('function _buildSearchRow(', self.html)

    def test_search_hot_cold_classes(self):
        self.assertIn('search-hot', self.html)
        self.assertIn('search-cold', self.html)

    def test_browse_items_hot_first(self):
        """Browse items built as hot sessions first, then cold."""
        self.assertIn('[...hot, ...cold].forEach', self.html)

    def test_search_hot_first(self):
        """Search matches ordered hot first."""
        self.assertIn('[...hotM, ...coldM]', self.html)


if __name__ == "__main__":
    unittest.main()
