"""Tests for profile-backed session lifecycle."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_root, "src"))
sys.path.insert(0, _root)

from tests.fakes import FakeBrowser, make_fake_session_factory, _PatchedManagerMixin
from sessions.persistence import PersistenceManager
from sessions.manager import ContainerManager
from sessions import cdp


# ---------------------------------------------------------------------------
# Profile create / delete / session_type
# ---------------------------------------------------------------------------

class TestProfileCreate(_PatchedManagerMixin, unittest.TestCase):

    def test_create_profile_container(self):
        """Creating a profile session sets session_type and creates dir."""
        self.mgr._chrome_mgr = type("CM", (), {"user_data_dir": self.tmp})()
        c = self.mgr.create_container("Gmail", session_type="profile")
        self.assertEqual(c["session_type"], "profile")
        self.assertTrue(c["profile_dir"].startswith("sessions-"))
        self.assertTrue(self.mgr.is_profile(c["id"]))
        # Profile directory should exist
        prof_path = os.path.join(self.tmp, cdp.profile_dir_name(c["id"]))
        self.assertTrue(os.path.isdir(prof_path))
        prefs = os.path.join(prof_path, "Preferences")
        self.assertTrue(os.path.isfile(prefs))

    def test_create_context_container_default(self):
        """Default create is a context (Lite Session), not a profile."""
        c = self.mgr.create_container("Random")
        self.assertEqual(c["session_type"], "context")
        self.assertIsNone(c["profile_dir"])
        self.assertFalse(self.mgr.is_profile(c["id"]))

    def test_delete_profile_removes_dir(self):
        """Deleting a profile session cleans up the profile directory."""
        self.mgr._chrome_mgr = type("CM", (), {"user_data_dir": self.tmp})()
        c = self.mgr.create_container("Okta", session_type="profile")
        prof_path = os.path.join(self.tmp, cdp.profile_dir_name(c["id"]))
        self.assertTrue(os.path.isdir(prof_path))
        self.mgr.delete(c["id"])
        self.assertFalse(os.path.isdir(prof_path))
        self.assertFalse(self.mgr.is_profile(c["id"]))
        self.assertIsNone(self.store.get_container(c["id"]))

    def test_delete_context_does_not_touch_profile(self):
        """Deleting a context session doesn't try to delete a profile dir."""
        c = self.mgr.create_container("Surf")
        self.mgr.delete(c["id"])
        self.assertIsNone(self.store.get_container(c["id"]))


# ---------------------------------------------------------------------------
# Profile hibernate
# ---------------------------------------------------------------------------

class TestProfileHibernate(_PatchedManagerMixin, unittest.TestCase):

    def test_hibernate_profile_saves_shadow_tabs(self):
        """Hibernating a profile session saves shadow tabs and closes targets."""
        self.mgr._chrome_mgr = type("CM", (), {"user_data_dir": self.tmp})()
        c = self.mgr.create_container("Work", session_type="profile")
        cid = c["id"]
        ctx = "CTX-PROFILE-1"
        self.mgr.hot[cid] = ctx
        self.mgr._profile_sessions.add(cid)
        self.store.mark_active(cid, True)
        # Seed live tabs
        self.fb.seed_tab(ctx, "https://gmail.com/inbox", "Inbox")
        self.fb.seed_tab(ctx, "https://docs.google.com", "Docs")

        result = self.mgr.hibernate(cid)
        self.assertEqual(result["tabs_saved"], 2)
        self.assertEqual(result["session_type"], "profile")
        self.assertNotIn(cid, self.mgr.hot)

        # Shadow tab file should exist
        shadow = cdp.load_profile_tabs(self.tmp, cid)
        self.assertEqual(len(shadow), 2)
        urls = {t["url"] for t in shadow}
        self.assertIn("https://gmail.com/inbox", urls)
        self.assertIn("https://docs.google.com", urls)

        # DB should have tabs saved
        full = self.store.get_container(cid)
        self.assertFalse(full["is_active"])
        self.assertEqual(len(full["tabs"]), 2)

    def test_hibernate_profile_does_not_dispose_context(self):
        """Profile hibernate must NOT call disposeBrowserContext."""
        self.mgr._chrome_mgr = type("CM", (), {"user_data_dir": self.tmp})()
        c = self.mgr.create_container("Test", session_type="profile")
        cid = c["id"]
        ctx = "CTX-PROF-2"
        self.mgr.hot[cid] = ctx
        self.mgr._profile_sessions.add(cid)
        self.store.mark_active(cid, True)
        self.fb.seed_tab(ctx, "https://example.com", "Ex")

        self.mgr.hibernate(cid)
        # disposed list tracks disposeBrowserContext calls
        self.assertNotIn(ctx, self.fb.disposed)

    def test_hibernate_context_does_dispose(self):
        """Regular context hibernate DOES dispose the context (regression)."""
        c = self.mgr.create_container("Lite")
        cid = c["id"]
        ctx = "CTX-LITE-1"
        self.fb.contexts.add(ctx)
        self.mgr.hot[cid] = ctx
        self.store.mark_active(cid, True)
        self.fb.seed_tab(ctx, "https://example.com", "Ex")
        self.fb.cookies[ctx] = []

        self.mgr.hibernate(cid)
        self.assertIn(ctx, self.fb.disposed)


# ---------------------------------------------------------------------------
# Profile snapshot
# ---------------------------------------------------------------------------

class TestProfileSnapshot(_PatchedManagerMixin, unittest.TestCase):

    def test_snapshot_profile_saves_shadow_tabs(self):
        """Profile snapshot only saves tab URLs (no cookies/storage)."""
        self.mgr._chrome_mgr = type("CM", (), {"user_data_dir": self.tmp})()
        c = self.mgr.create_container("Gmail", session_type="profile")
        cid = c["id"]
        ctx = "CTX-SNAP-1"
        self.mgr.hot[cid] = ctx
        self.mgr._profile_sessions.add(cid)
        self.fb.seed_tab(ctx, "https://gmail.com/inbox", "Inbox")

        result = self.mgr.snapshot(cid)
        self.assertEqual(result["tabs_saved"], 1)
        self.assertEqual(result["session_type"], "profile")

        # Shadow file written
        shadow = cdp.load_profile_tabs(self.tmp, cid)
        self.assertEqual(len(shadow), 1)
        self.assertEqual(shadow[0]["url"], "https://gmail.com/inbox")

    def test_snapshot_profile_skips_unchanged(self):
        """Second snapshot with same tabs should be skipped."""
        self.mgr._chrome_mgr = type("CM", (), {"user_data_dir": self.tmp})()
        c = self.mgr.create_container("Test", session_type="profile")
        cid = c["id"]
        ctx = "CTX-SNAP-2"
        self.mgr.hot[cid] = ctx
        self.mgr._profile_sessions.add(cid)
        self.fb.seed_tab(ctx, "https://example.com", "Ex")

        r1 = self.mgr.snapshot(cid)
        self.assertIn("tabs_saved", r1)
        r2 = self.mgr.snapshot(cid)
        self.assertEqual(r2.get("skipped"), "unchanged")

    def test_snapshot_profile_preserves_tabs_when_empty(self):
        """Profile snapshot must not overwrite saved tabs with empty list."""
        self.mgr._chrome_mgr = type("CM", (), {"user_data_dir": self.tmp})()
        c = self.mgr.create_container("Gmail", session_type="profile")
        cid = c["id"]
        ctx = "CTX-SNAP-EMPTY"
        self.mgr.hot[cid] = ctx
        self.mgr._profile_sessions.add(cid)

        # First snapshot with a real tab
        self.fb.seed_tab(ctx, "https://gmail.com/inbox", "Inbox")
        r1 = self.mgr.snapshot(cid)
        self.assertEqual(r1["tabs_saved"], 1)

        # Simulate window closing: remove all tabs from the fake browser
        self.fb.targets = {tid: t for tid, t in self.fb.targets.items()
                           if t.get("browserContextId") != ctx}
        # Force hash mismatch so the snapshot runs
        self.mgr._last_snapshot_hash.pop(cid, None)

        r2 = self.mgr.snapshot(cid)
        self.assertEqual(r2.get("skipped"), "preserve-tabs")

        # DB should still have the original tab
        full = self.store.get_container(cid)
        self.assertEqual(len(full["tabs"]), 1)
        self.assertEqual(full["tabs"][0]["url"], "https://gmail.com/inbox")

    def test_snapshot_not_hot(self):
        """Snapshot of non-hot container is skipped."""
        c = self.mgr.create_container("Cold")
        result = self.mgr.snapshot(c["id"])
        self.assertEqual(result.get("skipped"), "not-hot")


# ---------------------------------------------------------------------------
# Soft hibernate profile awareness
# ---------------------------------------------------------------------------

class TestSoftHibernateProfile(_PatchedManagerMixin, unittest.TestCase):

    def test_soft_hibernate_profile_skips_dispose(self):
        """_soft_hibernate for profile sessions must not dispose context."""
        self.mgr._chrome_mgr = type("CM", (), {"user_data_dir": self.tmp})()
        c = self.mgr.create_container("Prof", session_type="profile")
        cid = c["id"]
        ctx = "CTX-SOFT-1"
        self.mgr.hot[cid] = ctx
        self.mgr._profile_sessions.add(cid)
        self.store.mark_active(cid, True)

        self.mgr._soft_hibernate(cid)
        self.assertNotIn(ctx, self.fb.disposed)
        self.assertNotIn(cid, self.mgr.hot)
        full = self.store.get_container(cid)
        self.assertFalse(full["is_active"])

    def test_soft_hibernate_context_disposes(self):
        """_soft_hibernate for context sessions still disposes (regression)."""
        c = self.mgr.create_container("Lite")
        cid = c["id"]
        ctx = "CTX-SOFT-2"
        self.fb.contexts.add(ctx)
        self.mgr.hot[cid] = ctx
        self.store.mark_active(cid, True)

        self.mgr._soft_hibernate(cid)
        self.assertIn(ctx, self.fb.disposed)


# ---------------------------------------------------------------------------
# Auto-restore rebuilds _profile_sessions
# ---------------------------------------------------------------------------

class TestAutoRestoreProfile(_PatchedManagerMixin, unittest.TestCase):

    def test_auto_restore_rebuilds_profile_set(self):
        """auto_restore_hot must rebuild _profile_sessions from DB."""
        # Create a profile container directly in the store
        c = self.store.create_container("Gmail", session_type="profile",
                                        profile_dir="sessions-gmail")
        self.store.mark_active(c["id"], True)
        # Save some tabs so restore has something
        self.store.save_hibernation(c["id"], [], {},
                                    [{"url": "https://gmail.com", "title": "G"}],
                                    keep_active=True)
        # _profile_sessions should be empty before auto_restore
        self.assertEqual(len(self.mgr._profile_sessions), 0)

        # auto_restore will try to call _restore_profile which needs _chrome_mgr
        # but we haven't set one — it should add to _profile_sessions before failing
        self.mgr._chrome_mgr = type("CM", (), {"user_data_dir": self.tmp})()

        # We can't fully test restore without a real Chrome, but we can verify
        # the _profile_sessions set gets populated
        # Use a patched _restore_profile to avoid the Chrome launch
        restored = []

        def mock_restore_profile(cid, row, also_open_url=None):
            self.mgr.hot[cid] = "CTX-MOCK"
            self.mgr._profile_sessions.add(cid)
            restored.append(cid)
            return {"id": cid, "browserContextId": "CTX-MOCK",
                    "tabs_opened": 1, "session_type": "profile"}

        self.mgr._restore_profile = mock_restore_profile

        self.mgr.auto_restore_hot()
        self.assertTrue(self.mgr.is_profile(c["id"]))
        self.assertIn(c["id"], restored)


# ---------------------------------------------------------------------------
# Status includes session_type
# ---------------------------------------------------------------------------

class TestStatusSessionType(_PatchedManagerMixin, unittest.TestCase):

    def test_status_includes_session_type(self):
        """status() must include session_type for each container."""
        self.mgr.create_container("Lite")
        self.mgr._chrome_mgr = type("CM", (), {"user_data_dir": self.tmp})()
        self.mgr.create_container("Prof", session_type="profile")

        st = self.mgr.status()
        types = {c["id"]: c["session_type"] for c in st["containers"]}
        self.assertEqual(types["lite"], "context")
        self.assertEqual(types["prof"], "profile")


# ---------------------------------------------------------------------------
# API / server session_type passthrough
# ---------------------------------------------------------------------------

class TestAPISessionType(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ctxd-api-prof-")
        self.store = PersistenceManager(os.path.join(self.tmp, "s.db"))
        self.mgr = ContainerManager(store=self.store)
        fb = FakeBrowser()
        FBS, FTS = make_fake_session_factory(fb)
        self.mgr._browser_session = lambda: FBS()
        self.mgr._new_browser_session = lambda: FBS()
        self.mgr._tab_session = lambda tid: FTS(tid)
        self.mgr._get_targets_cached = lambda **_: []
        self.mgr._chrome_http_reachable = lambda **_: False
        self.mgr._chrome_mgr = type("CM", (), {"user_data_dir": self.tmp})()

        def fake_open(ctx, url, storage, idb=None, **_):
            fb._target_counter += 1
            tid = f"T{fb._target_counter}"
            fb.targets[tid] = {"targetId": tid, "url": url, "title": "",
                               "browserContextId": ctx, "type": "page"}
            return tid
        self.mgr._open_tab_with_storage = fake_open

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_create_container_with_session_type(self):
        """create_container(session_type='profile') flows through."""
        c = self.mgr.create_container("Test", session_type="profile")
        self.assertEqual(c["session_type"], "profile")
        full = self.store.get_container(c["id"])
        self.assertEqual(full["session_type"], "profile")

    def test_create_container_default_context(self):
        """Default session_type is 'context'."""
        c = self.mgr.create_container("Test2")
        self.assertEqual(c["session_type"], "context")


# ---------------------------------------------------------------------------
# Phase 3: Crash recovery — _profile_sessions rebuild
# ---------------------------------------------------------------------------

class TestCrashRecoveryProfile(_PatchedManagerMixin, unittest.TestCase):

    def test_profile_sessions_rebuilt_after_clear(self):
        """After clearing hot + _profile_sessions, rebuilding from DB works."""
        self.mgr._chrome_mgr = type("CM", (), {"user_data_dir": self.tmp})()
        c = self.mgr.create_container("Gmail", session_type="profile")
        cid = c["id"]
        self.assertTrue(self.mgr.is_profile(cid))
        # Simulate crash: clear everything
        self.mgr.hot.clear()
        self.mgr._profile_sessions.clear()
        self.assertFalse(self.mgr.is_profile(cid))
        # Rebuild from DB (what recover_chrome / auto_restore_hot does)
        for row in self.store.list_containers():
            if row.get("session_type") == "profile":
                self.mgr._profile_sessions.add(row["id"])
        self.assertTrue(self.mgr.is_profile(cid))

    def test_reconnect_marks_profile_session(self):
        """reconnect_to_existing sets _profile_sessions for profile containers."""
        self.mgr._chrome_mgr = type("CM", (), {"user_data_dir": self.tmp})()
        c = self.store.create_container("Prof", session_type="profile",
                                        profile_dir="sessions-prof")
        self.store.mark_active(c["id"], True)
        self.store.save_hibernation(c["id"], [], {},
                                   [{"url": "https://gmail.com", "title": "G"}],
                                   keep_active=True)
        # Seed a matching live tab
        self.fb.seed_tab("CTX-R1", "https://gmail.com", "Gmail")
        result = self.mgr.reconnect_to_existing()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["session_type"], "profile")
        self.assertTrue(self.mgr.is_profile(c["id"]))


# ---------------------------------------------------------------------------
# Phase 4: Cross-type tab movement
# ---------------------------------------------------------------------------

class TestCrossTypeTabMove(_PatchedManagerMixin, unittest.TestCase):

    def _setup_hot(self, name, session_type="context"):
        """Create a container and make it hot with a seeded tab."""
        if session_type == "profile":
            self.mgr._chrome_mgr = type("CM", (), {
                "user_data_dir": self.tmp,
                "launch_profile": lambda self, p, start_url="about:blank": None,
            })()
        c = self.mgr.create_container(name, session_type=session_type)
        cid = c["id"]
        ctx = f"CTX-{name.upper()}"
        self.fb.contexts.add(ctx)
        self.mgr.hot[cid] = ctx
        self.store.mark_active(cid, True)
        if session_type == "profile":
            self.mgr._profile_sessions.add(cid)
        self.fb.cookies[ctx] = []
        return cid, ctx

    def test_move_lite_to_lite(self):
        """Move tab between two Lite Sessions."""
        src, src_ctx = self._setup_hot("src")
        dst, dst_ctx = self._setup_hot("dst")
        self.fb.seed_tab(src_ctx, "https://example.com", "Ex")
        result = self.mgr.move_tab(src, dst, url="https://example.com")
        self.assertNotIn("error", result)

    def test_move_lite_to_profile(self):
        """Move tab from Lite Session to Session (profile)."""
        src, src_ctx = self._setup_hot("src")
        dst, dst_ctx = self._setup_hot("pdst", session_type="profile")
        self.fb.seed_tab(src_ctx, "https://example.com", "Ex")
        result = self.mgr.move_tab(src, dst, url="https://example.com")
        self.assertNotIn("error", result)

    def test_move_profile_to_lite(self):
        """Move tab from Session (profile) to Lite Session."""
        src, src_ctx = self._setup_hot("psrc", session_type="profile")
        dst, dst_ctx = self._setup_hot("dst")
        self.fb.seed_tab(src_ctx, "https://example.com", "Ex")
        result = self.mgr.move_tab(src, dst, url="https://example.com")
        self.assertNotIn("error", result)

    def test_move_profile_to_profile(self):
        """Move tab between two Sessions (profiles)."""
        src, src_ctx = self._setup_hot("psrc", session_type="profile")
        dst, dst_ctx = self._setup_hot("pdst", session_type="profile")
        self.fb.seed_tab(src_ctx, "https://example.com", "Ex")
        result = self.mgr.move_tab(src, dst, url="https://example.com")
        self.assertNotIn("error", result)

    def test_move_cold_lite_to_hot_profile(self):
        """Move tab from cold Lite Session to hot Session (profile)."""
        src = self.mgr.create_container("cold-src")
        self.store.save_hibernation(src["id"], [], {},
                                    [{"url": "https://example.com", "title": "Ex"}])
        dst, dst_ctx = self._setup_hot("pdst", session_type="profile")
        result = self.mgr.move_tab(src["id"], dst, url="https://example.com")
        self.assertNotIn("error", result)
        self.assertTrue(result.get("moved"))

    def test_move_cold_profile_to_hot_profile(self):
        """Move tab from cold Session (profile) to hot Session (profile)."""
        self.mgr._chrome_mgr = type("CM", (), {
            "user_data_dir": self.tmp,
            "launch_profile": lambda self, p, start_url="about:blank": None,
        })()
        src = self.mgr.create_container("cold-psrc", session_type="profile")
        self.store.save_hibernation(src["id"], [], {},
                                    [{"url": "https://example.com", "title": "Ex"}])
        dst, dst_ctx = self._setup_hot("pdst2", session_type="profile")
        result = self.mgr.move_tab(src["id"], dst, url="https://example.com")
        self.assertNotIn("error", result)
        self.assertTrue(result.get("moved"))


# ---------------------------------------------------------------------------
# Edge cases (Phase 7)
# ---------------------------------------------------------------------------

class TestProfileEdgeCases(_PatchedManagerMixin, unittest.TestCase):

    def test_delete_nonexistent_profile_dir(self):
        """Deleting a profile whose dir was already removed doesn't crash."""
        self.mgr._chrome_mgr = type("CM", (), {"user_data_dir": self.tmp})()
        c = self.mgr.create_container("Ghost", session_type="profile")
        prof_path = os.path.join(self.tmp, cdp.profile_dir_name(c["id"]))
        # Manually remove the dir before delete
        import shutil
        shutil.rmtree(prof_path)
        self.assertFalse(os.path.isdir(prof_path))
        # delete should not raise
        self.mgr.delete(c["id"])
        self.assertIsNone(self.store.get_container(c["id"]))

    def test_hibernate_profile_no_tabs(self):
        """Hibernating an empty profile session still works."""
        self.mgr._chrome_mgr = type("CM", (), {"user_data_dir": self.tmp})()
        c = self.mgr.create_container("Empty", session_type="profile")
        cid = c["id"]
        ctx = "CTX-EMPTY"
        self.mgr.hot[cid] = ctx
        self.mgr._profile_sessions.add(cid)
        self.store.mark_active(cid, True)
        result = self.mgr.hibernate(cid)
        self.assertEqual(result["tabs_saved"], 0)
        self.assertNotIn(cid, self.mgr.hot)

    def test_snapshot_profile_filters_chrome_urls(self):
        """Profile snapshot skips chrome:// and about:blank URLs."""
        self.mgr._chrome_mgr = type("CM", (), {"user_data_dir": self.tmp})()
        c = self.mgr.create_container("Filter", session_type="profile")
        cid = c["id"]
        ctx = "CTX-FILTER"
        self.mgr.hot[cid] = ctx
        self.mgr._profile_sessions.add(cid)
        self.fb.seed_tab(ctx, "chrome://settings", "Settings")
        self.fb.seed_tab(ctx, "about:blank", "New Tab")
        self.fb.seed_tab(ctx, "https://real.com", "Real")
        result = self.mgr.snapshot(cid)
        self.assertEqual(result["tabs_saved"], 1)
        shadow = cdp.load_profile_tabs(self.tmp, cid)
        self.assertEqual(len(shadow), 1)
        self.assertEqual(shadow[0]["url"], "https://real.com")

    def test_bulk_hibernate_with_mixed_types(self):
        """bulk_hibernate works with a mix of profile and context sessions."""
        self.mgr._chrome_mgr = type("CM", (), {"user_data_dir": self.tmp})()
        lite = self.mgr.create_container("Lite")
        prof = self.mgr.create_container("Prof", session_type="profile")
        # Make both hot
        ctx_l = "CTX-BL"
        ctx_p = "CTX-BP"
        self.fb.contexts.add(ctx_l)
        self.mgr.hot[lite["id"]] = ctx_l
        self.mgr.hot[prof["id"]] = ctx_p
        self.mgr._profile_sessions.add(prof["id"])
        self.store.mark_active(lite["id"], True)
        self.store.mark_active(prof["id"], True)
        self.fb.cookies[ctx_l] = []
        self.fb.seed_tab(ctx_l, "https://lite.com", "L")
        self.fb.seed_tab(ctx_p, "https://prof.com", "P")
        results = self.mgr.bulk_hibernate([lite["id"], prof["id"]])
        self.assertEqual(len(results), 2)
        self.assertNotIn(lite["id"], self.mgr.hot)
        self.assertNotIn(prof["id"], self.mgr.hot)
        # Context should be disposed, profile should not
        self.assertIn(ctx_l, self.fb.disposed)
        self.assertNotIn(ctx_p, self.fb.disposed)

    def test_bulk_delete_with_mixed_types(self):
        """bulk_delete works with profile and context sessions."""
        self.mgr._chrome_mgr = type("CM", (), {"user_data_dir": self.tmp})()
        lite = self.mgr.create_container("Lite")
        prof = self.mgr.create_container("Prof", session_type="profile")
        prof_path = os.path.join(self.tmp, cdp.profile_dir_name(prof["id"]))
        self.assertTrue(os.path.isdir(prof_path))
        results = self.mgr.bulk_delete([lite["id"], prof["id"]])
        self.assertEqual(len(results), 2)
        self.assertIsNone(self.store.get_container(lite["id"]))
        self.assertIsNone(self.store.get_container(prof["id"]))
        self.assertFalse(os.path.isdir(prof_path))


# ---------------------------------------------------------------------------
# Prefs-based restore (tab duplication fix)
# ---------------------------------------------------------------------------

class TestPrefsBasedRestore(_PatchedManagerMixin, unittest.TestCase):

    def _setup_for_restore(self, shadow_urls):
        """Create a profile container with shadow tabs saved, return cid."""
        self.mgr._chrome_mgr = type("CM", (), {
            "user_data_dir": self.tmp,
            "chrome_path": "chrome",
        })()
        # Stub launch_profile so it doesn't actually launch Chrome
        launched = []
        def fake_launch(prof, start_url="about:blank"):
            launched.append({"prof": prof, "start_url": start_url})
            # Simulate Chrome creating a target in a new context
            new_ctx = f"CTX-RESTORE-{len(launched)}"
            for url in shadow_urls:
                self.fb.seed_tab(new_ctx, url, "")
        self.mgr._chrome_mgr.launch_profile = fake_launch

        c = self.mgr.create_container("Restore", session_type="profile")
        cid = c["id"]
        tabs = [{"url": u, "title": ""} for u in shadow_urls]
        cdp.save_profile_tabs(self.tmp, cid, tabs)
        self.store.save_hibernation(cid, [], {}, tabs)
        return cid, launched

    def test_restore_no_url_uses_prefs(self):
        """Restore without also_open_url writes prefs and passes start_url=None."""
        urls = ["https://gmail.com/inbox", "https://docs.google.com"]
        cid, launched = self._setup_for_restore(urls)

        self.mgr.restore(cid)

        # launch_profile should have been called with start_url=None
        self.assertEqual(len(launched), 1)
        self.assertIsNone(launched[0]["start_url"])

        # Prefs should have been updated with startup_urls before launch
        import json
        prefs_path = os.path.join(
            self.tmp, cdp.profile_dir_name(cid), "Preferences")
        with open(prefs_path) as f:
            prefs = json.load(f)
        # After restore, prefs are reset back to restore_on_startup=1
        self.assertEqual(prefs["session"]["restore_on_startup"], 1)

    def test_restore_with_url_passes_it_directly(self):
        """Restore with also_open_url passes it as start_url (no prefs change)."""
        urls = ["https://gmail.com/inbox"]
        cid, launched = self._setup_for_restore(urls)

        self.mgr.restore(cid, also_open_url="https://gmail.com/inbox")

        self.assertEqual(len(launched), 1)
        self.assertEqual(launched[0]["start_url"], "https://gmail.com/inbox")

    def test_restore_prefs_reset_after_launch(self):
        """After prefs-based restore, prefs are reset to restore_on_startup=1."""
        urls = ["https://example.com"]
        cid, launched = self._setup_for_restore(urls)

        self.mgr.restore(cid)

        import json
        prefs_path = os.path.join(
            self.tmp, cdp.profile_dir_name(cid), "Preferences")
        with open(prefs_path) as f:
            prefs = json.load(f)
        self.assertEqual(prefs["session"]["restore_on_startup"], 1)
        self.assertNotIn("startup_urls", prefs.get("session", {}))

    def test_restore_prefs_clean_exit(self):
        """Prefs-based restore sets exit_type=Normal and exited_cleanly=True."""
        urls = ["https://example.com"]
        cid, _launched = self._setup_for_restore(urls)

        # Read prefs BEFORE restore completes (intercept launch_profile)
        prefs_before_launch = {}
        orig_launch = self.mgr._chrome_mgr.launch_profile
        def capture_launch(prof, start_url="about:blank"):
            import json
            prefs_path = os.path.join(
                self.tmp, cdp.profile_dir_name(cid), "Preferences")
            with open(prefs_path) as f:
                prefs_before_launch.update(json.load(f))
            orig_launch(prof, start_url=start_url)
        self.mgr._chrome_mgr.launch_profile = capture_launch

        self.mgr.restore(cid)

        self.assertEqual(prefs_before_launch["profile"]["exit_type"], "Normal")
        self.assertTrue(prefs_before_launch["profile"]["exited_cleanly"])
        self.assertEqual(prefs_before_launch["session"]["restore_on_startup"], 4)
        self.assertEqual(prefs_before_launch["session"]["startup_urls"],
                         ["https://example.com"])

    def test_hibernate_sets_clean_exit_prefs(self):
        """Hibernating a profile writes exit_type=Normal so next restore is clean."""
        self.mgr._chrome_mgr = type("CM", (), {"user_data_dir": self.tmp})()
        c = self.mgr.create_container("Hib", session_type="profile")
        cid = c["id"]
        ctx = "CTX-HIB"
        self.mgr.hot[cid] = ctx
        self.mgr._profile_sessions.add(cid)
        self.store.mark_active(cid, True)
        self.fb.seed_tab(ctx, "https://example.com", "Ex")

        self.mgr.hibernate(cid)

        import json
        prefs_path = os.path.join(
            self.tmp, cdp.profile_dir_name(cid), "Preferences")
        with open(prefs_path) as f:
            prefs = json.load(f)
        self.assertEqual(prefs["profile"]["exit_type"], "Normal")
        self.assertTrue(prefs["profile"]["exited_cleanly"])
        self.assertEqual(prefs["session"]["restore_on_startup"], 4)
        self.assertIn("https://example.com", prefs["session"]["startup_urls"])


# ---------------------------------------------------------------------------
# cdp helper tests
# ---------------------------------------------------------------------------

class TestProfilePrefsHelpers(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ctxd-prefs-")
        # Create a minimal profile dir
        cid = "test-prefs"
        self.cid = cid
        self.prof_path = os.path.join(self.tmp, cdp.profile_dir_name(cid))
        os.makedirs(self.prof_path, exist_ok=True)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_update_prefs_with_urls(self):
        """update_profile_prefs_for_restore writes startup_urls and clean exit."""
        import json
        cdp.update_profile_prefs_for_restore(
            self.tmp, self.cid, ["https://a.com", "https://b.com"])
        with open(os.path.join(self.prof_path, "Preferences")) as f:
            prefs = json.load(f)
        self.assertEqual(prefs["session"]["restore_on_startup"], 4)
        self.assertEqual(prefs["session"]["startup_urls"],
                         ["https://a.com", "https://b.com"])
        self.assertEqual(prefs["profile"]["exit_type"], "Normal")
        self.assertTrue(prefs["profile"]["exited_cleanly"])

    def test_update_prefs_no_urls(self):
        """update_profile_prefs_for_restore with no URLs falls back to restore_on_startup=1."""
        import json
        cdp.update_profile_prefs_for_restore(self.tmp, self.cid, [])
        with open(os.path.join(self.prof_path, "Preferences")) as f:
            prefs = json.load(f)
        self.assertEqual(prefs["session"]["restore_on_startup"], 1)

    def test_reset_prefs(self):
        """reset_profile_prefs_after_launch restores restore_on_startup=1."""
        import json
        cdp.update_profile_prefs_for_restore(
            self.tmp, self.cid, ["https://a.com"])
        cdp.reset_profile_prefs_after_launch(self.tmp, self.cid)
        with open(os.path.join(self.prof_path, "Preferences")) as f:
            prefs = json.load(f)
        self.assertEqual(prefs["session"]["restore_on_startup"], 1)
        self.assertNotIn("startup_urls", prefs["session"])

    def test_update_prefs_preserves_existing(self):
        """update_profile_prefs_for_restore preserves other prefs keys."""
        import json
        prefs_path = os.path.join(self.prof_path, "Preferences")
        with open(prefs_path, "w") as f:
            json.dump({"custom_key": "value", "profile": {"name": "Test"}}, f)
        cdp.update_profile_prefs_for_restore(
            self.tmp, self.cid, ["https://a.com"])
        with open(prefs_path) as f:
            prefs = json.load(f)
        self.assertEqual(prefs["custom_key"], "value")
        self.assertEqual(prefs["profile"]["name"], "Test")
        self.assertEqual(prefs["profile"]["exit_type"], "Normal")


# ---------------------------------------------------------------------------
# FIX: Profile restore when profile is already loaded in Chrome
# (context reuse after soft-hibernate)
# ---------------------------------------------------------------------------

class TestProfileRestoreAlreadyLoaded(_PatchedManagerMixin, unittest.TestCase):
    """Guard: restoring a profile that Chrome already has loaded must succeed
    by detecting a new target in the existing context, not only new contexts."""

    def _setup_already_loaded(self, urls, homepage_url="https://www.google.com/"):
        """Create a profile session with Chrome already having the context loaded.

        Simulates the scenario where _soft_hibernate closed the tabs but the
        profile context still exists in Chrome (as happens with Chrome profiles).
        *homepage_url* is what Chrome opens instead of the saved tabs.
        """
        self.mgr._chrome_mgr = type("CM", (), {
            "user_data_dir": self.tmp,
            "chrome_path": "chrome",
        })()

        c = self.mgr.create_container("Already", session_type="profile")
        cid = c["id"]
        tabs = [{"url": u, "title": ""} for u in urls]
        cdp.save_profile_tabs(self.tmp, cid, tabs)
        self.store.save_hibernation(cid, [], {}, tabs)

        # Seed an existing context for this profile (simulating Chrome
        # having the profile loaded but window closed / tabs cleared).
        existing_ctx = "CTX-ALREADY-LOADED"
        # The context has no page targets (tabs were closed by _soft_hibernate)

        launched = []
        def fake_launch(prof, start_url="about:blank"):
            launched.append({"prof": prof, "start_url": start_url})
            # Chrome reuses the EXISTING context (no new context created),
            # but opens a new tab (new target ID) with the homepage.
            self.fb.seed_tab(existing_ctx, homepage_url, "Homepage")

        self.mgr._chrome_mgr.launch_profile = fake_launch
        return cid, launched, existing_ctx

    def test_restore_reuses_existing_context(self):
        """Profile restore must detect the existing context via new target ID."""
        urls = ["https://gmail.com/inbox"]
        cid, launched, existing_ctx = self._setup_already_loaded(urls)

        result = self.mgr.restore(cid)

        self.assertEqual(len(launched), 1)
        self.assertEqual(result["id"], cid)
        self.assertIn(cid, self.mgr.hot)
        # The context should be the existing one, not a brand new one
        self.assertEqual(self.mgr.hot[cid], existing_ctx)
        # Session should be marked active
        row = self.store.get_container(cid)
        self.assertTrue(row["is_active"])

    def test_restore_opens_saved_tabs_via_cdp_when_homepage_only(self):
        """When Chrome opens only a homepage (not saved tabs), saved tabs
        must be opened via CDP create_target."""
        urls = ["https://gmail.com/inbox", "https://docs.google.com"]
        cid, launched, existing_ctx = self._setup_already_loaded(urls)

        self.mgr.restore(cid)

        # The saved tabs should have been opened via CDP (create_target)
        ctx_targets = [t for t in self.fb.targets.values()
                       if t["browserContextId"] == existing_ctx]
        ctx_urls = {t["url"] for t in ctx_targets}
        for url in urls:
            self.assertIn(url, ctx_urls,
                          f"Saved tab {url} not opened via CDP")

    def test_discover_profile_context_accepts_known_tids(self):
        """_discover_profile_context must accept known_tids parameter."""
        import inspect
        sig = inspect.signature(ContainerManager._discover_profile_context)
        self.assertIn("known_tids", sig.parameters)

    def test_url_fallback_checks_all_contexts(self):
        """URL fallback in _restore_profile must check ALL contexts, not just new."""
        import inspect
        src = inspect.getsource(ContainerManager._restore_profile)
        # The fallback should NOT filter by "ctx not in known_ctxs"
        # (old bug: it only checked new contexts, missing already-loaded profiles)
        self.assertNotIn("ctx not in known_ctxs", src)


if __name__ == "__main__":
    unittest.main()
