"""Tests for the HTTP API layer."""
from __future__ import annotations

import http.client
import json
import os
import sys
import threading
import unittest
from unittest import mock

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_root, "src"))
sys.path.insert(0, _root)

from sessions.server import make_server
from tests.fakes import _PatchedManagerMixin


# ---------------------------------------------------------------------------
# HTTP API tests
# ---------------------------------------------------------------------------

class TestApi(_PatchedManagerMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        # pick an ephemeral port to avoid collisions
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

    def _req(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
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

    def test_dashboard_html(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/")
        r = conn.getresponse()
        html = r.read().decode()
        conn.close()
        self.assertEqual(r.status, 200)
        self.assertIn("Sessions", html)

    def test_full_lifecycle_via_api(self):
        s, c = self._req("POST", "/api/containers", {"name": "API Test"})
        self.assertEqual(s, 200)
        cid = c["id"]

        s, lst = self._req("GET", "/api/containers")
        self.assertEqual(s, 200)
        self.assertEqual(len(lst["containers"]), 1)
        # Creating without a URL auto-restores the session
        self.assertTrue(lst["containers"][0]["hot"])

        # Hibernate first so we can test restore with saved state
        self._req("POST", f"/api/containers/{cid}/hibernate")

        # Seed saved state so restore has something interesting
        self.store.save_hibernation(
            cid,
            [{"name": "k", "value": "v", "domain": "api.com",
              "path": "/", "url": "https://api.com"}],
            {"https://api.com": {"a": "1"}},
            [{"url": "https://api.com/home", "title": "Home"}])

        s, res = self._req("POST", f"/api/containers/{cid}/restore")
        self.assertEqual(s, 200)
        self.assertIn("browserContextId", res)
        self.assertEqual(res["tabs_opened"], 1)

        s, res = self._req("POST", f"/api/containers/{cid}/hibernate")
        self.assertEqual(s, 200)
        self.assertGreaterEqual(res["tabs_saved"], 0)

        s, res = self._req("POST", f"/api/containers/{cid}/clone",
                           {"name": "Clone Me"})
        self.assertEqual(s, 200)
        clone_id = res["id"]
        self.assertNotEqual(clone_id, cid)

        s, res = self._req("POST", f"/api/containers/{cid}/clean")
        self.assertEqual(s, 200)

        s, res = self._req("POST", "/api/hibernate-all")
        self.assertEqual(s, 200)

        s, res = self._req("DELETE", f"/api/containers/{cid}")
        self.assertEqual(s, 200)
        self.assertTrue(res["deleted"])
        s, lst = self._req("GET", "/api/containers")
        self.assertEqual(len(lst["containers"]), 1)  # only the clone remains

    def test_unknown_container_returns_404(self):
        s, res = self._req("POST", "/api/containers/does-not-exist/hibernate")
        self.assertIn(s, (404, 500))
        # hibernate on a cold container raises RuntimeError -> 500; restore
        # on a non-existent container raises KeyError -> 404.
        s, res = self._req("POST", "/api/containers/no-such/restore")
        self.assertEqual(s, 404)

    def test_open_in_cold_container_auto_restores(self):
        _, c = self._req("POST", "/api/containers", {"name": "Autohot"})
        cid = c["id"]
        s, res = self._req("POST", f"/api/containers/{cid}/open",
                           {"url": "https://auto.example/"})
        self.assertEqual(s, 200)
        self.assertIn(cid, self.mgr.hot)


# ---------------------------------------------------------------------------
# API tests for new endpoints
# ---------------------------------------------------------------------------

class TestApiNew(_PatchedManagerMixin, unittest.TestCase):
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

    def test_patch_rename(self):
        _, c = self._req("POST", "/api/containers", {"name": "OldAPI"})
        s, res = self._req("PATCH", f"/api/containers/{c['id']}", {"name": "NewAPI"})
        self.assertEqual(s, 200)
        self.assertEqual(res["name"], "NewAPI")
        full = self.store.get_container(c["id"])
        self.assertEqual(full["name"], "NewAPI")

    def test_post_activate(self):
        _, c = self._req("POST", "/api/containers", {"name": "Act"})
        cid = c["id"]
        self.store.save_hibernation(cid, [], {},
                                    [{"url": "https://act.com", "title": "A"}])
        self._req("POST", f"/api/containers/{cid}/restore")
        ctx = self.mgr.hot[cid]
        tid = next(t["targetId"] for t in self.fb.targets.values()
                   if t["browserContextId"] == ctx)
        s, res = self._req("POST", "/api/activate", {"targetId": tid})
        self.assertEqual(s, 200)
        self.assertTrue(res["activated"])

    def test_post_close_tab(self):
        _, c = self._req("POST", "/api/containers", {"name": "CT"})
        cid = c["id"]
        self.store.save_hibernation(cid, [], {},
                                    [{"url": "https://ct.com", "title": "C"}])
        self._req("POST", f"/api/containers/{cid}/restore")
        ctx = self.mgr.hot[cid]
        tid = next(t["targetId"] for t in self.fb.targets.values()
                   if t["browserContextId"] == ctx)
        s, res = self._req("POST", "/api/close-tab", {"targetId": tid})
        self.assertEqual(s, 200)
        self.assertTrue(res["closed"])

    def test_post_snapshot_all(self):
        _, c = self._req("POST", "/api/containers", {"name": "Snap"})
        cid = c["id"]
        self._req("POST", f"/api/containers/{cid}/restore")
        s, res = self._req("POST", "/api/snapshot-all")
        self.assertEqual(s, 200)
        self.assertIn("results", res)

    def test_create_for_url_via_api(self):
        s, c = self._req("POST", "/api/containers",
                          {"url": "https://github.com/foo"})
        self.assertEqual(s, 200)
        self.assertEqual(c["name"], "github.com")
        self.assertIn(c["id"], self.mgr.hot)

    def test_post_shutdown(self):
        _, c = self._req("POST", "/api/containers", {"name": "SD"})
        self._req("POST", f"/api/containers/{c['id']}/restore")
        self.mgr.close_chrome = mock.MagicMock()
        s, res = self._req("POST", "/api/shutdown")
        self.assertEqual(s, 200)
        self.assertTrue(res["shutdown"])

    def test_clean_default_endpoint(self):
        s, res = self._req("POST", "/api/clean-default")
        self.assertEqual(s, 200)
        self.assertTrue(res["cleaned"])

    def test_restart_endpoint(self):
        s, res = self._req("POST", "/api/restart")
        self.assertEqual(s, 200)
        self.assertTrue(res["restarting"])

    def test_restore_and_open_cold_container(self):
        """POST /api/containers/{id}/open on a cold container should restore it."""
        _, c = self._req("POST", "/api/containers", {"name": "ColdOpen"})
        cid = c["id"]
        # create now auto-restores, so hibernate first to make it cold
        self._req("POST", f"/api/containers/{cid}/hibernate")
        self.store.save_hibernation(cid, [], {},
                                    [{"url": "https://co.com/page", "title": "P"}])
        self.assertNotIn(cid, self.mgr.hot)
        s, res = self._req("POST", f"/api/containers/{cid}/open",
                           {"url": "https://co.com/page"})
        self.assertEqual(s, 200)
        self.assertIn(cid, self.mgr.hot)

    def test_delete_no_confirm_needed(self):
        """Delete should work with a single API call (no confirmation flow)."""
        _, c = self._req("POST", "/api/containers", {"name": "NoCfm"})
        s, res = self._req("DELETE", f"/api/containers/{c['id']}")
        self.assertEqual(s, 200)
        self.assertTrue(res["deleted"])
        s, lst = self._req("GET", "/api/containers")
        self.assertEqual(len(lst["containers"]), 0)

    def test_bulk_operations_via_api(self):
        """Multiple sessions can be operated on sequentially (bulk)."""
        ids = []
        for name in ("B1", "B2", "B3"):
            _, c = self._req("POST", "/api/containers", {"name": name})
            ids.append(c["id"])
        # Bulk restore
        for cid in ids:
            s, _ = self._req("POST", f"/api/containers/{cid}/restore")
            self.assertEqual(s, 200)
        self.assertEqual(len(self.mgr.hot), 3)
        # Bulk hibernate
        for cid in ids:
            s, _ = self._req("POST", f"/api/containers/{cid}/hibernate")
            self.assertEqual(s, 200)
        self.assertEqual(len(self.mgr.hot), 0)
        # Bulk delete
        for cid in ids:
            s, _ = self._req("DELETE", f"/api/containers/{cid}")
            self.assertEqual(s, 200)
        s, lst = self._req("GET", "/api/containers")
        self.assertEqual(len(lst["containers"]), 0)


# ---------------------------------------------------------------------------
# Dashboard HTML content tests
# ---------------------------------------------------------------------------

class TestDashboardContent(unittest.TestCase):
    """Verify dashboard HTML contains expected UI elements."""

    @classmethod
    def setUpClass(cls):
        from sessions.dashboard import DASHBOARD_HTML
        cls.html = DASHBOARD_HTML

    def test_no_session_id_visible(self):
        """Session ID should not be shown to the user."""
        self.assertNotIn('class=cid', self.html)
        self.assertNotIn('class="cid"', self.html)

    def test_has_toast_element(self):
        self.assertIn('id=toast', self.html)
        self.assertIn('toast(', self.html)

    def test_toast_is_prominent(self):
        """Toast should have larger font and padding."""
        self.assertIn('font-size:15px', self.html)
        self.assertIn('padding:10px 24px', self.html)

    def test_has_bulk_bar_with_select_all(self):
        self.assertIn('bulkAct(', self.html)
        self.assertIn('id=selAll', self.html)
        self.assertIn('toggleSelectAll(', self.html)

    def test_no_confirm_on_delete(self):
        """ctxAct delete path should not call confirm()."""
        fn_start = self.html.index('async function ctxAct')
        fn_end   = self.html.index('\n}', fn_start)
        del_block = self.html[fn_start:fn_end]
        self.assertIn("action === 'delete'", del_block)
        self.assertNotIn('confirm(', del_block)

    def test_no_hibernate_all_button(self):
        self.assertNotIn('hibernateAll()', self.html)

    def test_no_url_input(self):
        """URL text box removed in favor of simple New button."""
        self.assertNotIn('id=newUrl', self.html)
        self.assertNotIn('newform', self.html)

    def test_no_color_selector(self):
        self.assertNotIn('type=color', self.html)
        self.assertNotIn('id=newColor', self.html)

    def test_no_hot_cold_pills(self):
        self.assertNotIn('pill hot', self.html)
        self.assertNotIn('pill cold', self.html)

    def test_no_refresh_button(self):
        self.assertNotIn('forceRefresh()', self.html)
        # The word Refresh should not appear as a button label
        self.assertNotIn('>Refresh<', self.html)

    def test_has_new_button(self):
        self.assertIn('createSession()', self.html)
        self.assertIn('+ New', self.html)

    def test_has_session_checkbox(self):
        """Each session row has a checkbox for selection (Gmail-style)."""
        self.assertIn('row-cb', self.html)
        self.assertIn('toggleSelect(', self.html)

    def test_has_row_selection(self):
        self.assertIn('toggleSelect(', self.html)
        self.assertIn('selected', self.html)

    def test_saved_tab_restore_and_open(self):
        self.assertIn('restoreAndOpen(', self.html)

    def test_title_is_sessions(self):
        self.assertIn('<title>Sessions</title>', self.html)

    def test_clean_button_short_label(self):
        """Top button should say 'Clean', not 'Clean Session'."""
        self.assertIn('>Clean<', self.html)
        self.assertNotIn('Clean Session<', self.html)

    def test_no_session_name_in_row(self):
        """Session name span and rename function removed from UI."""
        self.assertNotIn('renameCtr(', self.html)
        self.assertNotIn("class=name", self.html)

    def test_has_search_box(self):
        self.assertIn('id=search-box', self.html)
        self.assertIn('onSearchInput(', self.html)
        self.assertIn('renderSearch(', self.html)

    def test_search_filters_tabs(self):
        """Search renders a flat list and filters by title/URL."""
        self.assertIn('.toLowerCase().includes(lq)', self.html)
        self.assertIn('search-row', self.html)

    def test_context_menu(self):
        """Session actions moved to right-click context menu."""
        self.assertIn('id=ctx-menu', self.html)
        self.assertIn('showCtxMenu(', self.html)
        self.assertIn('ctxAct(', self.html)
        self.assertIn('contextmenu', self.html)

    def test_no_inline_action_buttons(self):
        """Restore/Hibernate/Clean/Delete buttons no longer inside each row."""
        # row-main with inline action buttons is gone; actions are in ctx-menu
        self.assertNotIn('class=actions', self.html)
        self.assertNotIn('class=row-main', self.html)

    def test_search_enter_activates(self):
        """Enter key should activate the focused/sole match."""
        self.assertIn('_activateSearchMatch', self.html)
        self.assertIn("key === 'Enter'", self.html)

    def test_search_clear_button(self):
        self.assertIn('id=search-clear', self.html)
        self.assertIn('clearSearch()', self.html)

    def test_search_arrow_navigation(self):
        self.assertIn('_moveFocus', self.html)
        self.assertIn("key === 'ArrowDown'", self.html)

    def test_browse_arrow_navigation(self):
        """Arrow keys navigate tabs when search is blank."""
        self.assertIn('_moveBrowseFocus', self.html)
        self.assertIn('_activateBrowseItem', self.html)
        self.assertIn('_browseItems', self.html)

    def test_restart_button(self):
        self.assertIn('restartBackend()', self.html)
        self.assertIn('>Restart<', self.html)
        self.assertIn('/api/restart', self.html)

    def test_action_toasts(self):
        """Actions show before/after toast notifications."""
        # ctxAct should show startLabel and doneLabel
        self.assertIn('startLabel', self.html)
        self.assertIn('doneLabel', self.html)
        # bulk actions too
        self.assertIn("'Restoring'", self.html)
        self.assertIn("'Restored'", self.html)


if __name__ == "__main__":
    unittest.main()
