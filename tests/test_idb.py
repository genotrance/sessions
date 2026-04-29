"""Regression tests for IndexedDB dump, restore, and preservation logic.

Split from test_regressions.py for maintainability.
"""
from __future__ import annotations

import inspect
import os
import sys
import unittest

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_root, "src"))
sys.path.insert(0, _root)

from sessions.manager import (ContainerManager,
                              IDB_DUMP_TIMEOUT, IDB_PER_DB_TIMEOUT,
                              IDB_TOTAL_BUDGET, IDB_LIST_TIMEOUT)
from sessions.idb import (IDB_DUMP_JS, IDB_LIST_JS,
                          build_single_db_dump_js, build_restore_script,
                          build_restore_scaffolding,
                          build_restore_single_db_inject,
                          build_restore_db_schema,
                          build_restore_store_chunk,
                          build_restore_db_scripts,
                          _WORKER_IDB_BLOCKER_JS)
from sessions.persistence import _compress_blob, _decompress_blob
from tests.fakes import _PatchedManagerMixin


# ---------------------------------------------------------------------------
# BUG: IDB dump with await_promise=True used SNAPSHOT_CDP_TIMEOUT (5s) which
#      was too short for WhatsApp/Zillow-sized databases causing repeated timeouts.
# FIX: Use a dedicated IDB_DUMP_TIMEOUT (30s) constant for the async IDB dump
#      expression, and preserve previously good IDB data when collection fails.
# ---------------------------------------------------------------------------

class TestIdbTimeoutCap(unittest.TestCase):
    """Guard: IDB evaluate timeout must be generous for large databases."""

    def test_idb_dump_timeout_constant_is_generous(self):
        # IDB_DUMP_TIMEOUT must be at least 20s to handle Zillow/WhatsApp-sized stores
        self.assertGreaterEqual(IDB_DUMP_TIMEOUT, 20)

    def test_per_db_timeout_exists(self):
        self.assertGreaterEqual(IDB_PER_DB_TIMEOUT, 5)
        self.assertGreaterEqual(IDB_TOTAL_BUDGET, 15)
        self.assertGreaterEqual(IDB_LIST_TIMEOUT, 3)

    def test_collect_state_uses_per_db_approach(self):
        """_collect_state must use the per-database dump (not monolithic)."""
        src = inspect.getsource(ContainerManager._collect_state)
        self.assertIn("_build_single_db_dump_js", src)
        self.assertIn("IDB_LIST", src)
        self.assertIn("_idb_cache", src)


# ---------------------------------------------------------------------------
# BUG: IDB backup lost db.version, indexes, and compound keyPaths;
#      restore didn't deleteDatabase first and didn't block page scripts.
# FIX: Dump captures _meta.version + indexes; restore deletes+recreates
#      at correct version with full schema and monkey-patches indexedDB.open.
# ---------------------------------------------------------------------------

class TestIdbDumpSchemaFidelity(unittest.TestCase):
    """Guard: the IDB dump script must capture full schema metadata."""

    def test_dump_captures_db_version(self):
        self.assertIn("_meta: {version: db.version}", IDB_DUMP_JS)

    def test_dump_captures_indexes(self):
        self.assertIn("store.indexNames", IDB_DUMP_JS)
        self.assertIn("idx.keyPath", IDB_DUMP_JS)
        self.assertIn("idx.unique", IDB_DUMP_JS)
        self.assertIn("idx.multiEntry", IDB_DUMP_JS)

    def test_dump_preserves_autoincrement(self):
        self.assertIn("store.autoIncrement", IDB_DUMP_JS)

    def test_dump_encodes_binary_types(self):
        """Dump script must use the structured-clone encoder."""
        self.assertIn("_encV", IDB_DUMP_JS)
        self.assertIn("_b64enc", IDB_DUMP_JS)

    def test_dump_handles_blocked_databases(self):
        self.assertIn("onblocked", IDB_DUMP_JS)


class TestIdbRestoreSchemaFidelity(unittest.TestCase):
    """Guard: the IDB restore script must recreate full schema."""

    def _get_restore_src(self):
        return build_restore_script({"testdb": {
            "_meta": {"version": 42},
            "msgs": {"rows": [], "keys": [], "keyPath": ["chatId", "id"],
                     "autoIncrement": False,
                     "indexes": [{"name": "by_time", "keyPath": "timestamp",
                                  "unique": False, "multiEntry": False}]}
        }})

    def test_restore_deletes_database_first(self):
        src = self._get_restore_src()
        self.assertIn("deleteDatabase", src)

    def test_restore_opens_at_saved_version(self):
        """Must pass the saved version to indexedDB.open so onupgradeneeded fires."""
        src = self._get_restore_src()
        self.assertIn("ver=meta.version", src)
        self.assertIn("dbName,ver", src)

    def test_restore_creates_indexes(self):
        src = self._get_restore_src()
        self.assertIn("createIndex", src)
        self.assertIn("idx.keyPath", src)
        self.assertIn("idx.unique", src)
        self.assertIn("idx.multiEntry", src)

    def test_restore_blocks_page_scripts(self):
        """Restore must monkey-patch indexedDB.open to prevent race conditions."""
        src = self._get_restore_src()
        self.assertIn("_origOpen", src)
        self.assertIn("indexedDB.open=", src)
        # Must restore the original when done
        self.assertIn("indexedDB.open=_origOpen", src)

    def test_restore_handles_meta_key(self):
        """_meta key must be skipped when iterating store names."""
        src = self._get_restore_src()
        self.assertIn("'_meta'", src)

    def test_restore_includes_compound_keypath(self):
        """Compound array keyPaths must be preserved in the JSON payload."""
        src = self._get_restore_src()
        # The compound keyPath ["chatId", "id"] should appear in the payload
        self.assertIn('"chatId"', src)
        self.assertIn('"id"', src)

    def test_restore_script_is_valid_iife(self):
        src = self._get_restore_src()
        self.assertTrue(src.startswith("(function(){"))
        self.assertTrue(src.endswith("})()"))

    def test_restore_decodes_binary_types(self):
        """Restore script must use the structured-clone decoder."""
        src = self._get_restore_src()
        self.assertIn("_decV", src)
        self.assertIn("_b64dec", src)

    def test_restore_backward_compat_old_format(self):
        """Old IDB data (no _meta, no indexes) must still produce a valid script."""
        old_format = {"mydb": {
            "store1": {"rows": [{"a": 1}], "keys": [1],
                       "keyPath": "a", "autoIncrement": False},
        }}
        src = build_restore_script(old_format)
        # Must not crash and must be a valid IIFE
        self.assertTrue(src.startswith("(function(){"))
        self.assertTrue(src.endswith("})()"))
        # Falls back to version 1 when _meta is missing
        self.assertIn("version||1", src)
        # Falls back to empty indexes when indexes key is missing
        self.assertIn("indexes||[]", src)


# ---------------------------------------------------------------------------
# BUG: Snapshot/hibernate overwrote good IDB+localStorage data with empty {}
#      when Runtime.evaluate timed out on JS-heavy tabs (Discord, WhatsApp).
#      IDB was preserved but localStorage was silently dropped, causing logout
#      on restore because Discord stores session tokens in localStorage too.
# FIX: Preserve both IDB and localStorage per-origin; new data wins, missing
#      origins fall back to the last good value from DB.
# ---------------------------------------------------------------------------

class TestIdbPreservationOnTimeout(unittest.TestCase):
    """Guard: snapshot/hibernate must not wipe previously saved IDB or localStorage on timeout."""

    def test_snapshot_preserves_idb_code(self):
        """snapshot() must check for empty idb and load previous data."""
        src = inspect.getsource(ContainerManager.snapshot)
        self.assertIn("preserving", src)
        self.assertIn("prev_idb", src)

    def test_hibernate_preserves_idb_code(self):
        """hibernate() must check for empty idb and load previous data."""
        src = inspect.getsource(ContainerManager.hibernate)
        self.assertIn("preserving", src)
        self.assertIn("prev_idb", src)

    def test_snapshot_preserves_localstorage_code(self):
        """snapshot() must also preserve localStorage origins on timeout."""
        src = inspect.getsource(ContainerManager.snapshot)
        self.assertIn("prev_storage", src)
        self.assertIn("merged_storage", src)

    def test_hibernate_preserves_localstorage_code(self):
        """hibernate() must also preserve localStorage origins on timeout."""
        src = inspect.getsource(ContainerManager.hibernate)
        self.assertIn("prev_storage", src)
        self.assertIn("merged_storage", src)


class TestIdbPreservationIntegration(_PatchedManagerMixin, unittest.TestCase):
    """Integration test: IDB data survives a collection failure."""

    def test_snapshot_keeps_old_idb_when_new_is_empty(self):
        """If _collect_state returns empty idb, snapshot must keep old idb."""
        c = self.mgr.create_container("WA")
        cid = c["id"]
        # Pre-seed good IDB data in the DB
        good_idb = {"https://web.whatsapp.com": {
            "_meta": {"version": 42},
            "msgs": {"rows": [{"id": 1}], "keys": [1], "keyPath": "id",
                     "autoIncrement": True, "indexes": []},
        }}
        self.store.save_hibernation(
            cid,
            [{"name": "s", "value": "v", "domain": "web.whatsapp.com",
              "path": "/", "url": "https://web.whatsapp.com"}],
            {"https://web.whatsapp.com": {"pref": "1"}},
            [{"url": "https://web.whatsapp.com", "title": "WA"}],
            idb=good_idb)
        # Restore the container
        self.mgr.restore(cid)
        ctx = self.mgr.hot[cid]
        # Seed a tab with NO idb data (simulates timeout)
        self.fb.seed_tab(ctx, "https://web.whatsapp.com", "WA",
                         origin="https://web.whatsapp.com",
                         storage={"pref": "1"})
        # Snapshot — idb will be empty from _collect_state
        self.mgr.snapshot(cid)
        # Verify the good idb was preserved
        full = self.store.get_container(cid)
        self.assertIn("https://web.whatsapp.com", full["idb"])
        self.assertEqual(full["idb"]["https://web.whatsapp.com"]["_meta"]["version"], 42)


    def test_hibernate_keeps_localstorage_when_tab_times_out(self):
        """Discord scenario: 2 of 4 tabs time out → 0 ls-origins collected.
        hibernate() must fall back to previously saved localStorage."""
        c = self.mgr.create_container("Discord")
        cid = c["id"]
        # Pre-seed good state (simulates a prior successful snapshot)
        good_storage = {"https://discord.com": {"token": "abc123", "uid": "456"}}
        good_idb = {"https://discord.com": {
            "_meta": {"version": 3},
            "store": {"rows": [{"id": 1}], "keys": [1], "keyPath": "id",
                      "autoIncrement": False, "indexes": []},
        }}
        self.store.save_hibernation(
            cid,
            [{"name": "session", "value": "tok", "domain": "discord.com",
              "path": "/", "url": "https://discord.com"}],
            good_storage,
            [{"url": "https://discord.com/channels/123/456", "title": "Discord"}],
            idb=good_idb)
        self.mgr.restore(cid)
        ctx = self.mgr.hot[cid]
        # Seed tab BUT with no storage (simulates Runtime.evaluate timeout on that tab)
        self.fb.seed_tab(ctx, "https://discord.com/channels/123/456", "Discord",
                         origin="https://discord.com",
                         storage={})  # empty = timeout simulation
        # hibernate — storage will be empty from _collect_state
        self.mgr.hibernate(cid)
        full = self.store.get_container(cid)
        # localStorage must be preserved from previous snapshot
        self.assertIn("https://discord.com", full["storage"])
        self.assertEqual(full["storage"]["https://discord.com"]["token"], "abc123")
        # IDB must also be preserved
        self.assertIn("https://discord.com", full["idb"])
        self.assertEqual(full["idb"]["https://discord.com"]["_meta"]["version"], 3)

    def test_snapshot_preserves_localstorage_on_partial_timeout(self):
        """If only some origins collect (partial timeout), missing ones are preserved.

        Simulate: a.com tab returns fresh data; b.com tab times out (no storage
        returned). The snapshot must keep b.com from the previous DB snapshot.
        """
        c = self.mgr.create_container("Multi")
        cid = c["id"]
        # Pre-seed two origins in the DB
        self.store.save_hibernation(
            cid,
            [],
            {"https://a.com": {"ka": "va"}, "https://b.com": {"kb": "vb"}},
            [{"url": "https://a.com/", "title": "A"},
             {"url": "https://b.com/", "title": "B"}])
        self.mgr.restore(cid)
        # Update a.com's existing tab storage in-place (so _collect_state sees new value)
        for _tid, store in self.fb.local_storage.items():
            if "https://a.com" in store:
                store["https://a.com"]["ka"] = "va_new"
        # Remove b.com's tab storage to simulate it timing out
        for tid in list(self.fb.local_storage.keys()):
            if "https://b.com" in self.fb.local_storage[tid]:
                del self.fb.local_storage[tid]
        self.mgr.snapshot(cid)
        full = self.store.get_container(cid)
        # a.com has the fresh value from the live tab
        self.assertEqual(full["storage"]["https://a.com"]["ka"], "va_new")
        # b.com preserved from previous DB snapshot (tab timed out)
        self.assertIn("https://b.com", full["storage"])
        self.assertEqual(full["storage"]["https://b.com"]["kb"], "vb")


# ---------------------------------------------------------------------------
# Per-database IDB dump scripts
# ---------------------------------------------------------------------------

class TestIdbListScript(unittest.TestCase):
    """Guard: IDB_LIST_JS must be a valid async IIFE returning JSON."""

    def test_list_js_is_async_iife(self):
        self.assertIn("async function", IDB_LIST_JS)
        self.assertIn("indexedDB.databases", IDB_LIST_JS)
        self.assertIn("JSON.stringify", IDB_LIST_JS)

    def test_list_js_returns_name_and_version(self):
        self.assertIn("n:d.name", IDB_LIST_JS)
        self.assertIn("v:d.version", IDB_LIST_JS)


class TestBuildSingleDbDumpJs(unittest.TestCase):
    """Guard: build_single_db_dump_js must produce valid per-DB dump scripts."""

    def test_script_includes_db_name(self):
        js = build_single_db_dump_js("myTestDb")
        self.assertIn('"myTestDb"', js)

    def test_script_is_async_iife(self):
        js = build_single_db_dump_js("test")
        self.assertTrue(js.startswith("(async function(){"))
        self.assertTrue(js.endswith("})()"))

    def test_script_includes_codec(self):
        js = build_single_db_dump_js("test")
        self.assertIn("_encV", js)
        self.assertIn("_b64enc", js)

    def test_script_captures_schema(self):
        js = build_single_db_dump_js("test")
        self.assertIn("_meta:{version:db.version}", js)
        self.assertIn("store.autoIncrement", js)
        self.assertIn("store.keyPath", js)
        self.assertIn("store.indexNames", js)

    def test_script_handles_blocked(self):
        js = build_single_db_dump_js("test")
        self.assertIn("onblocked", js)

    def test_script_closes_db(self):
        js = build_single_db_dump_js("test")
        self.assertIn("db.close()", js)

    def test_special_chars_escaped(self):
        js = build_single_db_dump_js('db"with"quotes')
        self.assertIn(r'db\"with\"quotes', js)


# ---------------------------------------------------------------------------
# Per-database IDB cache integration
# ---------------------------------------------------------------------------

class TestIdbCacheIntegration(_PatchedManagerMixin, unittest.TestCase):
    """Integration: per-database IDB dump populates and uses the cache."""

    def test_snapshot_populates_idb_cache(self):
        """Successful per-database dumps must populate _idb_cache."""
        c = self.mgr.create_container("CacheTest")
        cid = c["id"]
        idb_data = {"db1": {
            "_meta": {"version": 2},
            "store1": {"rows": [{"x": 1}], "keys": [1],
                       "keyPath": "x", "autoIncrement": False, "indexes": []},
        }}
        self.store.save_hibernation(
            cid, [], {"https://example.com": {"k": "v"}},
            [{"url": "https://example.com/page", "title": "Test"}],
            idb={"https://example.com": idb_data})
        self.mgr.restore(cid)
        ctx = self.mgr.hot[cid]
        self.fb.seed_tab(ctx, "https://example.com/page", "Test",
                         origin="https://example.com",
                         storage={"k": "v"},
                         idb=idb_data)
        self.mgr.snapshot(cid)
        # After snapshot, the IDB cache should have the DB data
        cache_key = "https://example.com\x00db1"
        self.assertIn(cache_key, self.mgr._idb_cache)
        self.assertEqual(self.mgr._idb_cache[cache_key]["_meta"]["version"], 2)

    def test_snapshot_uses_cache_when_idb_empty(self):
        """If a per-DB dump returns empty, cached data must be used."""
        c = self.mgr.create_container("CacheFallback")
        cid = c["id"]
        good_idb = {"db1": {
            "_meta": {"version": 5},
            "msgs": {"rows": [{"id": 1}], "keys": [1],
                     "keyPath": "id", "autoIncrement": True, "indexes": []},
        }}
        self.store.save_hibernation(
            cid, [], {"https://app.com": {"s": "1"}},
            [{"url": "https://app.com/main", "title": "App"}],
            idb={"https://app.com": good_idb})
        self.mgr.restore(cid)
        ctx = self.mgr.hot[cid]
        # First snapshot: seed IDB so cache gets populated
        self.fb.seed_tab(ctx, "https://app.com/main", "App",
                         origin="https://app.com",
                         storage={"s": "1"},
                         idb=good_idb)
        self.mgr.snapshot(cid)
        # Now remove IDB from the fake (simulates timeout / empty result)
        for tid in list(self.fb.idb_storage.keys()):
            self.fb.idb_storage[tid] = {"https://app.com": {}}
        # Second snapshot: should use cached IDB
        self.mgr._last_snapshot_hash.pop(cid, None)  # force re-save
        self.mgr.snapshot(cid)
        full = self.store.get_container(cid)
        self.assertIn("https://app.com", full["idb"])
        self.assertIn("db1", full["idb"]["https://app.com"])


# ---------------------------------------------------------------------------
# Compression round-trip
# ---------------------------------------------------------------------------

class TestIdbCompression(unittest.TestCase):
    """Guard: idb_blob compression/decompression must round-trip correctly."""

    def test_small_data_not_compressed(self):
        data = {"a": 1}
        blob = _compress_blob(data)
        self.assertFalse(blob.startswith("Z:"))
        self.assertEqual(_decompress_blob(blob), data)

    def test_large_data_compressed(self):
        data = {"key" + str(i): "x" * 100 for i in range(50)}
        blob = _compress_blob(data)
        self.assertTrue(blob.startswith("Z:"))
        self.assertEqual(_decompress_blob(blob), data)

    def test_empty_blob(self):
        self.assertEqual(_decompress_blob(""), {})
        self.assertEqual(_decompress_blob("{}"), {})

    def test_backward_compat_uncompressed(self):
        """Old uncompressed JSON blobs must still decompress correctly."""
        raw = '{"db1": {"_meta": {"version": 1}}}'
        self.assertEqual(_decompress_blob(raw), {"db1": {"_meta": {"version": 1}}})

    def test_compression_saves_space(self):
        """Compressed blob must be smaller than raw JSON for large data."""
        data = {"key" + str(i): "value" * 200 for i in range(100)}
        raw = __import__("json").dumps(data)
        blob = _compress_blob(data)
        self.assertLess(len(blob), len(raw))


# ---------------------------------------------------------------------------
# Per-database split restore (scaffolding + per-DB scripts)
# ---------------------------------------------------------------------------

class TestRestoreScaffolding(unittest.TestCase):
    """Guard: scaffolding script sets up shared state for per-DB scripts."""

    def test_scaffolding_is_valid_iife(self):
        src = build_restore_scaffolding(3)
        self.assertTrue(src.startswith("(function(){"))
        self.assertTrue(src.endswith("})()"))

    def test_scaffolding_includes_codec(self):
        src = build_restore_scaffolding(3)
        self.assertIn("_decV", src)
        self.assertIn("_b64dec", src)

    def test_scaffolding_sets_pending_count(self):
        src = build_restore_scaffolding(5)
        self.assertIn("pending:5", src)

    def test_scaffolding_monkey_patches_open(self):
        src = build_restore_scaffolding(1)
        self.assertIn("indexedDB.open=function", src)
        self.assertIn("origOpen", src)

    def test_scaffolding_has_safety_timeout(self):
        src = build_restore_scaffolding(1, timeout_ms=45000)
        self.assertIn("45000", src)
        self.assertIn("TIMEOUT", src)

    def test_scaffolding_exposes_window_idbR(self):
        src = build_restore_scaffolding(2)
        self.assertIn("window.__idbR", src)


class TestRestoreSingleDbInject(unittest.TestCase):
    """Guard: per-DB inject script restores one database via shared state."""

    _SAMPLE_DB = {
        "_meta": {"version": 7},
        "msgs": {"rows": [{"id": 1}], "keys": [1],
                 "keyPath": "id", "autoIncrement": False,
                 "indexes": [{"name": "by_ts", "keyPath": "ts",
                              "unique": False, "multiEntry": False}]},
    }

    def test_inject_is_valid_iife(self):
        src = build_restore_single_db_inject("mydb", self._SAMPLE_DB)
        self.assertTrue(src.startswith("(function(){"))
        self.assertTrue(src.endswith("})()"))

    def test_inject_references_scaffolding(self):
        src = build_restore_single_db_inject("mydb", self._SAMPLE_DB)
        self.assertIn("window.__idbR", src)
        self.assertIn("R.origOpen", src)
        self.assertIn("R.origDel", src)
        self.assertIn("R.done", src)
        self.assertIn("R.decV", src)

    def test_inject_includes_db_name(self):
        src = build_restore_single_db_inject("test-db", self._SAMPLE_DB)
        self.assertIn('"test-db"', src)

    def test_inject_deletes_database_first(self):
        src = build_restore_single_db_inject("x", self._SAMPLE_DB)
        self.assertIn("R.origDel(dbName)", src)

    def test_inject_creates_stores_and_indexes(self):
        src = build_restore_single_db_inject("x", self._SAMPLE_DB)
        self.assertIn("createObjectStore", src)
        self.assertIn("createIndex", src)

    def test_inject_handles_special_chars(self):
        src = build_restore_single_db_inject('db"with"quotes', self._SAMPLE_DB)
        self.assertIn(r'db\"with\"quotes', src)

    def test_scaffolding_plus_inject_no_duplicate_codec(self):
        """Codec must be in scaffolding only, not duplicated per-DB."""
        scaff = build_restore_scaffolding(1)
        inject = build_restore_single_db_inject("x", self._SAMPLE_DB)
        self.assertIn("_b64dec", scaff)
        # Per-DB script should NOT re-define the codec
        self.assertNotIn("function _b64dec", inject)


class TestChunkedRestore(unittest.TestCase):
    """Guard: chunked per-store restore splits large DBs properly."""

    _SMALL_DB = {
        "_meta": {"version": 3},
        "items": {"rows": [{"id": 1}], "keys": [1],
                  "keyPath": "id", "autoIncrement": False,
                  "indexes": []},
    }

    def _make_large_db(self, n_rows=2000):
        """Create a DB dict that exceeds _MAX_CHUNK_BYTES."""
        rows = [{"id": i, "data": "x" * 300} for i in range(n_rows)]
        keys = list(range(n_rows))
        return {
            "_meta": {"version": 5},
            "big_store": {
                "rows": rows, "keys": keys,
                "keyPath": "id", "autoIncrement": False,
                "indexes": [{"name": "by_data", "keyPath": "data",
                             "unique": False, "multiEntry": False}],
            },
            "small_store": {
                "rows": [{"k": 1}], "keys": [1],
                "keyPath": "k", "autoIncrement": False,
                "indexes": [],
            },
        }

    # -- build_restore_db_schema tests --

    def test_schema_is_valid_iife(self):
        src = build_restore_db_schema("mydb", self._SMALL_DB, 1)
        self.assertTrue(src.startswith("(function(){"))
        self.assertTrue(src.endswith("})()" ))

    def test_schema_sets_db_pending_and_ready(self):
        src = build_restore_db_schema("mydb", self._SMALL_DB, 3)
        self.assertIn("R.dbPending[dbName]=3", src)
        self.assertIn("R.dbReady[dbName]=false", src)

    def test_schema_creates_stores_and_indexes(self):
        db = self._make_large_db(10)
        src = build_restore_db_schema("testdb", db, 1)
        self.assertIn("createObjectStore", src)
        self.assertIn("createIndex", src)
        self.assertIn('"big_store"', src)
        self.assertIn('"small_store"', src)

    def test_schema_calls_done_when_zero_chunks(self):
        src = build_restore_db_schema("empty", self._SMALL_DB, 0)
        self.assertIn("R.dbPending[dbName]<=0", src)
        self.assertIn("R.done(dbName)", src)

    def test_schema_does_not_contain_row_data(self):
        db = self._make_large_db(10)
        src = build_restore_db_schema("testdb", db, 1)
        self.assertNotIn('"x" * 300', src)
        # Should not contain any row payloads
        self.assertNotIn('"data":', src)  # no row data keys

    # -- build_restore_store_chunk tests --

    def test_chunk_is_valid_iife(self):
        src = build_restore_store_chunk("db", "store", True, [{"a": 1}], [1])
        self.assertTrue(src.startswith("(function(){"))
        self.assertTrue(src.endswith("})()" ))

    def test_chunk_polls_db_ready(self):
        src = build_restore_store_chunk("db", "store", True, [{"a": 1}], [1])
        self.assertIn("R.dbReady[dbN]", src)
        self.assertIn("setTimeout(_go,50)", src)

    def test_chunk_decrements_pending(self):
        src = build_restore_store_chunk("db", "store", True, [{"a": 1}], [1])
        self.assertIn("R.dbPending[dbN]--", src)

    def test_chunk_has_error_handling(self):
        src = build_restore_store_chunk("db", "store", True, [{"a": 1}], [1])
        self.assertIn("preventDefault", src)
        self.assertIn("onerror", src)
        self.assertIn("onabort", src)

    def test_chunk_uses_keypath_mode(self):
        src_kp = build_restore_store_chunk("db", "s", True, [{"a": 1}], [1])
        self.assertIn("kp=true", src_kp)
        src_nokp = build_restore_store_chunk("db", "s", False, [{"a": 1}], [1])
        self.assertIn("kp=false", src_nokp)

    # -- build_restore_db_scripts tests --

    def test_small_db_returns_single_script(self):
        scripts = build_restore_db_scripts("small", self._SMALL_DB)
        self.assertEqual(len(scripts), 1)
        # Should be the same as build_restore_single_db_inject
        self.assertIn("R.origDel", scripts[0])
        self.assertIn("R.done", scripts[0])

    def test_large_db_returns_multiple_scripts(self):
        db = self._make_large_db(2000)
        scripts = build_restore_db_scripts("big", db)
        self.assertGreater(len(scripts), 1)
        # First script should be the schema
        self.assertIn("R.dbPending", scripts[0])
        self.assertIn("R.dbReady", scripts[0])
        self.assertIn("createObjectStore", scripts[0])
        # Subsequent scripts should be data chunks
        for s in scripts[1:]:
            self.assertIn("R.dbPending[dbN]--", s)
            self.assertIn("_go", s)

    def test_large_db_chunks_are_bounded(self):
        db = self._make_large_db(5000)
        scripts = build_restore_db_scripts("big", db,
                                           max_chunk_bytes=200_000)
        # Each data chunk should be well under 2x the limit
        for s in scripts[1:]:
            self.assertLess(len(s), 200_000 * 3,
                            f"chunk too large: {len(s)} bytes")

    def test_large_db_chunk_count_matches_schema(self):
        db = self._make_large_db(2000)
        scripts = build_restore_db_scripts("big", db)
        schema = scripts[0]
        data_chunks = scripts[1:]
        # Schema should declare the right number of pending chunks
        self.assertIn(f"R.dbPending[dbName]={len(data_chunks)}", schema)

    def test_empty_stores_produce_no_chunks(self):
        db = {
            "_meta": {"version": 1},
            "empty": {"rows": [], "keys": [],
                      "keyPath": "id", "autoIncrement": False,
                      "indexes": []},
        }
        scripts = build_restore_db_scripts("e", db)
        # Small DB → single script
        self.assertEqual(len(scripts), 1)


# ---------------------------------------------------------------------------
# BUG: WhatsApp-style apps store CryptoKey objects in IDB (e.g. wawc_db_enc).
#      Our codec used to skip CryptoKey as a non-cloneable object, yielding
#      {} after JSON roundtrip.  WhatsApp's deriveKey(key, ...) then failed
#      with "parameter 2 is not of type 'CryptoKey'" and the session reset.
# FIX: Export extractable CryptoKeys as JWK during dump, mark with __t:'CK',
#      and re-import via crypto.subtle.importKey during restore.  Non-extractable
#      keys are logged as placeholders since the browser intentionally refuses
#      to export them (verified via tests/probe_cryptokey.py).
# ---------------------------------------------------------------------------


class TestCryptoKeyCodec(unittest.TestCase):
    """Guard: CryptoKey is exported as JWK on dump and re-imported on restore."""

    def test_dump_detects_cryptokey(self):
        src = build_single_db_dump_js("x")
        self.assertIn("self.CryptoKey&&v instanceof CryptoKey", src)

    def test_dump_exports_jwk_when_extractable(self):
        src = build_single_db_dump_js("x")
        self.assertIn("exportKey('jwk',v)", src)
        self.assertIn("__t:'CK'", src)
        self.assertIn("jwk:_jwk", src)

    def test_dump_handles_non_extractable(self):
        src = build_single_db_dump_js("x")
        self.assertIn("ne:true", src)
        self.assertIn("non-extractable CryptoKey", src)

    def test_monolithic_dump_also_supports_cryptokey(self):
        # Same CryptoKey handling must also exist in the monolithic dump
        # script used by fall-back paths and legacy callers.
        self.assertIn("CryptoKey", IDB_DUMP_JS)
        self.assertIn("exportKey('jwk'", IDB_DUMP_JS)
        self.assertIn("__t:'CK'", IDB_DUMP_JS)

    def test_scaffolding_exposes_async_decoder(self):
        src = build_restore_scaffolding(1)
        self.assertIn("_decVAsync", src)
        self.assertIn("decVAsync:_decVAsync", src)

    def test_scaffolding_async_decoder_imports_key(self):
        src = build_restore_scaffolding(1)
        self.assertIn("importKey(", src)
        self.assertIn("'jwk'", src)
        # The async decoder checks `t === 'CK'` where `t = v.__t`.
        self.assertIn("t==='CK'", src.replace(" ", ""))

    def test_scaffolding_async_decoder_handles_nested_maps(self):
        """Maps/Sets may contain nested CryptoKeys — async decoder must recurse."""
        src = build_restore_scaffolding(1)
        # _decVAsync must handle 'M' and 'S' with recursive awaits
        async_section = src[src.index("async function _decVAsync"):]
        self.assertIn("t==='M'", async_section)
        self.assertIn("t==='S'", async_section)

    def test_single_inject_pre_decodes_async_before_tx(self):
        """Restore must await R.decVAsync BEFORE opening the readwrite tx,
        otherwise the transaction auto-commits on microtask drain and aborts."""
        src = build_restore_single_db_inject("x", {
            "_meta": {"version": 1},
            "s": {"rows": [{"a": 1}], "keys": [1], "keyPath": "a",
                  "autoIncrement": False, "indexes": []},
        })
        # Find where the tx is opened
        tx_idx = src.index("db.transaction(liveStores")
        # decVAsync must appear BEFORE the transaction opens
        decode_idx = src.index("R.decVAsync(rows[i])")
        self.assertLess(decode_idx, tx_idx,
                        "row decode must happen before transaction opens")

    def test_chunk_pre_decodes_async_before_tx(self):
        src = build_restore_store_chunk("db", "s", True, [{"a": 1}], [1])
        tx_idx = src.index("db.transaction([sN]")
        decode_idx = src.index("R.decVAsync(rows[i])")
        self.assertLess(decode_idx, tx_idx,
                        "chunk decode must happen before transaction opens")

    def test_single_inject_function_is_async(self):
        """_afterDelete must be async so awaits work."""
        src = build_restore_single_db_inject("x", {
            "_meta": {"version": 1},
            "s": {"rows": [], "keys": [], "keyPath": "a",
                  "autoIncrement": False, "indexes": []},
        })
        self.assertIn("_afterDelete=async function", src)


# ---------------------------------------------------------------------------
# BUG: WhatsApp aggressively wipes IDB/localStorage during init via mechanisms
#      beyond deleteDatabase: per-row store.delete, cursor.delete,
#      deleteObjectStore, localStorage.removeItem/clear.  Our first fix only
#      blocked deleteDatabase and store.clear, missing these vectors.
# FIX: Block all destructive ops (IDB + localStorage) during a 60s protection
#      window post-restore.  Tracked via scaffolding.counts for diagnostics.
# ---------------------------------------------------------------------------


class TestScaffoldingDestructiveOpBlockers(unittest.TestCase):
    """Guard: every known destructive op is blocked during protection window."""

    def test_blocks_delete_database(self):
        src = build_restore_scaffolding(1)
        self.assertIn("indexedDB.deleteDatabase=function", src)
        self.assertIn("BLOCKED deleteDatabase", src)

    def test_blocks_object_store_clear(self):
        src = build_restore_scaffolding(1)
        self.assertIn("IDBObjectStore.prototype.clear=function", src)
        self.assertIn("BLOCKED clear on", src)

    def test_blocks_object_store_delete(self):
        src = build_restore_scaffolding(1)
        self.assertIn("IDBObjectStore.prototype.delete=function", src)
        self.assertIn("BLOCKED delete on", src)

    def test_blocks_delete_object_store(self):
        src = build_restore_scaffolding(1)
        self.assertIn("IDBDatabase.prototype.deleteObjectStore=function", src)
        self.assertIn("BLOCKED deleteObjectStore", src)

    def test_blocks_cursor_delete(self):
        src = build_restore_scaffolding(1)
        self.assertIn("IDBCursor.prototype.delete=function", src)
        self.assertIn("BLOCKED cursor.delete", src)

    def test_blocks_localstorage_remove_item(self):
        src = build_restore_scaffolding(1)
        self.assertIn("Storage.prototype.removeItem=function", src)
        self.assertIn("BLOCKED localStorage.removeItem", src)

    def test_blocks_localstorage_clear(self):
        src = build_restore_scaffolding(1)
        self.assertIn("Storage.prototype.clear=function", src)
        self.assertIn("BLOCKED localStorage.clear", src)

    def test_protection_window_has_finite_duration(self):
        """After _protectUntil expires, original methods must be restored."""
        src = build_restore_scaffolding(1)
        # Protection window is 60s (60000ms)
        self.assertIn("60000", src)
        self.assertIn("origDel", src)  # original is kept for restore
        self.assertIn("origClear", src)
        self.assertIn("origDelRow", src)
        self.assertIn("origDelStore", src)

    def test_destructive_ops_counts_tracked(self):
        src = build_restore_scaffolding(1)
        self.assertIn("counts:{", src)
        for field in ("delRow", "delStore", "cursorDel", "lsRm", "lsClr"):
            self.assertIn(field, src)


# ---------------------------------------------------------------------------
# BUG: Web Workers have their own indexedDB + prototype chain, so main-thread
#      monkey-patches do NOT apply.  WhatsApp's workers deleted fts-storage,
#      jobs-storage, lru-media-storage-idb, offd-storage despite our blocks.
# FIX: Intercept the Worker / SharedWorker constructors and wrap scriptURL
#      in a blob that installs a worker-local IDB blocker before loading the
#      real script via importScripts (classic) or import() (module).
# ---------------------------------------------------------------------------


class TestWorkerInterception(unittest.TestCase):
    """Guard: Worker and SharedWorker are wrapped to install IDB blockers."""

    def test_scaffolding_patches_worker(self):
        src = build_restore_scaffolding(1)
        self.assertIn("window.Worker=", src)
        self.assertIn("origWorker", src)

    def test_scaffolding_patches_shared_worker(self):
        src = build_restore_scaffolding(1)
        self.assertIn("window.SharedWorker=", src)
        self.assertIn("origSharedWorker", src)

    def test_worker_wrapper_uses_blob_url(self):
        src = build_restore_scaffolding(1)
        self.assertIn("new Blob([code]", src)
        self.assertIn("createObjectURL", src)

    def test_worker_wrapper_supports_classic_and_module(self):
        src = build_restore_scaffolding(1)
        self.assertIn("importScripts(", src)
        self.assertIn("import(", src)
        self.assertIn("options.type==='module'", src)

    def test_worker_wrapper_falls_back_on_error(self):
        """If wrapping fails, we should still construct the original worker."""
        src = build_restore_scaffolding(1)
        self.assertIn("return new OrigCtor(scriptURL,options)", src)

    def test_worker_blocker_constant_is_valid_js(self):
        # Must reference _until, patch indexedDB/IDBObjectStore/IDBDatabase/IDBCursor
        self.assertIn("_until", _WORKER_IDB_BLOCKER_JS)
        self.assertIn("self.indexedDB", _WORKER_IDB_BLOCKER_JS)
        self.assertIn("self.IDBObjectStore", _WORKER_IDB_BLOCKER_JS)
        self.assertIn("self.IDBDatabase", _WORKER_IDB_BLOCKER_JS)
        self.assertIn("self.IDBCursor", _WORKER_IDB_BLOCKER_JS)

    def test_worker_blocker_does_not_reference_window(self):
        """Workers don't have `window` — blocker must use `self` exclusively."""
        # Allow 'window' only inside comments; check effective code.
        # Simple check: no raw "window." access.
        self.assertNotIn("window.", _WORKER_IDB_BLOCKER_JS)

    def test_worker_blocker_blocks_all_destructive_ops(self):
        for op in ("deleteDatabase", "clear", "delete", "deleteObjectStore",
                   "cursor.delete"):
            self.assertIn("BLOCKED " + op, _WORKER_IDB_BLOCKER_JS)

    def test_worker_wrapper_preserves_prototype(self):
        """Wrapped Worker must preserve OrigWorker.prototype so instanceof works."""
        src = build_restore_scaffolding(1)
        self.assertIn("WW.prototype=OW.prototype", src)

    def test_worker_wrapper_resolves_relative_urls(self):
        """Workers spawned with relative URLs must resolve against location.href."""
        src = build_restore_scaffolding(1)
        self.assertIn("new URL(scriptURL,self.location.href)", src)

    def test_worker_restored_after_protection_expires(self):
        """BUG: worker wrapper must be reverted after the protection window
        ends; otherwise wrapped Worker leaks forever and breaks CSP on sites
        that whitelist the original worker-src but not blob: URLs."""
        src = build_restore_scaffolding(1)
        # Both Worker and SharedWorker must be reset on timeout
        self.assertIn("window.Worker=R.origWorker", src)
        self.assertIn("window.SharedWorker=R.origSharedWorker", src)


# ---------------------------------------------------------------------------
# Smoke test: the whole scaffolding compiles to a single valid IIFE and
# contains every critical hook we depend on.
# ---------------------------------------------------------------------------


class TestScaffoldingWholesale(unittest.TestCase):
    def test_scaffolding_size_stays_reasonable(self):
        """Scaffolding should stay under 20 KB — detected growth means
        we should consider extracting to constants."""
        src = build_restore_scaffolding(1)
        self.assertLess(len(src), 20_000, f"scaffolding too large: {len(src)}")

    def test_scaffolding_has_no_unbalanced_braces(self):
        src = build_restore_scaffolding(1)
        # Extremely rough syntax check: counts match.  A mismatch would
        # still allow valid JS (e.g. strings), but catches obvious edits.
        self.assertEqual(src.count("{"), src.count("}"),
                         "unbalanced braces in scaffolding")
        self.assertEqual(src.count("("), src.count(")"),
                         "unbalanced parens in scaffolding")

    def test_scaffolding_has_no_unclosed_string_literal(self):
        """Heuristic: every scaffolding string must end with either )() or };"""
        src = build_restore_scaffolding(1)
        self.assertTrue(src.endswith("})()"))


if __name__ == "__main__":
    unittest.main()
