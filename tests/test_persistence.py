"""Tests for PersistenceManager."""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_root, "src"))
sys.path.insert(0, _root)

from sessions.persistence import PersistenceManager


# ---------------------------------------------------------------------------
# PersistenceManager tests
# ---------------------------------------------------------------------------

class TestPersistence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ctxd-p-")
        self.pm = PersistenceManager(os.path.join(self.tmp, "s.db"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_slugify(self):
        self.assertEqual(PersistenceManager.slugify("Work GitHub!"), "work-github")
        self.assertTrue(PersistenceManager.slugify("   ").startswith("ctr-"))

    def test_create_lists_get(self):
        c = self.pm.create_container("Work GitHub", color="#ff0")
        self.assertEqual(c["name"], "Work GitHub")
        self.assertEqual(c["color"], "#ff0")
        listing = self.pm.list_containers()
        self.assertEqual(len(listing), 1)
        self.assertEqual(listing[0]["id"], c["id"])
        full = self.pm.get_container(c["id"])
        self.assertEqual(full["cookies"], [])
        self.assertEqual(full["storage"], {})
        self.assertEqual(full["tabs"], [])

    def test_duplicate_name_creates_unique_id(self):
        a = self.pm.create_container("Dup")
        b = self.pm.create_container("Dup")
        self.assertNotEqual(a["id"], b["id"])

    def test_save_and_retrieve_hibernation(self):
        c = self.pm.create_container("LinkedIn")
        cookies = [{"name": "sess", "value": "abc", "domain": "linkedin.com"}]
        storage = {"https://linkedin.com": {"k": "v"}}
        tabs = [{"url": "https://linkedin.com/feed", "title": "Feed"},
                {"url": "https://linkedin.com/jobs", "title": "Jobs"}]
        self.pm.save_hibernation(c["id"], cookies, storage, tabs)
        full = self.pm.get_container(c["id"])
        self.assertEqual(full["cookies"], cookies)
        self.assertEqual(full["storage"], storage)
        self.assertEqual([t["url"] for t in full["tabs"]],
                         ["https://linkedin.com/feed", "https://linkedin.com/jobs"])
        self.assertEqual(full["is_active"], 0)

    def test_clean_preserves_tabs_but_wipes_secrets(self):
        c = self.pm.create_container("X")
        self.pm.save_hibernation(c["id"],
                                 [{"name": "s", "value": "v", "domain": "x.com"}],
                                 {"https://x.com": {"k": "1"}},
                                 [{"url": "https://x.com/", "title": "X"}])
        self.pm.clean_container(c["id"])
        full = self.pm.get_container(c["id"])
        self.assertEqual(full["cookies"], [])
        self.assertEqual(full["storage"], {})
        self.assertEqual(len(full["tabs"]), 1)

    def test_clone_copies_cookies_storage_and_tabs(self):
        src = self.pm.create_container("Origin")
        self.pm.save_hibernation(src["id"],
                                 [{"name": "a", "value": "b", "domain": "z.com"}],
                                 {"https://z.com": {"a": "b"}},
                                 [{"url": "https://z.com/", "title": "Z"}])
        clone = self.pm.clone_container(src["id"], "Clone Of Origin")
        self.assertNotEqual(clone["id"], src["id"])
        full = self.pm.get_container(clone["id"])
        self.assertEqual(len(full["cookies"]), 1)
        self.assertEqual(full["storage"], {"https://z.com": {"a": "b"}})
        self.assertEqual(full["tabs"][0]["url"], "https://z.com/")

    def test_delete_cascades_tabs(self):
        c = self.pm.create_container("Y")
        self.pm.save_hibernation(c["id"], [], {},
                                 [{"url": "https://y.com", "title": "Y"}])
        self.pm.delete_container(c["id"])
        self.assertIsNone(self.pm.get_container(c["id"]))
        con = sqlite3.connect(self.pm.db_path)
        n = con.execute("SELECT COUNT(*) FROM container_tabs").fetchone()[0]
        con.close()
        self.assertEqual(n, 0)


# ---------------------------------------------------------------------------
# Persistence tests for new features
# ---------------------------------------------------------------------------

class TestPersistenceNew(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ctxd-pn-")
        self.pm = PersistenceManager(os.path.join(self.tmp, "s.db"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_rename_container(self):
        c = self.pm.create_container("Old Name")
        self.pm.rename_container(c["id"], "New Name")
        full = self.pm.get_container(c["id"])
        self.assertEqual(full["name"], "New Name")

    def test_save_hibernation_keep_active(self):
        c = self.pm.create_container("KA")
        self.pm.save_hibernation(c["id"], [], {}, [], keep_active=True)
        full = self.pm.get_container(c["id"])
        self.assertEqual(full["is_active"], 1)

    def test_save_hibernation_default_inactive(self):
        c = self.pm.create_container("DI")
        self.pm.mark_active(c["id"], True)
        self.pm.save_hibernation(c["id"], [], {}, [])
        full = self.pm.get_container(c["id"])
        self.assertEqual(full["is_active"], 0)

    def test_reset_all_active(self):
        a = self.pm.create_container("A")
        b = self.pm.create_container("B")
        self.pm.mark_active(a["id"], True)
        self.pm.mark_active(b["id"], True)
        self.pm.reset_all_active()
        self.assertEqual(self.pm.get_container(a["id"])["is_active"], 0)
        self.assertEqual(self.pm.get_container(b["id"])["is_active"], 0)

    def test_delete_tab(self):
        c = self.pm.create_container("DT")
        self.pm.save_hibernation(c["id"], [], {},
                                  [{"url": "https://a.com", "title": "A"},
                                   {"url": "https://b.com", "title": "B"},
                                   {"url": "https://a.com", "title": "A dup"}])
        self.assertTrue(self.pm.delete_tab(c["id"], "https://a.com"))
        tabs = self.pm.get_container(c["id"])["tabs"]
        urls = [t["url"] for t in tabs]
        self.assertEqual(urls, ["https://b.com", "https://a.com"])
        self.assertFalse(self.pm.delete_tab(c["id"], "https://nope.com"))


# ---------------------------------------------------------------------------
# Session type + profile directory tests
# ---------------------------------------------------------------------------

class TestSessionType(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ctxd-st-")
        self.pm = PersistenceManager(os.path.join(self.tmp, "s.db"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_default_session_type_is_context(self):
        c = self.pm.create_container("Test")
        full = self.pm.get_container(c["id"])
        self.assertEqual(full["session_type"], "context")
        self.assertIsNone(full["profile_dir"])

    def test_create_profile_session(self):
        c = self.pm.create_container("Gmail", session_type="profile",
                                     profile_dir="sessions-gmail")
        full = self.pm.get_container(c["id"])
        self.assertEqual(full["session_type"], "profile")
        self.assertEqual(full["profile_dir"], "sessions-gmail")

    def test_list_containers_includes_session_type(self):
        self.pm.create_container("A")
        self.pm.create_container("B", session_type="profile",
                                 profile_dir="sessions-b")
        listing = self.pm.list_containers()
        types = {r["id"]: r["session_type"] for r in listing}
        dirs = {r["id"]: r["profile_dir"] for r in listing}
        self.assertEqual(types["a"], "context")
        self.assertIsNone(dirs["a"])
        self.assertEqual(types["b"], "profile")
        self.assertEqual(dirs["b"], "sessions-b")

    def test_migration_adds_columns_to_old_db(self):
        """Simulate an old DB without session_type/profile_dir columns."""
        db_path = os.path.join(self.tmp, "old.db")
        conn = sqlite3.connect(db_path)
        conn.execute("""CREATE TABLE containers (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, color TEXT DEFAULT '#3b82f6',
            cookies_blob TEXT DEFAULT '[]', storage_blob TEXT DEFAULT '{}',
            idb_blob TEXT DEFAULT '{}', is_active INTEGER DEFAULT 0,
            created_at INTEGER DEFAULT (strftime('%s','now')),
            last_accessed_at INTEGER DEFAULT 0
        )""")
        conn.execute("INSERT INTO containers(id, name) VALUES ('old', 'Old')")
        conn.execute("""CREATE TABLE container_tabs (
            container_id TEXT NOT NULL, url TEXT NOT NULL,
            title TEXT DEFAULT '', last_scrolled INTEGER DEFAULT 0,
            FOREIGN KEY(container_id) REFERENCES containers(id) ON DELETE CASCADE
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tabs_container ON container_tabs(container_id)")
        conn.commit()
        conn.close()
        pm2 = PersistenceManager(db_path)
        full = pm2.get_container("old")
        self.assertEqual(full["session_type"], "context")
        self.assertIsNone(full["profile_dir"])


class TestProfileDirHelpers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ctxd-pd-")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_profile_dir_name(self):
        from sessions.cdp import profile_dir_name
        self.assertEqual(profile_dir_name("gmail"), "sessions-gmail")

    def test_create_minimal_profile(self):
        """create_profile_dir writes only a minimal Preferences file."""
        from sessions.cdp import create_profile_dir
        import json as _json
        path = create_profile_dir(self.tmp, "test-session")
        self.assertTrue(os.path.isdir(path))
        prefs_path = os.path.join(path, "Preferences")
        self.assertTrue(os.path.isfile(prefs_path))
        with open(prefs_path) as f:
            prefs = _json.load(f)
        self.assertEqual(prefs["session"]["restore_on_startup"], 1)
        self.assertEqual(prefs["profile"]["name"], "test-session")
        # Should NOT contain cloned databases, caches, etc.
        self.assertFalse(os.path.exists(os.path.join(path, "Secure Preferences")))
        self.assertFalse(os.path.isdir(os.path.join(path, "Extensions")))

    def test_create_registers_in_local_state(self):
        from sessions.cdp import create_profile_dir, profile_dir_name
        import json as _json
        create_profile_dir(self.tmp, "reg-test")
        ls_path = os.path.join(self.tmp, "Local State")
        self.assertTrue(os.path.isfile(ls_path))
        with open(ls_path) as f:
            state = _json.load(f)
        prof_dir = profile_dir_name("reg-test")
        self.assertIn(prof_dir, state["profile"]["info_cache"])
        self.assertIn(prof_dir, state["profile"]["profiles_order"])
        self.assertEqual(state["profile"]["info_cache"][prof_dir]["name"],
                         "reg-test")

    def test_delete_cleans_local_state(self):
        from sessions.cdp import (create_profile_dir, delete_profile_dir,
                                   profile_dir_name)
        import json as _json
        # Create a fake Local State with our profile registered
        prof_dir = profile_dir_name("rm-test")
        local_state = {
            "profile": {
                "info_cache": {
                    "Default": {"name": "Person 1"},
                    prof_dir: {"name": "rm-test"},
                },
                "profiles_order": ["Default", prof_dir],
                "last_active_profiles": [prof_dir],
            }
        }
        with open(os.path.join(self.tmp, "Local State"), "w") as f:
            _json.dump(local_state, f)
        create_profile_dir(self.tmp, "rm-test")
        self.assertTrue(delete_profile_dir(self.tmp, "rm-test"))
        # Local State should no longer reference the profile
        with open(os.path.join(self.tmp, "Local State")) as f:
            state = _json.load(f)
        self.assertNotIn(prof_dir, state["profile"]["info_cache"])
        self.assertNotIn(prof_dir, state["profile"]["profiles_order"])
        self.assertNotIn(prof_dir, state["profile"]["last_active_profiles"])
        # Default should still be there
        self.assertIn("Default", state["profile"]["info_cache"])

    def test_create_and_delete_profile_dir(self):
        from sessions.cdp import create_profile_dir, delete_profile_dir
        import json as _json
        path = create_profile_dir(self.tmp, "test-session")
        self.assertTrue(os.path.isdir(path))
        prefs_path = os.path.join(path, "Preferences")
        self.assertTrue(os.path.isfile(prefs_path))
        with open(prefs_path) as f:
            prefs = _json.load(f)
        self.assertEqual(prefs["session"]["restore_on_startup"], 1)
        self.assertTrue(delete_profile_dir(self.tmp, "test-session"))
        self.assertFalse(os.path.isdir(path))

    def test_delete_nonexistent_profile_dir(self):
        from sessions.cdp import delete_profile_dir
        self.assertFalse(delete_profile_dir(self.tmp, "nope"))

    def test_create_does_not_overwrite_existing_prefs(self):
        from sessions.cdp import create_profile_dir
        import json as _json
        path = create_profile_dir(self.tmp, "keep")
        prefs_path = os.path.join(path, "Preferences")
        with open(prefs_path, "w") as f:
            _json.dump({"custom": True}, f)
        create_profile_dir(self.tmp, "keep")
        with open(prefs_path) as f:
            prefs = _json.load(f)
        self.assertTrue(prefs["custom"])

    def test_create_existing_registers_in_local_state(self):
        """Even for an existing profile, registration is ensured."""
        from sessions.cdp import create_profile_dir, profile_dir_name
        import json as _json
        create_profile_dir(self.tmp, "exists")
        # Call again — should still register
        create_profile_dir(self.tmp, "exists")
        ls_path = os.path.join(self.tmp, "Local State")
        with open(ls_path) as f:
            state = _json.load(f)
        prof_dir = profile_dir_name("exists")
        self.assertIn(prof_dir, state["profile"]["info_cache"])

    def test_save_and_load_profile_tabs(self):
        from sessions.cdp import (create_profile_dir, save_profile_tabs,
                                   load_profile_tabs)
        create_profile_dir(self.tmp, "tabs-test")
        tabs = [{"url": "https://a.com", "title": "A", "window_id": 1},
                {"url": "https://b.com", "title": "B", "window_id": 2}]
        save_profile_tabs(self.tmp, "tabs-test", tabs)
        loaded = load_profile_tabs(self.tmp, "tabs-test")
        self.assertEqual(loaded, tabs)

    def test_load_profile_tabs_missing(self):
        from sessions.cdp import load_profile_tabs
        self.assertEqual(load_profile_tabs(self.tmp, "nope"), [])

    def test_load_profile_tabs_corrupt(self):
        from sessions.cdp import create_profile_dir, load_profile_tabs
        create_profile_dir(self.tmp, "corrupt")
        path = os.path.join(self.tmp, "sessions-corrupt", "sessions_tabs.json")
        with open(path, "w") as f:
            f.write("not json{{{")
        self.assertEqual(load_profile_tabs(self.tmp, "corrupt"), [])

    def test_create_sets_exit_type_normal(self):
        """New profiles must have exit_type=Normal so Chrome doesn't show
        the 'not shut down correctly' recovery prompt."""
        from sessions.cdp import create_profile_dir
        import json as _json
        path = create_profile_dir(self.tmp, "exit-test")
        with open(os.path.join(path, "Preferences")) as f:
            prefs = _json.load(f)
        self.assertEqual(prefs["profile"]["exit_type"], "Normal")
        self.assertTrue(prefs["profile"]["exited_cleanly"])

    def test_cleanup_stale_profiles(self):
        """cleanup_stale_profiles removes entries for missing profile dirs."""
        from sessions.cdp import (cleanup_stale_profiles, create_profile_dir,
                                   profile_dir_name)
        import json as _json
        # Create two profiles
        create_profile_dir(self.tmp, "keep-me")
        create_profile_dir(self.tmp, "remove-me")
        # Delete one on disk only (not via delete_profile_dir, to simulate
        # a crash where the Local State wasn't updated)
        import shutil
        shutil.rmtree(os.path.join(self.tmp, profile_dir_name("remove-me")))
        # Run cleanup
        cleanup_stale_profiles(self.tmp)
        with open(os.path.join(self.tmp, "Local State")) as f:
            state = _json.load(f)
        self.assertIn(profile_dir_name("keep-me"),
                      state["profile"]["info_cache"])
        self.assertNotIn(profile_dir_name("remove-me"),
                         state["profile"]["info_cache"])
        self.assertNotIn(profile_dir_name("remove-me"),
                         state["profile"]["profiles_order"])

    def test_cleanup_stale_preserves_non_sessions_profiles(self):
        """cleanup_stale_profiles must not remove non-sessions entries."""
        import json as _json
        local_state = {
            "profile": {
                "info_cache": {
                    "Default": {"name": "Person 1"},
                    "Profile 1": {"name": "Work"},
                },
                "profiles_order": ["Default", "Profile 1"],
            }
        }
        with open(os.path.join(self.tmp, "Local State"), "w") as f:
            _json.dump(local_state, f)
        from sessions.cdp import cleanup_stale_profiles
        cleanup_stale_profiles(self.tmp)
        with open(os.path.join(self.tmp, "Local State")) as f:
            state = _json.load(f)
        self.assertIn("Default", state["profile"]["info_cache"])
        self.assertIn("Profile 1", state["profile"]["info_cache"])


if __name__ == "__main__":
    unittest.main()
