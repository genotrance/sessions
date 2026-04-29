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


if __name__ == "__main__":
    unittest.main()
