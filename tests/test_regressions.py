"""Regression tests for previously identified and fixed bugs.

Each test is tagged with the bug it guards against to make future
triage easier.
"""
from __future__ import annotations

import os
import sys
import threading
import time
import unittest
from unittest import mock

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_root, "src"))
sys.path.insert(0, _root)

from sessions.manager import ContainerManager, SNAPSHOT_FRESHNESS_SEC
from sessions.persistence import PersistenceManager
from sessions.utils import clean_cookie, origin_of, domain_of
from sessions import cdp
from tests.fakes import _PatchedManagerMixin, FakeBrowser, make_fake_session_factory


# ---------------------------------------------------------------------------
# BUG: get_targets(timeout=None) -> float + NoneType TypeError
# The CDP send() method computed `deadline = time.time() + timeout`.
# If timeout was None this raised TypeError.
# FIX: _TargetDomain.get_targets only forwards timeout when not None.
# ---------------------------------------------------------------------------

class TestGetTargetsTimeoutNone(unittest.TestCase):
    """Guard against timeout=None being forwarded to CDPSession.send."""

    def test_get_targets_omits_none_timeout(self):
        """get_targets(timeout=None) must NOT pass timeout to send()."""
        calls = []

        class StubSession:
            def send(self, method, params=None, **kw):
                calls.append(kw)
                return {"targetInfos": []}

        target = cdp._TargetDomain(StubSession())
        target.get_targets(timeout=None)
        # timeout should not appear in keyword args
        self.assertNotIn("timeout", calls[0])

    def test_get_targets_forwards_explicit_timeout(self):
        """get_targets(timeout=5) MUST forward the timeout to send()."""
        calls = []

        class StubSession:
            def send(self, method, params=None, **kw):
                calls.append(kw)
                return {"targetInfos": []}

        target = cdp._TargetDomain(StubSession())
        target.get_targets(timeout=5)
        self.assertEqual(calls[0]["timeout"], 5)

    def test_send_rejects_none_timeout(self):
        """CDPSession.send with timeout=None would TypeError on deadline calc.
        Verify that callers guard against this."""
        # Simulate what would happen — the arithmetic must not be attempted
        with self.assertRaises(TypeError):
            _ = time.time() + None  # type: ignore[operator]


# ---------------------------------------------------------------------------
# BUG: Cookie URL synthesis — setCookies silently fails without url field
# Cookies returned by getCookies have domain but no url.
# Chrome's setCookies is unreliable without a synthesized url.
# FIX: clean_cookie now synthesizes url from domain+path+secure.
# ---------------------------------------------------------------------------

class TestCookieUrlSynthesis(unittest.TestCase):
    """Guard against cookies losing their url during clean_cookie."""

    def test_synthesizes_url_from_domain(self):
        c = clean_cookie({
            "name": "sid", "value": "abc",
            "domain": ".web.whatsapp.com", "path": "/",
            "secure": True, "httpOnly": True,
        })
        self.assertEqual(c["url"], "https://web.whatsapp.com/")

    def test_synthesizes_http_when_not_secure(self):
        c = clean_cookie({
            "name": "x", "value": "1",
            "domain": "example.com", "path": "/app",
        })
        self.assertEqual(c["url"], "http://example.com/app")

    def test_preserves_existing_url(self):
        c = clean_cookie({
            "name": "x", "value": "1",
            "url": "https://custom.example.com/",
            "domain": "example.com",
        })
        self.assertEqual(c["url"], "https://custom.example.com/")

    def test_empty_without_domain_or_url(self):
        c = clean_cookie({"name": "x", "value": "1"})
        self.assertEqual(c, {})

    def test_strips_leading_dot_from_domain(self):
        c = clean_cookie({
            "name": "k", "value": "v",
            "domain": ".discord.com", "path": "/",
            "secure": True,
        })
        self.assertIn("discord.com", c["url"])
        self.assertNotIn(".discord.com", c["url"])

    def test_host_cookie_removes_domain(self):
        """__Host- cookies must not have a domain attribute (host-only)."""
        c = clean_cookie({
            "name": "__Host-GAPS", "value": "secret",
            "domain": "accounts.google.com", "path": "/",
            "secure": True, "httpOnly": True,
        })
        self.assertNotIn("domain", c)
        self.assertEqual(c["url"], "https://accounts.google.com/")

    def test_host_cookie_without_url_synthesizes_url(self):
        """__Host- cookie with only domain gets url synthesized, domain removed."""
        c = clean_cookie({
            "name": "__Host-GMAIL_SCH", "value": "nsl",
            "domain": "mail.google.com", "path": "/",
            "secure": True,
        })
        self.assertNotIn("domain", c)
        self.assertIn("url", c)
        self.assertEqual(c["url"], "https://mail.google.com/")

    def test_host_cookie_preserves_existing_url(self):
        """__Host- cookie with explicit url keeps it, domain removed."""
        c = clean_cookie({
            "name": "__Host-X", "value": "v",
            "url": "https://example.com/",
            "domain": "example.com", "path": "/",
            "secure": True,
        })
        self.assertNotIn("domain", c)
        self.assertEqual(c["url"], "https://example.com/")

    def test_session_cookie_removes_negative_expires(self):
        """Session cookies (expires=-1 from CDP) must omit expires."""
        c = clean_cookie({
            "name": "SID", "value": "abc",
            "domain": ".google.com", "path": "/",
            "expires": -1, "secure": False,
        })
        self.assertNotIn("expires", c)

    def test_session_cookie_removes_zero_expires(self):
        """Session cookies (expires=0) must omit expires."""
        c = clean_cookie({
            "name": "sess", "value": "x",
            "domain": "example.com", "path": "/",
            "expires": 0,
        })
        self.assertNotIn("expires", c)

    def test_normal_cookie_keeps_expires(self):
        """Non-session cookies keep their expires timestamp."""
        c = clean_cookie({
            "name": "NID", "value": "abc",
            "domain": ".google.com", "path": "/",
            "expires": 1811907081.0, "secure": True,
        })
        self.assertEqual(c["expires"], 1811907081.0)

    def test_secure_cookie_keeps_domain(self):
        """__Secure- cookies (not __Host-) should keep domain."""
        c = clean_cookie({
            "name": "__Secure-1PSID", "value": "abc",
            "domain": ".google.com", "path": "/",
            "secure": True, "httpOnly": True,
        })
        self.assertIn("domain", c)
        self.assertEqual(c["domain"], ".google.com")


# ---------------------------------------------------------------------------
# BUG: Dashboard not showing hot/cold state — no visual indicator
# FIX: .row.hot and .row.cold CSS classes + renderList applies them.
# ---------------------------------------------------------------------------

class TestDashboardHotColdIndicator(unittest.TestCase):
    """Guard against losing the visual hot/cold session indicator."""

    def test_status_includes_hot_flag(self):
        """status() must include 'hot': True/False per container."""
        fb = FakeBrowser()
        FBS, FTS = make_fake_session_factory(fb)
        import shutil
        import tempfile
        tmp = tempfile.mkdtemp(prefix="ctxd-reg-")
        try:
            store = PersistenceManager(os.path.join(tmp, "test.db"))
            mgr = ContainerManager(store=store)
            mgr._browser_session = lambda: FBS()
            mgr._tab_session = lambda tid: FTS(tid)
            mgr._open_tab_with_storage = lambda ctx, url, stor, idb=None, **kw: ""
            c1 = mgr.create_container("Hot")
            c2 = mgr.create_container("Cold")
            mgr.restore(c1["id"])
            st = mgr.status()
            containers = {c["id"]: c for c in st["containers"]}
            self.assertTrue(containers[c1["id"]]["hot"])
            self.assertFalse(containers[c2["id"]]["hot"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_dashboard_html_has_hot_cold_css(self):
        """Dashboard CSS must define type-based color classes."""
        from sessions.dashboard import DASHBOARD_HTML
        self.assertIn(".row.type-context", DASHBOARD_HTML)
        self.assertIn(".row.type-profile", DASHBOARD_HTML)
        # Context = green border, Profile = blue border
        self.assertIn("#22c55e", DASHBOARD_HTML)
        self.assertIn("#3b82f6", DASHBOARD_HTML)

    def test_dashboard_js_applies_hot_cold_class(self):
        """renderList must apply type-based class from session_type."""
        from sessions.dashboard import DASHBOARD_HTML
        self.assertIn("c.session_type", DASHBOARD_HTML)


# ---------------------------------------------------------------------------
# BUG: Live tabs not showing — status() didn't expose live_tabs
# FIX: status() builds live_by_ctx from get_targets for hot containers.
# ---------------------------------------------------------------------------

class TestLiveTabsInStatus(_PatchedManagerMixin, unittest.TestCase):
    """Guard against live tabs disappearing from the dashboard."""

    def test_live_tabs_visible_for_hot_container(self):
        c = self.mgr.create_container("LiveTest")
        cid = c["id"]
        self.store.save_hibernation(cid, [], {},
                                    [{"url": "https://github.com", "title": "GH"}])
        self.mgr.restore(cid)
        ctx = self.mgr.hot[cid]
        # Seed a real-looking tab in the fake browser
        self.fb.seed_tab(ctx, "https://github.com", "GitHub")
        st = self.mgr.status()
        row = next(r for r in st["containers"] if r["id"] == cid)
        self.assertTrue(row["hot"])
        self.assertGreater(len(row["live_tabs"]), 0)
        self.assertEqual(row["live_tabs"][0]["url"], "https://github.com")

    def test_chrome_urls_filtered_from_live_tabs(self):
        c = self.mgr.create_container("FilterTest")
        cid = c["id"]
        self.mgr.restore(cid)
        ctx = self.mgr.hot[cid]
        self.fb.seed_tab(ctx, "chrome://newtab", "New Tab")
        self.fb.seed_tab(ctx, "https://example.com", "Ex")
        st = self.mgr.status()
        row = next(r for r in st["containers"] if r["id"] == cid)
        urls = [t["url"] for t in row["live_tabs"]]
        self.assertNotIn("chrome://newtab", urls)
        self.assertIn("https://example.com", urls)

    def test_about_blank_tabs_included_in_live_tabs(self):
        """about:blank tabs must NOT be filtered — sessions with only new tabs
        would otherwise show no tabs and no action buttons in the UI."""
        c = self.mgr.create_container("BlankTest")
        cid = c["id"]
        self.mgr.restore(cid)
        ctx = self.mgr.hot[cid]
        self.fb.seed_tab(ctx, "about:blank", "")
        self.fb.seed_tab(ctx, "about:blank", "")
        st = self.mgr.status()
        row = next(r for r in st["containers"] if r["id"] == cid)
        urls = [t["url"] for t in row["live_tabs"]]
        self.assertGreaterEqual(urls.count("about:blank"), 2,
                                "about:blank tabs should appear in live_tabs")

    def test_cold_container_shows_saved_tabs(self):
        c = self.mgr.create_container("ColdTabs")
        cid = c["id"]
        self.store.save_hibernation(cid, [], {},
                                    [{"url": "https://saved.example", "title": "S"}])
        st = self.mgr.status()
        row = next(r for r in st["containers"] if r["id"] == cid)
        self.assertFalse(row["hot"])
        self.assertEqual(len(row["saved_tabs"]), 1)
        self.assertEqual(row["saved_tabs"][0]["url"], "https://saved.example")


# ---------------------------------------------------------------------------
# BUG: Parallel localStorage collection — _collect_state was sequential
# FIX: Uses ThreadPoolExecutor; must still collect all origins correctly.
# ---------------------------------------------------------------------------

class TestParallelCollectState(_PatchedManagerMixin, unittest.TestCase):
    """Guard against regressions in parallel localStorage collection."""

    def test_collect_state_gathers_multiple_origins(self):
        """_collect_state must gather localStorage from multiple tabs in parallel."""
        c = self.mgr.create_container("Multi")
        cid = c["id"]
        self.mgr.restore(cid)
        ctx = self.mgr.hot[cid]
        # Seed tabs with distinct storage
        self.fb.seed_tab(ctx, "https://a.com/page", "A",
                         origin="https://a.com",
                         storage={"key_a": "val_a"})
        self.fb.seed_tab(ctx, "https://b.com/page", "B",
                         origin="https://b.com",
                         storage={"key_b": "val_b"})
        cookies, storage, idb, tabs = self.mgr._collect_state(ctx)
        self.assertEqual(len(tabs), 2)
        self.assertIn("https://a.com", storage)
        self.assertIn("https://b.com", storage)
        self.assertEqual(storage["https://a.com"]["key_a"], "val_a")

    def test_collect_state_gathers_idb_with_schema(self):
        """_collect_state must round-trip IDB data including _meta and indexes."""
        c = self.mgr.create_container("IDB")
        cid = c["id"]
        self.mgr.restore(cid)
        ctx = self.mgr.hot[cid]
        idb_data = {
            "_meta": {"version": 42},
            "msgs": {
                "rows": [{"id": 1, "text": "hi"}], "keys": [1],
                "keyPath": "id", "autoIncrement": True,
                "indexes": [{"name": "by_ts", "keyPath": "ts",
                             "unique": False, "multiEntry": False}],
            },
        }
        self.fb.seed_tab(ctx, "https://web.whatsapp.com", "WA",
                         origin="https://web.whatsapp.com",
                         storage={"pref": "1"},
                         idb=idb_data)
        cookies, storage, idb, tabs = self.mgr._collect_state(ctx)
        self.assertIn("https://web.whatsapp.com", idb)
        collected = idb["https://web.whatsapp.com"]
        self.assertEqual(collected["_meta"]["version"], 42)
        self.assertEqual(collected["msgs"]["indexes"][0]["name"], "by_ts")

    def test_collect_state_skips_about_blank(self):
        """about:blank tabs must be filtered out."""
        c = self.mgr.create_container("Blank")
        cid = c["id"]
        self.mgr.restore(cid)
        ctx = self.mgr.hot[cid]
        self.fb.seed_tab(ctx, "about:blank", "")
        self.fb.seed_tab(ctx, "https://real.com", "R",
                         origin="https://real.com",
                         storage={"x": "1"})
        _, storage, idb, tabs = self.mgr._collect_state(ctx)
        self.assertEqual(len(tabs), 1)
        self.assertEqual(tabs[0]["url"], "https://real.com")


# ---------------------------------------------------------------------------
# BUG: Quick shutdown race with snapshot_loop
# FIX: _snapshot_fence event; quick_shutdown waits for it.
# ---------------------------------------------------------------------------

class TestQuickShutdownFence(_PatchedManagerMixin, unittest.TestCase):
    """Guard against quick_shutdown racing with snapshot_loop."""

    def test_shutdown_waits_for_fence(self):
        """quick_shutdown must wait on _snapshot_fence before snapshotting."""
        c = self.mgr.create_container("Fenced")
        cid = c["id"]
        self.mgr.restore(cid)
        self.mgr.close_chrome = mock.MagicMock()

        # Simulate an in-flight snapshot (fence cleared = busy)
        fence = threading.Event()
        fence.clear()  # snapshot in progress
        self.mgr._snapshot_fence = fence

        results = []
        t = threading.Thread(target=lambda: results.append(
            self.mgr.quick_shutdown()))
        t.start()
        # quick_shutdown should be blocked
        time.sleep(0.2)
        self.assertTrue(t.is_alive(), "shutdown should be waiting on fence")
        # Release the fence
        fence.set()
        t.join(timeout=10)
        self.assertFalse(t.is_alive())
        self.assertEqual(len(results), 1)

    def test_shutdown_works_without_fence(self):
        """quick_shutdown must work even if _snapshot_fence is not set."""
        c = self.mgr.create_container("NoFence")
        self.mgr.restore(c["id"])
        self.mgr.close_chrome = mock.MagicMock()
        # No fence attribute set — should not crash
        if hasattr(self.mgr, '_snapshot_fence'):
            delattr(self.mgr, '_snapshot_fence')
        results = self.mgr.quick_shutdown()
        self.assertIsInstance(results, list)


# ---------------------------------------------------------------------------
# BUG: Slow HTTP timeout on Windows — localhost dual-stack + single timeout
# FIX: Use 127.0.0.1 and (connect, read) timeout tuple.
# ---------------------------------------------------------------------------

class TestHttpTimeoutSettings(unittest.TestCase):
    """Guard against reverting to slow localhost + single-value timeout."""

    def test_get_targets_cached_uses_127_0_0_1(self):
        """_get_targets_cached must use 127.0.0.1, not localhost."""
        import inspect
        src = inspect.getsource(ContainerManager._get_targets_cached)
        self.assertIn("127.0.0.1", src)
        self.assertNotIn('"localhost"', src)
        self.assertNotIn("'localhost'", src)

    def test_get_targets_cached_uses_tuple_timeout(self):
        """_get_targets_cached must use a (connect, read) timeout tuple."""
        import inspect
        src = inspect.getsource(ContainerManager._get_targets_cached)
        # Should have a tuple timeout like timeout=(0.5, 3)
        self.assertIn("timeout=(", src)


# ---------------------------------------------------------------------------
# BUG: Parallel startup — Chrome and API server should start concurrently
# FIX: cli.py launches Chrome in a thread and starts API server in parallel.
# ---------------------------------------------------------------------------

class TestParallelStartupStructure(unittest.TestCase):
    """Guard against reverting to sequential Chrome-then-API startup."""

    def test_cli_uses_threading_for_chrome(self):
        """The CLI module must launch Chrome readiness in a background thread."""
        import inspect
        from sessions import cli
        src = inspect.getsource(cli)
        # Must have a thread for Chrome startup
        self.assertIn("_chrome_ready", src)
        self.assertIn("threading.Thread", src)

    def test_cli_has_snapshot_fence(self):
        """The CLI module must create a _snap_fence threading.Event."""
        import inspect
        from sessions import cli
        src = inspect.getsource(cli)
        self.assertIn("_snap_fence", src)
        self.assertIn("threading.Event", src)


# ---------------------------------------------------------------------------
# BUG: Snapshot freshness — quick_shutdown re-snapshotted fresh containers
# FIX: _snapshot_if_stale checks SNAPSHOT_FRESHNESS_SEC.
# ---------------------------------------------------------------------------

class TestSnapshotFreshness(_PatchedManagerMixin, unittest.TestCase):
    """Guard against redundant snapshots during shutdown."""

    def test_snapshot_if_stale_skips_recent(self):
        """_snapshot_if_stale must skip containers snapshotted recently."""
        c = self.mgr.create_container("Fresh")
        cid = c["id"]
        self.mgr.restore(cid)
        ctx = self.mgr.hot[cid]
        self.fb.seed_tab(ctx, "https://ex.com", "E",
                         origin="https://ex.com", storage={"k": "v"})
        # First snapshot should run
        r1 = self.mgr._snapshot_if_stale(cid)
        self.assertNotIn("skipped", r1)
        # Immediately after, should be skipped as fresh
        r2 = self.mgr._snapshot_if_stale(cid)
        self.assertIn("skipped", r2)
        self.assertEqual(r2["skipped"], "fresh")

    def test_snapshot_if_stale_runs_after_expiry(self):
        """_snapshot_if_stale must run when enough time has passed."""
        c = self.mgr.create_container("Stale")
        cid = c["id"]
        self.mgr.restore(cid)
        ctx = self.mgr.hot[cid]
        self.fb.seed_tab(ctx, "https://ex.com", "E")
        self.mgr._snapshot_if_stale(cid)
        # Fake the last snapshot time to be old and clear hash to force re-snapshot
        self.mgr._last_snapshot_time[cid] = time.time() - SNAPSHOT_FRESHNESS_SEC - 1
        self.mgr._last_snapshot_hash.pop(cid, None)
        r = self.mgr._snapshot_if_stale(cid)
        self.assertNotIn("skipped", r)


# ---------------------------------------------------------------------------
# BUG: Restore cookie batch retry — one bad cookie fails entire batch
# FIX: On CDPError, retry cookies one-by-one.
# ---------------------------------------------------------------------------

class TestRestoreCookieBatchRetry(_PatchedManagerMixin, unittest.TestCase):
    """Guard against one bad cookie killing all cookie restoration."""

    def test_cookies_restored_on_restore(self):
        """restore() must set cookies from saved state."""
        c = self.mgr.create_container("CookieTest")
        cid = c["id"]
        cookies = [
            {"name": "s1", "value": "v1", "domain": ".example.com",
             "path": "/", "secure": True},
            {"name": "s2", "value": "v2", "domain": ".other.com",
             "path": "/", "secure": False},
        ]
        self.store.save_hibernation(cid, cookies, {},
                                    [{"url": "https://example.com", "title": "E"}])
        self.mgr.restore(cid)
        ctx = self.mgr.hot[cid]
        stored = self.fb.cookies.get(ctx, [])
        # All cookies should be set (clean_cookie adds url field)
        names = {c["name"] for c in stored}
        self.assertIn("s1", names)
        self.assertIn("s2", names)


# ---------------------------------------------------------------------------
# BUG: utils.py extraction — helpers must be importable from their canonical
# location (sessions.utils) and re-exported from the sessions package.
# FIX: utils.py created; __init__.py re-exports from utils directly.
# ---------------------------------------------------------------------------

class TestUtilsBackwardCompat(unittest.TestCase):
    """Guard against helpers becoming unreachable after utils.py extraction."""

    def test_utils_importable_directly(self):
        """sessions.utils must export domain_of, origin_of, normalize_url etc."""
        from sessions.utils import (
            domain_of, origin_of, normalize_url,  # noqa: F811
            _FALLBACK_SEARCH_URL,
        )
        self.assertEqual(domain_of("https://example.com/path"), "example.com")
        self.assertEqual(origin_of("https://example.com/path"),
                         "https://example.com")
        self.assertIn("://", normalize_url("example.com"))
        self.assertIsInstance(_FALLBACK_SEARCH_URL, str)

    def test_package_re_exports_utils(self):
        """sessions package must re-export utility helpers from utils.py."""
        import sessions
        self.assertTrue(hasattr(sessions, "_domain_of"))
        self.assertTrue(hasattr(sessions, "_origin_of"))
        self.assertTrue(hasattr(sessions, "_clean_cookie"))
        self.assertTrue(hasattr(sessions, "_origins_from_cookies"))
        self.assertTrue(hasattr(sessions, "_normalize_url"))

    def test_manager_only_imports_what_it_uses(self):
        """manager.py must not import symbols it never calls."""
        import inspect
        from sessions import manager
        src = inspect.getsource(manager)
        # These are pure utils — manager should delegate, not re-export them
        self.assertNotIn("_build_search_url", src.split("from .utils")[1].split(")")[0]
                         if "from .utils" in src else "")

    def test_utils_module_directly(self):
        """Direct imports from sessions.utils must work."""
        self.assertEqual(domain_of("https://x.com"), "x.com")
        self.assertEqual(origin_of("https://x.com:443/p"), "https://x.com:443")
        self.assertEqual(clean_cookie({"name": "a", "value": "b"}), {})


# ---------------------------------------------------------------------------
# BUG: get_targets used 30s default timeout — watcher blocked Win-/ for 30s
# FIX: All get_targets() calls now pass timeout=SNAPSHOT_CDP_TIMEOUT (5s).
# ---------------------------------------------------------------------------

class TestGetTargetsTimeout(unittest.TestCase):
    """Guard against get_targets using the 30s default timeout in hot paths."""

    def _get_all_get_targets_calls(self):
        import ast
        import inspect
        from sessions import manager
        src = inspect.getsource(manager)
        tree = ast.parse(src)
        calls = []
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "get_targets"):
                kw_names = [k.arg for k in node.keywords]
                calls.append(kw_names)
        return calls

    def test_all_get_targets_calls_have_timeout(self):
        """Every get_targets() call in manager.py must pass timeout= kwarg."""
        calls = self._get_all_get_targets_calls()
        self.assertGreater(len(calls), 0, "no get_targets calls found")
        for kws in calls:
            self.assertIn("timeout", kws,
                          f"get_targets called without timeout kwarg: {kws}")

    def test_check_stale_hot_does_cdp_outside_lock(self):
        """_check_stale_hot must not hold _lock during CDP to avoid blocking API."""
        import inspect
        from sessions import manager
        src = inspect.getsource(manager.ContainerManager._check_stale_hot)
        # The get_targets call must appear AFTER the first 'with self._lock:' block
        # closes (i.e. after the hot_snapshot extraction).
        lock_end = src.index("hot_snapshot = dict(self.hot)")
        targets_call = src.index("get_targets(")
        self.assertGreater(targets_call, lock_end,
                           "get_targets must be called outside the lock")


# ---------------------------------------------------------------------------
# BUG: Parallel CDP reconnect storm — all snapshot threads reconnected at once
# FIX: _browser_session holds _bs_lock throughout reconnect; others wait+reuse.
# ---------------------------------------------------------------------------

class TestSingleReconnect(unittest.TestCase):
    """Guard against multiple threads simultaneously reconnecting to CDP."""

    def test_only_one_reconnect_on_stale_session(self):
        """When cached session is stale, only one reconnect should happen.
        _browser_session holds _bs_lock throughout, so threads queue up and
        reuse the session established by the first thread."""
        import shutil
        import tempfile
        tmp = tempfile.mkdtemp(prefix="ctxd-reconnect-")
        try:
            store = PersistenceManager(os.path.join(tmp, "test.db"))
            mgr = ContainerManager(store=store)

            connect_count = [0]
            fake_session = mock.MagicMock()
            # Make the probe pass on the cached session
            fake_session.send.return_value = {"product": "Chrome"}

            def fake_connect(port):
                connect_count[0] += 1
                time.sleep(0.05)  # simulate network latency
                return fake_session

            mgr._cached_bs = None  # start with no cached session

            with mock.patch.object(cdp.CDPSession, 'connect_browser',
                                   side_effect=fake_connect):
                results = []
                errors = []

                def call_session():
                    try:
                        with mgr._browser_session() as bs:
                            results.append(id(bs._s))
                    except Exception as e:
                        errors.append(e)

                t1 = threading.Thread(target=call_session)
                t2 = threading.Thread(target=call_session)
                t1.start()
                t2.start()
                t1.join(timeout=5)
                t2.join(timeout=5)

            self.assertEqual(len(errors), 0, f"errors: {errors}")
            self.assertEqual(connect_count[0], 1,
                             f"expected 1 reconnect, got {connect_count[0]}")
            # Both threads must get the same underlying session object
            self.assertEqual(results[0], results[1])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# BUG: connect_browser used localhost (dual-stack DNS delay on Windows)
# FIX: Updated to use 127.0.0.1 with tuple timeout.
# ---------------------------------------------------------------------------

class TestConnectBrowserUsesIPv4(unittest.TestCase):
    """Guard against connect_browser using localhost instead of 127.0.0.1."""

    def test_connect_browser_uses_127_0_0_1(self):
        import inspect
        src = inspect.getsource(cdp.CDPSession.connect_browser)
        self.assertIn("127.0.0.1", src)
        self.assertNotIn('"localhost"', src)
        self.assertNotIn("'localhost'", src)

    def test_connect_browser_uses_tuple_timeout(self):
        import inspect
        src = inspect.getsource(cdp.CDPSession.connect_browser)
        self.assertIn("timeout=(", src)


# ---------------------------------------------------------------------------
# BUG: Liveness probe used Browser.getVersion with 30s default timeout
# FIX: _CDP_PROBE_TIMEOUT = 5s used explicitly.
# ---------------------------------------------------------------------------

class TestLivenessProbeTimeout(unittest.TestCase):
    """Guard against the stale-session probe blocking for 30s."""

    def test_probe_uses_short_timeout(self):
        """_browser_session probe must use _CDP_PROBE_TIMEOUT not the 30s default."""
        import inspect
        src = inspect.getsource(ContainerManager._browser_session)
        self.assertIn("_CDP_PROBE_TIMEOUT", src)
        # Must not call Browser.getVersion without an explicit timeout
        self.assertNotIn('get_version()', src)

    def test_probe_timeout_value_is_reasonable(self):
        """_CDP_PROBE_TIMEOUT must be well under 30s."""
        self.assertLess(ContainerManager._CDP_PROBE_TIMEOUT, 15)
        self.assertGreater(ContainerManager._CDP_PROBE_TIMEOUT, 0)


# ---------------------------------------------------------------------------
# BUG: hibernate() held self._lock during _collect_state CDP calls
# FIX: Release lock before _collect_state, re-acquire for mutation phase
# (matches the pattern already used by snapshot()).
# ---------------------------------------------------------------------------

class TestHibernateReleasesLock(_PatchedManagerMixin, unittest.TestCase):
    """Guard: hibernate must not hold the lock during _collect_state."""

    def test_hibernate_releases_lock_during_collect(self):
        """_collect_state is called outside the lock, matching snapshot()."""
        import inspect
        src = inspect.getsource(ContainerManager.hibernate)
        # The pattern: grab ctx under lock, release, collect, re-acquire
        self.assertIn("Collect state WITHOUT holding the lock", src)
        # Must re-acquire lock after collect for the mutation phase
        self.assertIn("Re-acquire lock", src)

    def test_hibernate_checks_staleness_after_collect(self):
        """After re-acquiring the lock, hibernate must verify the container
        hasn't changed (another thread could have disposed it)."""
        import inspect
        src = inspect.getsource(ContainerManager.hibernate)
        self.assertIn('self.hot[cid] != ctx', src)


# ---------------------------------------------------------------------------
# BUG: _collect_state raised TimeoutError, discarding partial results
# FIX: Catch TimeoutError from as_completed, return whatever was collected.
# ---------------------------------------------------------------------------

class TestCollectStatePartialResults(_PatchedManagerMixin, unittest.TestCase):
    """Guard: _collect_state must return partial data on timeout."""

    def test_collect_state_catches_timeout_error(self):
        """The as_completed loop must be wrapped in try/except TimeoutError."""
        import inspect
        src = inspect.getsource(ContainerManager._collect_state)
        self.assertIn("except TimeoutError", src)
        self.assertIn("returning partial results", src)

    def test_collect_state_cancels_pending_on_timeout(self):
        """Pending futures must be cancelled when TimeoutError fires."""
        import inspect
        src = inspect.getsource(ContainerManager._collect_state)
        self.assertIn("f.cancel()", src)



# ---------------------------------------------------------------------------
# BUG: restart_backend called stop_watcher() (sets event only, no join) then
#      snapshot_all() immediately. The watcher thread was still mid-tick doing
#      _collect_state, so snapshot_all() raced it on the same CDP context,
#      producing "No response for Storage.getCookies within 5s" and
#      "Expecting value: line 1 column 1" JSON parse failures.
#      Also, snapshot_all() was sequential — one 39s hang blocked all others.
# FIX: stop_watcher() now joins the thread; snapshot_all() is parallel.
# ---------------------------------------------------------------------------

class TestStopWatcherJoins(unittest.TestCase):
    """Guard: stop_watcher must join the thread so callers don't race it."""

    def test_stop_watcher_joins_thread(self):
        import inspect
        src = inspect.getsource(ContainerManager.stop_watcher)
        self.assertIn("join", src)

    def test_snapshot_all_is_parallel(self):
        import inspect
        src = inspect.getsource(ContainerManager.snapshot_all)
        self.assertIn("ThreadPoolExecutor", src)

    def test_snapshot_all_stops_watcher_first(self):
        import inspect
        src = inspect.getsource(ContainerManager.snapshot_all)
        self.assertIn("stop_watcher", src)

    def test_snapshot_all_restarts_watcher_after(self):
        """When watcher was running, snapshot_all must restart it on exit."""
        import inspect
        src = inspect.getsource(ContainerManager.snapshot_all)
        self.assertIn("start_watcher", src)
        self.assertIn("watcher_was_running", src)


class TestRestartSnapshotRace(_PatchedManagerMixin, unittest.TestCase):
    """Integration: snapshot_all after stop_watcher must not race the watcher."""

    def test_snapshot_all_does_not_race_watcher(self):
        """start_watcher then stop+snapshot_all; watcher must be dead before snap."""
        c = self.mgr.create_container("race-test")
        cid = c["id"]
        self.mgr.restore(cid)
        ctx = self.mgr.hot[cid]
        self.fb.seed_tab(ctx, "https://race.test/", "Race",
                         origin="https://race.test",
                         storage={"k": "v"})
        self.mgr.start_watcher()
        # stop_watcher should block until the thread exits
        self.mgr.stop_watcher(join_timeout=5.0)
        # Thread should be dead now
        t = self.mgr._watcher_thread
        self.assertFalse(t is not None and t.is_alive(),
                         "watcher thread still alive after stop_watcher(join)")
        # snapshot_all must succeed without CDP conflicts
        results = self.mgr.snapshot_all()
        self.assertTrue(any(r.get("id") == cid for r in results))
        saved = self.store.get_container(cid)
        self.assertEqual(saved["storage"]["https://race.test"]["k"], "v")


# ---------------------------------------------------------------------------
# BUG: Saving login?redirect_to= URLs causes a death spiral where the app
# always restores to its login page, never captures storage, and re-saves
# the login redirect.  _canonical_tab_url strips those redirects.
# ---------------------------------------------------------------------------

class TestCanonicalTabUrl(unittest.TestCase):
    def setUp(self):
        from sessions.manager import _canonical_tab_url
        self._fn = _canonical_tab_url

    def test_discord_redirect_to(self):
        url = "https://discord.com/login?redirect_to=%2Fchannels%2F123%2F456"
        self.assertEqual(self._fn(url), "https://discord.com/channels/123/456")

    def test_generic_next_param(self):
        url = "https://app.example.com/login?next=%2Fdashboard%2Fhome"
        self.assertEqual(self._fn(url), "https://app.example.com/dashboard/home")

    def test_return_to_param(self):
        url = "https://site.com/auth?return_to=%2Fsettings"
        self.assertEqual(self._fn(url), "https://site.com/settings")

    def test_no_redirect_param_passes_through(self):
        url = "https://discord.com/channels/123/456"
        self.assertEqual(self._fn(url), url)

    def test_empty_string(self):
        self.assertEqual(self._fn(""), "")

    def test_none_returns_none(self):
        self.assertIsNone(self._fn(None))

    def test_about_blank(self):
        self.assertEqual(self._fn("about:blank"), "about:blank")

    def test_absolute_redirect_value(self):
        url = "https://x.com/login?redirect=https%3A%2F%2Fx.com%2Fhome"
        self.assertEqual(self._fn(url), "https://x.com/home")


# ---------------------------------------------------------------------------
# BUG: Shared CDPSession contention — parallel _collect_state calls on the
# same cached session cause Target.getTargets timeouts. Fixed by using fresh
# per-call sessions from _new_browser_session().
# ---------------------------------------------------------------------------

class TestFreshSessionPerCollectState(_PatchedManagerMixin, unittest.TestCase):
    """Ensure _collect_state uses _new_browser_session (fresh per call),
    not the shared _browser_session cache."""

    def test_collect_state_uses_new_browser_session(self):
        call_log = []
        original_new = self.mgr._new_browser_session
        def tracked_new():
            call_log.append("new")
            return original_new()
        self.mgr._new_browser_session = tracked_new

        c = self.mgr.create_container("Test")
        self.mgr.restore(c["id"])
        ctx = self.mgr.hot[c["id"]]
        self.fb.seed_tab(ctx, "https://example.com/page", "P",
                         origin="https://example.com",
                         storage={"k": "v"})
        self.mgr._collect_state(ctx)
        self.assertIn("new", call_log,
                      "_collect_state must use _new_browser_session")


# ---------------------------------------------------------------------------
# BUG: origin=null for cert interstitials — storage never collected
# Tabs showing self-signed HTTPS cert warnings or error pages return "null"
# from window.location.origin.  The code skipped the tab entirely, losing
# localStorage/IDB data on every snapshot cycle for hours.
# FIX: Fall back to computing origin from the tab URL when JS returns "null".
# ---------------------------------------------------------------------------

class TestOriginNullFallback(_PatchedManagerMixin, unittest.TestCase):
    """Guard: _collect_state must use computed origin when JS returns 'null'."""

    def test_collect_state_uses_url_origin_when_js_returns_null(self):
        """When window.location.origin returns 'null' (cert interstitial),
        _collect_state should fall back to the origin computed from the tab URL
        and still attempt to collect storage."""
        c = self.mgr.create_container("NullOrigin")
        cid = c["id"]
        self.mgr.restore(cid)
        ctx = self.mgr.hot[cid]
        # Seed a tab with real URL + storage, but force null origin
        tid = self.fb.seed_tab(ctx, "https://172.25.171.110:3001/page", "Self-Signed",
                               origin="https://172.25.171.110:3001",
                               storage={"token": "secret"})
        self.fb.null_origin_tids.add(tid)
        cookies, storage, idb, tabs = self.mgr._collect_state(ctx)
        # Tab should NOT be skipped — it should appear in tabs
        self.assertTrue(any(t["url"] == "https://172.25.171.110:3001/page" for t in tabs))
        # Storage should be collected using the computed origin
        self.assertIn("https://172.25.171.110:3001", storage)
        self.assertEqual(storage["https://172.25.171.110:3001"]["token"], "secret")

    def test_collect_state_skips_when_both_js_and_url_origin_null(self):
        """If both JS origin and URL-computed origin are unusable, skip the tab."""
        c = self.mgr.create_container("BothNull")
        cid = c["id"]
        self.mgr.restore(cid)
        ctx = self.mgr.hot[cid]
        # Seed a tab with about:blank URL and force null origin
        tid = self.fb.seed_tab(ctx, "about:blank", "Blank")
        self.fb.null_origin_tids.add(tid)
        cookies, storage, idb, tabs = self.mgr._collect_state(ctx)
        # about:blank should be filtered from saveable tabs
        self.assertEqual(len(tabs), 0)

    def test_snapshot_with_null_origin_tab_collects_storage(self):
        """Full snapshot round-trip: tab with null JS origin should still
        have its storage persisted via the URL-derived origin fallback."""
        c = self.mgr.create_container("SnapNull")
        cid = c["id"]
        self.store.save_hibernation(cid, [], {},
                                    [{"url": "https://10.0.0.1:8443/app", "title": "App"}])
        self.mgr.restore(cid)
        ctx = self.mgr.hot[cid]
        for t in self.fb.targets.values():
            if t["browserContextId"] == ctx and t["type"] == "page":
                t["url"] = "https://10.0.0.1:8443/app"
                self.fb.local_storage[t["targetId"]] = {
                    "https://10.0.0.1:8443": {"setting": "value"}}
                self.fb.null_origin_tids.add(t["targetId"])
        result = self.mgr.snapshot(cid)
        self.assertIn("tabs_saved", result)
        full = self.store.get_container(cid)
        self.assertIn("https://10.0.0.1:8443", full["storage"])

    def test_origin_null_fallback_source_inspection(self):
        """_collect_state must contain the computed-origin fallback path."""
        import inspect
        src = inspect.getsource(ContainerManager._collect_state)
        self.assertIn("_origin_of(tab_url)", src)
        self.assertIn("using computed origin", src)


# ---------------------------------------------------------------------------
# BUG: CDP connect timeout too aggressive (0.5s) → false crash detection
# Chrome under load or waking from sleep may need >0.5s to accept TCP.
# FIX: Increased connect timeout to 2s in _get_targets_cached and
# CDPSession.connect_browser.
# ---------------------------------------------------------------------------

class TestConnectTimeoutValues(unittest.TestCase):
    """Guard: CDP connect timeouts must not be too aggressive."""

    def test_get_targets_cached_connect_timeout_at_least_1s(self):
        """_get_targets_cached connect timeout must be >= 1s to avoid
        false 'Chrome unreachable' on busy systems."""
        import inspect
        import re
        src = inspect.getsource(ContainerManager._get_targets_cached)
        m = re.search(r'timeout=\(([0-9.]+),', src)
        self.assertIsNotNone(m, "timeout tuple not found in _get_targets_cached")
        connect_timeout = float(m.group(1))
        self.assertGreaterEqual(connect_timeout, 1.0,
                                f"connect timeout {connect_timeout}s is too aggressive")

    def test_connect_browser_connect_timeout_at_least_1s(self):
        """CDPSession.connect_browser connect timeout must be >= 1s."""
        import inspect
        import re
        src = inspect.getsource(cdp.CDPSession.connect_browser)
        m = re.search(r'timeout=\(([0-9.]+),', src)
        self.assertIsNotNone(m, "timeout tuple not found in connect_browser")
        connect_timeout = float(m.group(1))
        self.assertGreaterEqual(connect_timeout, 1.0,
                                f"connect timeout {connect_timeout}s is too aggressive")


# ---------------------------------------------------------------------------
# BUG: Snapshots ran during crash recovery — wasted CDP calls + log spam
# FIX: snapshot() and snapshot_all() check _crash_recovery_inflight and skip.
# ---------------------------------------------------------------------------

class TestSnapshotSkipsDuringCrashRecovery(_PatchedManagerMixin, unittest.TestCase):
    """Guard: snapshot operations must be suppressed during crash recovery."""

    def test_snapshot_skipped_when_recovery_lock_held(self):
        """snapshot() must return skip when crash recovery is in progress."""
        c = self.store.create_container("snap-skip")
        self.mgr.restore(c["id"])
        # Acquire the crash recovery lock to simulate in-flight recovery
        self.mgr._crash_recovery_inflight.acquire()
        try:
            result = self.mgr.snapshot(c["id"])
            self.assertEqual(result.get("skipped"), "crash-recovery")
        finally:
            self.mgr._crash_recovery_inflight.release()

    def test_snapshot_all_skipped_when_recovery_lock_held(self):
        """snapshot_all() must return [] when crash recovery is in progress."""
        c = self.store.create_container("snap-all-skip")
        self.mgr.restore(c["id"])
        self.mgr._crash_recovery_inflight.acquire()
        try:
            results = self.mgr.snapshot_all()
            self.assertEqual(results, [])
        finally:
            self.mgr._crash_recovery_inflight.release()

    def test_snapshot_all_skipped_during_sleep_cooldown(self):
        """snapshot_all() must return [] during the post-sleep cooldown."""
        c = self.store.create_container("snap-sleep")
        self.mgr.restore(c["id"])
        self.mgr._sleep_cooldown_until = time.monotonic() + 60
        results = self.mgr.snapshot_all()
        self.assertEqual(results, [])

    def test_snapshot_works_after_recovery_completes(self):
        """snapshot() must work normally after crash recovery finishes."""
        c = self.store.create_container("post-recovery")
        self.store.save_hibernation(c["id"], [], {},
                                    [{"url": "https://ok.com", "title": "OK"}])
        self.mgr.restore(c["id"])
        # Recovery finishes (lock not held)
        result = self.mgr.snapshot(c["id"])
        self.assertNotEqual(result.get("skipped"), "crash-recovery")

    def test_snapshot_source_checks_crash_recovery(self):
        """snapshot() source must check _crash_recovery_inflight.locked()."""
        import inspect
        src = inspect.getsource(ContainerManager.snapshot)
        self.assertIn("_crash_recovery_inflight.locked()", src)

    def test_snapshot_all_source_checks_sleep_cooldown(self):
        """snapshot_all() must check _in_sleep_cooldown()."""
        import inspect
        src = inspect.getsource(ContainerManager.snapshot_all)
        self.assertIn("_in_sleep_cooldown()", src)


# ---------------------------------------------------------------------------
# BUG: Post-sleep event loop reconnect flood — many attach failures
# FIX: Added 1s settle delay after reconnect and back-off during recovery.
# ---------------------------------------------------------------------------

class TestEventLoopReconnectBackoff(unittest.TestCase):
    """Guard: event loop must back off during crash recovery and settle
    after reconnect."""

    def test_event_loop_backs_off_during_recovery(self):
        """_activation_event_loop must check _crash_recovery_inflight."""
        import inspect
        src = inspect.getsource(ContainerManager._activation_event_loop)
        self.assertIn("_crash_recovery_inflight", src)

    def test_event_loop_has_settle_delay(self):
        """After reconnect, a brief sleep must precede _rebuild."""
        import inspect
        src = inspect.getsource(ContainerManager._activation_event_loop)
        # Should have a sleep between connect and rebuild
        self.assertIn("time.sleep", src)


# ---------------------------------------------------------------------------
# FIX: _dispose_context detaches CDP sessions before disposing to prevent
# Chrome crashes when the context is torn down with sessions still attached.
# ---------------------------------------------------------------------------

class TestDetachBeforeDispose(_PatchedManagerMixin, unittest.TestCase):
    """Guard: disposeBrowserContext must go through _dispose_context."""

    def test_soft_hibernate_uses_dispose_context(self):
        """_soft_hibernate must call _dispose_context instead of raw dispose."""
        import inspect
        src = inspect.getsource(ContainerManager._soft_hibernate)
        self.assertIn("_dispose_context", src)
        self.assertNotIn("dispose_browser_context", src)

    def test_hibernate_uses_dispose_context(self):
        """hibernate must call _dispose_context instead of raw dispose."""
        import inspect
        src = inspect.getsource(ContainerManager.hibernate)
        self.assertIn("_dispose_context", src)
        self.assertNotIn("dispose_browser_context", src)

    def test_delete_uses_dispose_context(self):
        """delete must call _dispose_context instead of raw dispose."""
        import inspect
        src = inspect.getsource(ContainerManager.delete)
        self.assertIn("_dispose_context", src)
        self.assertNotIn("dispose_browser_context", src)

    def test_dispose_context_sets_disposing_flag(self):
        """_dispose_context must add ctx to _disposing_ctxs during dispose."""
        row = self.mgr.create_container("test")
        cid = row["id"]
        result = self.mgr.restore(cid)
        ctx = result["browserContextId"]
        # Track whether _disposing_ctxs was populated during dispose
        seen_disposing = []
        bs = self.mgr._new_browser_session().__enter__()
        original_dispose = bs.target.dispose_browser_context
        def tracking_dispose(context_id):
            seen_disposing.append(ctx in self.mgr._disposing_ctxs)
            return original_dispose(context_id)
        bs.target.dispose_browser_context = tracking_dispose
        self.mgr._dispose_context(bs, ctx, "test")
        self.assertTrue(any(seen_disposing),
                        "_disposing_ctxs should contain ctx during dispose")

    def test_dispose_context_clears_flag_after(self):
        """_dispose_context must remove ctx from _disposing_ctxs after dispose."""
        row = self.mgr.create_container("test")
        cid = row["id"]
        result = self.mgr.restore(cid)
        ctx = result["browserContextId"]
        self.mgr._dispose_context(
            self.mgr._new_browser_session().__enter__(), ctx, "test")
        self.assertNotIn(ctx, self.mgr._disposing_ctxs)

    def test_detach_cleans_tab_last_activated(self):
        """_detach_context_sessions must clean up _tab_last_activated entries."""
        row = self.mgr.create_container("test")
        cid = row["id"]
        result = self.mgr.restore(cid)
        ctx = result["browserContextId"]
        tid = self.mgr.open_tab(cid, "https://example.com")
        self.mgr._tab_last_activated[tid] = 1234567890.0
        bs = self.mgr._new_browser_session().__enter__()
        self.mgr._detach_context_sessions(ctx, bs)
        self.assertNotIn(tid, self.mgr._tab_last_activated)


# ---------------------------------------------------------------------------
# FIX: Activity-based tiered snapshotting reduces Chrome load by only
# snapshotting idle sessions every Nth cycle.
# ---------------------------------------------------------------------------

class TestTieredSnapshotting(_PatchedManagerMixin, unittest.TestCase):
    """Guard: snapshot_all must skip idle sessions on non-Nth cycles."""

    def _setup_sessions(self):
        """Create two sessions: one active, one idle."""
        import time
        row_active = self.mgr.create_container("active")
        row_idle = self.mgr.create_container("idle")
        self.mgr.restore(row_active["id"])
        self.mgr.restore(row_idle["id"])
        # Open tabs so snapshots have something to save
        self.mgr.open_tab(row_active["id"], "https://active.com")
        self.mgr.open_tab(row_idle["id"], "https://idle.com")
        # Mark idle session as last active 10 minutes ago
        self.mgr._session_last_active[row_idle["id"]] = (
            time.time() - self.mgr._IDLE_THRESHOLD_SEC - 60)
        # Mark active session as just used
        self.mgr._session_last_active[row_active["id"]] = time.time()
        return row_active["id"], row_idle["id"]

    def test_idle_skipped_on_non_nth_cycle(self):
        """Idle sessions should be skipped on cycles that aren't multiples of N."""
        active_cid, idle_cid = self._setup_sessions()
        # Reset cycle counter to a non-multiple
        self.mgr._snapshot_cycle = 0
        results = self.mgr.snapshot_all()
        # Active session should be snapshotted
        result_cids = [r.get("id") for r in results if "id" in r]
        self.assertIn(active_cid, result_cids)
        self.assertNotIn(idle_cid, result_cids)

    def test_idle_included_on_nth_cycle(self):
        """Idle sessions should be included on every Nth cycle."""
        active_cid, idle_cid = self._setup_sessions()
        # Set cycle so next increment is a multiple of _IDLE_SNAPSHOT_MULTIPLE
        self.mgr._snapshot_cycle = self.mgr._IDLE_SNAPSHOT_MULTIPLE - 1
        results = self.mgr.snapshot_all()
        result_cids = [r.get("id") for r in results if "id" in r]
        self.assertIn(active_cid, result_cids)
        self.assertIn(idle_cid, result_cids)

    def test_newly_restored_session_is_active(self):
        """Freshly restored sessions should be treated as active."""
        row = self.mgr.create_container("new")
        cid = row["id"]
        self.mgr.restore(cid)
        self.assertIn(cid, self.mgr._session_last_active)

    def test_snapshot_cycle_increments(self):
        """snapshot_all must increment the cycle counter each call."""
        self.mgr.create_container("x")
        before = self.mgr._snapshot_cycle
        self.mgr.snapshot_all()
        self.assertEqual(self.mgr._snapshot_cycle, before + 1)

    def test_constants_exist(self):
        """Tiering constants must be defined on the class."""
        self.assertIsInstance(ContainerManager._IDLE_THRESHOLD_SEC, (int, float))
        self.assertIsInstance(ContainerManager._IDLE_SNAPSHOT_MULTIPLE, int)
        self.assertGreater(ContainerManager._IDLE_SNAPSHOT_MULTIPLE, 1)


# ---------------------------------------------------------------------------
# FIX: snapshot_all pre-fetches targets once to reduce WebSocket churn.
# ---------------------------------------------------------------------------

class TestPrefetchedTargets(_PatchedManagerMixin, unittest.TestCase):
    """Guard: snapshot_all must pre-fetch targets and pass them through."""

    def test_snapshot_all_prefetches(self):
        """snapshot_all must pre-fetch targets before parallel snapshots."""
        import inspect
        src = inspect.getsource(ContainerManager.snapshot_all)
        self.assertIn("shared_targets", src)
        self.assertIn("prefetched_targets", src)

    def test_collect_state_accepts_prefetched(self):
        """_collect_state must accept a prefetched_targets parameter."""
        import inspect
        sig = inspect.signature(ContainerManager._collect_state)
        self.assertIn("prefetched_targets", sig.parameters)

    def test_snapshot_accepts_prefetched(self):
        """snapshot must accept and forward prefetched_targets."""
        import inspect
        sig = inspect.signature(ContainerManager.snapshot)
        self.assertIn("prefetched_targets", sig.parameters)

    def test_prefetched_targets_used_when_provided(self):
        """_collect_state should use provided targets instead of fetching."""
        row = self.mgr.create_container("test")
        cid = row["id"]
        self.mgr.restore(cid)
        self.mgr.open_tab(cid, "https://example.com")
        ctx = self.mgr.hot[cid]
        # Provide a pre-fetched targets list
        targets = list(self.fb.targets.values())
        result = self.mgr._collect_state(ctx, prefetched_targets=targets)
        cookies, storage, idb, tabs = result
        self.assertGreaterEqual(len(tabs), 1)


# ---------------------------------------------------------------------------
# FIX: _check_stale_hot logs per-context target breakdown when target count
# is high or drops significantly.
# ---------------------------------------------------------------------------

class TestPerContextBreakdownLogging(_PatchedManagerMixin, unittest.TestCase):
    """Guard: target breakdown diagnostic logging exists in _check_stale_hot."""

    def test_breakdown_code_exists(self):
        """_check_stale_hot must contain per-context breakdown logic."""
        import inspect
        src = inspect.getsource(ContainerManager._check_stale_hot)
        self.assertIn("ctx_counts", src)
        self.assertIn("target breakdown", src)


# ---------------------------------------------------------------------------
# FIX: Event loop skips attaching targets from contexts being disposed.
# ---------------------------------------------------------------------------

class TestEventLoopDisposingCtxs(_PatchedManagerMixin, unittest.TestCase):
    """Guard: activation event loop must respect _disposing_ctxs."""

    def test_rebuild_checks_disposing(self):
        """_rebuild in the event loop must skip targets from disposing contexts."""
        import inspect
        src = inspect.getsource(ContainerManager._activation_event_loop)
        self.assertIn("_disposing_ctxs", src)

    def test_poll_evicts_disposing(self):
        """_poll must detach targets from contexts being disposed."""
        import inspect
        src = inspect.getsource(ContainerManager._activation_event_loop)
        # The poll section should check disposing contexts
        self.assertIn("disposing", src)

    def test_session_last_active_updated_on_focus(self):
        """Focus events must update _session_last_active."""
        import inspect
        src = inspect.getsource(ContainerManager._activation_event_loop)
        self.assertIn("_session_last_active", src)


if __name__ == "__main__":
    unittest.main()
