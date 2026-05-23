"""Profile-backed session lifecycle methods (mixin for ContainerManager)."""
from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import TYPE_CHECKING

from . import cdp
from .cdp import profile_dir_name

if TYPE_CHECKING:
    from .manager import ContainerManager

log = logging.getLogger("sessions")


class ProfileMixin:
    """Methods for profile-backed sessions, mixed into ContainerManager.

    Profile sessions use a dedicated Chrome profile directory instead of a
    CDP browser context.  Chrome manages its own data (cookies, storage,
    extensions, passkeys) on disk — the daemon only tracks tab URLs in a
    lightweight shadow list and relies on Chrome's native session restore.
    """

    # Provided by ContainerManager (declared here for type-checking only)
    if TYPE_CHECKING:
        hot: dict[str, str]
        _profile_sessions: set[str]
        _chrome_mgr: cdp.ChromeManager | None
        _lock: object
        _snapshot_cdp_failures: int
        store: object
        _last_snapshot_time: dict[str, float]
        _last_snapshot_hash: dict[str, str]

    # -- helpers ---------------------------------------------------------------

    def is_profile(self: ContainerManager, cid: str) -> bool:
        """Return True if *cid* is a profile-backed session."""
        return cid in self._profile_sessions

    def _user_data_dir(self: ContainerManager) -> str:
        """Return the Chrome user-data-dir (from ChromeManager or default)."""
        if self._chrome_mgr:
            return self._chrome_mgr.user_data_dir
        return cdp.USER_DATA_DIR

    # -- snapshot --------------------------------------------------------------

    def _snapshot_profile(self: ContainerManager, cid: str, ctx: str) -> dict:
        """Lightweight snapshot for profile sessions: just save tab URLs/titles."""
        from .manager import SNAPSHOT_CDP_TIMEOUT

        log.debug("_snapshot_profile: cid=%s ctx=%s", cid, ctx)
        try:
            with self._new_browser_session() as bs:
                all_targets = bs.target.get_targets(timeout=SNAPSHOT_CDP_TIMEOUT)
        except Exception as e:
            log.debug("_snapshot_profile: getTargets failed for %s: %s", cid, e)
            self._maybe_trigger_snapshot_crash_recovery(cid, e)
            return {"id": cid, "error": str(e)}
        self._snapshot_cdp_failures = 0
        tabs = []
        for t in all_targets:
            if t.get("browserContextId") != ctx or t.get("type") != "page":
                continue
            url = t.get("url", "")
            if url and not url.startswith("chrome://") \
                    and url not in ("", "about:blank"):
                tabs.append({"url": url, "title": t.get("title", "")})
        state_hash = hashlib.md5(
            json.dumps(tabs, sort_keys=True).encode(),
            usedforsecurity=False).hexdigest()
        with self._lock:
            if cid not in self.hot or self.hot[cid] != ctx:
                return {"id": cid, "skipped": "stale"}
            if self._last_snapshot_hash.get(cid) == state_hash:
                self._last_snapshot_time[cid] = time.time()
                return {"id": cid, "skipped": "unchanged"}
            # Never overwrite non-empty saved tabs with an empty list
            # (tabs may appear empty when the window is mid-close or loading)
            if not tabs:
                prev = self.store.get_container(cid)
                prev_tabs = prev.get("tabs", []) if prev else []
                if prev_tabs:
                    log.debug("_snapshot_profile: 0 live tabs for %s but "
                              "%d saved, preserving", cid, len(prev_tabs))
                    self._last_snapshot_time[cid] = time.time()
                    return {"id": cid, "skipped": "preserve-tabs"}
            cdp.save_profile_tabs(self._user_data_dir(), cid, tabs)
            # Also update DB tabs for dashboard display
            db_tabs = [{"url": t["url"], "title": t.get("title", "")}
                       for t in tabs]
            self.store.save_hibernation(cid, [], {}, db_tabs, keep_active=True)
            self._last_snapshot_time[cid] = time.time()
            self._last_snapshot_hash[cid] = state_hash
            log.debug("_snapshot_profile: saved %d tabs for %s", len(tabs), cid)
            return {"id": cid, "tabs_saved": len(tabs),
                    "session_type": "profile"}

    # -- hibernate -------------------------------------------------------------

    def _hibernate_profile(self: ContainerManager, cid: str, ctx: str) -> dict:
        """Hibernate a profile-backed session: save shadow tabs, close targets.

        Unlike context sessions, profile sessions don't call
        disposeBrowserContext — the profile data stays on disk and Chrome's
        session restore will bring tabs back when the profile is re-launched.
        """
        from .manager import SNAPSHOT_CDP_TIMEOUT

        log.debug("_hibernate_profile: cid=%s ctx=%s", cid, ctx)
        tabs_to_save: list[dict] = []
        try:
            with self._new_browser_session() as bs:
                all_targets = bs.target.get_targets(timeout=SNAPSHOT_CDP_TIMEOUT)
                ctx_tids = []
                for t in all_targets:
                    if t.get("browserContextId") != ctx:
                        continue
                    if t.get("type") != "page":
                        continue
                    tid = t["targetId"]
                    url = t.get("url", "")
                    if url and not url.startswith("chrome://") \
                            and url not in ("", "about:blank"):
                        tabs_to_save.append({
                            "url": url,
                            "title": t.get("title", ""),
                        })
                    ctx_tids.append(tid)
                # Save shadow tabs before closing
                udd = self._user_data_dir()
                cdp.save_profile_tabs(udd, cid, tabs_to_save)
                # Also persist tab URLs in the DB for dashboard display
                db_tabs = [{"url": t["url"], "title": t.get("title", "")}
                           for t in tabs_to_save]
                self.store.save_hibernation(cid, [], {}, db_tabs)
                # Close all targets
                for tid in ctx_tids:
                    try:
                        bs.target.close_target(tid)
                    except Exception:
                        pass
                log.debug("_hibernate_profile: closed %d tabs", len(ctx_tids))
        except Exception as e:
            log.warning("_hibernate_profile: error for %s: %s", cid, e)

        with self._lock:
            self.hot.pop(cid, None)
            self._last_snapshot_hash.pop(cid, None)
            self._last_snapshot_time.pop(cid, None)
            self.store.mark_active(cid, False)

        log.debug("_hibernate_profile: done cid=%s tabs=%d",
                  cid, len(tabs_to_save))
        return {"id": cid, "tabs_saved": len(tabs_to_save),
                "session_type": "profile"}

    # -- restore ---------------------------------------------------------------

    def _discover_profile_context(self: ContainerManager,
                                  known_ctxs: set[str],
                                  timeout: float = 30) -> str | None:
        """Poll getTargets() to find a new browserContextId that wasn't in
        *known_ctxs*.  Returns the new context ID, or None on timeout."""
        from .manager import SNAPSHOT_CDP_TIMEOUT

        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with self._browser_session() as bs:
                    targets = bs.target.get_targets(timeout=SNAPSHOT_CDP_TIMEOUT)
                for t in targets:
                    ctx = t.get("browserContextId")
                    if ctx and ctx not in known_ctxs and t.get("type") == "page":
                        return ctx
            except Exception:
                pass
            time.sleep(0.3)
        return None

    def _restore_profile(self: ContainerManager, cid: str, row: dict,
                         also_open_url: str | None = None) -> dict:
        """Restore a profile-backed session by launching its Chrome profile."""
        from .manager import SNAPSHOT_CDP_TIMEOUT

        log.debug("_restore_profile: cid=%s", cid)
        chrome_mgr = self._chrome_mgr
        if not chrome_mgr:
            raise RuntimeError("No ChromeManager configured for profile sessions")

        prof_name = row.get("profile_dir") or profile_dir_name(cid)
        # Ensure profile directory exists with session restore prefs
        cdp.create_profile_dir(chrome_mgr.user_data_dir, cid)

        # Record known contexts before launch
        known_ctxs: set[str] = set()
        try:
            with self._browser_session() as bs:
                for t in bs.target.get_targets(timeout=SNAPSHOT_CDP_TIMEOUT):
                    ctx = t.get("browserContextId")
                    if ctx:
                        known_ctxs.add(ctx)
        except Exception:
            pass

        # Determine the start URL for Chrome:
        # - If the caller specified a URL (tab click), use it.
        # - Otherwise, use the first shadow tab URL so Chrome restores
        #   it natively (avoids an extra about:blank tab).
        shadow_tabs = cdp.load_profile_tabs(chrome_mgr.user_data_dir, cid)
        if also_open_url:
            start_url = also_open_url
        elif shadow_tabs:
            start_url = shadow_tabs[0]["url"]
        else:
            start_url = "about:blank"
        chrome_mgr.launch_profile(prof_name, start_url=start_url)

        # Discover the new browserContextId
        new_ctx = self._discover_profile_context(known_ctxs)
        if not new_ctx:
            # Fallback: try to find a target in the profile by URL match
            log.warning("_restore_profile: could not discover context for %s, "
                        "falling back to tab list match", cid)
            shadow_tabs = cdp.load_profile_tabs(chrome_mgr.user_data_dir, cid)
            saved_urls = {t["url"] for t in (row.get("tabs") or shadow_tabs)}
            try:
                with self._browser_session() as bs:
                    for t in bs.target.get_targets(timeout=SNAPSHOT_CDP_TIMEOUT):
                        ctx = t.get("browserContextId")
                        if ctx and ctx not in known_ctxs:
                            url = t.get("url", "")
                            if url in saved_urls or url == start_url:
                                new_ctx = ctx
                                break
            except Exception:
                pass
        if not new_ctx:
            raise RuntimeError(
                f"Could not discover browserContextId for profile {cid}")

        self.hot[cid] = new_ctx
        self._profile_sessions.add(cid)
        self.store.mark_active(cid, True)
        self.store.touch_accessed(cid)

        # Wait for Chrome to register the tab in the context.
        time.sleep(1.5)
        try:
            profile_tabs = self._targets_for(new_ctx)
            live_urls = {t.get("url", "") for t in profile_tabs}
        except Exception:
            live_urls = set()

        # Only open shadow tabs if Chrome didn't launch any real tabs AND
        # we didn't already pass a real URL as start_url (which Chrome is
        # loading — it just may not have appeared in targets yet).
        need_shadow = (not live_urls - {"about:blank", ""}
                       and start_url in ("about:blank", ""))
        if need_shadow and shadow_tabs:
            log.debug("_restore_profile: Chrome did not restore tabs, "
                      "opening %d from shadow list", len(shadow_tabs))
            with self._browser_session() as bs:
                for tab in shadow_tabs:
                    try:
                        bs.target.create_target(
                            url=tab["url"], browser_context_id=new_ctx)
                    except Exception:
                        pass

        tabs_count = max(len(live_urls - {"about:blank", ""}),
                         len(shadow_tabs))
        log.debug("_restore_profile: done cid=%s ctx=%s tabs=%d",
                  cid, new_ctx, tabs_count)
        return {"id": cid, "browserContextId": new_ctx,
                "tabs_opened": tabs_count,
                "session_type": "profile"}
