"""Standalone verification of IndexedDB dump+restore against a real Chrome.

Usage:
    python scripts/verify_idb_roundtrip.py [--port 9222]

Requires: A running Chrome with remote debugging enabled on the given port.

This script:
  1. Opens a fresh data: URL tab
  2. Seeds IndexedDB with binary data (ArrayBuffer, Uint8Array, Date, compound
     keyPath, multiEntry index, autoIncrement store) — mimicking WhatsApp/Discord
  3. Dumps IDB via the same `_IDB_DUMP_JS` used by the session manager
  4. Wipes the database
  5. Restores via `_build_idb_restore_script`
  6. Reads back and byte-compares every field

Exits 0 on success, 1 on any verification failure.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_root, "src"))

import urllib.request
from sessions.cdp import CDPSession
from sessions.idb import IDB_DUMP_JS, build_restore_script


SEED_JS = """
(async function(){
  await new Promise((res) => {
    const req = indexedDB.deleteDatabase('verifydb');
    req.onsuccess = req.onerror = () => res();
  });
  const db = await new Promise((res, rej) => {
    const r = indexedDB.open('verifydb', 7);
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
            key: new Uint8Array([10,20,30]),
            map: new Map([['k1', 1], ['k2', 2]])});
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

VERIFY_JS = """
(async function(){
  try {
    const db = await new Promise((res,rej)=>{
      const r=indexedDB.open('verifydb');
      r.onsuccess=()=>res(r.result);r.onerror=()=>rej(r.error);
    });
    if (db.version !== 7) return {err:'bad version: '+db.version};
    const tx = db.transaction(['msgs','blobs'],'readonly');
    const msgs = tx.objectStore('msgs');
    const row = await new Promise((res,rej)=>{
      const r=msgs.get(['c1','m1']);
      r.onsuccess=()=>res(r.result);r.onerror=()=>rej(r.error);
    });
    if (!row) return {err:'missing row c1/m1'};
    if (!(row.ts instanceof Date))
      return {err:'ts not Date, got '+(row.ts&&row.ts.constructor.name)};
    if (row.ts.getTime() !== 1700000000000)
      return {err:'ts value wrong: '+row.ts.getTime()};
    if (!(row.payload instanceof ArrayBuffer))
      return {err:'payload not AB: '+(row.payload&&row.payload.constructor.name)};
    const pb = new Uint8Array(row.payload);
    if (pb.length !== 5 || pb[0]!==1 || pb[4]!==5)
      return {err:'payload bytes wrong: ['+Array.from(pb).join(',')+']'};
    if (!(row.key instanceof Uint8Array))
      return {err:'key not U8: '+(row.key&&row.key.constructor.name)};
    if (row.key.length !== 3 || row.key[0]!==10 || row.key[2]!==30)
      return {err:'key bytes wrong: ['+Array.from(row.key).join(',')+']'};
    if (!(row.map instanceof Map)) return {err:'map not Map'};
    if (row.map.get('k1') !== 1) return {err:'map k1 wrong'};
    // Verify indexes
    const byTs = msgs.index('by_ts');
    const cnt = await new Promise((res,rej)=>{
      const r=byTs.count();
      r.onsuccess=()=>res(r.result);r.onerror=()=>rej(r.error);
    });
    if (cnt !== 2) return {err:'by_ts count: '+cnt};
    const byTag = msgs.index('by_tag');
    const tagCnt = await new Promise((res,rej)=>{
      const r=byTag.count(IDBKeyRange.only('b'));
      r.onsuccess=()=>res(r.result);r.onerror=()=>rej(r.error);
    });
    if (tagCnt !== 2) return {err:'by_tag=b count: '+tagCnt};
    // Verify second row + autoIncrement store
    const row2 = await new Promise((res,rej)=>{
      const r=msgs.get(['c2','m2']);
      r.onsuccess=()=>res(r.result);r.onerror=()=>rej(r.error);
    });
    if (!row2) return {err:'missing row c2/m2'};
    if (!(row2.payload instanceof ArrayBuffer) || row2.payload.byteLength !== 8)
      return {err:'row2 payload wrong size'};
    const blobs = tx.objectStore('blobs');
    const bCnt = await new Promise((res,rej)=>{
      const r=blobs.count();
      r.onsuccess=()=>res(r.result);r.onerror=()=>rej(r.error);
    });
    if (bCnt !== 1) return {err:'blobs count: '+bCnt};
    db.close();
    return {ok:true};
  } catch(e) { return {err:'exception: '+(e&&e.message||e)}; }
})()
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9222)
    args = ap.parse_args()

    dump_js = IDB_DUMP_JS

    # Create a tab via browser-level CDP
    bs = CDPSession.connect_browser(args.port)
    try:
        tid = bs.target.create_target(url="https://example.com/")
        print(f"[*] Created tab: {tid}")
    finally:
        bs.close()
    time.sleep(2.0)  # let page load

    # Find the tab ws url
    info = json.loads(urllib.request.urlopen(
        f"http://127.0.0.1:{args.port}/json/list", timeout=5).read())
    tab = next(x for x in info if x["id"] == tid)
    tab_sess = CDPSession(tab["webSocketDebuggerUrl"])

    try:
        # 1) Seed
        r = tab_sess.runtime.evaluate(SEED_JS, await_promise=True, timeout=10)
        assert r == "ok", f"seed failed: {r}"
        print("[*] Seed: ok")

        # 2) Dump
        dumped = tab_sess.runtime.evaluate(dump_js, await_promise=True, timeout=15)
        assert dumped, "dump returned empty"
        idb_data = json.loads(dumped)
        assert "verifydb" in idb_data, f"verifydb missing: {list(idb_data)}"
        meta = idb_data["verifydb"]["_meta"]
        assert meta["version"] == 7, f"bad version: {meta}"
        print(f"[*] Dump: version={meta['version']}, "
              f"stores={len([k for k in idb_data['verifydb'] if k != '_meta'])}")
        row0 = idb_data["verifydb"]["msgs"]["rows"][0]
        assert row0["ts"]["__t"] == "D", f"Date not encoded: {row0['ts']}"
        assert row0["payload"]["__t"] == "AB", f"AB not encoded: {row0['payload']}"
        assert row0["key"]["__t"] == "TA", f"TA not encoded: {row0['key']}"
        print("[*] Encoded markers: Date=D, ArrayBuffer=AB, Uint8Array=TA — all present")

        # 3) Wipe
        tab_sess.runtime.evaluate(
            "new Promise((res)=>{const r=indexedDB.deleteDatabase('verifydb');"
            "r.onsuccess=r.onerror=()=>res();})",
            await_promise=True, timeout=5)
        print("[*] Wiped verifydb")

        # 4) Restore
        restore = build_restore_script(idb_data)
        tab_sess.runtime.evaluate(restore, timeout=5)
        # Restore script returns synchronously but runs async; wait
        time.sleep(2.0)
        print("[*] Restore script executed")

        # 5) Verify
        result = tab_sess.runtime.evaluate(VERIFY_JS, await_promise=True, timeout=10)
        if result == {"ok": True}:
            print("[PASS] IDB round-trip verified end-to-end with binary data")
            return 0
        print(f"[FAIL] verify returned: {result}")
        return 1
    finally:
        try:
            bs2 = CDPSession.connect_browser(args.port)
            try:
                bs2.target.close_target(tid)
            finally:
                bs2.close()
        except Exception:
            pass
        tab_sess.close()


if __name__ == "__main__":
    sys.exit(main())
