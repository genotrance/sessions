"""CLI entry points for Sessions: start, stop, status."""
from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import signal
import socket as _socket
import subprocess as _subprocess
import sys
import threading
import time
import urllib.request
from typing import Any

from . import cdp
from .cdp import ChromeManager
from .manager import ContainerManager, DEFAULT_BROWSER_PORT
from .server import DEFAULT_API_PORT, make_server

try:
    import keyboard as _keyboard
except Exception:
    _keyboard = None

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
IS_WINDOWS = sys.platform == "win32"

SNAPSHOT_INTERVAL_SEC = 30
DAEMON_PID_FILE = os.path.join(os.path.dirname(cdp._default_pid_file()),
                               "sessions-api.pid")

log = logging.getLogger("sessions")


def setup_logging(debug: bool = False, log_path: str | None = None) -> None:
    level = logging.DEBUG if debug else logging.WARNING
    if debug:
        path = log_path or os.path.join(SCRIPT_DIR, "debug.log")
        handler = logging.FileHandler(path, mode="a", encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s  %(message)s"))
        logging.root.addHandler(handler)
        # Write a visible separator so restarts are easy to spot in the log
        with open(path, "a", encoding="utf-8") as _f:
            _f.write(f"\n{'-'*80}\n"
                     f"  PROCESS START  {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                     f"{'-'*80}\n")
    logging.root.setLevel(level)


# ---------------------------------------------------------------------------
# Daemon PID helpers
# ---------------------------------------------------------------------------

def _write_daemon_pid(pid: int, api_port: int) -> None:
    os.makedirs(os.path.dirname(DAEMON_PID_FILE), exist_ok=True)
    with open(DAEMON_PID_FILE, "w") as f:
        json.dump({"pid": pid, "api_port": api_port}, f)


def _read_daemon_pid() -> dict | None:
    try:
        with open(DAEMON_PID_FILE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _remove_daemon_pid() -> None:
    try:
        os.remove(DAEMON_PID_FILE)
    except OSError:
        pass


def _rebuild_argv(args) -> list[str]:
    """Reconstruct CLI argv from parsed args for re-exec."""
    argv = []
    if args.api_port != DEFAULT_API_PORT:
        argv += ["--api-port", str(args.api_port)]
    if args.browser_port != DEFAULT_BROWSER_PORT:
        argv += ["--browser-port", str(args.browser_port)]
    argv.append("start")
    if getattr(args, "headless", False):
        argv.append("--headless")
    if getattr(args, "no_browser_open", False):
        argv.append("--no-browser-open")
    if getattr(args, "debug", False):
        argv.append("--debug")
    argv.append("--foreground")
    return argv


# ---------------------------------------------------------------------------
# Stale Chrome helpers
# ---------------------------------------------------------------------------

def _reclaim_stale_chrome(browser_port: int) -> bool:
    """If a Chrome instance from a previous unclean exit is still listening
    on the debugging port, decide whether to reuse or kill it.
    Returns True if Chrome is running and should be reused (reconnected)."""
    cm = ChromeManager(port=browser_port)
    if not cm.is_running():
        return False
    # There's a Chrome on this port already. Check if our daemon is managing it.
    info = _read_daemon_pid()
    if info and info.get("api_port") and info.get("pid") != os.getpid():
        # Try asking the old daemon to shut down first
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{info['api_port']}/api/shutdown",
                method="POST",
                headers={"Content-Type": "application/json"},
                data=b"{}")
            urllib.request.urlopen(req, timeout=3)
            time.sleep(1)
            if not cm.is_running():
                _remove_daemon_pid()
                log.debug("reclaimed stale daemon via API shutdown")
                return False
        except Exception:
            pass
    # No daemon responding but Chrome is alive — reuse it instead of killing
    log.debug("found orphan Chrome on port %s, will reconnect", browser_port)
    _remove_daemon_pid()
    return True


def _wait_for_chrome_exit(browser_port: int, timeout: float = 5) -> None:
    """Poll until Chrome on the given port stops responding."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            import urllib.request as _ur
            _ur.urlopen(f"http://localhost:{browser_port}/json/version",
                        timeout=1)
            time.sleep(0.3)
        except Exception:
            return


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_start(args) -> int:
    if not getattr(args, "foreground", False):
        # Re-launch via the same entry point (run.py or __main__.py)
        entry_script = sys.argv[0]
        child_argv = [sys.executable, entry_script] + _rebuild_argv(args)
        popen_kw: dict[str, Any] = {
            "stdout": _subprocess.DEVNULL,
            "stderr": _subprocess.DEVNULL,
            "stdin": _subprocess.DEVNULL,
        }
        if IS_WINDOWS:
            popen_kw["creationflags"] = (
                _subprocess.CREATE_NEW_PROCESS_GROUP | _subprocess.DETACHED_PROCESS)
        else:
            popen_kw["start_new_session"] = True
        proc = _subprocess.Popen(child_argv, **popen_kw)
        dash_url = f"http://127.0.0.1:{args.api_port}/"
        # Wait briefly for the child to be ready
        for _ in range(40):
            # Quick TCP probe before attempting a full HTTP request
            try:
                s = _socket.create_connection(("127.0.0.1", args.api_port), timeout=0.5)
                s.close()
                urllib.request.urlopen(
                    f"http://127.0.0.1:{args.api_port}/api/containers", timeout=2)
                break
            except Exception:
                time.sleep(0.5)
        print(f"Sessions running at {dash_url}  (pid {proc.pid})")
        return 0

    # ---- foreground mode: the actual server ----
    _debug = getattr(args, "debug", False)
    _log_path = os.path.join(SCRIPT_DIR, "debug.log") if _debug else None
    setup_logging(_debug, _log_path)
    log.debug("cmd_start api_port=%s browser_port=%s", args.api_port, args.browser_port)
    # Stop any existing daemon on this port only if something is listening.
    # Check TCP first to avoid sending shutdown to ourselves after binding.
    try:
        _s = _socket.create_connection(("127.0.0.1", args.api_port), timeout=0.5)
        _s.close()
        urllib.request.urlopen(
            f"http://127.0.0.1:{args.api_port}/api/shutdown",
            data=b"", timeout=2)
        time.sleep(1.0)
    except Exception:
        pass

    # Create manager + bind server port FIRST (fast, no Chrome needed)
    try:
        manager = ContainerManager(browser_port=args.browser_port)
        manager._debug_mode = _debug
        manager._log_path = _log_path
        log.debug("ContainerManager created")
    except Exception:
        log.exception("ContainerManager init failed")
        raise
    try:
        server = make_server(manager, port=args.api_port)
        log.debug("server bound to port %s", args.api_port)
    except Exception:
        log.exception("make_server failed")
        raise
    _write_daemon_pid(os.getpid(), args.api_port)

    # Start Chrome in a background thread so the HTTP server is responsive immediately
    _chrome_ready = threading.Event()
    _chrome_error: list[Exception] = []
    _chrome_ref: list = []  # [ChromeManager] populated once Chrome is ready

    def _start_chrome_bg():
        try:
            _reclaim_stale_chrome(args.browser_port)
            _cm = cdp.ensure_chrome(port=args.browser_port, headless=args.headless)
            _chrome_ref.append(_cm)
            log.debug("chrome ready")
        except Exception as e:
            log.exception("ensure_chrome failed")
            _chrome_error.append(e)
        finally:
            _chrome_ready.set()

    threading.Thread(target=_start_chrome_bg, daemon=True).start()

    stop_evt = threading.Event()

    # Fence event: signals when snapshot_loop is idle (not mid-snapshot)
    _snap_fence = threading.Event()
    _snap_fence.set()  # starts idle
    manager._snapshot_fence = _snap_fence

    def snapshot_loop():
        # Wait for Chrome before starting the snapshot loop
        _chrome_ready.wait(timeout=30)
        if _chrome_error:
            return
        while not stop_evt.wait(SNAPSHOT_INTERVAL_SEC):
            _snap_fence.clear()
            try:
                manager.snapshot_all()
            except Exception as e:
                log.warning("snapshot failed: %s", e)
            finally:
                _snap_fence.set()

    snap_thread = threading.Thread(target=snapshot_loop, daemon=True)
    snap_thread.start()

    # Watcher also needs Chrome — start it from _post_start instead
    def _start_watcher_when_ready():
        _chrome_ready.wait(timeout=30)
        if not _chrome_error:
            manager.start_watcher()

    threading.Thread(target=_start_watcher_when_ready, daemon=True).start()

    def graceful_exit(*_a):
        if stop_evt.is_set():
            return
        stop_evt.set()
        log.debug("graceful_exit triggered")
        try:
            manager.quick_shutdown()
        except Exception:
            pass
        # Wait for Chrome to actually exit before cleaning up PID files
        _wait_for_chrome_exit(args.browser_port, timeout=5)
        _remove_daemon_pid()
        if _chrome_ref:
            try:
                _chrome_ref[0]._remove_pid()
            except Exception:
                pass
        # Shut down HTTP server then hard-exit so non-daemon threads don't
        # keep the process alive (watcher, snapshot_loop are non-daemon)
        threading.Thread(target=server.shutdown, daemon=True).start()
        os._exit(0)

    # ---- restart: keep Chrome running, re-exec the backend ----
    # _restart_argv is set by restart_backend() to signal the finally block
    # to spawn a new process instead of doing a normal exit.
    _restart_argv: list[str] = []

    def restart_backend():
        log.debug("restart_backend triggered")

        def _do_restart():
            stop_evt.set()
            manager.stop_watcher(stop_evt_thread=True)
            try:
                manager.snapshot_all()
            except Exception as e:
                log.warning("pre-restart snapshot failed: %s", e)
            _remove_daemon_pid()
            # Build child argv and stash it so the main thread's finally block
            # can spawn it after serve_forever() unblocks.
            entry_script = sys.argv[0]
            child_argv = [sys.executable, entry_script] + _rebuild_argv(args)
            _restart_argv.extend(child_argv)
            # Give the HTTP response a moment to be sent, then stop the server.
            # server.shutdown() wakes up serve_forever() on the main thread.
            time.sleep(0.3)
            server.shutdown()

        threading.Thread(target=_do_restart, daemon=True, name="restart").start()

    # Wire restart callback into the HTTP handler.
    # Must wrap in staticmethod so self.restart_cb() won't pass `self`
    # as first arg (Python's descriptor protocol would bind it otherwise).
    server.RequestHandlerClass.restart_cb = staticmethod(restart_backend)

    # Register UI-close callback so closing the Chrome window triggers shutdown
    manager._on_ui_close = graceful_exit

    _recovering = threading.Lock()  # guard: only one recovery attempt at a time

    def recover_chrome():
        """Called when Chrome is confirmed dead while the backend is running.
        Restarts Chrome and restores containers that are still marked active
        in the DB.  Containers that a bulk operation already marked inactive
        (recording the user's intent) are left hibernated."""
        if not _recovering.acquire(blocking=False):
            log.debug("recover_chrome: already in progress, skipping")
            return
        try:
            if stop_evt.is_set():
                return
            log.warning("recover_chrome: Chrome died unexpectedly, attempting restart")
            # 1. Clear in-memory hot map (browser contexts are gone) and
            #    check DB is_active to learn which containers the user
            #    intended to keep alive vs. those already marked for
            #    hibernation by a bulk operation.
            with manager._lock:
                hot_cids = list(manager.hot.keys())
                manager.hot.clear()
            db_active = {c["id"] for c in manager.store.list_containers()
                         if c.get("is_active")}
            restore_cids = [c for c in hot_cids if c in db_active]
            skip_cids = [c for c in hot_cids if c not in db_active]
            for cid in hot_cids:
                manager._last_snapshot_time.pop(cid, None)
                manager._last_snapshot_hash.pop(cid, None)
            # Mark genuinely-active containers inactive while Chrome is down
            # (restore() will re-mark them active).
            if restore_cids:
                manager.store.mark_active_bulk(restore_cids, False)
            manager._invalidate_browser_session()
            if skip_cids:
                log.debug("recover_chrome: skipping %d containers already "
                          "marked inactive (user intent): %s",
                          len(skip_cids), skip_cids)
            # Clear the stale dashboard target ID so _check_dashboard_alive
            # does not mistake the missing old target for a user-close event
            # while Chrome is restarting.
            manager._dashboard_target_id = None
            # 2. Kill hung Chrome (if any) before restart
            if not _chrome_ref:
                log.error("recover_chrome: no ChromeManager ref, falling back to exit")
                graceful_exit()
                return
            chrome_mgr = _chrome_ref[0]
            log.debug("recover_chrome: force-stopping Chrome before restart")
            try:
                chrome_mgr.stop(force=True)
            except Exception as e:
                log.debug("recover_chrome: Chrome stop error (expected if already dead): %s", e)
            # 3. Restart Chrome with longer timeout (hung Chrome may take time to fully exit)
            try:
                log.debug("recover_chrome: restarting Chrome (30s timeout)")
                chrome_mgr.start(headless=getattr(args, 'headless', False), timeout=30)
                log.debug("recover_chrome: Chrome restarted successfully")
            except Exception as e:
                log.error("recover_chrome: Chrome restart failed (%s), will retry on next crash detection", e)
                # Don't exit — let crash detection retry on the next cycle
                return
            # 4. Reopen the dashboard tab
            if not getattr(args, 'no_browser_open', False):
                manager.open_dashboard_in_default_tab(dash_url)
            # 5. Restore containers that were genuinely active
            for cid in restore_cids:
                try:
                    log.debug("recover_chrome: restoring container %s", cid)
                    manager.restore(cid)
                except Exception as e:
                    log.warning("recover_chrome: restore %s failed: %s", cid, e)
            log.warning("recover_chrome: recovery complete, %d restored, "
                        "%d left hibernated",
                        len(restore_cids), len(skip_cids))
        except Exception as e:
            log.error("recover_chrome: unexpected error: %s", e)
            graceful_exit()
        finally:
            _recovering.release()

    manager._on_chrome_crash = recover_chrome

    signal.signal(signal.SIGTERM, graceful_exit)
    if not IS_WINDOWS:
        try:
            signal.signal(signal.SIGHUP, graceful_exit)
        except (AttributeError, ValueError):
            pass

    dash_url = f"http://127.0.0.1:{args.api_port}/"

    def _post_start():
        try:
            # Wait for Chrome to be ready before doing any CDP work
            log.debug("_post_start: waiting for Chrome…")
            _chrome_ready.wait(timeout=30)
            if _chrome_error:
                log.error("_post_start: Chrome failed to start: %s", _chrome_error[0])
                return
            log.debug("_post_start running")
            if not getattr(args, "no_browser_open", False):
                manager.open_dashboard_in_default_tab(dash_url)
            if _keyboard and not getattr(args, "no_hotkey", False):
                _hk = "windows+/" if IS_WINDOWS else "ctrl+/"
                try:
                    _keyboard.add_hotkey(_hk, lambda: manager.activate_dashboard())
                    log.debug("registered %s hotkey for dashboard", _hk)
                except Exception as e:
                    log.warning("could not register global hotkey: %s", e)
            # Try reconnecting to existing Chrome contexts first
            reconnected = manager.reconnect_to_existing()
            if reconnected:
                log.debug("reconnected %d containers to existing Chrome",
                          len(reconnected))
            # Restore any active containers that weren't reconnected
            restored = manager.auto_restore_hot()
            if restored:
                log.debug("auto-restored %d containers", len(restored))
            log.debug("_post_start done")
        except Exception:
            log.exception("_post_start failed")

    threading.Thread(target=_post_start, daemon=True).start()

    log.debug("server listening at %s", dash_url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        graceful_exit()
    finally:
        stop_evt.set()
        server.server_close()
        if _restart_argv:
            log.debug("restart_backend: old server stopped, spawning new process")
            popen_kw: dict[str, Any] = {
                "stdout": _subprocess.DEVNULL,
                "stderr": _subprocess.DEVNULL,
                "stdin": _subprocess.DEVNULL,
            }
            if IS_WINDOWS:
                popen_kw["creationflags"] = (
                    _subprocess.CREATE_NEW_PROCESS_GROUP | _subprocess.DETACHED_PROCESS)
            else:
                popen_kw["start_new_session"] = True
            _subprocess.Popen(_restart_argv, **popen_kw)
            log.debug("restart_backend: new process spawned, exiting")
            os._exit(0)
    return 0


def cmd_stop(_args) -> int:
    info = _read_daemon_pid()
    if not info:
        print("daemon not running")
        return 0
    api_port = info.get("api_port", DEFAULT_API_PORT)
    pid = info.get("pid")
    # Try clean HTTP shutdown first (triggers hibernate + chrome close)
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{api_port}/api/shutdown",
            method="POST",
            headers={"Content-Type": "application/json"},
            data=b"{}")
        urllib.request.urlopen(req, timeout=60)  # wait for snapshots to finish
        # Wait for process to actually exit (os._exit in graceful_exit)
        if pid:
            for _ in range(120):  # up to 30s
                try:
                    os.kill(pid, 0)
                    time.sleep(0.25)
                except OSError:
                    break
        _remove_daemon_pid()
        print("stopped (clean shutdown)")
        return 0
    except Exception:
        pass
    # Fall back to SIGTERM
    if pid:
        try:
            if IS_WINDOWS:
                os.kill(pid, signal.SIGTERM)
            else:
                os.kill(pid, signal.SIGTERM)
            print("stopped (SIGTERM)")
        except ProcessLookupError:
            print("stale PID file (process already gone), cleaning up")
        except OSError as e:
            print(f"could not signal process {pid}: {e}")
    _remove_daemon_pid()
    return 0


def cmd_status(args) -> int:
    info = _read_daemon_pid() or {}
    info["browser"] = ChromeManager(port=args.browser_port).status()
    print(json.dumps(info, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="sessions")
    p.add_argument("--api-port", type=int, default=DEFAULT_API_PORT)
    p.add_argument("--browser-port", type=int, default=DEFAULT_BROWSER_PORT)
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("start")
    sp.add_argument("--headless", action="store_true")
    sp.add_argument("--no-browser-open", action="store_true")
    sp.add_argument("--debug", action="store_true",
                    help="Write debug output to debug.log in the script directory")
    sp.add_argument("--foreground", action="store_true",
                    help="Run in foreground (default is to daemonize)")
    sp.add_argument("--no-hotkey", action="store_true",
                    help="Disable the global keyboard hotkey for the dashboard")
    sp.set_defaults(func=cmd_start)
    sub.add_parser("stop").set_defaults(func=cmd_stop)
    sub.add_parser("status").set_defaults(func=cmd_status)
    args = p.parse_args(argv)
    return args.func(args)
