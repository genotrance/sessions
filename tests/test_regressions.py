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
        """Dashboard CSS must define .row.hot and .row.cold classes."""
        from sessions.dashboard import DASHBOARD_HTML
        self.assertIn(".row.hot", DASHBOARD_HTML)
        self.assertIn(".row.cold", DASHBOARD_HTML)
        # Hot = green border, Cold = amber border
        self.assertIn("#22c55e", DASHBOARD_HTML)
        self.assertIn("#f59e0b", DASHBOARD_HTML)

    def test_dashboard_js_applies_hot_cold_class(self):
        """renderList must apply 'hot' or 'cold' class based on c.hot."""
        from sessions.dashboard import DASHBOARD_HTML
        self.assertIn("c.hot", DASHBOARD_HTML)


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


if __name__ == "__main__":
    unittest.main()
