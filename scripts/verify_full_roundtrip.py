"""End-to-end verification of COMPLETE session state roundtrip with a real Chrome.

Usage:
    python scripts/verify_full_roundtrip.py [--port 9222]

Requires: A running Chrome with remote debugging enabled on the given port.

This script exercises the **entire** snapshot-restore pipeline exactly as it
works in production:

  1. Creates a browser context via browser-level CDP
  2. Opens a tab, seeds cookies + localStorage + IndexedDB (diverse types)
  3. Uses the real _collect_state path to snapshot all state
  4. Disposes the context (simulating hibernate — all browser state wiped)
  5. Creates a new context and calls _open_tab_with_storage (the real restore)
  6. Waits for the page to load and the restore scripts to run
  7. Verifies cookies, localStorage, and IndexedDB are ALL present and correct

This covers the exact scenario that kept failing for Discord/WhatsApp: multiple
storage mechanisms must ALL survive the hibernate-restore cycle.

Exits 0 if all checks pass; 1 on any failure.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_root, "src"))

from sessions.cdp import CDPSession
from sessions.idb import IDB_DUMP_JS, build_restore_script
from sessions.manager import ContainerManager, _origin_of, SNAPSHOT_CDP_TIMEOUT
from sessions.persistence import PersistenceManager

# ---------------------------------------------------------------------------
# JS payloads
# ---------------------------------------------------------------------------

SEED_LS_JS = r"""
(function(){
  localStorage.setItem('auth_token', 'tok_abc123xyz');
  localStorage.setItem('theme', 'dark');
  localStorage.setItem('locale', 'en-US');
  localStorage.setItem('json_value', JSON.stringify({nested: true, n: 42}));
  return JSON.stringify(Object.fromEntries(Object.entries(localStorage)));
})()
"""

SEED_IDB_JS = r"""
(async function(){
  // Create a database resembling Discord/WhatsApp storage
  await new Promise((res)=>{
    const r=indexedDB.deleteDatabase('app_session_db');
    r.onsuccess=r.onerror=()=>res();
  });
  const db = await new Promise((res,rej)=>{
    const r=indexedDB.open('app_session_db', 3);
    r.onupgradeneeded = (e)=>{
      const d=e.target.result;
      const msgs=d.createObjectStore('messages',{keyPath:['chatId','msgId']});
      msgs.createIndex('by_ts','ts',{unique:false});
      msgs.createIndex('by_tags','tags',{unique:false,multiEntry:true});
      d.createObjectStore('blobs',{autoIncrement:true});
      d.createObjectStore('kv');
    };
    r.onsuccess=()=>res(r.result);
    r.onerror=()=>rej(r.error);
  });
  const tx=db.transaction(['messages','blobs','kv'],'readwrite');
  tx.objectStore('messages').put({
    chatId:'c1', msgId:'m1',
    ts: new Date(1700000000000),
    tags: ['important','unread'],
    body: 'hello world',
    attachment: new Uint8Array([10,20,30,40,50]).buffer
  });
  tx.objectStore('messages').put({
    chatId:'c2', msgId:'m2',
    ts: new Date(1700000001000),
    tags: ['unread'],
    body: 'another message',
    attachment: new ArrayBuffer(0)
  });
  tx.objectStore('blobs').put(new Uint8Array([99,100,101]));
  tx.objectStore('kv').put({user:'alice',pref:'dark'},'session_info');
  await new Promise((res,rej)=>{tx.oncomplete=res;tx.onerror=()=>rej(tx.error);});
  db.close();
  return 'ok';
})()
"""

VERIFY_LS_JS = r"""
(function(){
  var items = {};
  for(var i=0;i<localStorage.length;i++){
    var k=localStorage.key(i);
    items[k]=localStorage.getItem(k);
  }
  return JSON.stringify(items);
})()
"""

VERIFY_IDB_JS = r"""
(async function(){
  try {
    const db = await new Promise((res,rej)=>{
      const r=indexedDB.open('app_session_db');
      r.onsuccess=()=>res(r.result);r.onerror=()=>rej(r.error);
    });
    var errors = [];
    if(db.version !== 3) errors.push('version='+db.version+' want 3');
    // Check object stores exist
    var names = Array.from(db.objectStoreNames);
    if(names.indexOf('messages')===-1) errors.push('missing messages store');
    if(names.indexOf('blobs')===-1) errors.push('missing blobs store');
    if(names.indexOf('kv')===-1) errors.push('missing kv store');

    const tx=db.transaction(['messages','blobs','kv'],'readonly');

    // Verify messages
    const msgs=tx.objectStore('messages');
    const row1=await new Promise((r,e)=>{const q=msgs.get(['c1','m1']);q.onsuccess=()=>r(q.result);q.onerror=()=>e(q.error);});
    if(!row1) errors.push('missing c1/m1');
    else {
      if(!(row1.ts instanceof Date)) errors.push('ts not Date');
      else if(row1.ts.getTime()!==1700000000000) errors.push('ts wrong: '+row1.ts.getTime());
      if(!(row1.attachment instanceof ArrayBuffer)) errors.push('attachment not AB');
      else {
        var ab=new Uint8Array(row1.attachment);
        if(ab.length!==5||ab[0]!==10||ab[4]!==50) errors.push('attachment bytes wrong');
      }
      if(!Array.isArray(row1.tags)||row1.tags.length!==2) errors.push('tags wrong');
      if(row1.body!=='hello world') errors.push('body wrong');
    }
    const row2=await new Promise((r,e)=>{const q=msgs.get(['c2','m2']);q.onsuccess=()=>r(q.result);q.onerror=()=>e(q.error);});
    if(!row2) errors.push('missing c2/m2');

    // Verify indexes
    const byTs=msgs.index('by_ts');
    const tsCnt=await new Promise((r,e)=>{const q=byTs.count();q.onsuccess=()=>r(q.result);q.onerror=()=>e(q.error);});
    if(tsCnt!==2) errors.push('by_ts count='+tsCnt);
    const byTags=msgs.index('by_tags');
    const tagCnt=await new Promise((r,e)=>{const q=byTags.count(IDBKeyRange.only('unread'));q.onsuccess=()=>r(q.result);q.onerror=()=>e(q.error);});
    if(tagCnt!==2) errors.push('by_tags=unread count='+tagCnt);

    // Verify blobs
    const blobs=tx.objectStore('blobs');
    const bCnt=await new Promise((r,e)=>{const q=blobs.count();q.onsuccess=()=>r(q.result);q.onerror=()=>e(q.error);});
    if(bCnt!==1) errors.push('blobs count='+bCnt);

    // Verify kv
    const kv=tx.objectStore('kv');
    const si=await new Promise((r,e)=>{const q=kv.get('session_info');q.onsuccess=()=>r(q.result);q.onerror=()=>e(q.error);});
    if(!si||si.user!=='alice') errors.push('kv session_info wrong');

    db.close();
    return JSON.stringify(errors.length?{errors:errors}:{ok:true});
  } catch(e) {
    return JSON.stringify({errors:['exception: '+(e&&e.message||e)]});
  }
})()
"""


def _tab_ws(port: int, target_id: str) -> str:
    """Get WebSocket debugger URL for a target."""
    import urllib.request
    info = json.loads(urllib.request.urlopen(
        f"http://127.0.0.1:{port}/json/list", timeout=5).read())
    t = next((x for x in info if x["id"] == target_id), None)
    if not t:
        raise RuntimeError(f"target {target_id} not found in /json/list")
    return t["webSocketDebuggerUrl"]


def _wait_for_ws(port: int, target_id: str, retries: int = 20) -> str:
    """Wait until the target appears in /json/list with a webSocketDebuggerUrl."""
    for _ in range(retries):
        try:
            return _tab_ws(port, target_id)
        except (RuntimeError, StopIteration):
            time.sleep(0.2)
    raise RuntimeError(f"target {target_id} never appeared")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9222)
    args = ap.parse_args()
    port = args.port

    passed = 0
    failed = 0

    def check(name: str, condition: bool, detail: str = ""):
        nonlocal passed, failed
        if condition:
            print(f"  [PASS] {name}")
            passed += 1
        else:
            print(f"  [FAIL] {name}: {detail}")
            failed += 1

    # ── Phase 1: Create context, open tab, seed data ─────────────────────
    print("\n=== Phase 1: Seed data ===")
    bs = CDPSession.connect_browser(port)
    try:
        ctx = bs.target.create_browser_context()
        print(f"  Context: {ctx}")
        tid = bs.target.create_target(url="https://example.com/", browser_context_id=ctx)
        print(f"  Tab: {tid}")

        # Set cookies via Storage.setCookies
        cookies_to_set = [
            {"name": "session_id", "value": "sess_abc123", "domain": "example.com",
             "path": "/", "secure": True, "httpOnly": True},
            {"name": "pref", "value": "dark_mode", "domain": "example.com",
             "path": "/"},
        ]
        bs.storage.set_cookies(cookies_to_set, browser_context_id=ctx)
        print(f"  Set {len(cookies_to_set)} cookies")
    finally:
        bs.close()

    time.sleep(2.0)  # let page load

    ws = _wait_for_ws(port, tid)
    ts = CDPSession(ws, timeout=15)
    try:
        # Seed localStorage
        ls_result = ts.runtime.evaluate(SEED_LS_JS, timeout=5)
        ls_seeded = json.loads(ls_result) if ls_result else {}
        check("localStorage seeded", len(ls_seeded) >= 4,
              f"only {len(ls_seeded)} keys")

        # Seed IndexedDB
        idb_result = ts.runtime.evaluate(SEED_IDB_JS, await_promise=True, timeout=10)
        check("IndexedDB seeded", idb_result == "ok", str(idb_result))
    finally:
        ts.close()

    # ── Phase 2: Collect state (exactly as snapshot does) ────────────────
    print("\n=== Phase 2: Collect state ===")
    tmp = tempfile.mkdtemp(prefix="verify-roundtrip-")
    db_path = os.path.join(tmp, "test.db")
    store = PersistenceManager(db_path)
    mgr = ContainerManager(store=store, browser_port=port)

    cookies, storage, idb_data, tabs = mgr._collect_state(ctx)
    print(f"  Cookies: {len(cookies)}")
    print(f"  LocalStorage origins: {list(storage.keys())}")
    print(f"  IDB origins: {list(idb_data.keys())}")
    print(f"  Tabs: {[t['url'] for t in tabs]}")

    check("cookies collected", len(cookies) >= 2,
          f"only {len(cookies)}")
    check("localStorage collected for example.com",
          "https://example.com" in storage,
          f"origins={list(storage.keys())}")
    if "https://example.com" in storage:
        ls = storage["https://example.com"]
        check("localStorage has auth_token", ls.get("auth_token") == "tok_abc123xyz",
              f"got {ls.get('auth_token')}")
        check("localStorage has json_value",
              "json_value" in ls and json.loads(ls["json_value"]).get("nested") is True,
              f"got {ls.get('json_value')}")
    check("IDB collected for example.com",
          "https://example.com" in idb_data,
          f"origins={list(idb_data.keys())}")
    if "https://example.com" in idb_data:
        idb = idb_data["https://example.com"]
        check("IDB has app_session_db", "app_session_db" in idb,
              f"dbs={list(idb.keys())}")
        if "app_session_db" in idb:
            db_snap = idb["app_session_db"]
            check("IDB version=3", db_snap.get("_meta", {}).get("version") == 3,
                  str(db_snap.get("_meta")))
            stores = [k for k in db_snap if k != "_meta"]
            check("IDB has 3 stores", len(stores) == 3, str(stores))

    # ── Phase 3: Dispose context (simulate hibernate) ────────────────────
    print("\n=== Phase 3: Dispose (hibernate) ===")
    bs2 = CDPSession.connect_browser(port)
    try:
        bs2.target.dispose_browser_context(ctx)
        print(f"  Context {ctx} disposed")
    finally:
        bs2.close()

    # ── Phase 4: Restore into a new context ──────────────────────────────
    print("\n=== Phase 4: Restore ===")
    tab_url = tabs[0]["url"] if tabs else "https://example.com/"
    origin = _origin_of(tab_url)
    restore_ls = storage.get(origin, {})
    restore_idb = idb_data.get(origin, {})

    bs3 = CDPSession.connect_browser(port)
    try:
        new_ctx = bs3.target.create_browser_context()
        print(f"  New context: {new_ctx}")
        # Set cookies
        bs3.storage.set_cookies(cookies, browser_context_id=new_ctx)
        print(f"  Restored {len(cookies)} cookies")
    finally:
        bs3.close()

    # Use the real _open_tab_with_storage, but we need to wire it up
    new_tid = mgr._open_tab_with_storage(
        new_ctx, tab_url,
        {origin: restore_ls} if restore_ls else {},
        {origin: restore_idb} if restore_idb else {})
    print(f"  Opened tab: {new_tid} for {tab_url}")

    # Wait for page + restore scripts to execute
    time.sleep(3.0)

    # ── Phase 5: Verify everything survived ──────────────────────────────
    print("\n=== Phase 5: Verify ===")

    # 5a. Cookies
    bs4 = CDPSession.connect_browser(port)
    try:
        restored_cookies = bs4.storage.get_cookies(browser_context_id=new_ctx)
    finally:
        bs4.close()
    check("cookies restored", len(restored_cookies) >= 2,
          f"only {len(restored_cookies)}")
    cookie_names = {c["name"] for c in restored_cookies}
    check("session_id cookie present", "session_id" in cookie_names,
          str(cookie_names))
    check("pref cookie present", "pref" in cookie_names,
          str(cookie_names))

    # 5b. localStorage
    ws2 = _wait_for_ws(port, new_tid)
    ts2 = CDPSession(ws2, timeout=15)
    try:
        ls_raw = ts2.runtime.evaluate(VERIFY_LS_JS, timeout=5)
        restored_ls = json.loads(ls_raw) if ls_raw else {}
        check("localStorage restored", len(restored_ls) >= 4,
              f"keys={list(restored_ls.keys())}")
        check("auth_token value correct",
              restored_ls.get("auth_token") == "tok_abc123xyz",
              f"got {restored_ls.get('auth_token')}")
        check("theme value correct",
              restored_ls.get("theme") == "dark",
              f"got {restored_ls.get('theme')}")
        check("json_value round-trips",
              "json_value" in restored_ls and
              json.loads(restored_ls["json_value"]).get("nested") is True,
              f"got {restored_ls.get('json_value')}")

        # 5c. IndexedDB
        idb_raw = ts2.runtime.evaluate(VERIFY_IDB_JS, await_promise=True, timeout=15)
        idb_result = json.loads(idb_raw) if idb_raw else {"errors": ["empty result"]}
        if idb_result.get("ok"):
            check("IndexedDB fully restored", True, "")
        else:
            for err in idb_result.get("errors", ["unknown"]):
                check(f"IDB: {err}", False, err)
    finally:
        ts2.close()

    # ── Cleanup ──────────────────────────────────────────────────────────
    try:
        bs5 = CDPSession.connect_browser(port)
        try:
            bs5.target.dispose_browser_context(new_ctx)
        finally:
            bs5.close()
    except Exception:
        pass

    import shutil
    shutil.rmtree(tmp, ignore_errors=True)

    # ── Summary ──────────────────────────────────────────────────────────
    total = passed + failed
    print(f"\n{'='*60}")
    if failed == 0:
        print(f"ALL {total} CHECKS PASSED — full roundtrip verified")
        return 0
    else:
        print(f"{failed}/{total} checks FAILED")
        return 1


if __name__ == "__main__":
    sys.exit(main())
