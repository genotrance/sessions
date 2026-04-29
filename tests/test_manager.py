"""Tests for ContainerManager and helper functions."""
from __future__ import annotations

import concurrent.futures
import os
import sys
import threading
import time
import unittest
from unittest import mock

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_root, "src"))
sys.path.insert(0, _root)

from sessions.utils import (domain_of as _domain_of, origin_of as _origin_of,
                            normalize_url as _normalize_url,
                            build_search_url as _build_search_url,
                            _get_chrome_search_template, _FALLBACK_SEARCH_URL)
from tests.fakes import _PatchedManagerMixin


# ---------------------------------------------------------------------------
# ContainerManager tests (unit, with FakeBrowser)
# ---------------------------------------------------------------------------

class TestContainerManager(_PatchedManagerMixin, unittest.TestCase):

    def test_restore_creates_context_and_opens_saved_tabs(self):
        c = self.store.create_container("W")
        self.store.save_hibernation(
            c["id"],
            [{"name": "s", "value": "v", "domain": "example.com",
              "path": "/", "url": "https://example.com"}],
            {"https://example.com": {"token": "abc"}},
            [{"url": "https://example.com/a", "title": "A"},
             {"url": "https://example.com/b", "title": "B"}])

        res = self.mgr.restore(c["id"])

        self.assertEqual(res["tabs_opened"], 2)
        ctx = res["browserContextId"]
        self.assertIn(ctx, self.fb.contexts)
        # Cookies injected
        self.assertEqual(len(self.fb.cookies[ctx]), 1)
        # Two tabs created with correct urls
        urls = sorted(t["url"] for t in self.fb.targets.values()
                      if t["browserContextId"] == ctx)
        self.assertEqual(urls, ["https://example.com/a", "https://example.com/b"])
        # LocalStorage injection occurred on at least one tab
        self.assertTrue(any("token" in s
                             for lst in self.fb.new_doc_scripts.values()
                             for s in lst))
        # Tabs preserved in DB after restore (bug fix: no longer cleared)
        self.assertEqual(len(self.store.get_container(c["id"])["tabs"]), 2)
        # Marked active
        self.assertEqual(self.store.get_container(c["id"])["is_active"], 1)

    def test_restore_then_hibernate_preserves_state(self):
        c = self.store.create_container("RT")
        self.store.save_hibernation(
            c["id"],
            [{"name": "k", "value": "v", "domain": "foo.com",
              "path": "/", "url": "https://foo.com"}],
            {"https://foo.com": {"x": "1"}},
            [{"url": "https://foo.com/page", "title": "P"}])
        self.mgr.restore(c["id"])
        ctx = self.mgr.hot[c["id"]]

        # Simulate user navigating to another url in that same tab
        # (by updating the fake browser state directly)
        for t in self.fb.targets.values():
            if t["browserContextId"] == ctx:
                # Also seed a fresh localStorage the runtime will report
                t["url"] = "https://foo.com/page"
                self.fb.local_storage[t["targetId"]] = {
                    "https://foo.com": {"x": "1", "y": "2"}}

        result = self.mgr.hibernate(c["id"])

        # Context was disposed
        self.assertIn(ctx, self.fb.disposed)
        self.assertNotIn(c["id"], self.mgr.hot)
        # State was persisted
        full = self.store.get_container(c["id"])
        self.assertEqual(len(full["tabs"]), 1)
        self.assertEqual(full["tabs"][0]["url"], "https://foo.com/page")
        self.assertEqual(full["storage"]["https://foo.com"],
                         {"x": "1", "y": "2"})
        self.assertEqual(full["is_active"], 0)
        self.assertEqual(result["tabs_saved"], 1)

    def test_hibernate_preserves_idb_schema(self):
        """IDB round-trip must preserve _meta.version, indexes, compound keyPaths."""
        c = self.store.create_container("IDB")
        idb_snapshot = {
            "_meta": {"version": 69},
            "messages": {
                "rows": [{"chatId": "c1", "id": "m1", "body": "hello"}],
                "keys": [["c1", "m1"]],
                "keyPath": ["chatId", "id"],
                "autoIncrement": False,
                "indexes": [
                    {"name": "by_time", "keyPath": "timestamp",
                     "unique": False, "multiEntry": False},
                    {"name": "by_sender", "keyPath": "sender",
                     "unique": False, "multiEntry": False},
                ],
            },
            "contacts": {
                "rows": [{"id": 1, "name": "Alice"}],
                "keys": [1],
                "keyPath": "id",
                "autoIncrement": True,
                "indexes": [
                    {"name": "by_name", "keyPath": "name",
                     "unique": True, "multiEntry": False},
                ],
            },
        }
        self.store.save_hibernation(
            c["id"],
            [{"name": "k", "value": "v", "domain": "web.whatsapp.com",
              "path": "/", "url": "https://web.whatsapp.com"}],
            {"https://web.whatsapp.com": {"wa_prefs": "{}"}},
            [{"url": "https://web.whatsapp.com", "title": "WhatsApp"}],
            idb={"https://web.whatsapp.com": idb_snapshot})

        # Verify round-trip through DB
        full = self.store.get_container(c["id"])
        saved_idb = full["idb"]["https://web.whatsapp.com"]
        self.assertEqual(saved_idb["_meta"]["version"], 69)
        self.assertEqual(saved_idb["messages"]["keyPath"], ["chatId", "id"])
        self.assertEqual(len(saved_idb["messages"]["indexes"]), 2)
        self.assertTrue(saved_idb["contacts"]["autoIncrement"])
        self.assertTrue(saved_idb["contacts"]["indexes"][0]["unique"])

        # Verify the restore script includes the schema
        from sessions.idb import build_restore_script
        script = build_restore_script(saved_idb)
        # Must reference the version, indexes, and compound keyPaths
        self.assertIn('"version": 69', script)
        self.assertIn("createIndex", script)
        self.assertIn('"chatId"', script)

    def test_hibernate_error_when_cold(self):
        c = self.store.create_container("cold")
        with self.assertRaises(RuntimeError):
            self.mgr.hibernate(c["id"])

    def test_clone_of_cold_container(self):
        c = self.store.create_container("orig")
        self.store.save_hibernation(
            c["id"],
            [{"name": "a", "value": "b", "domain": "e.com",
              "path": "/", "url": "https://e.com"}],
            {"https://e.com": {"k": "v"}},
            [{"url": "https://e.com/", "title": "E"}])
        cloned = self.mgr.clone(c["id"], "copy")
        self.assertNotEqual(cloned["id"], c["id"])
        self.assertEqual(cloned["name"], "copy")
        self.assertEqual(len(cloned["cookies"]), 1)

    def test_clean_wipes_cookies_and_preserves_hot_context(self):
        c = self.store.create_container("dirty")
        self.store.save_hibernation(
            c["id"],
            [{"name": "a", "value": "b", "domain": "d.com",
              "path": "/", "url": "https://d.com"}],
            {"https://d.com": {"k": "v"}},
            [{"url": "https://d.com/", "title": "D"}])
        self.mgr.restore(c["id"])
        ctx = self.mgr.hot[c["id"]]
        self.mgr.clean(c["id"])
        # Context must NOT be disposed — session stays running
        self.assertNotIn(ctx, self.fb.disposed)
        self.assertIn(c["id"], self.mgr.hot)
        full = self.store.get_container(c["id"])
        self.assertEqual(full["cookies"], [])
        self.assertEqual(full["storage"], {})

    def test_clean_wipes_cold_session_blobs(self):
        c = self.store.create_container("cold-dirty")
        self.store.save_hibernation(
            c["id"],
            [{"name": "x", "value": "y", "domain": "e.com",
              "path": "/", "url": "https://e.com"}],
            {"https://e.com": {"k": "v"}},
            [{"url": "https://e.com/", "title": "E"}])
        self.mgr.clean(c["id"])
        # Cold session: nothing disposed, blobs wiped, tabs preserved
        self.assertEqual(self.fb.disposed, [])
        full = self.store.get_container(c["id"])
        self.assertEqual(full["cookies"], [])
        self.assertEqual(full["storage"], {})
        self.assertEqual(len(full["tabs"]), 1)

    def test_delete_removes_container_and_disposes_context(self):
        c = self.store.create_container("gone")
        self.mgr.restore(c["id"])
        ctx = self.mgr.hot[c["id"]]
        self.mgr.delete(c["id"])
        self.assertIn(ctx, self.fb.disposed)
        self.assertIsNone(self.store.get_container(c["id"]))

    def test_hibernate_all(self):
        a = self.store.create_container("a")
        b = self.store.create_container("b")
        self.mgr.restore(a["id"])
        self.mgr.restore(b["id"])
        results = self.mgr.hibernate_all()
        self.assertEqual(len(results), 2)
        self.assertEqual(self.mgr.hot, {})

    def test_status_reports_hot_cold(self):
        a = self.store.create_container("hot-a")
        b = self.store.create_container("cold-b")
        self.mgr.restore(a["id"])
        s = self.mgr.status()
        hot_ids = {c["id"] for c in s["containers"] if c["hot"]}
        cold_ids = {c["id"] for c in s["containers"] if not c["hot"]}
        self.assertEqual(hot_ids, {a["id"]})
        self.assertEqual(cold_ids, {b["id"]})

    def test_isolation_between_containers(self):
        """Cookies set in one container must not leak into another."""
        a = self.store.create_container("A")
        b = self.store.create_container("B")
        self.store.save_hibernation(
            a["id"],
            [{"name": "sa", "value": "A1", "domain": "site.com",
              "path": "/", "url": "https://site.com"}],
            {}, [{"url": "https://site.com/", "title": "S"}])
        self.store.save_hibernation(
            b["id"],
            [{"name": "sb", "value": "B1", "domain": "site.com",
              "path": "/", "url": "https://site.com"}],
            {}, [{"url": "https://site.com/", "title": "S"}])
        self.mgr.restore(a["id"])
        self.mgr.restore(b["id"])
        ctx_a = self.mgr.hot[a["id"]]
        ctx_b = self.mgr.hot[b["id"]]
        self.assertNotEqual(ctx_a, ctx_b)
        a_cookie_names = {c["name"] for c in self.fb.cookies[ctx_a]}
        b_cookie_names = {c["name"] for c in self.fb.cookies[ctx_b]}
        self.assertEqual(a_cookie_names, {"sa"})
        self.assertEqual(b_cookie_names, {"sb"})


# ---------------------------------------------------------------------------
# ContainerManager tests for new features
# ---------------------------------------------------------------------------

class TestContainerManagerNew(_PatchedManagerMixin, unittest.TestCase):

    def test_rename(self):
        c = self.store.create_container("orig")
        result = self.mgr.rename(c["id"], "renamed")
        self.assertEqual(result["name"], "renamed")
        self.assertEqual(self.store.get_container(c["id"])["name"], "renamed")

    def test_close_tab(self):
        c = self.store.create_container("ct")
        self.mgr.restore(c["id"])
        ctx = self.mgr.hot[c["id"]]
        tids = [t["targetId"] for t in self.fb.targets.values()
                if t["browserContextId"] == ctx]
        self.assertTrue(len(tids) > 0)
        tid = tids[0]
        result = self.mgr.close_tab(tid)
        self.assertTrue(result["closed"])
        self.assertNotIn(tid, self.fb.targets)

    def test_activate_tab(self):
        c = self.store.create_container("at")
        self.mgr.restore(c["id"])
        ctx = self.mgr.hot[c["id"]]
        tid = next(t["targetId"] for t in self.fb.targets.values()
                   if t["browserContextId"] == ctx)
        result = self.mgr.activate_tab(tid)
        self.assertTrue(result["activated"])

    def test_snapshot_saves_without_disposing(self):
        c = self.store.create_container("snap")
        self.store.save_hibernation(
            c["id"], [], {},
            [{"url": "https://snap.com/page", "title": "P"}])
        self.mgr.restore(c["id"])
        ctx = self.mgr.hot[c["id"]]
        # Seed localStorage on the live tab
        for t in self.fb.targets.values():
            if t["browserContextId"] == ctx:
                self.fb.local_storage[t["targetId"]] = {
                    "https://snap.com": {"k": "v"}}
        result = self.mgr.snapshot(c["id"])
        self.assertIn("tabs_saved", result)
        self.assertIn(c["id"], self.mgr.hot)  # still hot
        full = self.store.get_container(c["id"])
        self.assertEqual(full["is_active"], 1)

    def test_snapshot_cold_skipped(self):
        c = self.store.create_container("cold-snap")
        result = self.mgr.snapshot(c["id"])
        self.assertEqual(result["skipped"], "not-hot")

    def test_snapshot_all(self):
        a = self.store.create_container("sa")
        b = self.store.create_container("sb")
        self.mgr.restore(a["id"])
        self.mgr.restore(b["id"])
        results = self.mgr.snapshot_all()
        self.assertEqual(len(results), 2)
        self.assertIn(a["id"], self.mgr.hot)
        self.assertIn(b["id"], self.mgr.hot)

    def test_snapshot_all_timeout_restores_watcher(self):
        c = self.store.create_container("sa-timeout")
        self.mgr.restore(c["id"])
        th = mock.Mock()
        th.is_alive.return_value = True
        self.mgr._watcher_thread = th
        self.mgr.stop_watcher = mock.MagicMock()
        self.mgr.start_watcher = mock.MagicMock()

        with mock.patch(
            "sessions.manager.concurrent.futures.as_completed",
            side_effect=concurrent.futures.TimeoutError,
        ):
            results = self.mgr.snapshot_all()

        self.assertIsInstance(results, list)
        self.mgr.stop_watcher.assert_called_once()
        self.mgr.start_watcher.assert_called_once()

    def test_snapshot_failure_triggers_crash_recovery_when_chrome_unreachable(self):
        c = self.store.create_container("snap-crash")
        self.mgr.restore(c["id"])
        self.mgr._collect_state = mock.MagicMock(
            side_effect=RuntimeError("ConnectTimeoutError: Connection to 127.0.0.1 timed out")
        )
        self.mgr._chrome_http_reachable = mock.MagicMock(return_value=False)
        recovered = threading.Event()
        calls: list[str] = []

        def _recover():
            calls.append("called")
            recovered.set()

        self.mgr._on_chrome_crash = _recover

        first = self.mgr.snapshot(c["id"])
        second = self.mgr.snapshot(c["id"])

        self.assertIn("error", first)
        self.assertIn("error", second)
        self.assertTrue(recovered.wait(1.0))
        self.assertEqual(len(calls), 1)

    def test_quick_shutdown(self):
        c = self.store.create_container("qs")
        self.mgr.restore(c["id"])
        self.mgr.close_chrome = mock.MagicMock()
        results = self.mgr.quick_shutdown()
        self.assertEqual(len(results), 1)
        self.assertEqual(self.mgr.hot, {})
        self.mgr.close_chrome.assert_called_once()
        full = self.store.get_container(c["id"])
        self.assertEqual(full["is_active"], 1)  # kept for auto-restore

    def test_create_for_url(self):
        result = self.mgr.create_for_url("https://example.com/path")
        self.assertEqual(result["name"], "example.com")
        self.assertIn(result["id"], self.mgr.hot)

    def test_create_for_url_adds_scheme(self):
        result = self.mgr.create_for_url("example.org")
        self.assertEqual(result["name"], "example.org")

    def test_create_for_url_duplicate_domain(self):
        a = self.mgr.create_for_url("https://dup.com/a")
        b = self.mgr.create_for_url("https://dup.com/b")
        self.assertNotEqual(a["id"], b["id"])
        self.assertEqual(a["name"], "dup.com")
        self.assertEqual(b["name"], "dup.com")

    def test_auto_restore_hot(self):
        a = self.store.create_container("ar1")
        b = self.store.create_container("ar2")
        c = self.store.create_container("ar3")
        # a and b were hot, c was cold
        self.store.mark_active(a["id"], True)
        self.store.mark_active(b["id"], True)
        self.store.save_hibernation(a["id"], [], {},
                                    [{"url": "https://a.com", "title": "A"}],
                                    keep_active=True)
        self.store.save_hibernation(b["id"], [], {},
                                    [{"url": "https://b.com", "title": "B"}],
                                    keep_active=True)
        results = self.mgr.auto_restore_hot()
        self.assertEqual(len(results), 2)
        self.assertIn(a["id"], self.mgr.hot)
        self.assertIn(b["id"], self.mgr.hot)
        self.assertNotIn(c["id"], self.mgr.hot)

    def test_status_filters_chrome_urls(self):
        c = self.store.create_container("filt")
        self.mgr.restore(c["id"])
        ctx = self.mgr.hot[c["id"]]
        self.fb.seed_tab(ctx, "chrome://newtab/", "New Tab")
        self.fb.seed_tab(ctx, "about:blank")
        self.fb.seed_tab(ctx, "https://visible.com", "Visible")
        status = self.mgr.status()
        ctr = next(r for r in status["containers"] if r["id"] == c["id"])
        urls = [t["url"] for t in ctr["live_tabs"]]
        self.assertNotIn("chrome://newtab/", urls)
        self.assertIn("about:blank", urls)  # about:blank tabs ARE shown (displayed as 'New Tab')
        self.assertIn("https://visible.com", urls)

    def test_auto_hibernate_on_window_close(self):
        """If all page targets for a hot container disappear, _check_stale_hot
        should soft-hibernate it, preserving the last-snapshotted tabs."""
        c = self.store.create_container("auto-hib")
        self.store.save_hibernation(
            c["id"],
            [{"name": "s", "value": "v", "domain": "auto.com"}],
            {"https://auto.com": {"k": "1"}},
            [{"url": "https://auto.com/page", "title": "P"}])
        self.mgr.restore(c["id"])
        ctx = self.mgr.hot[c["id"]]
        self.assertIn(c["id"], self.mgr.hot)

        # Simulate a snapshot saving current state while tabs are live
        for t in self.fb.targets.values():
            if t.get("browserContextId") == ctx:
                t["url"] = "https://auto.com/page"
                self.fb.local_storage[t["targetId"]] = {
                    "https://auto.com": {"k": "1"}}
        self.fb.cookies[ctx] = [{"name": "s", "value": "v", "domain": "auto.com"}]
        self.mgr.snapshot(c["id"])

        # Simulate closing all tabs (remove them from fake browser)
        for tid in list(self.fb.targets):
            if self.fb.targets[tid].get("browserContextId") == ctx:
                del self.fb.targets[tid]

        # Run the watcher check
        self.mgr._check_stale_hot()

        # Container should now be cold
        self.assertNotIn(c["id"], self.mgr.hot)
        full = self.store.get_container(c["id"])
        self.assertEqual(full["is_active"], 0)
        # Tabs and cookies from the last snapshot must be preserved
        self.assertEqual(len(full["tabs"]), 1)
        self.assertEqual(full["tabs"][0]["url"], "https://auto.com/page")
        self.assertEqual(len(full["cookies"]), 1)

    def test_restore_preserves_tabs_in_db(self):
        """Bug fix: restore must NOT clear saved tabs so they survive until
        the next snapshot/hibernate writes fresh state."""
        c = self.store.create_container("tab-keep")
        self.store.save_hibernation(
            c["id"], [], {},
            [{"url": "https://keep.com/a", "title": "A"},
             {"url": "https://keep.com/b", "title": "B"}])
        self.mgr.restore(c["id"])
        # After restore, the DB should still have the tabs
        full = self.store.get_container(c["id"])
        self.assertEqual(len(full["tabs"]), 2)
        urls = [t["url"] for t in full["tabs"]]
        self.assertIn("https://keep.com/a", urls)
        self.assertIn("https://keep.com/b", urls)


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------

class TestHelpers(unittest.TestCase):
    def test_domain_of(self):
        self.assertEqual(_domain_of("https://example.com/path"), "example.com")
        self.assertEqual(_domain_of("http://sub.example.com:8080/"), "sub.example.com")
        self.assertEqual(_domain_of("example.org"), "example.org")
        self.assertIsNone(_domain_of(""))

    def test_origin_of(self):
        self.assertEqual(_origin_of("https://example.com/path"), "https://example.com")
        self.assertEqual(_origin_of("http://x.com:8080/a"), "http://x.com:8080")
        self.assertIsNone(_origin_of("not-a-url"))
        self.assertIsNone(_origin_of(""))

    def test_normalize_url_passthrough(self):
        self.assertEqual(_normalize_url("https://example.com"), "https://example.com")
        self.assertEqual(_normalize_url("http://localhost:8080/path"), "http://localhost:8080/path")

    def test_normalize_url_adds_scheme(self):
        self.assertEqual(_normalize_url("example.com"), "https://example.com")
        self.assertEqual(_normalize_url("www.google.com"), "https://www.google.com")
        self.assertEqual(_normalize_url("github.io/repo"), "https://github.io/repo")

    def test_normalize_url_search_query(self):
        result = _normalize_url("how to fix python imports")
        self.assertIn("how+to+fix+python+imports", result)
        # Must be a real URL, not a bare query
        self.assertTrue(result.startswith("http"))

    def test_normalize_url_search_no_dns(self):
        result = _normalize_url("hello world")
        self.assertTrue(result.startswith("http"))
        self.assertNotIn("hello%20world", result.split("?")[0])

    def test_normalize_url_empty(self):
        self.assertEqual(_normalize_url(""), "about:blank")
        self.assertEqual(_normalize_url("   "), "about:blank")

    def test_build_search_url(self):
        url = _build_search_url("hello world")
        self.assertIn("hello+world", url)
        self.assertTrue(url.startswith("http"))

    def test_get_chrome_search_template_fallback(self):
        # When no Preferences file exists, should return fallback
        tpl = _get_chrome_search_template()
        self.assertIn("{searchTerms}", tpl)

    def test_fallback_search_url_has_placeholder(self):
        self.assertIn("{searchTerms}", _FALLBACK_SEARCH_URL)


# ---------------------------------------------------------------------------
# Restore-and-open (click saved tab in hibernated session)
# ---------------------------------------------------------------------------

class TestRestoreAndOpen(_PatchedManagerMixin, unittest.TestCase):

    def test_open_tab_on_cold_container_restores_it(self):
        """Calling open_tab on a cold container should auto-restore it."""
        c = self.store.create_container("cold-open")
        self.store.save_hibernation(
            c["id"], [], {},
            [{"url": "https://saved.com/page", "title": "P"}])
        self.assertNotIn(c["id"], self.mgr.hot)
        tid = self.mgr.open_tab(c["id"], "https://saved.com/page")
        # Now hot
        self.assertIn(c["id"], self.mgr.hot)
        self.assertTrue(tid)

    def test_open_tab_on_hot_container_creates_tab(self):
        c = self.store.create_container("hot-open")
        self.mgr.restore(c["id"])
        before = len([t for t in self.fb.targets.values()
                      if t["browserContextId"] == self.mgr.hot[c["id"]]])
        self.mgr.open_tab(c["id"], "https://new-tab.com")
        after = len([t for t in self.fb.targets.values()
                     if t["browserContextId"] == self.mgr.hot[c["id"]]])
        self.assertGreater(after, before)


# ---------------------------------------------------------------------------
# Isolation tests
# ---------------------------------------------------------------------------

class TestIsolation(_PatchedManagerMixin, unittest.TestCase):

    def test_cookies_isolated_between_sessions(self):
        """Cookies in session A must not be visible in session B."""
        a = self.store.create_container("iso-a")
        b = self.store.create_container("iso-b")
        self.store.save_hibernation(
            a["id"],
            [{"name": "auth", "value": "secret-a", "domain": "site.com",
              "path": "/", "url": "https://site.com"}],
            {"https://site.com": {"token": "a"}},
            [{"url": "https://site.com/", "title": "S"}])
        self.store.save_hibernation(
            b["id"],
            [{"name": "auth", "value": "secret-b", "domain": "site.com",
              "path": "/", "url": "https://site.com"}],
            {"https://site.com": {"token": "b"}},
            [{"url": "https://site.com/", "title": "S"}])
        self.mgr.restore(a["id"])
        self.mgr.restore(b["id"])
        ctx_a = self.mgr.hot[a["id"]]
        ctx_b = self.mgr.hot[b["id"]]
        # Different browser contexts
        self.assertNotEqual(ctx_a, ctx_b)
        # Cookies scoped to each context
        a_vals = {c["value"] for c in self.fb.cookies[ctx_a]}
        b_vals = {c["value"] for c in self.fb.cookies[ctx_b]}
        self.assertIn("secret-a", a_vals)
        self.assertNotIn("secret-b", a_vals)
        self.assertIn("secret-b", b_vals)
        self.assertNotIn("secret-a", b_vals)

    def test_localstorage_isolated_between_sessions(self):
        """localStorage injected in session A must not appear in session B."""
        a = self.store.create_container("ls-a")
        b = self.store.create_container("ls-b")
        self.store.save_hibernation(
            a["id"], [],
            {"https://site.com": {"key": "val-a"}},
            [{"url": "https://site.com/", "title": "S"}])
        self.store.save_hibernation(
            b["id"], [],
            {"https://site.com": {"key": "val-b"}},
            [{"url": "https://site.com/", "title": "S"}])
        self.mgr.restore(a["id"])
        self.mgr.restore(b["id"])
        ctx_a = self.mgr.hot[a["id"]]
        ctx_b = self.mgr.hot[b["id"]]
        # Each context has its own tabs with separate localStorage
        a_tids = [t["targetId"] for t in self.fb.targets.values()
                  if t["browserContextId"] == ctx_a]
        b_tids = [t["targetId"] for t in self.fb.targets.values()
                  if t["browserContextId"] == ctx_b]
        # Storage injected into A's tabs, not B's and vice versa
        a_scripts = [s for tid in a_tids
                     for s in self.fb.new_doc_scripts.get(tid, [])]
        b_scripts = [s for tid in b_tids
                     for s in self.fb.new_doc_scripts.get(tid, [])]
        self.assertTrue(any("val-a" in s for s in a_scripts))
        self.assertFalse(any("val-b" in s for s in a_scripts))
        self.assertTrue(any("val-b" in s for s in b_scripts))
        self.assertFalse(any("val-a" in s for s in b_scripts))

    def test_separate_browser_contexts_per_session(self):
        """Each session must get a unique browserContextId for full isolation
        (history, downloads, cache are all scoped to the context)."""
        ctxs = set()
        for i in range(3):
            c = self.store.create_container(f"ctx-{i}")
            self.mgr.restore(c["id"])
            ctxs.add(self.mgr.hot[c["id"]])
        self.assertEqual(len(ctxs), 3)


# ---------------------------------------------------------------------------
# Activation event loop tests (document.hasFocus() polling)
# ---------------------------------------------------------------------------

class _MockFocusTarget:
    """Mimics the _TargetDomain for the activation loop's dedicated session."""
    def __init__(self, targets_fn):
        self._targets_fn = targets_fn
        self.attached: dict[str, str] = {}   # tid -> sid
        self.detached: list[str] = []

    def get_targets(self, timeout=None):
        return self._targets_fn()

    def attach_to_target(self, tid, flatten=True):
        sid = f"sid-{tid}"
        self.attached[tid] = sid
        return sid

    def detach_from_target(self, sid):
        self.detached.append(sid)


class _MockFocusSession:
    """Mimics a CDPSession used by the activation loop."""
    def __init__(self, targets_fn, focus_fn):
        self.target = _MockFocusTarget(targets_fn)
        self._focus_fn = focus_fn
        self.closed = False

    def send(self, method, params=None, session_id=None, timeout=None):
        if method == "Runtime.evaluate":
            tid = session_id.replace("sid-", "") if session_id else ""
            return {"result": {"value": self._focus_fn(tid)}}
        return {}

    def close(self):
        self.closed = True


class TestActivationEventLoop(_PatchedManagerMixin, unittest.TestCase):
    """Unit tests for _activation_event_loop (hasFocus polling)."""

    def _run_loop(self, duration: float = 0.6):
        """Start the activation loop, run for *duration* seconds, then stop."""
        self.mgr._FOCUS_POLL_INTERVAL = 0.1
        self.mgr._FOCUS_REBUILD_INTERVAL = 0.1
        self.mgr._evt_stop.clear()
        t = threading.Thread(target=self.mgr._activation_event_loop, daemon=True)
        t.start()
        time.sleep(duration)
        self.mgr._evt_stop.set()
        t.join(timeout=3)

    def _setup_two_sessions(self):
        """Create two hot sessions with one tab each.
        Returns (a_cid, b_cid, tid_a, tid_b, ctx_a, ctx_b)."""
        a = self.store.create_container("focus-a")
        b = self.store.create_container("focus-b")
        self.mgr.restore(a["id"])
        self.mgr.restore(b["id"])
        ctx_a = self.mgr.hot[a["id"]]
        ctx_b = self.mgr.hot[b["id"]]
        tid_a = next(t["targetId"] for t in self.fb.targets.values()
                     if t["browserContextId"] == ctx_a)
        tid_b = next(t["targetId"] for t in self.fb.targets.values()
                     if t["browserContextId"] == ctx_b)
        return a["id"], b["id"], tid_a, tid_b, ctx_a, ctx_b

    def test_focus_updates_tab_timestamp_and_touches_session(self):
        """When a tab has focus, its timestamp and session recency are updated."""
        cid_a, cid_b, tid_a, tid_b, ctx_a, ctx_b = self._setup_two_sessions()
        targets = [
            {"targetId": tid_a, "type": "page", "browserContextId": ctx_a},
            {"targetId": tid_b, "type": "page", "browserContextId": ctx_b},
        ]
        focus = {tid_a: True, tid_b: False}
        sess = _MockFocusSession(lambda: list(targets), lambda tid: focus.get(tid, False))
        # Set last_accessed_at far in the past so touch_accessed is detectable
        with self.store._conn() as c:
            c.execute("UPDATE containers SET last_accessed_at=1000000 WHERE id=?",
                      (cid_a,))

        with mock.patch("sessions.cdp.CDPSession.connect_browser", return_value=sess):
            self._run_loop(0.6)

        self.assertIn(tid_a, self.mgr._tab_last_activated)
        self.assertNotIn(tid_b, self.mgr._tab_last_activated)
        after_a = self.store.get_container(cid_a).get("last_accessed_at", 0)
        self.assertGreater(after_a, 1000000, "touch_accessed should update timestamp")

    def test_focus_switch_updates_new_tab(self):
        """Switching focus from tab A to tab B updates tab B's timestamp."""
        cid_a, cid_b, tid_a, tid_b, ctx_a, ctx_b = self._setup_two_sessions()
        targets = [
            {"targetId": tid_a, "type": "page", "browserContextId": ctx_a},
            {"targetId": tid_b, "type": "page", "browserContextId": ctx_b},
        ]
        # Start with A focused, then switch to B
        focus = {tid_a: True, tid_b: False}
        poll_count = [0]
        def focus_fn(tid):
            poll_count[0] += 1
            if poll_count[0] > 4:  # after a few polls, switch focus
                focus[tid_a] = False
                focus[tid_b] = True
            return focus.get(tid, False)

        sess = _MockFocusSession(lambda: list(targets), focus_fn)

        with mock.patch("sessions.cdp.CDPSession.connect_browser", return_value=sess):
            self._run_loop(1.0)

        # Both tabs should have been activated at some point
        self.assertIn(tid_a, self.mgr._tab_last_activated)
        self.assertIn(tid_b, self.mgr._tab_last_activated)
        # Tab B should be more recent
        self.assertGreater(self.mgr._tab_last_activated[tid_b],
                           self.mgr._tab_last_activated[tid_a])

    def test_no_focus_no_update(self):
        """When no tab has focus (Chrome in background), nothing is touched."""
        cid_a, _, tid_a, tid_b, ctx_a, ctx_b = self._setup_two_sessions()
        targets = [
            {"targetId": tid_a, "type": "page", "browserContextId": ctx_a},
            {"targetId": tid_b, "type": "page", "browserContextId": ctx_b},
        ]
        sess = _MockFocusSession(lambda: list(targets), lambda tid: False)
        before = self.store.get_container(cid_a).get("last_accessed_at", "")

        with mock.patch("sessions.cdp.CDPSession.connect_browser", return_value=sess):
            self._run_loop(0.5)

        self.assertEqual(self.mgr._tab_last_activated, {})
        after = self.store.get_container(cid_a).get("last_accessed_at", "")
        self.assertEqual(before, after)

    def test_rebuild_attaches_new_tabs(self):
        """When a new tab appears in a hot session, it gets attached."""
        cid_a, _, tid_a, _, ctx_a, ctx_b = self._setup_two_sessions()
        targets = [
            {"targetId": tid_a, "type": "page", "browserContextId": ctx_a},
        ]
        sess = _MockFocusSession(lambda: list(targets), lambda tid: False)

        with mock.patch("sessions.cdp.CDPSession.connect_browser", return_value=sess):
            self._run_loop(0.4)

        # tid_a should have been attached
        self.assertIn(tid_a, sess.target.attached)

    def test_non_hot_tabs_are_ignored(self):
        """Tabs in browser contexts not in self.hot are not attached."""
        cid_a, _, tid_a, _, ctx_a, _ = self._setup_two_sessions()
        targets = [
            {"targetId": tid_a, "type": "page", "browserContextId": ctx_a},
            {"targetId": "foreign", "type": "page", "browserContextId": "unknown-ctx"},
        ]
        sess = _MockFocusSession(lambda: list(targets), lambda tid: False)

        with mock.patch("sessions.cdp.CDPSession.connect_browser", return_value=sess):
            self._run_loop(0.4)

        self.assertIn(tid_a, sess.target.attached)
        self.assertNotIn("foreign", sess.target.attached)

    def test_connection_failure_recovery(self):
        """If the CDP connection fails, the loop reconnects on the next cycle."""
        cid_a, _, tid_a, _, ctx_a, _ = self._setup_two_sessions()
        targets = [
            {"targetId": tid_a, "type": "page", "browserContextId": ctx_a},
        ]
        call_count = [0]

        def mock_connect(port):
            call_count[0] += 1
            if call_count[0] == 1:
                raise ConnectionError("fail first time")
            return _MockFocusSession(lambda: list(targets), lambda tid: tid == tid_a)

        with mock.patch("sessions.cdp.CDPSession.connect_browser", side_effect=mock_connect):
            self._run_loop(0.8)

        # Should have recovered and eventually detected focus
        self.assertGreater(call_count[0], 1)
        self.assertIn(tid_a, self.mgr._tab_last_activated)

    def test_stale_tab_detached_on_rebuild(self):
        """When a tab disappears from targets, its session is detached."""
        cid_a, _, tid_a, tid_b, ctx_a, ctx_b = self._setup_two_sessions()
        # Start with both tabs visible
        live_targets = [
            {"targetId": tid_a, "type": "page", "browserContextId": ctx_a},
            {"targetId": tid_b, "type": "page", "browserContextId": ctx_b},
        ]
        sess = _MockFocusSession(lambda: list(live_targets), lambda tid: False)

        with mock.patch("sessions.cdp.CDPSession.connect_browser", return_value=sess):
            # Let it attach both
            self.mgr._FOCUS_POLL_INTERVAL = 0.1
            self.mgr._FOCUS_REBUILD_INTERVAL = 0.1
            self.mgr._evt_stop.clear()
            t = threading.Thread(target=self.mgr._activation_event_loop, daemon=True)
            t.start()
            time.sleep(0.4)

            # Remove tid_b from targets (simulates tab close)
            live_targets[:] = [
                {"targetId": tid_a, "type": "page", "browserContextId": ctx_a},
            ]
            time.sleep(0.4)

            self.mgr._evt_stop.set()
            t.join(timeout=3)

        # tid_b's session should have been detached
        self.assertIn(f"sid-{tid_b}", sess.target.detached)


if __name__ == "__main__":
    unittest.main()
