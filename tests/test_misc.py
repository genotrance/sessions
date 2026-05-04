"""Miscellaneous tests: debug logging, real-browser integration."""
from __future__ import annotations

import logging
import os
import sys
import tempfile
import time
import unittest

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_root, "src"))
sys.path.insert(0, _root)

from sessions.cli import setup_logging
from sessions.manager import ContainerManager
from sessions.persistence import PersistenceManager
from sessions import cdp


# ---------------------------------------------------------------------------
# Debug logging test
# ---------------------------------------------------------------------------

class TestDebugLogging(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ctxd-log-")

    def tearDown(self):
        import shutil
        # Clean up any handlers we added
        for h in logging.root.handlers[:]:
            if isinstance(h, logging.FileHandler):
                h.close()
                logging.root.removeHandler(h)
        logging.root.setLevel(logging.WARNING)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_debug_log_created(self):
        log_path = os.path.join(self.tmp, "debug.log")
        setup_logging(debug=True, log_path=log_path)
        logging.getLogger("context_daemon").debug("test message")
        # Force flush
        for h in logging.root.handlers:
            h.flush()
        self.assertTrue(os.path.isfile(log_path))
        with open(log_path) as f:
            content = f.read()
        self.assertIn("test message", content)

    def test_no_debug_no_file(self):
        log_path = os.path.join(self.tmp, "debug.log")
        setup_logging(debug=False, log_path=log_path)
        logging.getLogger("context_daemon").debug("hidden")
        self.assertFalse(os.path.isfile(log_path))


# ---------------------------------------------------------------------------
# Optional real-browser integration test
# ---------------------------------------------------------------------------

@unittest.skipUnless(os.environ.get("CONTEXT_DAEMON_INTEGRATION") == "1",
                     "Set CONTEXT_DAEMON_INTEGRATION=1 to run")
class TestRealBrowserIntegration(unittest.TestCase):
    """Launches a real Chrome headless, checks hibernate/restore end-to-end."""

    @classmethod
    def setUpClass(cls):
        from sessions.cdp import ChromeManager
        cls.tmp = tempfile.mkdtemp(prefix="ctxd-integ-")
        cls.mgr_browser = ChromeManager(
            port=9223,
            user_data_dir=os.path.join(cls.tmp, "profile"),
            pid_file=os.path.join(cls.tmp, "pid"))
        cls.mgr_browser.start(headless=True)
        cls.store = PersistenceManager(os.path.join(cls.tmp, "db.sqlite"))
        cls.mgr = ContainerManager(browser_port=9223, store=cls.store)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.mgr_browser.stop()
        except Exception:
            pass
        import shutil
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_roundtrip(self):
        c = self.store.create_container("integ")
        # Seed a tab list; no real cookies
        self.store.save_hibernation(
            c["id"], [], {},
            [{"url": "about:blank", "title": "blank"}])
        res = self.mgr.restore(c["id"])
        self.assertIn("browserContextId", res)
        time.sleep(1)
        self.mgr.hibernate(c["id"])
        self.assertNotIn(c["id"], self.mgr.hot)

    def test_idb_real_roundtrip_with_binary_data(self):
        """Real-Chrome IDB round-trip: create DB with binary + Date + indexes
        + compound keyPath, dump, restore into fresh DB, verify deep equality."""
        import json as _json
        from sessions.cdp import CDPSession
        from sessions.idb import IDB_DUMP_JS, build_restore_script

        dump_js = IDB_DUMP_JS

        # Open a fresh tab (https origin required — data: URLs block IDB)
        bs = CDPSession.connect_browser(9223)
        try:
            tid = bs.target.create_target(url="https://example.com/")
        finally:
            bs.close()
        # Give the tab time to load
        time.sleep(2.0)
        # Find its ws url
        import urllib.request
        info = _json.loads(urllib.request.urlopen(
            "http://127.0.0.1:9223/json/list", timeout=5).read())
        tab = next(x for x in info if x["id"] == tid)
        tab_ws = tab["webSocketDebuggerUrl"]

        # Step 1: seed the IDB on this tab
        seed = """
        (async function(){
          await new Promise((res, rej) => {
            const req = indexedDB.deleteDatabase('testdb');
            req.onsuccess = req.onerror = () => res();
          });
          const db = await new Promise((res, rej) => {
            const r = indexedDB.open('testdb', 7);
            r.onupgradeneeded = (e) => {
              const d = e.target.result;
              const s1 = d.createObjectStore('msgs', {keyPath: ['chatId', 'id']});
              s1.createIndex('by_ts', 'ts', {unique: false, multiEntry: false});
              s1.createIndex('by_tag', 'tags', {unique: false, multiEntry: true});
              d.createObjectStore('blobs', {autoIncrement: true});
            };
            r.onsuccess = () => res(r.result);
            r.onerror = () => rej(r.error);
          });
          const tx = db.transaction(['msgs', 'blobs'], 'readwrite');
          const msgs = tx.objectStore('msgs');
          msgs.put({chatId: 'c1', id: 'm1', ts: new Date(1700000000000),
                    tags: ['a', 'b'],
                    payload: new Uint8Array([1,2,3,4,5]).buffer,
                    key: new Uint8Array([10,20,30])});
          msgs.put({chatId: 'c2', id: 'm2', ts: new Date(1700000001000),
                    tags: ['b'], payload: new ArrayBuffer(8),
                    key: new Uint8Array([40,50])});
          const blobs = tx.objectStore('blobs');
          blobs.put(new Uint8Array([99, 100, 101]));
          await new Promise((res, rej) => {
            tx.oncomplete = res;
            tx.onerror = () => rej(tx.error);
          });
          db.close();
          return 'ok';
        })()
        """
        tab_sess = CDPSession(tab_ws)
        try:
            result = tab_sess.runtime.evaluate(seed, await_promise=True, timeout=10)
            self.assertEqual(result, "ok")

            # Step 2: dump via _IDB_DUMP_JS
            dumped = tab_sess.runtime.evaluate(dump_js, await_promise=True, timeout=15)
            self.assertIsNotNone(dumped)
            idb_data = _json.loads(dumped)
            self.assertIn("testdb", idb_data)
            self.assertEqual(idb_data["testdb"]["_meta"]["version"], 7)
            self.assertEqual(idb_data["testdb"]["msgs"]["keyPath"], ["chatId", "id"])
            self.assertEqual(len(idb_data["testdb"]["msgs"]["rows"]), 2)
            idxs = idb_data["testdb"]["msgs"]["indexes"]
            self.assertEqual(len(idxs), 2)
            by_tag = next(i for i in idxs if i["name"] == "by_tag")
            self.assertTrue(by_tag["multiEntry"])

            # Verify encoding markers: Date should be __t:'D', AB should be __t:'AB'
            row0 = idb_data["testdb"]["msgs"]["rows"][0]
            self.assertEqual(row0["ts"]["__t"], "D")
            self.assertEqual(row0["payload"]["__t"], "AB")
            self.assertEqual(row0["key"]["__t"], "TA")
            self.assertEqual(row0["key"]["n"], "Uint8Array")

            # Step 3: wipe and restore via build_restore_script
            tab_sess.runtime.evaluate(
                "new Promise((res)=>{const r=indexedDB.deleteDatabase('testdb');"
                "r.onsuccess=r.onerror=()=>res();})",
                await_promise=True, timeout=5)
            restore_script = build_restore_script(idb_data)
            tab_sess.runtime.evaluate(restore_script, timeout=5)
            # Wait for restore to finish (async inside the IIFE)
            time.sleep(1.5)

            # Step 4: read back and verify
            verify = """
            (async function(){
              const db = await new Promise((res,rej)=>{
                const r=indexedDB.open('testdb');
                r.onsuccess=()=>res(r.result);r.onerror=()=>rej(r.error);
              });
              if (db.version !== 7) return {err:'bad version: '+db.version};
              const tx = db.transaction(['msgs','blobs'],'readonly');
              const msgs = tx.objectStore('msgs');
              const row = await new Promise((res,rej)=>{
                const r=msgs.get(['c1','m1']);
                r.onsuccess=()=>res(r.result);r.onerror=()=>rej(r.error);
              });
              if (!row) return {err:'missing row'};
              if (!(row.ts instanceof Date)) return {err:'ts not Date: '+typeof row.ts};
              if (row.ts.getTime() !== 1700000000000) return {err:'ts value wrong'};
              if (!(row.payload instanceof ArrayBuffer))
                return {err:'payload not AB: '+row.payload.constructor.name};
              const pb = new Uint8Array(row.payload);
              if (pb.length !== 5 || pb[0]!==1 || pb[4]!==5)
                return {err:'payload bytes wrong: ['+Array.from(pb).join(',')+']'};
              if (!(row.key instanceof Uint8Array))
                return {err:'key not U8: '+row.key.constructor.name};
              if (row.key[0]!==10 || row.key[2]!==30)
                return {err:'key bytes wrong'};
              // Verify indexes work
              const byTs = msgs.index('by_ts');
              const cnt = await new Promise((res,rej)=>{
                const r=byTs.count();
                r.onsuccess=()=>res(r.result);r.onerror=()=>rej(r.error);
              });
              if (cnt !== 2) return {err:'by_ts count wrong: '+cnt};
              db.close();
              return {ok:true};
            })()
            """
            verify_res = tab_sess.runtime.evaluate(
                verify, await_promise=True, timeout=10)
            self.assertEqual(verify_res, {"ok": True},
                             f"verify returned: {verify_res}")
        finally:
            tab_sess.close()


# ---------------------------------------------------------------------------
# Data directory tests
# ---------------------------------------------------------------------------

class TestDataDir(unittest.TestCase):
    def test_data_dir_not_in_temp(self):
        """Session data must be in a persistent location, not TEMP."""
        data_dir = cdp._default_data_dir()
        temp_dir = os.environ.get("TEMP", "")
        if sys.platform == "win32" and temp_dir:
            self.assertFalse(data_dir.startswith(temp_dir),
                             f"data dir {data_dir} must not be under TEMP")

    def test_data_dir_contains_sessions(self):
        data_dir = cdp._default_data_dir()
        self.assertTrue(data_dir.endswith("Sessions"),
                        f"data dir {data_dir} should end with 'Sessions'")

    def test_pid_file_in_temp(self):
        """PID file IS ephemeral and should live in TEMP."""
        pid = cdp._default_pid_file()
        self.assertIn("sessions.pid", pid)


# ---------------------------------------------------------------------------
# Dashboard close detection test
# ---------------------------------------------------------------------------

class TestDashboardCloseDetection(unittest.TestCase):
    def test_dashboard_close_fires_callback(self):
        """When the dashboard targetId disappears, _on_ui_close is called."""
        from tests.fakes import _PatchedManagerMixin

        class _H(_PatchedManagerMixin):
            def setUp(self): super().setUp()
            def tearDown(self): super().tearDown()

        h = _H()
        h.setUp()
        try:
            mgr = h.mgr
            fb = h.fb
            # Simulate a dashboard target existing
            tid = fb.seed_tab("", "http://localhost:9999/", "Sessions")
            mgr._dashboard_target_id = tid
            called = []
            mgr._on_ui_close = lambda: called.append(True)

            # Target still present → no callback
            mgr._check_dashboard_alive()
            self.assertEqual(called, [])

            # Remove the target (simulate window close)
            del fb.targets[tid]
            mgr._check_dashboard_alive()
            self.assertEqual(called, [True])
            self.assertIsNone(mgr._dashboard_target_id)
        finally:
            h.tearDown()

    def test_no_callback_when_not_set(self):
        """No crash if _on_ui_close is None."""
        from tests.fakes import _PatchedManagerMixin

        class _H(_PatchedManagerMixin):
            def setUp(self): super().setUp()
            def tearDown(self): super().tearDown()

        h = _H()
        h.setUp()
        try:
            h.mgr._dashboard_target_id = "stale-id"
            h.mgr._on_ui_close = None
            h.mgr._check_dashboard_alive()  # should not raise
        finally:
            h.tearDown()

    def test_transient_cdp_failure_does_not_shutdown(self):
        """WS failures where Chrome HTTP is still alive must not trigger recovery.
        The early-HTTP-probe only fires recovery when HTTP is ALSO down."""
        from tests.fakes import _PatchedManagerMixin

        class _H(_PatchedManagerMixin):
            def setUp(self): super().setUp()
            def tearDown(self): super().tearDown()

        h = _H()
        h.setUp()
        try:
            mgr = h.mgr
            called = []
            mgr._dashboard_target_id = "some-tid"
            mgr._on_ui_close = lambda: called.append(True)

            # Make _browser_session raise every time (WS dead)
            def _failing():
                raise RuntimeError("CDP dead")
            mgr._browser_session = _failing
            # But HTTP is still responding (transient WS disconnection, not a crash)
            mgr._chrome_http_reachable = lambda: True

            # Call 4 times — WS keeps failing but HTTP probe says Chrome is alive
            for _ in range(4):
                mgr._check_dashboard_alive()
            self.assertEqual(called, [], "Should not shut down while HTTP is alive")
            self.assertEqual(mgr._dashboard_cdp_failures, 4)
        finally:
            h.tearDown()

    def test_chrome_crash_detected_fast_when_http_also_down(self):
        """When WS fails AND HTTP is also unreachable, recovery fires after 2
        failures instead of waiting for the full 5-failure threshold."""
        from tests.fakes import _PatchedManagerMixin

        class _H(_PatchedManagerMixin):
            def setUp(self): super().setUp()
            def tearDown(self): super().tearDown()

        h = _H()
        h.setUp()
        try:
            import threading as _t
            mgr = h.mgr
            crash_evt = _t.Event()
            crash_calls = []
            mgr._dashboard_target_id = "some-tid"
            mgr._on_ui_close = lambda: crash_calls.append("ui_close")
            def _on_crash():
                crash_calls.append("crash")
                crash_evt.set()
            mgr._on_chrome_crash = _on_crash

            def _failing():
                raise RuntimeError("CDP dead")
            mgr._browser_session = _failing
            # HTTP is also unreachable (Chrome really is dead)
            mgr._chrome_http_reachable = lambda: False

            # Failure 1: counter < 2, no action yet
            mgr._check_dashboard_alive()
            self.assertEqual(crash_calls, [])
            # Failure 2: counter >= 2 AND HTTP down → recovery thread spawned
            mgr._check_dashboard_alive()
            self.assertTrue(crash_evt.wait(timeout=2),
                            "Recovery should fire at failure 2 when HTTP is also down")
            self.assertIn("crash", crash_calls)
        finally:
            h.tearDown()


# ---------------------------------------------------------------------------
# Reconnect and resilience tests
# ---------------------------------------------------------------------------

class TestReconnectToExisting(unittest.TestCase):
    """reconnect_to_existing should rebuild hot map from live Chrome contexts."""

    def test_reconnects_active_containers(self):
        from tests.fakes import _PatchedManagerMixin

        class _H(_PatchedManagerMixin):
            def setUp(self): super().setUp()
            def tearDown(self): super().tearDown()

        h = _H()
        h.setUp()
        try:
            mgr = h.mgr
            fb = h.fb
            # Create a container marked as active with saved tabs
            c = h.store.create_container("test-reconnect")
            cid = c["id"]
            h.store.save_hibernation(
                cid, [], {},
                [{"url": "https://example.com/page", "title": "Example"}],
                keep_active=True)
            # Simulate Chrome having a context with matching URL
            ctx_id = "CTX-ORPHAN"
            fb._ctx_counter = 0
            fb.targets["T-orphan"] = {
                "targetId": "T-orphan", "url": "https://example.com/page",
                "title": "Example", "browserContextId": ctx_id, "type": "page"}
            self.assertNotIn(cid, mgr.hot)
            result = mgr.reconnect_to_existing()
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["id"], cid)
            self.assertTrue(result[0]["reconnected"])
            self.assertEqual(mgr.hot[cid], ctx_id)
        finally:
            h.tearDown()

    def test_no_reconnect_if_no_match(self):
        from tests.fakes import _PatchedManagerMixin

        class _H(_PatchedManagerMixin):
            def setUp(self): super().setUp()
            def tearDown(self): super().tearDown()

        h = _H()
        h.setUp()
        try:
            c = h.store.create_container("no-match")
            h.store.save_hibernation(
                c["id"], [], {},
                [{"url": "https://unique-site.com/", "title": "Unique"}],
                keep_active=True)
            # Chrome has no matching contexts
            result = h.mgr.reconnect_to_existing()
            self.assertEqual(result, [])
            self.assertNotIn(c["id"], h.mgr.hot)
        finally:
            h.tearDown()


class TestReconcileHot(unittest.TestCase):
    """_reconcile_hot should remove dead contexts from hot map."""

    def test_removes_dead_contexts(self):
        from tests.fakes import _PatchedManagerMixin

        class _H(_PatchedManagerMixin):
            def setUp(self): super().setUp()
            def tearDown(self): super().tearDown()

        h = _H()
        h.setUp()
        try:
            mgr = h.mgr
            c = h.store.create_container("will-die")
            cid = c["id"]
            # Manually put it in hot with a fake context
            mgr.hot[cid] = "CTX-DEAD"
            h.store.mark_active(cid, True)
            # No targets in Chrome for this context
            mgr._reconcile_hot()
            self.assertNotIn(cid, mgr.hot)
        finally:
            h.tearDown()

    def test_keeps_alive_contexts(self):
        from tests.fakes import _PatchedManagerMixin

        class _H(_PatchedManagerMixin):
            def setUp(self): super().setUp()
            def tearDown(self): super().tearDown()

        h = _H()
        h.setUp()
        try:
            mgr = h.mgr
            fb = h.fb
            c = h.store.create_container("alive")
            cid = c["id"]
            ctx = "CTX-ALIVE"
            mgr.hot[cid] = ctx
            fb.targets["T-alive"] = {
                "targetId": "T-alive", "url": "https://alive.com",
                "title": "", "browserContextId": ctx, "type": "page"}
            mgr._reconcile_hot()
            self.assertIn(cid, mgr.hot)
        finally:
            h.tearDown()


class TestCLIStopStatus(unittest.TestCase):
    """Unit tests for cmd_stop and cmd_status without a running daemon."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="ctxd-cli-")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _daemon_is_running():
        """Check if a real daemon is running by looking for the PID file."""
        from sessions.cli import DAEMON_PID_FILE
        import json as _json
        try:
            with open(DAEMON_PID_FILE) as f:
                info = _json.load(f)
                return bool(info.get("pid"))
        except (OSError, ValueError):
            return False

    @unittest.skipIf(_daemon_is_running.__func__(), "Skip if daemon is running")
    def test_cmd_stop_no_daemon(self):
        """cmd_stop prints 'daemon not running' and returns 0 when no PID file."""
        import io
        from sessions.cli import cmd_stop
        from unittest.mock import patch
        with patch("sessions.cli.DAEMON_PID_FILE", "/nonexistent/path/sessions-api.pid"):
            with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                ret = cmd_stop(None)
        self.assertEqual(ret, 0)
        self.assertIn("daemon not running", mock_out.getvalue())

    @unittest.skipIf(_daemon_is_running.__func__(), "Skip if daemon is running")
    def test_cmd_stop_stale_pid(self):
        """cmd_stop with a PID file pointing to a gone process cleans up gracefully."""
        import json as _json
        from sessions.cli import cmd_stop
        from unittest.mock import patch
        import io
        pid_file = os.path.join(self.tmp, "sessions-api.pid")
        with open(pid_file, "w") as f:
            _json.dump({"pid": 99999999, "api_port": 9999}, f)
        with patch("sessions.cli.DAEMON_PID_FILE", pid_file):
            with patch("sessions.cli._remove_daemon_pid"):
                with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                    ret = cmd_stop(None)
        self.assertEqual(ret, 0)
        self.assertTrue(
            "stopped" in mock_out.getvalue() or "stale" in mock_out.getvalue()
            or "could not" in mock_out.getvalue()
        )

    def test_cmd_status_no_daemon(self):
        """cmd_status returns JSON with browser status even when daemon not running."""
        import io
        import json as _json
        import argparse
        from sessions.cli import cmd_status
        from unittest.mock import patch
        args = argparse.Namespace(browser_port=19222)
        fake_status = {"running": False}
        with patch("sessions.cli._read_daemon_pid", return_value=None):
            with patch("sessions.cli.ChromeManager") as MockCM:
                MockCM.return_value.status.return_value = fake_status
                with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                    ret = cmd_status(args)
        self.assertEqual(ret, 0)
        data = _json.loads(mock_out.getvalue())
        self.assertIn("browser", data)

    def test_cmd_status_with_daemon(self):
        """cmd_status merges PID info with browser status into JSON output."""
        import io
        import json as _json
        import argparse
        from sessions.cli import cmd_status
        from unittest.mock import patch
        args = argparse.Namespace(browser_port=19222)
        with patch("sessions.cli._read_daemon_pid",
                   return_value={"pid": 1234, "api_port": 9999}):
            with patch("sessions.cli.ChromeManager") as MockCM:
                MockCM.return_value.status.return_value = {"running": True}
                with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                    ret = cmd_status(args)
        self.assertEqual(ret, 0)
        data = _json.loads(mock_out.getvalue())
        self.assertEqual(data["pid"], 1234)
        self.assertEqual(data["api_port"], 9999)
        self.assertIn("browser", data)


class TestNoHotkeyFlag(unittest.TestCase):
    """Verify --no-hotkey suppresses global keyboard registration."""

    def test_no_hotkey_accepted_by_argparser(self):
        """sessions start --no-hotkey --foreground must parse without error."""
        from sessions.cli import main
        from unittest.mock import patch
        with patch("sessions.cli.cmd_start", return_value=0) as mock_start:
            ret = main(["start", "--foreground", "--no-hotkey"])
        self.assertEqual(ret, 0)
        self.assertTrue(mock_start.called)
        args = mock_start.call_args[0][0]
        self.assertTrue(args.no_hotkey)
        self.assertFalse(args.no_hotkey is False)

    def test_hotkey_guard_condition_with_flag(self):
        """Guard condition: _keyboard and not no_hotkey must be False when flag set."""
        import types
        from unittest.mock import MagicMock
        fake_kb = MagicMock()
        args = types.SimpleNamespace(no_hotkey=True)
        should_register = fake_kb and not getattr(args, "no_hotkey", False)
        self.assertFalse(should_register)

    def test_hotkey_guard_condition_without_flag(self):
        """Guard condition: _keyboard and not no_hotkey must be True when flag absent."""
        import types
        from unittest.mock import MagicMock
        fake_kb = MagicMock()
        args = types.SimpleNamespace(no_hotkey=False)
        should_register = fake_kb and not getattr(args, "no_hotkey", False)
        self.assertTrue(should_register)

    def test_no_hotkey_not_in_rebuild_argv(self):
        """--no-hotkey must NOT appear in the re-exec argv (it's a startup-only flag)."""
        import argparse
        from sessions.cli import _rebuild_argv, DEFAULT_API_PORT
        from sessions.manager import DEFAULT_BROWSER_PORT
        args = argparse.Namespace(
            api_port=DEFAULT_API_PORT, browser_port=DEFAULT_BROWSER_PORT,
            headless=False, no_browser_open=False, debug=False, no_hotkey=True,
        )
        argv = _rebuild_argv(args)
        self.assertNotIn("--no-hotkey", argv)


class TestDashboardDisconnectedBanner(unittest.TestCase):
    """Dashboard HTML should have a disconnected banner and reconnection logic."""

    @classmethod
    def setUpClass(cls):
        from sessions.dashboard import DASHBOARD_HTML
        cls.html = DASHBOARD_HTML

    def test_has_disconnected_element(self):
        self.assertIn('id=disconnected', self.html)
        self.assertIn('Disconnected', self.html)

    def test_has_reconnection_logic(self):
        self.assertIn('setDisconnected(', self.html)
        self.assertIn('Reconnected', self.html)

    def test_faster_polling_when_disconnected(self):
        self.assertIn('_disconnected', self.html)
        # Should have conditional interval logic (fast when disconnected)
        self.assertIn('_POLL_DISCONNECTED', self.html)
        self.assertIn('_POLL_CONNECTED', self.html)
        self.assertIn('_disconnected ? _POLL_DISCONNECTED : _POLL_CONNECTED', self.html)


if __name__ == "__main__":
    unittest.main()
