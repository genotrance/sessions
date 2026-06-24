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
                # Mark profile as cleanly exited so next restore does not
                # trigger Chrome's "didn't shut down correctly" bar.
                cdp.update_profile_prefs_for_restore(
                    udd, cid,
                    [t["url"] for t in tabs_to_save])
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
                                  known_tids: set[str] | None = None,
                                  timeout: float = 30) -> str | None:
        """Poll getTargets() to find the browserContextId for a just-launched
        profile.

        First looks for a brand-new context ID (normal case when the profile
        was not previously loaded).  If none appears, falls back to detecting
        a *new target* (page) in an already-known context — this handles the
        case where Chrome already had the profile loaded (e.g. after
        soft-hibernate) and reuses the same context ID."""
        from .manager import SNAPSHOT_CDP_TIMEOUT

        if known_tids is None:
            known_tids = set()
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with self._browser_session() as bs:
                    targets = bs.target.get_targets(timeout=SNAPSHOT_CDP_TIMEOUT)
                # Prefer a brand-new context (profile was freshly loaded)
                for t in targets:
                    ctx = t.get("browserContextId")
                    if ctx and ctx not in known_ctxs and t.get("type") == "page":
                        return ctx
                # Fallback: a new target appeared in an existing context
                # (profile was already loaded, Chrome reused its context)
                if known_tids:
                    for t in targets:
                        ctx = t.get("browserContextId")
                        tid = t.get("targetId", "")
                        if ctx and tid and tid not in known_tids \
                                and t.get("type") == "page":
                            return ctx
            except Exception:
                pass
            time.sleep(0.3)
        return None

    def _restore_profile(self: ContainerManager, cid: str, row: dict,
                         also_open_url: str | None = None) -> dict:
        """Restore a profile-backed session by launching its Chrome profile.

        When *also_open_url* is ``None`` (crash recovery / hibernation
        resume), the profile's ``Preferences`` are updated so Chrome opens
        **all** previously-open tabs via ``restore_on_startup: 4`` +
        ``startup_urls``.  This avoids the "Chrome didn't shut down
        correctly" prompt and the duplicate-tab problem that occurs when a
        single URL is passed on the command line alongside Chrome's own
        session restore.

        When *also_open_url* is given (user clicked a specific tab), only
        that URL is passed to Chrome so it opens in a new tab.
        """
        from .manager import SNAPSHOT_CDP_TIMEOUT

        log.debug("_restore_profile: cid=%s also_open_url=%s", cid, also_open_url)
        chrome_mgr = self._chrome_mgr
        if not chrome_mgr:
            # Lazy fallback: ensure_chrome may have failed at startup while
            # Chrome was still starting up.  Try to create a ChromeManager now.
            try:
                cm = cdp.ChromeManager(port=self.browser_port)
                if cm.is_running():
                    self._chrome_mgr = cm
                    chrome_mgr = cm
                    log.debug("_restore_profile: lazily created ChromeManager")
            except Exception:
                pass
            if not chrome_mgr:
                raise RuntimeError("No ChromeManager configured for profile sessions")

        prof_name = row.get("profile_dir") or profile_dir_name(cid)
        # Ensure profile directory exists with session restore prefs
        container_name = row.get("name") or cid
        cdp.create_profile_dir(chrome_mgr.user_data_dir, cid,
                               display_name=container_name)
        # Update display name + avatar in case it was renamed since creation
        cdp.update_profile_display(chrome_mgr.user_data_dir, cid,
                                   container_name)

        # Record known contexts and target IDs before launch so we can
        # detect new contexts (fresh profile load) or new targets in an
        # existing context (profile already loaded, e.g. after soft-hibernate).
        known_ctxs: set[str] = set()
        known_tids: set[str] = set()
        try:
            with self._browser_session() as bs:
                for t in bs.target.get_targets(timeout=SNAPSHOT_CDP_TIMEOUT):
                    ctx = t.get("browserContextId")
                    if ctx:
                        known_ctxs.add(ctx)
                    tid = t.get("targetId")
                    if tid:
                        known_tids.add(tid)
        except Exception:
            pass

        shadow_tabs = cdp.load_profile_tabs(chrome_mgr.user_data_dir, cid)

        if also_open_url:
            # User clicked a specific tab — open just that URL.
            chrome_mgr.launch_profile(prof_name, start_url=also_open_url)
        else:
            # Crash recovery or hibernation resume — open ALL saved tabs
            # by writing them into the profile prefs so Chrome handles it
            # natively, without a "restore tabs?" prompt or duplication.
            restore_urls = [t["url"] for t in shadow_tabs] if shadow_tabs else []
            if not restore_urls and row.get("tabs"):
                restore_urls = [t["url"] for t in row["tabs"]
                                if t.get("url") and t["url"] != "about:blank"]
            cdp.update_profile_prefs_for_restore(
                chrome_mgr.user_data_dir, cid, restore_urls)
            chrome_mgr.launch_profile(prof_name, start_url=None)

        # Discover the new browserContextId
        new_ctx = self._discover_profile_context(
            known_ctxs, known_tids=known_tids)
        if not new_ctx:
            # Fallback: try to find a target in the profile by URL match
            log.warning("_restore_profile: could not discover context for %s, "
                        "falling back to tab list match", cid)
            saved_urls = {t["url"] for t in (row.get("tabs") or shadow_tabs)}
            if also_open_url:
                saved_urls.add(also_open_url)
            try:
                with self._browser_session() as bs:
                    for t in bs.target.get_targets(timeout=SNAPSHOT_CDP_TIMEOUT):
                        ctx = t.get("browserContextId")
                        if not ctx or t.get("type") != "page":
                            continue
                        url = t.get("url", "")
                        if url in saved_urls:
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

        # Reset prefs back to restore_on_startup=1 so that future Chrome
        # restarts (by the user or after a crash) use native session restore
        # rather than the one-time startup_urls list.
        if not also_open_url:
            cdp.reset_profile_prefs_after_launch(
                chrome_mgr.user_data_dir, cid)

        # Wait for Chrome to register tabs in the context.
        time.sleep(1.5)
        try:
            profile_tabs = self._targets_for(new_ctx)
            live_urls = {t.get("url", "") for t in profile_tabs}
        except Exception:
            live_urls = set()

        # Build the set of URLs we expected Chrome to restore.
        expected_urls: set[str] = set()
        tabs_source = shadow_tabs or row.get("tabs") or []
        for t in tabs_source:
            u = t.get("url", "")
            if u and u != "about:blank":
                expected_urls.add(u)
        if also_open_url:
            expected_urls.add(also_open_url)

        # Fallback: open saved tabs via CDP if Chrome did not restore them.
        # This triggers when:
        #  - Chrome opened zero real tabs (brand-new profile, corrupt prefs)
        #  - Chrome only opened a homepage instead of saved tabs (profile
        #    was already loaded — "Opening in existing browser session" —
        #    and ignored the startup_urls prefs).
        real_urls = {u for u in live_urls
                     if u and u != "about:blank"
                     and not u.startswith("chrome://")}
        tabs_restored = bool(expected_urls & real_urls)
        if not tabs_restored and not also_open_url and expected_urls:
            log.debug("_restore_profile: Chrome did not restore tabs "
                      "(live=%d, expected=%d), opening via CDP",
                      len(real_urls), len(expected_urls))
            with self._browser_session() as bs:
                for t in tabs_source:
                    url = t.get("url", "")
                    if url and url != "about:blank":
                        try:
                            bs.target.create_target(
                                url=url, browser_context_id=new_ctx)
                        except Exception:
                            pass

        # Close any leftover tabs (homepage, chrome://newtab/, about:blank)
        # that Chrome opened automatically when the profile window was
        # created.  These are unwanted when we already opened the desired
        # tabs above.  _close_newtab_targets waits for tabs to finish
        # loading before deciding what to close.
        self._close_newtab_targets(new_ctx, expected_urls=expected_urls)

        tabs_count = max(len(real_urls), len(expected_urls))
        log.debug("_restore_profile: done cid=%s ctx=%s tabs=%d",
                  cid, new_ctx, tabs_count)
        return {"id": cid, "browserContextId": new_ctx,
                "tabs_opened": tabs_count,
                "session_type": "profile"}
