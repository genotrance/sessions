"""ContainerManager - Maps persisted containers to live browser contexts."""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import logging
import sys
import threading
import time
import urllib.parse
from typing import Any

from . import cdp
from .cdp import CDPSession, CDPError
from .idb import (
    IDB_DUMP_JS,
    IDB_LIST_JS,
    build_restore_db_scripts as _build_idb_db_scripts,
    build_restore_scaffolding as _build_idb_scaffolding,
    build_single_db_dump_js as _build_single_db_dump_js,
)
from .persistence import PersistenceManager
from .utils import (                        # pure helpers live in utils.py
    clean_cookie as _clean_cookie,
    domain_of as _domain_of,
    normalize_url as _normalize_url,
    origin_of as _origin_of,
)

log = logging.getLogger("sessions")


def _canonical_tab_url(url: str) -> str:
    """Return the real destination of a login-redirect URL.

    Apps like Discord redirect to ``/login?redirect_to=%2Fchannels%2F...``
    when there is no active session.  Saving that redirect URL and restoring
    it on the next launch perpetuates the logout loop because the login page
    has no localStorage/IDB to collect.  Instead save the decoded destination
    so the restore injects storage into the right origin and navigates directly
    to the intended page.
    """
    if not url:
        return url
    try:
        parsed = urllib.parse.urlparse(url)
        qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        for key in ("redirect_to", "redirectTo", "redirect", "next", "return_to"):
            dest = qs.get(key, [None])[0]
            if dest:
                dest = urllib.parse.unquote(dest)
                # Relative path → make absolute using the same origin
                if dest.startswith("/"):
                    dest = f"{parsed.scheme}://{parsed.netloc}{dest}"
                log.debug("_canonical_tab_url: %s → %s", url, dest)
                return dest
    except Exception:
        pass
    return url


DEFAULT_BROWSER_PORT = 9222
WINDOW_WATCHER_INTERVAL_SEC = 5
# How recent a snapshot must be (seconds) to skip re-collecting on shutdown
SNAPSHOT_FRESHNESS_SEC = 25
# Timeout for CDP calls during snapshot (seconds)
SNAPSHOT_CDP_TIMEOUT = 5
# Timeout for the monolithic async IDB dump expression (fallback).
IDB_DUMP_TIMEOUT = 30
# Per-database IDB dump timeout (seconds).  Much shorter than the monolithic
# dump because each call only processes a single database.
IDB_PER_DB_TIMEOUT = 10
# Total time budget (seconds) for all per-database IDB dumps on one tab.
# After this, remaining databases use cached data from previous snapshots.
IDB_TOTAL_BUDGET = 25
# Timeout for the quick indexedDB.databases() metadata call.
IDB_LIST_TIMEOUT = 5
# IDB database names to skip during snapshot.  These are large cache/index
# databases that are not needed for session restoration and consistently time
# out, burning the entire IDB_TOTAL_BUDGET.  Auth state for these apps lives
# in localStorage, not in these databases.
_IDB_SKIP_NAMES: frozenset[str] = frozenset({
    # WhatsApp: search index, job queue, media LRU cache, offline store
    "fts-storage",
    "jobs-storage",
    "lru-media-storage-idb",
    "offd-storage",
})


# Backward-compatible alias used by tests and scripts
_IDB_DUMP_JS_MODULE = IDB_DUMP_JS


class ContainerManager:
    """Maps persisted containers to live browser contexts."""

    def __init__(self, browser_port: int = DEFAULT_BROWSER_PORT,
                 store: PersistenceManager | None = None):
        self.browser_port = browser_port
        self.store = store or PersistenceManager()
        # id -> browserContextId
        self.hot: dict[str, str] = {}
        self._lock = threading.RLock()
        self._cached_bs: CDPSession | None = None
        self._bs_lock = threading.Lock()
        self._watcher_stop = threading.Event()
        self._watcher_thread: threading.Thread | None = None
        self._dashboard_target_id: str | None = None
        self._on_ui_close: Any = None  # callback when dashboard window closed
        self._on_chrome_crash: Any = None  # callback: Chrome process died unexpectedly
        # Snapshot caching: {cid: epoch} of last successful snapshot
        self._last_snapshot_time: dict[str, float] = {}
        # Content-hash of last saved state per container — skip write if unchanged
        self._last_snapshot_hash: dict[str, str] = {}
        # Per-database IDB cache: "origin\x00dbName" -> db_dump_dict
        # Used to avoid losing data when a single DB times out on dump.
        self._idb_cache: dict[str, dict] = {}
        # Shared targets cache for batch operations (avoids redundant /json/list calls)
        self._targets_cache: list[dict] | None = None
        self._targets_cache_time: float = 0
        # Consecutive snapshot failures that look like CDP transport outage.
        # Used as a secondary crash detector in case watcher is temporarily down.
        self._snapshot_cdp_failures: int = 0
        self._crash_recovery_inflight = threading.Lock()
        # Per-tab last-activated timestamp (targetId -> epoch) for recency sort
        self._tab_last_activated: dict[str, float] = {}
        # Focus-polling thread: polls document.hasFocus() via CDP flatten sessions
        self._evt_thread: threading.Thread | None = None
        self._evt_stop = threading.Event()  # separate from _watcher_stop

    # -- low-level CDP helpers ------------------------------------------------

    class _BorrowedSession:
        """Wrapper that acts as a context manager but skips close()."""
        def __init__(self, session: CDPSession):
            self._s = session
        def __getattr__(self, name):
            return getattr(self._s, name)
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            pass  # don't close — session is cached

    _CDP_RECONNECT_ATTEMPTS = 3
    _CDP_RECONNECT_DELAY = 1.0  # seconds between attempts
    # Short timeout for the liveness probe — enough for a healthy session
    _CDP_PROBE_TIMEOUT = 5.0
    _SNAPSHOT_CRASH_FAILURE_THRESHOLD = 2

    def _browser_session(self) -> _BorrowedSession:
        with self._bs_lock:
            # If another thread already reconnected, reuse immediately
            if self._cached_bs is not None:
                # Quick liveness probe — use short timeout so a dead WebSocket
                # fails fast instead of blocking for 30s
                try:
                    self._cached_bs.send("Browser.getVersion",
                                         timeout=self._CDP_PROBE_TIMEOUT)
                except Exception:
                    log.debug("cached CDP session is stale, reconnecting")
                    try:
                        self._cached_bs.close()
                    except Exception:
                        pass
                    self._cached_bs = None
            if self._cached_bs is not None:
                return self._BorrowedSession(self._cached_bs)
            # Reconnect while holding the lock — prevents parallel reconnect storms
            last_err: Exception | None = None
            for attempt in range(self._CDP_RECONNECT_ATTEMPTS):
                try:
                    log.debug("connecting to browser CDP on port %s (attempt %d)",
                              self.browser_port, attempt + 1)
                    sess = CDPSession.connect_browser(self.browser_port)
                    log.debug("browser CDP session established")
                    self._cached_bs = sess
                    return self._BorrowedSession(sess)
                except Exception as e:
                    last_err = e
                    if attempt < self._CDP_RECONNECT_ATTEMPTS - 1:
                        time.sleep(self._CDP_RECONNECT_DELAY)
            raise RuntimeError(
                f"CDP connect failed after {self._CDP_RECONNECT_ATTEMPTS} attempts: {last_err}")

    def _new_browser_session(self) -> CDPSession:
        """Open a fresh (non-shared) browser-level CDP session.

        Using fresh sessions for concurrent operations (e.g. parallel snapshot
        threads) avoids WebSocket receive-loop collisions that happen when
        multiple threads share a single CDPSession and send commands concurrently.
        Tests can monkeypatch this method to return a fake session."""
        return CDPSession.connect_browser(self.browser_port)

    def _invalidate_browser_session(self):
        with self._bs_lock:
            bs = self._cached_bs
            self._cached_bs = None
            if bs:
                try:
                    bs.close()
                except Exception:
                    pass

    def _get_targets_cached(self, max_age: float = 2.0) -> list[dict]:
        """Return /json/list, using a short-lived cache to avoid repeated HTTP calls."""
        now = time.time()
        if self._targets_cache is not None and (now - self._targets_cache_time) < max_age:
            return self._targets_cache
        info = cdp.requests.get(
            f"http://127.0.0.1:{self.browser_port}/json/list",
            timeout=(0.5, 3)).json()
        self._targets_cache = info
        self._targets_cache_time = now
        return info

    def _invalidate_targets_cache(self):
        self._targets_cache = None
        self._targets_cache_time = 0

    def _tab_session(self, target_id: str) -> CDPSession:
        info = self._get_targets_cached()
        t = next((x for x in info if x["id"] == target_id), None)
        if not t:
            raise RuntimeError(f"target {target_id} not found")
        return CDPSession(t["webSocketDebuggerUrl"], timeout=SNAPSHOT_CDP_TIMEOUT)

    # -- window watcher (auto-hibernate on close) -----------------------------

    def start_watcher(self) -> None:
        """Start a background thread that auto-hibernates containers whose
        browser windows have been closed by the user."""
        if self._watcher_thread and self._watcher_thread.is_alive():
            return
        self._watcher_stop.clear()
        self._watcher_thread = threading.Thread(
            target=self._watcher_loop, daemon=True, name="window-watcher")
        self._watcher_thread.start()
        # Start the CDP event listener that tracks Chrome-native tab activation
        # Uses its own _evt_stop so snapshot_all can pause the watcher without
        # killing the focus-polling loop.
        if not (self._evt_thread and self._evt_thread.is_alive()):
            self._evt_stop.clear()
            self._evt_thread = threading.Thread(
                target=self._activation_event_loop, daemon=True, name="cdp-events")
            self._evt_thread.start()

    def stop_watcher(self, join_timeout: float = 12.0,
                     stop_evt_thread: bool = False) -> None:
        self._watcher_stop.set()
        if self._watcher_thread and self._watcher_thread.is_alive():
            self._watcher_thread.join(timeout=join_timeout)
        if stop_evt_thread:
            self._evt_stop.set()
            if self._evt_thread and self._evt_thread.is_alive():
                self._evt_thread.join(timeout=join_timeout)

    _FOCUS_POLL_INTERVAL = 2.0
    _FOCUS_REBUILD_INTERVAL = 30.0

    def _activation_event_loop(self) -> None:
        """Poll document.hasFocus() on each hot tab to track Chrome-native
        tab/window activation.

        Uses a dedicated browser-level CDP session with
        Target.attachToTarget(flatten=True) so that Runtime.evaluate can be
        issued per-tab via ``session_id`` without conflicting with the per-tab
        direct WS connections used for snapshots.

        Correctly detects:
          • Tab switches within a window
          • Window switches (alt-tab, mouse click)
          • Focus returning to Chrome from another application

        The focused tab's ``_tab_last_activated`` timestamp is updated, and
        the owning session's ``last_accessed_at`` is touched in the DB so the
        UI reorders both tabs and sessions by recency."""
        bs: CDPSession | None = None
        # tid -> (session_id, browserContextId)
        attached: dict[str, tuple[str, str]] = {}
        prev_focused: str | None = None
        last_rebuild: float = 0

        log.debug("cdp-events: activation poller started (hasFocus polling)")

        def _connect() -> CDPSession | None:
            try:
                s = CDPSession.connect_browser(self.browser_port)
                log.debug("cdp-events: browser session connected")
                return s
            except Exception as exc:
                log.debug("cdp-events: browser connect failed: %s", exc)
                return None

        def _rebuild() -> None:
            nonlocal last_rebuild
            last_rebuild = time.monotonic()
            with self._lock:
                ctx_to_cid = {v: k for k, v in self.hot.items()}
            try:
                targets = bs.target.get_targets(timeout=5)
            except Exception:
                return
            current_tids: set[str] = set()
            for t in targets:
                if t.get("type") != "page":
                    continue
                ctx = t.get("browserContextId", "")
                if ctx not in ctx_to_cid:
                    continue
                tid = t.get("targetId", "")
                current_tids.add(tid)
                if tid not in attached:
                    try:
                        sid = bs.target.attach_to_target(tid, flatten=True)
                        attached[tid] = (sid, ctx)
                        log.debug("cdp-events: attached tid=%s", tid[:8])
                    except Exception as exc:
                        log.debug("cdp-events: attach %s failed: %s",
                                  tid[:8], exc)
            # Detach tabs that are no longer hot
            for tid in list(attached):
                if tid not in current_tids:
                    try:
                        bs.target.detach_from_target(attached[tid][0])
                    except Exception:
                        pass
                    del attached[tid]
            log.debug("cdp-events: rebuild done, attached=%d", len(attached))

        def _poll() -> None:
            nonlocal prev_focused
            with self._lock:
                ctx_to_cid = {v: k for k, v in self.hot.items()}
            focused_tid: str | None = None
            focused_ctx: str | None = None
            for tid, (sid, ctx) in list(attached.items()):
                try:
                    result = bs.send(
                        "Runtime.evaluate",
                        {"expression": "document.hasFocus()",
                         "returnByValue": True},
                        session_id=sid, timeout=3)
                    val = result.get("result", {}).get("value")
                    if val is True:
                        focused_tid = tid
                        focused_ctx = ctx
                        break  # only one tab can have focus
                except CDPError as e:
                    # Session-level error (e.g. stale session_id after tab
                    # close/navigate).  Remove from attached so _rebuild()
                    # will re-attach the tab fresh on the next cycle.
                    log.debug("cdp-events: session error tid=%s: %s",
                              tid[:8], e)
                    attached.pop(tid, None)
                except TimeoutError:
                    # Individual tab unresponsive (frozen renderer, navigating
                    # to a new page, etc.).  Evict it from attached so the
                    # next _rebuild() will re-attach it fresh.  Do NOT tear
                    # down the whole WS connection — other tabs are fine.
                    log.debug("cdp-events: tab timeout, evicting tid=%s",
                              tid[:8])
                    attached.pop(tid, None)
                # Other exceptions (WebSocket closed, OS errors, …) are NOT
                # caught here.  They propagate to the outer try/except which
                # sets bs=None and triggers a full reconnect + rebuild.
            if focused_tid and focused_tid != prev_focused:
                prev_focused = focused_tid
                now = time.time()
                self._tab_last_activated[focused_tid] = now
                cid = ctx_to_cid.get(focused_ctx, "")
                if cid:
                    self.store.touch_accessed(cid)
                    log.debug("cdp-events: focus tid=%s cid=%s",
                              focused_tid[:8], cid)

        # --- main loop -------------------------------------------------------
        bs = _connect()
        if bs:
            _rebuild()

        while not self._evt_stop.wait(self._FOCUS_POLL_INTERVAL):
            # Reconnect if needed
            if bs is None:
                bs = _connect()
                if bs is None:
                    continue
                _rebuild()
                continue
            # Periodic rebuild to pick up new/closed tabs
            if time.monotonic() - last_rebuild > self._FOCUS_REBUILD_INTERVAL:
                _rebuild()
            try:
                _poll()
            except Exception as exc:
                log.debug("cdp-events: poll error: %s", exc)
                try:
                    bs.close()
                except Exception:
                    pass
                bs = None
                attached.clear()
                prev_focused = None

        # Cleanup on stop
        if bs:
            try:
                bs.close()
            except Exception:
                pass

    def _watcher_loop(self) -> None:
        _consecutive_failures = 0
        _tick = 0
        while not self._watcher_stop.wait(WINDOW_WATCHER_INTERVAL_SEC):
            _tick += 1
            log.debug("watcher tick %d, hot=%d", _tick, len(self.hot))
            try:
                self._check_stale_hot()
                _consecutive_failures = 0
            except Exception as e:
                _consecutive_failures += 1
                log.debug("watcher tick error (%d): %s", _consecutive_failures, e)
                if _consecutive_failures >= 3:
                    # Likely a dead websocket from sleep/wake — force reconnect
                    log.debug("too many watcher failures, invalidating CDP session")
                    self._invalidate_browser_session()
                    _consecutive_failures = 0
                    try:
                        self._reconcile_hot()
                    except Exception as re:
                        log.debug("reconcile_hot failed: %s", re)
            try:
                self._check_dashboard_alive()
            except Exception as e:
                log.debug("dashboard-alive check error: %s", e)

    def _chrome_http_reachable(self) -> bool:
        """Quick check: is Chrome's HTTP debug endpoint responding?"""
        try:
            cdp.requests.get(
                f"http://127.0.0.1:{self.browser_port}/json/version",
                timeout=(0.5, 2))
            return True
        except Exception:
            return False

    def _check_dashboard_alive(self) -> None:
        """If the UI window (dashboard target) was closed, trigger shutdown.
        Transient CDP errors (sleep/wake) are retried; we only shut down if
        Chrome's HTTP endpoint is also unreachable after several attempts."""
        tid = self._dashboard_target_id
        if not tid or not self._on_ui_close:
            return
        try:
            with self._browser_session() as bs:
                targets = bs.target.get_targets(timeout=SNAPSHOT_CDP_TIMEOUT)
            # Success — reset failure counter
            self._dashboard_cdp_failures = 0
        except Exception as e:
            self._dashboard_cdp_failures = getattr(
                self, '_dashboard_cdp_failures', 0) + 1
            log.debug("dashboard CDP check failed (%d): %s",
                      self._dashboard_cdp_failures, e)
            if self._dashboard_cdp_failures < 5:
                # After 2 failures do an early HTTP probe — if Chrome's HTTP is
                # also unreachable we can skip waiting for 5 WS failures.
                if self._dashboard_cdp_failures < 2 or self._chrome_http_reachable():
                    return  # Transient — wait and retry next tick
                # >= 2 WS failures AND HTTP gone: Chrome is dead
            elif self._chrome_http_reachable():
                log.debug("CDP WS dead but Chrome HTTP alive — will reconnect")
                self._invalidate_browser_session()
                self._dashboard_cdp_failures = 0
                return
            log.debug("Chrome unreachable after %d failures, triggering crash recovery",
                      self._dashboard_cdp_failures)
            self._dashboard_target_id = None
            self._dashboard_cdp_failures = 0
            if self._on_chrome_crash:
                threading.Thread(target=self._on_chrome_crash, daemon=True,
                                 name="chrome-recovery").start()
            elif self._on_ui_close:
                self._on_ui_close()
            return
        if not any(t.get("targetId") == tid for t in targets):
            log.debug("dashboard target %s gone, triggering UI-close", tid)
            self._dashboard_target_id = None
            self._on_ui_close()

    @staticmethod
    def _is_cdp_connectivity_error(err: Exception) -> bool:
        msg = str(err).lower()
        needles = (
            "max retries exceeded",
            "connecttimeout",
            "connection refused",
            "failed to establish a new connection",
            "read timed out",
            "websocket",
            "connection reset",
            "no response for",
            "timed out waiting for",
        )
        return any(n in msg for n in needles)

    def _maybe_trigger_snapshot_crash_recovery(self, cid: str, err: Exception) -> None:
        if not self._on_chrome_crash:
            return
        if not self._is_cdp_connectivity_error(err):
            self._snapshot_cdp_failures = 0
            return
        self._snapshot_cdp_failures += 1
        if self._snapshot_cdp_failures < self._SNAPSHOT_CRASH_FAILURE_THRESHOLD:
            return
        if self._chrome_http_reachable():
            return
        if not self._crash_recovery_inflight.acquire(blocking=False):
            return
        log.warning("snapshot: Chrome unreachable during %s, triggering crash recovery", cid)
        self._invalidate_browser_session()

        def _run_recovery():
            try:
                self._on_chrome_crash()
            finally:
                self._snapshot_cdp_failures = 0
                self._crash_recovery_inflight.release()

        threading.Thread(target=_run_recovery, daemon=True,
                         name="snapshot-crash-recovery").start()

    def _reconcile_hot(self) -> None:
        """After a CDP reconnect, verify which hot contexts are still alive
        in Chrome and remove dead ones from the hot map."""
        with self._lock:
            if not self.hot:
                return
            try:
                with self._browser_session() as bs:
                    all_targets = bs.target.get_targets(
                        timeout=SNAPSHOT_CDP_TIMEOUT)
            except Exception:
                log.debug("reconcile_hot: CDP still unreachable")
                return
            live_ctxs: set[str] = set()
            for t in all_targets:
                ctx = t.get("browserContextId")
                if ctx:
                    live_ctxs.add(ctx)
            dead_cids = [cid for cid, ctx in self.hot.items()
                         if ctx not in live_ctxs]
            for cid in dead_cids:
                log.debug("reconcile_hot: context for %s is gone, marking cold", cid)
                self.hot.pop(cid, None)
                self.store.mark_active(cid, False)
            if not dead_cids:
                log.debug("reconcile_hot: all %d contexts still alive", len(self.hot))

    def _check_stale_hot(self) -> None:
        """For each hot container, verify its browserContextId still has at
        least one live page target. If not, auto-hibernate it."""
        with self._lock:
            if not self.hot:
                return
            log.debug("_check_stale_hot: checking %d hot containers", len(self.hot))
            hot_snapshot = dict(self.hot)
        # Do CDP outside the lock — prevents blocking Win-/ activate for 30s
        try:
            with self._browser_session() as bs:
                all_targets = bs.target.get_targets(
                    timeout=SNAPSHOT_CDP_TIMEOUT)
            log.debug("_check_stale_hot: got %d targets from Chrome", len(all_targets))
        except Exception as e:
            log.debug("_check_stale_hot: CDP error getting targets: %s", e)
            return
        live_ctxs: set[str] = set()
        for t in all_targets:
            if t.get("type") == "page":
                ctx = t.get("browserContextId")
                if ctx:
                    live_ctxs.add(ctx)
        stale_cids = []
        with self._lock:
            for cid, ctx in list(self.hot.items()):
                # Only consider containers that were hot when we started the check
                if cid in hot_snapshot and ctx not in live_ctxs:
                    stale_cids.append(cid)

        if stale_cids:
            log.debug("_check_stale_hot: stale cids=%s", stale_cids)
        for cid in stale_cids:
            try:
                log.debug("auto-hibernating %s (window closed)", cid)
                self._soft_hibernate(cid)
            except Exception as e:
                log.debug("auto-hibernate failed for %s: %s", cid, e)
                with self._lock:
                    self.hot.pop(cid, None)
                    self.store.mark_active(cid, False)

    def _soft_hibernate(self, cid: str) -> None:
        """Mark a container cold and dispose its browser context WITHOUT
        overwriting the persisted state.  Used by the window-close watcher
        where the tabs are already gone from Chrome so _collect_state would
        return empty lists.  The last snapshot's tabs/cookies/storage are
        preserved in the DB."""
        log.debug("_soft_hibernate: cid=%s", cid)
        with self._lock:
            ctx = self.hot.pop(cid, None)
            if ctx:
                log.debug("_soft_hibernate: disposing context %s", ctx)
                with self._new_browser_session() as bs:
                    try:
                        all_targets = bs.target.get_targets(timeout=SNAPSHOT_CDP_TIMEOUT)
                        for t in all_targets:
                            if t.get("browserContextId") == ctx and t.get("type") == "page":
                                try:
                                    bs.target.close_target(t["targetId"])
                                except Exception:
                                    pass
                    except Exception:
                        pass
                    try:
                        bs.target.dispose_browser_context(ctx)
                        log.debug("_soft_hibernate: context %s disposed", ctx)
                    except CDPError as e:
                        log.debug("_soft_hibernate: dispose CDPError (ignored): %s", e)
            self.store.mark_active(cid, False)
            log.debug("soft-hibernated %s (preserved last snapshot)", cid)

    # -- create / open --------------------------------------------------------

    def create_container(self, name: str, color: str = "#3b82f6") -> dict:
        return self.store.create_container(name, color)

    def open_tab(self, cid: str, url: str = "about:blank") -> str:
        log.debug("open_tab cid=%s url=%s", cid, url)
        with self._lock:
            row = self.store.get_container(cid)
            if not row:
                raise KeyError(cid)
            if cid not in self.hot:
                result = self.restore(cid, also_open_url=url)
                return result.get("activate_target_id") or ""
            ctx = self.hot[cid]
            with self._browser_session() as bs:
                return bs.target.create_target(url=url, browser_context_id=ctx)

    def _targets_for(self, browser_context_id: str) -> list[dict]:
        with self._browser_session() as bs:
            return [t for t in bs.target.get_targets(timeout=SNAPSHOT_CDP_TIMEOUT)
                    if t.get("browserContextId") == browser_context_id
                    and t.get("type") == "page"]

    # -- hibernate ------------------------------------------------------------

    def _collect_state(self, ctx: str) -> tuple[list[dict], dict, list[dict]]:
        log.debug("_collect_state: ctx=%s", ctx)
        # Use a fresh per-call browser session instead of the shared cached one.
        # The shared session is not thread-safe for concurrent sends: when 3
        # containers snapshot in parallel each thread sends a command on the same
        # WebSocket, the receive loops cross-read each other's responses and
        # timeout.  A fresh session per _collect_state call avoids this entirely.
        fresh_bs = self._new_browser_session()
        try:
            all_targets = fresh_bs.target.get_targets(
                timeout=SNAPSHOT_CDP_TIMEOUT)
            log.debug("_collect_state: %d total targets from Chrome", len(all_targets))
            tab_infos = [t for t in all_targets
                         if t.get("browserContextId") == ctx
                         and t.get("type") == "page"]
            tab_infos = [t for t in tab_infos
                         if t.get("url")
                         and not t["url"].startswith("chrome://")
                         and t["url"] not in ("", "about:blank")]
            log.debug("_collect_state: %d saveable tabs in ctx %s", len(tab_infos), ctx)
            tabs_to_save = [{"url": _canonical_tab_url(t.get("url", "")),
                             "title": t.get("title", ""),
                             "last_scrolled": 0}
                            for t in tab_infos]
            cookies = fresh_bs.storage.get_cookies(
                browser_context_id=ctx, timeout=SNAPSHOT_CDP_TIMEOUT)
            log.debug("_collect_state: %d cookies collected", len(cookies))
        finally:
            try:
                fresh_bs.close()
            except Exception:
                pass

        # Pre-populate targets cache so parallel _tab_session calls don't each HTTP
        try:
            self._get_targets_cached(max_age=0)
        except Exception:
            pass

        _idb_cache = self._idb_cache
        _IDB_LIST = IDB_LIST_JS

        def _get_ls_and_idb(t):
            tid = t["targetId"]
            tab_url = t.get("url", "?")
            try:
                with self._tab_session(tid) as ts:
                    origin = ts.runtime.evaluate("window.location.origin",
                                                 timeout=SNAPSHOT_CDP_TIMEOUT)
                    if not origin or origin == "null":
                        log.debug("_collect_state: tab %s origin=%s (url=%s), skipping",
                                  tid, origin, tab_url)
                        return None
                    # Use CDP DOMStorage domain to read localStorage.
                    # This bypasses sites (e.g. Discord) that delete
                    # window.localStorage from the JS prototype.
                    ls = {}
                    try:
                        dom_st = ts.send("DOMStorage.getDOMStorageItems", {
                            "storageId": {
                                "securityOrigin": origin,
                                "isLocalStorage": True,
                            }
                        }, timeout=SNAPSHOT_CDP_TIMEOUT)
                        for k, v in dom_st.get("entries", []):
                            ls[k] = v
                    except Exception:
                        # Fallback: JS evaluation (works when DOMStorage
                        # domain is unavailable, e.g. some headless modes).
                        ls_dump = ts.runtime.evaluate(
                            "try{JSON.stringify(Object.fromEntries("
                            "Object.entries(localStorage)))}catch(e){null}",
                            timeout=SNAPSHOT_CDP_TIMEOUT)
                        ls = json.loads(ls_dump) if ls_dump else {}
                    # --- Per-database IDB dump with time budget + cache ---
                    idb: dict = {}
                    try:
                        db_list_raw = ts.runtime.evaluate(
                            _IDB_LIST, await_promise=True,
                            timeout=IDB_LIST_TIMEOUT)
                        db_list = json.loads(db_list_raw) if db_list_raw else []
                    except Exception as e:
                        log.debug("_collect_state: idb list error for %s: %s", tid, e)
                        db_list = []
                    if db_list:
                        idb_t0 = time.time()
                        for db_info in db_list:
                            db_name = db_info.get("n") or ""
                            if not db_name:
                                continue
                            if db_name in _IDB_SKIP_NAMES:
                                log.debug("idb: %s/%s skipped (known cache db)",
                                          origin, db_name)
                                continue
                            cache_key = f"{origin}\x00{db_name}"
                            elapsed = time.time() - idb_t0
                            # Budget exhausted — fill remaining from cache
                            if elapsed > IDB_TOTAL_BUDGET:
                                cached = _idb_cache.get(cache_key)
                                if cached:
                                    idb[db_name] = cached
                                    log.debug("idb: %s/%s budget exceeded, using cache",
                                              origin, db_name)
                                else:
                                    log.debug("idb: %s/%s budget exceeded, no cache",
                                              origin, db_name)
                                continue
                            per_timeout = min(
                                IDB_PER_DB_TIMEOUT,
                                max(IDB_TOTAL_BUDGET - elapsed, 2))
                            try:
                                js = _build_single_db_dump_js(db_name)
                                raw = ts.runtime.evaluate(
                                    js, await_promise=True,
                                    timeout=per_timeout)
                                data = json.loads(raw) if raw else {}
                                if data:
                                    idb[db_name] = data
                                    _idb_cache[cache_key] = data
                                else:
                                    cached = _idb_cache.get(cache_key)
                                    if cached:
                                        idb[db_name] = cached
                            except Exception:
                                cached = _idb_cache.get(cache_key)
                                if cached:
                                    idb[db_name] = cached
                                    log.debug("idb: %s/%s timed out (%.1fs), "
                                              "using cache", origin, db_name,
                                              time.time() - idb_t0)
                                else:
                                    log.debug("idb: %s/%s timed out (%.1fs), "
                                              "no cache", origin, db_name,
                                              time.time() - idb_t0)
                    log.debug("_collect_state: tab %s origin=%s ls_keys=%d idb_dbs=%d",
                              tid, origin, len(ls), len(idb))
                    return (origin, ls, idb)
            except Exception as e:
                log.debug("_collect_state: storage error for %s (url=%s): %s",
                          tid, tab_url, e)
            return None

        storage: dict[str, dict] = {}
        idb_data: dict[str, dict] = {}
        if tab_infos:
            with concurrent.futures.ThreadPoolExecutor(
                    max_workers=min(len(tab_infos), 4)) as pool:
                futs = {pool.submit(_get_ls_and_idb, t): t for t in tab_infos}
                try:
                    completed = concurrent.futures.as_completed(futs, timeout=IDB_DUMP_TIMEOUT + 2 * SNAPSHOT_CDP_TIMEOUT)
                    for fut in completed:
                        try:
                            result = fut.result(timeout=2)
                            if result:
                                origin, ls, idb = result
                                if ls:
                                    storage[origin] = ls
                                if idb:
                                    idb_data[origin] = idb
                        except Exception as e:
                            tid = futs[fut]["targetId"]
                            log.debug("_collect_state: storage timeout for %s: %s", tid, e)
                except TimeoutError:
                    pending = [futs[f]["targetId"] for f in futs if not f.done()]
                    log.warning("_collect_state: %d futures timed out (targets=%s), returning partial results",
                                len(pending), pending)
                    for f in futs:
                        f.cancel()
        log.debug("_collect_state: %d origins with localStorage, %d with idb",
                  len(storage), len(idb_data))
        return cookies, storage, idb_data, tabs_to_save

    def snapshot(self, cid: str) -> dict:
        """Persist current state without disposing the context. Crash-safe save."""
        log.debug("snapshot: cid=%s", cid)
        # Grab ctx under lock, then release to avoid blocking during CDP calls
        with self._lock:
            if cid not in self.hot:
                log.debug("snapshot: %s not hot, skipping", cid)
                return {"id": cid, "skipped": "not-hot"}
            ctx = self.hot[cid]
        # Collect state WITHOUT holding the lock (CDP calls can be slow)
        try:
            cookies, storage, idb, tabs = self._collect_state(ctx)
        except Exception as e:
            log.debug("snapshot: collect_state failed for %s: %s", cid, e)
            self._maybe_trigger_snapshot_crash_recovery(cid, e)
            return {"id": cid, "error": str(e)}
        self._snapshot_cdp_failures = 0
        # Compute a cheap fingerprint — skip the DB write if nothing changed
        state_hash = hashlib.md5(
            json.dumps([tabs, cookies, storage, idb], sort_keys=True).encode(),
            usedforsecurity=False).hexdigest()
        # Re-acquire lock only for the quick save
        with self._lock:
            if cid not in self.hot or self.hot[cid] != ctx:
                log.debug("snapshot: %s changed while collecting, skipping save", cid)
                return {"id": cid, "skipped": "stale"}
            if self._last_snapshot_hash.get(cid) == state_hash:
                log.debug("snapshot: %s unchanged (hash match), skipping write", cid)
                self._last_snapshot_time[cid] = time.time()  # refresh freshness
                return {"id": cid, "skipped": "unchanged"}
            # If storage/IDB collection failed (empty or fewer origins than
            # previously saved), preserve the last good data per-origin so
            # a single tab timeout doesn't wipe previously captured state.
            prev = self.store.get_container(cid)
            if prev:
                prev_storage = prev.get("storage", {})
                if prev_storage:
                    merged_storage = dict(prev_storage)
                    merged_storage.update(storage)  # new wins where present
                    if len(merged_storage) > len(storage):
                        preserved = len(merged_storage) - len(storage)
                        log.debug("snapshot: localStorage missing %d origins for %s, preserving",
                                  preserved, cid)
                    storage = merged_storage
                prev_idb = prev.get("idb", {})
                if prev_idb:
                    merged_idb = dict(prev_idb)
                    merged_idb.update(idb)  # new wins where present
                    if len(merged_idb) > len(idb):
                        preserved = len(merged_idb) - len(idb)
                        log.debug("snapshot: IDB empty for %s, preserving %d previous idb-origins",
                                  cid, preserved)
                    idb = merged_idb
            self.store.save_hibernation(cid, cookies, storage, tabs,
                                        keep_active=True, idb=idb)
            self._last_snapshot_time[cid] = time.time()
            self._last_snapshot_hash[cid] = state_hash
            log.debug("snapshot: saved %d tabs, %d cookies, %d ls-origins, %d idb-origins for %s",
                      len(tabs), len(cookies), len(storage), len(idb), cid)
            return {"id": cid, "tabs_saved": len(tabs),
                    "cookies_saved": len(cookies),
                    "origins_with_storage": len(storage),
                    "origins_with_idb": len(idb)}

    def snapshot_all(self) -> list[dict]:
        """Snapshot all hot containers in parallel.

        Stops the watcher first so it doesn't race with our CDP calls, then
        restarts it when done.
        """
        watcher_was_running = (
            self._watcher_thread is not None and self._watcher_thread.is_alive()
        )
        if watcher_was_running:
            self.stop_watcher()  # waits for current tick to finish
        self._invalidate_targets_cache()  # force one fresh fetch
        try:
            self._get_targets_cached(max_age=0)
        except Exception:
            pass
        cids = list(self.hot.keys())
        results: list[dict] = []
        try:
            if not cids:
                return results
            with concurrent.futures.ThreadPoolExecutor(
                    max_workers=min(len(cids), 4)) as pool:
                def _snap(cid):
                    try:
                        return self.snapshot(cid)
                    except Exception as e:
                        log.warning("snapshot_all: %s failed: %s", cid, e)
                        return {"id": cid, "error": str(e)}
                futs = {pool.submit(_snap, cid): cid for cid in cids}
                try:
                    for fut in concurrent.futures.as_completed(futs, timeout=60):
                        try:
                            results.append(fut.result(timeout=2))
                        except Exception as e:
                            results.append({"error": str(e)})
                except concurrent.futures.TimeoutError:
                    pending = [futs[f] for f in futs if not f.done()]
                    log.warning("snapshot_all: timed out waiting for %d snapshots (cids=%s)",
                                len(pending), pending)
                    for f in futs:
                        if not f.done():
                            f.cancel()
        finally:
            if watcher_was_running:
                self.start_watcher()
        return results

    def _snapshot_if_stale(self, cid: str) -> dict:
        """Snapshot only if the last save is older than SNAPSHOT_FRESHNESS_SEC."""
        last = self._last_snapshot_time.get(cid, 0)
        age = time.time() - last
        if age < SNAPSHOT_FRESHNESS_SEC:
            log.debug("snapshot: %s fresh (%.1fs ago), skipping", cid, age)
            return {"id": cid, "skipped": "fresh"}
        return self.snapshot(cid)

    def hibernate(self, cid: str) -> dict:
        log.debug("hibernate cid=%s hot=%s", cid, list(self.hot.keys()))
        # Grab ctx under lock, then release to avoid blocking during CDP calls
        with self._lock:
            if cid not in self.hot:
                raise RuntimeError(f"container {cid} is not hot")
            ctx = self.hot[cid]
        # Collect state WITHOUT holding the lock (CDP calls can be slow)
        log.debug("hibernate: collecting state for ctx=%s", ctx)
        cookies, storage, idb, tabs_to_save = self._collect_state(ctx)
        # Re-acquire lock for the mutation phase
        with self._lock:
            if cid not in self.hot or self.hot[cid] != ctx:
                log.debug("hibernate: %s changed while collecting, skipping", cid)
                return {"id": cid, "error": "stale"}
            log.debug("hibernate: disposing context %s", ctx)
            # Use a fresh (non-shared) session for the close+dispose sequence.
            # disposeBrowserContext triggers unsolicited Target.targetDestroyed
            # events that pile up in the shared _cached_bs receive buffer and
            # corrupt the next send() on that session, making the watcher think
            # Chrome has died.  A dedicated session is closed immediately after,
            # so those events are discarded cleanly.
            try:
                with self._new_browser_session() as bs:
                    try:
                        all_targets = bs.target.get_targets(timeout=SNAPSHOT_CDP_TIMEOUT)
                        ctx_tids = [t["targetId"] for t in all_targets
                                    if t.get("browserContextId") == ctx
                                    and t.get("type") == "page"]
                        for tid in ctx_tids:
                            try:
                                bs.target.close_target(tid)
                            except Exception:
                                pass
                        if ctx_tids:
                            log.debug("hibernate: closed %d tabs before dispose", len(ctx_tids))
                    except Exception as e:
                        log.debug("hibernate: pre-dispose tab close error (ignored): %s", e)
                    try:
                        bs.target.dispose_browser_context(ctx)
                        log.debug("hibernate: context %s disposed", ctx)
                    except CDPError as e:
                        log.debug("hibernate: dispose CDPError (ignored): %s", e)
            except Exception as e:
                log.debug("hibernate: fresh session error (ignored): %s", e)

            # Preserve previously saved storage/IDB for any origin that
            # failed to collect this time (tab timeout, JS error, etc.).
            prev = self.store.get_container(cid)
            if prev:
                prev_storage = prev.get("storage", {})
                if prev_storage:
                    merged_storage = dict(prev_storage)
                    merged_storage.update(storage)  # new wins where present
                    if len(merged_storage) > len(storage):
                        preserved = len(merged_storage) - len(storage)
                        log.debug("hibernate: localStorage missing %d origins for %s, preserving",
                                  preserved, cid)
                    storage = merged_storage
                prev_idb = prev.get("idb", {})
                if prev_idb:
                    merged_idb = dict(prev_idb)
                    merged_idb.update(idb)  # new wins where present
                    if len(merged_idb) > len(idb):
                        preserved = len(merged_idb) - len(idb)
                        log.debug("hibernate: IDB empty for %s, preserving %d previous idb-origins",
                                  cid, preserved)
                    idb = merged_idb
            self.store.save_hibernation(cid, cookies, storage, tabs_to_save, idb=idb)
            self.hot.pop(cid, None)
            self._last_snapshot_hash.pop(cid, None)
            self._last_snapshot_time.pop(cid, None)
            log.debug("hibernate: done cid=%s tabs=%d", cid, len(tabs_to_save))
            return {"id": cid, "tabs_saved": len(tabs_to_save),
                    "cookies_saved": len(cookies),
                    "origins_with_storage": len(storage)}

    # -- restore --------------------------------------------------------------

    def restore(self, cid: str, also_open_url: str | None = None) -> dict:
        log.debug("restore cid=%s also_open_url=%s", cid, also_open_url)
        with self._lock:
            row = self.store.get_container(cid)
            if not row:
                raise KeyError(cid)
            if cid in self.hot:
                # Already hot - optionally open URL
                if also_open_url:
                    with self._browser_session() as bs:
                        bs.target.create_target(url=also_open_url,
                                                browser_context_id=self.hot[cid])
                return {"id": cid, "status": "already-hot",
                        "browserContextId": self.hot[cid]}

            log.debug("restore: creating browser context for %s", cid)
            with self._browser_session() as bs:
                ctx = bs.target.create_browser_context()
                log.debug("restore: got context %s for %s", ctx, cid)
                if row["cookies"]:
                    clean = [c for c in (_clean_cookie(c) for c in row["cookies"]) if c]
                    if clean:
                        log.debug("restore: setting %d cookies in ctx %s", len(clean), ctx)
                        try:
                            bs.storage.set_cookies(clean, browser_context_id=ctx)
                        except CDPError as e:
                            log.debug("restore: set_cookies CDPError: %s", e)
                            # Retry in smaller batches — some cookies may fail
                            ok = 0
                            for cookie in clean:
                                try:
                                    bs.storage.set_cookies([cookie],
                                                           browser_context_id=ctx)
                                    ok += 1
                                except CDPError:
                                    pass
                            log.debug("restore: batch retry set %d/%d cookies",
                                      ok, len(clean))

            self.hot[cid] = ctx
            self.store.mark_active(cid, True)
            self.store.touch_accessed(cid)

            urls = [t["url"] for t in row["tabs"]]
            if also_open_url and also_open_url not in urls:
                urls = urls + [also_open_url]
            if not urls:
                urls = ["about:blank"]

            log.debug("restore: opening %d urls in ctx %s: %s", len(urls), ctx, urls)
            opened: list[str] = []
            activate_tid: str | None = None
            for url in urls:
                tid = self._open_tab_with_storage(ctx, url, row["storage"],
                                                   row.get("idb", {}))
                log.debug("restore: opened tab %s for url %s", tid, url)
                opened.append(tid)
                if also_open_url and url == also_open_url and activate_tid is None:
                    activate_tid = tid
            # Immediately persist the tab URLs we just opened so that
            # _soft_hibernate (window-close watcher) has accurate state
            # even before the first periodic snapshot fires.
            tabs_for_db = [{"url": u, "title": ""} for u in urls
                           if u and u != "about:blank"]
            if tabs_for_db:
                self.store.save_hibernation(
                    cid, row["cookies"], row["storage"],
                    tabs_for_db, keep_active=True, idb=row.get("idb", {}))
            return {"id": cid, "browserContextId": ctx,
                    "tabs_opened": len(opened),
                    "activate_target_id": activate_tid or (opened[0] if opened else None)}

    def _open_tab_with_storage(self, ctx: str, url: str,
                               storage_by_origin: dict,
                               idb_by_origin: dict | None = None) -> str:
        origin = _origin_of(url) if url else None
        ls_data = storage_by_origin.get(origin) if origin else None
        idb_data = (idb_by_origin or {}).get(origin) if origin else None
        needs_inject = bool(ls_data or idb_data)
        open_url = "about:blank" if needs_inject else (url or "about:blank")
        with self._browser_session() as bs:
            tid = bs.target.create_target(
                url=open_url, browser_context_id=ctx)
        if not tid:
            return ""
        self._maximize_tab(tid)
        if not needs_inject:
            return tid
        ws_url = None
        for _ in range(10):
            info = cdp.requests.get(
                f"http://127.0.0.1:{self.browser_port}/json/list",
                timeout=(0.5, 3)).json()
            t = next((x for x in info if x["id"] == tid), None)
            if t and t.get("webSocketDebuggerUrl"):
                ws_url = t["webSocketDebuggerUrl"]
                break
            time.sleep(0.05)
        if not ws_url:
            return tid
        try:
            sess = CDPSession(ws_url)
        except Exception:
            return tid
        try:
            parts = []
            if ls_data:
                parts.append(
                    "(function(){try{var d="
                    + json.dumps(ls_data)
                    + ";for(var k in d){localStorage.setItem(k,d[k]);}}catch(e){}})()"
                )
            if idb_data:
                db_sizes = {k: len(json.dumps(v)) for k, v in idb_data.items()}
                log.debug("restore: idb %d databases, per_db_bytes=%s total=%d",
                          len(idb_data), db_sizes, sum(db_sizes.values()))
                # Check for null rows (indicates non-serializable values)
                for db_name, db_data in idb_data.items():
                    for store_name, store in db_data.items():
                        if store_name == "_meta":
                            continue
                        if not isinstance(store, dict):
                            continue
                        rows = store.get("rows", [])
                        null_count = sum(1 for r in rows if r is None)
                        if null_count:
                            log.debug(
                                "restore: WARNING %s/%s has %d/%d null rows "
                                "(non-serializable data lost)",
                                db_name, store_name, null_count, len(rows))
            # --- inject scripts via separate addScript calls ---
            # localStorage first (small)
            if parts:
                ls_script = ";".join(parts) + ";"
                log.debug("restore: inject ls script len=%d for %s",
                          len(ls_script), origin)
                try:
                    sess.page.add_script_to_evaluate_on_new_document(ls_script)
                except CDPError as exc:
                    log.debug("restore: addScript(ls) FAILED for %s: %s",
                              origin, exc)
            # IDB: split into scaffolding + per-database scripts
            if idb_data:
                total_bytes = sum(db_sizes.values())
                timeout_ms = max(30000, total_bytes // 200)
                scaffolding = _build_idb_scaffolding(
                    len(idb_data), timeout_ms=timeout_ms)
                log.debug("restore: inject idb scaffolding len=%d "
                          "timeout=%dms for %s",
                          len(scaffolding), timeout_ms, origin)
                try:
                    sess.page.add_script_to_evaluate_on_new_document(
                        scaffolding)
                except CDPError as exc:
                    log.debug("restore: addScript(scaffolding) FAILED "
                              "for %s: %s", origin, exc)
                for db_name, db_data_item in idb_data.items():
                    db_scripts = _build_idb_db_scripts(
                        db_name, db_data_item)
                    log.debug(
                        "restore: inject db %s (%d scripts, "
                        "total=%d bytes, max=%d bytes)",
                        db_name, len(db_scripts),
                        sum(len(s) for s in db_scripts),
                        max(len(s) for s in db_scripts))
                    for idx, db_script in enumerate(db_scripts):
                        try:
                            sess.page.add_script_to_evaluate_on_new_document(
                                db_script)
                        except CDPError as exc:
                            log.debug(
                                "restore: addScript(%s chunk %d/%d "
                                "len=%d) FAILED: %s",
                                db_name, idx + 1, len(db_scripts),
                                len(db_script), exc)
            # Grant durableStorage so sites like WhatsApp don't get
            # "storage bucket persistence denied" when calling
            # navigator.storage.persist() in a new browser context.
            if origin and idb_data:
                try:
                    with self._browser_session() as bs:
                        bs.send("Browser.grantPermissions", {
                            "origin": origin,
                            "browserContextId": ctx,
                            "permissions": ["durableStorage"],
                        }, timeout=3)
                except Exception:
                    pass
            if url and url != "about:blank":
                try:
                    sess.page.navigate(url, wait_for_load=False)
                except Exception:
                    pass
            # Belt-and-suspenders: also set localStorage via CDP
            # DOMStorage domain.  This works even when the page JS
            # deletes window.localStorage (e.g. Discord anti-bot).
            if ls_data and origin:
                try:
                    sid = {"securityOrigin": origin,
                           "isLocalStorage": True}
                    for k, v in ls_data.items():
                        sess.send("DOMStorage.setDOMStorageItem", {
                            "storageId": sid, "key": str(k),
                            "value": str(v),
                        }, timeout=2)
                except Exception:
                    pass
        finally:
            sess.close()
        return tid

    # -- clone / clean / delete / bulk ---------------------------------------

    def clone(self, cid: str, new_name: str | None = None) -> dict | None:
        src = self.store.get_container(cid)
        if not src:
            return None
        name = new_name or f"{src['name']} (clone)"
        # If src is hot, snapshot its current state first.
        if cid in self.hot:
            self.hibernate(cid)
            new = self.store.clone_container(cid, name)
            self.restore(cid)
            return new
        return self.store.clone_container(cid, name)

    def clean(self, cid: str) -> dict:
        """Wipe cookies + storage while preserving the session.

        Hot sessions: clear cookies via CDP on the live context (session keeps
        running), then wipe the stored blobs in the DB.
        Cold sessions: just wipe the stored blobs in the DB (tabs preserved).
        """
        log.debug("clean cid=%s", cid)
        with self._lock:
            ctx = self.hot.get(cid)
        if ctx:
            log.debug("clean: clearing cookies for live ctx=%s", ctx)
            try:
                with self._new_browser_session() as bs:
                    bs.storage.clear_cookies(browser_context_id=ctx)
                    log.debug("clean: cookies cleared for ctx=%s", ctx)
            except Exception as e:
                log.debug("clean: CDP clear_cookies error (ignored): %s", e)
        self.store.clean_container(cid)
        log.debug("clean: done cid=%s (hot=%s)", cid, ctx is not None)
        return {"id": cid, "cleaned": True}

    def delete(self, cid: str) -> None:
        log.debug("delete cid=%s", cid)
        with self._lock:
            if cid in self.hot:
                ctx = self.hot.pop(cid)
                log.debug("delete: disposing context %s for %s", ctx, cid)
                with self._new_browser_session() as bs:
                    try:
                        all_targets = bs.target.get_targets(timeout=SNAPSHOT_CDP_TIMEOUT)
                        for t in all_targets:
                            if t.get("browserContextId") == ctx and t.get("type") == "page":
                                try:
                                    bs.target.close_target(t["targetId"])
                                except Exception:
                                    pass
                    except Exception:
                        pass
                    try:
                        bs.target.dispose_browser_context(ctx)
                    except CDPError as e:
                        log.debug("delete: dispose CDPError (ignored): %s", e)
            self.store.delete_container(cid)
            self._last_snapshot_time.pop(cid, None)
            self._last_snapshot_hash.pop(cid, None)
            log.debug("delete: done cid=%s", cid)

    def hibernate_all(self) -> list[dict]:
        results = []
        for cid in list(self.hot.keys()):
            try:
                results.append(self.hibernate(cid))
            except Exception as e:
                results.append({"id": cid, "error": str(e)})
        return results

    def quick_shutdown(self) -> list[dict]:
        log.debug("quick_shutdown starting, hot=%s", list(self.hot.keys()))
        self.stop_watcher(stop_evt_thread=True)
        # Wait for any in-flight snapshot to finish (set by snapshot_loop callers)
        fence = getattr(self, "_snapshot_fence", None)
        if fence is not None:
            fence.wait(timeout=15)
        cids = list(self.hot.keys())
        log.debug("quick_shutdown: saving %d containers (skip fresh)", len(cids))
        self._invalidate_targets_cache()
        # Pre-populate targets cache once for all parallel snapshots
        try:
            self._get_targets_cached(max_age=0)
        except Exception:
            pass
        # Snapshot all containers in parallel
        results: list[dict] = []
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(len(cids), 4) if cids else 1) as pool:
            def _snap(cid):
                try:
                    return self._snapshot_if_stale(cid)
                except Exception as e:
                    log.warning("quick_shutdown: snapshot %s failed: %s", cid, e)
                    return {"id": cid, "error": str(e)}
            futs = {pool.submit(_snap, cid): cid for cid in cids}
            try:
                for fut in concurrent.futures.as_completed(futs, timeout=IDB_DUMP_TIMEOUT + 15):
                    try:
                        results.append(fut.result(timeout=1))
                    except Exception as e:
                        log.warning("quick_shutdown: snapshot result error: %s", e)
            except TimeoutError:
                pending = [cid for f, cid in futs.items() if not f.done()]
                log.warning("quick_shutdown: %d snapshots timed out (%s), proceeding with close",
                            len(pending), pending)
                for f in futs:
                    f.cancel()
        log.debug("quick_shutdown: snapshot results=%s", results)
        # Keep is_active=1 for hot containers so they auto-restore on next start
        self.hot.clear()
        log.debug("quick_shutdown: closing Chrome")
        self.close_chrome()
        log.debug("quick_shutdown: done")
        return results

    def status(self) -> dict:
        listing = self.store.list_containers()
        live_by_ctx: dict[str, list[dict]] = {}
        if self.hot:
            try:
                with self._browser_session() as bs:
                    all_targets = bs.target.get_targets(
                        timeout=SNAPSHOT_CDP_TIMEOUT)
            except Exception:
                all_targets = []
            # Build lookup of hot container ids -> ctx in one pass
            hot_ctxs = set(self.hot.values())
            for t in all_targets:
                if t.get("type") != "page":
                    continue
                ctx = t.get("browserContextId")
                if not ctx or ctx not in hot_ctxs:
                    continue
                tab_url = t.get("url", "")
                if not tab_url or tab_url.startswith("chrome://"):
                    continue
                live_by_ctx.setdefault(ctx, []).append({
                    "targetId": t.get("targetId"),
                    "url": tab_url,
                    "title": t.get("title", ""),
                })
        # Fetch all saved_tabs in a single DB round-trip for cold containers
        cold_ids = [r["id"] for r in listing if r["id"] not in self.hot]
        cold_tabs: dict[str, list] = {}
        for cid in cold_ids:
            full = self.store.get_container(cid)
            cold_tabs[cid] = full["tabs"] if full else []
        tab_ts = self._tab_last_activated
        for row in listing:
            row["hot"] = row["id"] in self.hot
            ctx = self.hot.get(row["id"])
            row["browserContextId"] = ctx
            tabs = live_by_ctx.get(ctx, []) if ctx else []
            # Sort live tabs by last-activated timestamp (most recent first)
            if tabs:
                tabs.sort(key=lambda t: tab_ts.get(t.get("targetId", ""), 0),
                          reverse=True)
            row["live_tabs"] = tabs
            row["saved_tabs"] = [] if row["hot"] else cold_tabs.get(row["id"], [])
        # Sort: hot sessions first, then cold; within each group by recency desc
        listing.sort(key=lambda r: (
            0 if r["hot"] else 1,
            -(r.get("last_accessed_at") or r.get("created_at") or 0),
        ))
        return {"containers": listing,
                "hot_count": len(self.hot),
                "cold_count": len(cold_ids),
                "debug_mode": getattr(self, "_debug_mode", False)}

    def trim_log(self) -> dict:
        """Trim the debug log, keeping only from the most recent PROCESS START separator."""
        path = getattr(self, "_log_path", None)
        if not path:
            return {"trimmed": False, "reason": "debug mode not active"}
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                content = f.read()
            marker = "  PROCESS START  "
            idx = content.rfind(marker)
            if idx == -1:
                return {"trimmed": False, "reason": "no startup marker found"}
            # Walk back to the start of the separator line (the dashes line before it)
            sep_start = content.rfind("\n", 0, idx)
            sep_start = content.rfind("\n", 0, sep_start) if sep_start > 0 else 0
            kept = content[sep_start:].lstrip("\n")
            with open(path, "w", encoding="utf-8") as f:
                f.write(kept)
            log.debug("trim_log: trimmed %d bytes, kept %d bytes",
                      len(content) - len(kept), len(kept))
            return {"trimmed": True, "kept_bytes": len(kept)}
        except Exception as e:
            log.warning("trim_log failed: %s", e)
            return {"trimmed": False, "reason": str(e)}

    def activate_tab(self, target_id: str) -> dict:
        log.debug("activate_tab targetId=%s", target_id)
        with self._browser_session() as bs:
            bs.target.activate_target(target_id)
        now = time.time()
        self._tab_last_activated[target_id] = now
        # Find which session owns this target and touch its recency
        try:
            with self._browser_session() as bs:
                targets = bs.target.get_targets(timeout=SNAPSHOT_CDP_TIMEOUT)
            for t in targets:
                if t.get("targetId") == target_id:
                    ctx = t.get("browserContextId")
                    if ctx:
                        with self._lock:
                            for cid, c in self.hot.items():
                                if c == ctx:
                                    self.store.touch_accessed(cid)
                                    break
                    break
        except Exception:
            pass
        return {"targetId": target_id, "activated": True}

    def rename(self, cid: str, new_name: str) -> dict:
        self.store.rename_container(cid, new_name)
        return {"id": cid, "name": new_name}

    def close_tab(self, target_id: str) -> dict:
        log.debug("close_tab targetId=%s", target_id)
        with self._browser_session() as bs:
            bs.target.close_target(target_id)
        return {"targetId": target_id, "closed": True}

    def close_chrome(self) -> None:
        log.debug("close_chrome called")
        try:
            with self._browser_session() as bs:
                bs.browser.close()
            log.debug("close_chrome: browser.close() sent")
        except Exception as e:
            log.debug("close_chrome: error (ignored): %s", e)
        self._invalidate_browser_session()

    def _maximize_tab(self, target_id: str) -> None:
        try:
            with self._browser_session() as bs:
                win = bs.browser.get_window_for_target(target_id)
                wid = win.get("windowId")
                if wid is not None:
                    bs.browser.set_window_bounds(wid, {"windowState": "maximized"})
        except Exception:
            pass

    def open_dashboard_in_default_tab(self, dash_url: str) -> str | None:
        """Open dashboard in default context: create new tab, close old one.
        Returns the targetId or None."""
        try:
            with self._browser_session() as bs:
                custom_ctxs = set(bs.target.get_browser_contexts())
                targets = bs.target.get_targets(timeout=SNAPSHOT_CDP_TIMEOUT)
                old_default = [t for t in targets
                               if t.get("type") == "page"
                               and t.get("browserContextId", "") not in custom_ctxs]
                new_tid = bs.target.create_target(url=dash_url)
                for t in old_default:
                    try:
                        bs.target.close_target(t["targetId"])
                    except Exception:
                        pass
                bs.target.activate_target(new_tid)
            self._maximize_tab(new_tid)
            self._dashboard_target_id = new_tid
            return new_tid
        except Exception as e:
            log.warning("could not open dashboard in default tab: %s", e)
            return None

    def activate_dashboard(self) -> bool:
        tid = getattr(self, "_dashboard_target_id", None)
        if not tid:
            return False
        try:
            with self._browser_session() as bs:
                bs.target.activate_target(tid)
            # Focus the search box in the dashboard
            try:
                with CDPSession(self.browser_port, tid, flatten=True) as sess:
                    sess.runtime.evaluate("document.getElementById('search-box').focus()", timeout=2)
            except Exception:
                pass
            self._foreground_chrome(maximized=True)
            return True
        except Exception:
            return False

    def _foreground_chrome(self, maximized: bool = False) -> None:
        """Bring the Chrome window to the foreground on Windows."""
        if sys.platform != "win32":
            return
        try:
            import ctypes
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            # Attach to the foreground window's thread so SetForegroundWindow works
            # from any context (not just when Chrome is already focused).
            cur_thread = kernel32.GetCurrentThreadId()
            fg_hwnd = user32.GetForegroundWindow()
            fg_thread = user32.GetWindowThreadProcessId(fg_hwnd, None)
            EnumWindowsProc = ctypes.WINFUNCTYPE(
                ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
            found = []
            def _find_chrome(hwnd, _):
                if user32.IsWindowVisible(hwnd):
                    text_len = user32.GetWindowTextLengthW(hwnd)
                    if text_len > 0:
                        buf = ctypes.create_unicode_buffer(text_len + 1)
                        user32.GetWindowTextW(hwnd, buf, text_len + 1)
                        if "Chrome" in buf.value or "Chromium" in buf.value:
                            found.append(hwnd)
                            return False
                return True
            user32.EnumWindows(EnumWindowsProc(_find_chrome), 0)
            if not found:
                return
            hwnd = found[0]
            if fg_thread != cur_thread:
                user32.AttachThreadInput(fg_thread, cur_thread, True)
            user32.ShowWindow(hwnd, 3 if maximized else 9)  # SW_MAXIMIZE or SW_RESTORE
            user32.SetForegroundWindow(hwnd)
            user32.BringWindowToTop(hwnd)
            if fg_thread != cur_thread:
                user32.AttachThreadInput(fg_thread, cur_thread, False)
        except Exception:
            pass

    def reconnect_to_existing(self) -> list[dict]:
        """If Chrome is already running with browser contexts from a previous
        daemon session, rebuild our hot map instead of re-creating contexts.
        Returns list of reconnected container dicts."""
        try:
            with self._browser_session() as bs:
                all_targets = bs.target.get_targets(
                    timeout=SNAPSHOT_CDP_TIMEOUT)
        except Exception as e:
            log.debug("reconnect_to_existing: no CDP connection: %s", e)
            return []
        # Map browserContextId -> list of page URLs
        ctx_pages: dict[str, list[str]] = {}
        for t in all_targets:
            if t.get("type") == "page":
                ctx = t.get("browserContextId", "")
                url = t.get("url", "")
                if ctx and url and not url.startswith("chrome://") \
                        and url not in ("", "about:blank"):
                    ctx_pages.setdefault(ctx, []).append(url)
        if not ctx_pages:
            return []
        # For each persisted active container, try to match it to a live ctx
        reconnected = []
        for c in self.store.list_containers():
            if not c.get("is_active"):
                continue
            cid = c["id"]
            if cid in self.hot:
                continue
            full = self.store.get_container(cid)
            saved_urls = set()
            if full and full.get("tabs"):
                saved_urls = {t["url"] for t in full["tabs"]}
            # Find the context that best matches this container's saved URLs
            best_ctx, best_score = None, 0
            for ctx, pages in ctx_pages.items():
                overlap = len(saved_urls & set(pages))
                if overlap > best_score:
                    best_score = overlap
                    best_ctx = ctx
            if best_ctx and best_score > 0:
                self.hot[cid] = best_ctx
                # Remove from candidates so no other container claims it
                del ctx_pages[best_ctx]
                reconnected.append({"id": cid, "browserContextId": best_ctx,
                                    "reconnected": True})
                log.debug("reconnected container %s to context %s (%d url matches)",
                          cid, best_ctx, best_score)
        return reconnected

    def auto_restore_hot(self) -> list[dict]:
        """Restore containers that were hot when the daemon last shut down."""
        results = []
        for c in self.store.list_containers():
            if c.get("is_active"):
                log.debug("auto-restoring container %s (%s)", c["id"], c["name"])
                try:
                    results.append(self.restore(c["id"]))
                except Exception as e:
                    log.warning("auto-restore failed for %s: %s", c["id"], e)
        return results

    def create_for_url(self, url: str, color: str = "#3b82f6") -> dict:
        normalized = _normalize_url(url)
        log.debug("create_for_url url=%s normalized=%s", url, normalized)
        name = _domain_of(normalized) or url or "container"
        row = self.store.create_container(name, color)
        self.open_tab(row["id"], normalized)
        return self.store.get_container(row["id"])

    def clean_default_context(self) -> None:
        """Clear cookies + storage from the default (dashboard) browser context."""
        try:
            with self._browser_session() as bs:
                bs.storage.clear_cookies()
                bs.storage.clear_data_for_origin(
                    f"http://localhost:{DEFAULT_BROWSER_PORT}",
                    "all")
        except Exception:
            pass
