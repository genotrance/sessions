#!/usr/bin/env python3
"""Round-trip test for IDB backup and restore.

Tests the full cycle: collect → persist → restore → collect → compare.
Uses a temporary SQLite database to avoid interfering with production data.

Usage:
    1. Ensure Chrome is running with --remote-debugging-port=9222
    2. python tests/roundtrip_test.py
    3. Follow the interactive prompts
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import threading
import time

import requests as _http

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_root, "src"))

# Configure logging so we see manager.py debug output
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(name)s  %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
# Quiet down noisy loggers
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("websocket").setLevel(logging.WARNING)

from sessions.cdp import CDPSession
from sessions.manager import ContainerManager
from sessions.persistence import PersistenceManager

CHROME_PORT = 9222
WHATSAPP_URL = "https://web.whatsapp.com/"


def _summarize_idb(idb: dict) -> dict:
    """Return a compact summary: {origin: {db: {store: row_count}}}."""
    summary = {}
    for origin, dbs in idb.items():
        origin_summary = {}
        for db_name, db_data in dbs.items():
            if not isinstance(db_data, dict):
                continue
            store_summary = {}
            for store_name, store in db_data.items():
                if store_name == "_meta":
                    continue
                if not isinstance(store, dict):
                    continue
                store_summary[store_name] = len(store.get("rows", []))
            if store_summary:
                origin_summary[db_name] = store_summary
        if origin_summary:
            summary[origin] = origin_summary
    return summary


def _print_idb_summary(label: str, idb: dict) -> None:
    summary = _summarize_idb(idb)
    total_dbs = 0
    total_rows = 0
    for origin, dbs in summary.items():
        print(f"  {label} origin={origin}:")
        for db_name, stores in sorted(dbs.items()):
            db_rows = sum(stores.values())
            total_rows += db_rows
            total_dbs += 1
            store_list = ", ".join(f"{s}={n}" for s, n in sorted(stores.items()))
            print(f"    {db_name}: {db_rows} rows ({store_list})")
    print(f"  {label} total: {total_dbs} databases, {total_rows} rows")


def _compare_idb(src_idb: dict, dst_idb: dict) -> list[str]:
    """Compare IDB data, return list of difference descriptions."""
    diffs = []
    src_sum = _summarize_idb(src_idb)
    dst_sum = _summarize_idb(dst_idb)

    for origin in sorted(set(src_sum) | set(dst_sum)):
        src_dbs = src_sum.get(origin, {})
        dst_dbs = dst_sum.get(origin, {})

        missing_dbs = set(src_dbs) - set(dst_dbs)
        extra_dbs = set(dst_dbs) - set(src_dbs)
        for db in sorted(missing_dbs):
            diffs.append(f"MISSING DB: {origin}/{db} "
                         f"(had {sum(src_dbs[db].values())} rows)")
        for db in sorted(extra_dbs):
            diffs.append(f"EXTRA DB: {origin}/{db} "
                         f"(has {sum(dst_dbs[db].values())} rows)")

        for db_name in sorted(set(src_dbs) & set(dst_dbs)):
            src_stores = src_dbs[db_name]
            dst_stores = dst_dbs[db_name]
            missing_stores = set(src_stores) - set(dst_stores)
            extra_stores = set(dst_stores) - set(src_stores)
            for s in sorted(missing_stores):
                diffs.append(f"MISSING STORE: {origin}/{db_name}/{s} "
                             f"(had {src_stores[s]} rows)")
            for s in sorted(extra_stores):
                diffs.append(f"EXTRA STORE: {origin}/{db_name}/{s} "
                             f"(has {dst_stores[s]} rows)")
            for s in sorted(set(src_stores) & set(dst_stores)):
                if src_stores[s] != dst_stores[s]:
                    diffs.append(
                        f"ROW COUNT MISMATCH: {origin}/{db_name}/{s}: "
                        f"{src_stores[s]} → {dst_stores[s]}")
    return diffs


def _compare_ls(src_storage: dict, dst_storage: dict) -> list[str]:
    diffs = []
    for origin in sorted(set(src_storage) | set(dst_storage)):
        src_ls = src_storage.get(origin, {})
        dst_ls = dst_storage.get(origin, {})
        if len(src_ls) != len(dst_ls):
            diffs.append(f"LS key count: {origin}: {len(src_ls)} → {len(dst_ls)}")
        missing = set(src_ls) - set(dst_ls)
        if missing:
            diffs.append(f"LS missing keys in {origin}: "
                         f"{sorted(missing)[:10]}"
                         f"{'...' if len(missing) > 10 else ''}")
    return diffs


def main():
    db_fd, db_path = tempfile.mkstemp(suffix=".db", prefix="roundtrip_")
    os.close(db_fd)
    try:
        _run_test(db_path)
    finally:
        try:
            os.unlink(db_path)
        except OSError:
            pass


def _run_test(db_path: str):
    store = PersistenceManager(db_path=db_path)
    mgr = ContainerManager(browser_port=CHROME_PORT, store=store)

    print("=" * 60)
    print("  IDB Round-Trip Test")
    print("=" * 60)

    # ---- Step 1: source container ----
    print("\n[1/6] Creating source container and opening WhatsApp...")
    src = mgr.create_container("RT-Source")
    src_id = src["id"]
    src_tab_id = ""
    try:
        src_tab_id = mgr.open_tab(src_id, WHATSAPP_URL)
    except Exception as e:
        print(f"  ERROR opening tab: {e}")
        print("  Is Chrome running with --remote-debugging-port=9222?")
        return

    input("\n>>> Log into WhatsApp and wait for it to fully load, "
          "then press Enter...")

    # ---- Step 2: snapshot source ----
    print("\n[2/6] Snapshotting source container...")
    time.sleep(3)  # let IDB settle
    snap1 = mgr.snapshot(src_id)
    print(f"  Result: {json.dumps(snap1, indent=2)}")

    src_data = store.get_container(src_id)
    src_idb = src_data.get("idb", {})
    src_storage = src_data.get("storage", {})
    src_cookies = src_data.get("cookies", [])

    print(f"\n  Source: {len(src_cookies)} cookies, "
          f"{len(src_storage)} LS origins")
    _print_idb_summary("Source", src_idb)

    if not src_idb:
        print("\n  WARNING: No IDB data collected! "
              "WhatsApp may not have fully loaded.")
        input("  Press Enter to continue anyway, or Ctrl+C to abort...")

    # ---- Step 2b: close source tab to prevent dual-session conflict ----
    # WhatsApp's server detects two simultaneous sessions with the same
    # credentials and forces the newer one to reinitialize, wiping data.
    if src_tab_id:
        print("\n  Closing source tab to prevent dual-session conflict...")
        try:
            _http.get(
                f"http://127.0.0.1:{CHROME_PORT}/json/close/{src_tab_id}",
                timeout=5)
            print(f"  Closed source tab {src_tab_id}")
        except Exception as e:
            print(f"  WARNING: Could not close source tab: {e}")
        time.sleep(3)  # let WhatsApp server clean up the session

    # ---- Step 3: create dest and copy data ----
    print("\n[3/6] Creating destination container with cloned data...")
    dst = store.clone_container(src_id, "RT-Dest")
    dst_id = dst["id"]
    print(f"  Cloned {src_id} → {dst_id}")

    # ---- Step 4: restore dest ----
    print("\n[4/6] Restoring destination container (injecting data)...")
    result = mgr.restore(dst_id)
    print(f"  Result: {json.dumps(result, indent=2)}")

    wait_secs = 45
    print(f"\n  Waiting {wait_secs}s for WhatsApp to load in new context...")

    # Capture browser console from the restored tab
    console_lines: list[str] = []
    tab_tid = result.get("activate_target_id", "")
    console_sess = None
    if tab_tid:
        try:
            info = _http.get(
                f"http://127.0.0.1:{CHROME_PORT}/json/list",
                timeout=(0.5, 3)).json()
            t = next((x for x in info if x["id"] == tab_tid), None)
            if t and t.get("webSocketDebuggerUrl"):
                console_sess = CDPSession(t["webSocketDebuggerUrl"])
                console_sess.send("Runtime.enable", {})
                console_sess.send("Log.enable", {})
                def _collect_console(msg: dict) -> None:
                    p = msg.get("params", {})
                    args = p.get("args", [])
                    text = " ".join(
                        str(a.get("value", a.get("description", "")))
                        for a in args
                    )
                    console_lines.append(f"[{p.get('type','?')}] {text}")
                def _collect_log(msg: dict) -> None:
                    entry = msg.get("params", {}).get("entry", {})
                    console_lines.append(
                        f"[{entry.get('level','?')}] {entry.get('text','')}")
                console_sess.on("Runtime.consoleAPICalled", _collect_console)
                console_sess.on("Log.entryAdded", _collect_log)
                # Pump events in background
                def _pump():
                    import websocket as _ws
                    while console_sess and console_sess.ws:
                        try:
                            console_sess.ws.settimeout(1.0)
                            raw = console_sess.ws.recv()
                            msg = json.loads(raw)
                            console_sess._dispatch_event(msg)
                        except _ws.WebSocketTimeoutException:
                            continue
                        except Exception:
                            break
                pump_thread = threading.Thread(target=_pump, daemon=True)
                pump_thread.start()
                print("  Console capture enabled on restored tab")
        except Exception as e:
            print(f"  WARNING: Could not attach console capture: {e}")

    for i in range(wait_secs, 0, -5):
        print(f"    {i}s remaining...", flush=True)
        time.sleep(5)

    # Print captured console logs
    if console_lines:
        print(f"\n  Browser console ({len(console_lines)} messages):")
        for line in console_lines:
            if "[IDB" in line or "idb" in line.lower() or "error" in line.lower():
                print(f"    >>> {line}")
            # else: skip noisy non-IDB messages
        idb_lines = [l for l in console_lines if "[IDB" in l]
        if idb_lines:
            print(f"\n  IDB-specific console lines ({len(idb_lines)}):")
            for line in idb_lines:
                print(f"    {line}")
    else:
        print("\n  No browser console messages captured.")

    # Diagnostic: read window.__idbRL and check restore state
    if console_sess:
        try:
            rl = console_sess.runtime.evaluate(
                "JSON.stringify(window.__idbRL || [])", timeout=5)
            if rl:
                entries = json.loads(rl)
                print(f"\n  IDB Restore Log ({len(entries)} entries):")
                for entry in entries:
                    print(f"    {entry}")
            else:
                print("\n  window.__idbRL is empty/absent")
        except Exception as e:
            print(f"\n  Could not read __idbRL: {e}")
        try:
            diag = console_sess.runtime.evaluate(
                "JSON.stringify({"
                "  idbR: window.__idbR "
                "    ? {pending: window.__idbR.pending} "
                "    : 'cleaned up (good)'"
                "})",
                timeout=5)
            print(f"  Restore state: {diag}")
        except Exception as e:
            print(f"  Could not evaluate diagnostics: {e}")
        try:
            db_list = console_sess.runtime.evaluate(
                "indexedDB.databases().then(dbs => "
                "  JSON.stringify(dbs.map(d => ({name: d.name, version: d.version})))"
                ")",
                await_promise=True, timeout=5)
            print(f"  Live databases: {db_list}")
        except Exception as e:
            print(f"  Could not list databases: {e}")
        try:
            console_sess.close()
        except Exception:
            pass
        console_sess = None

    # ---- Step 5: snapshot dest ----
    print("\n[5/6] Snapshotting destination container...")
    snap2 = mgr.snapshot(dst_id)
    print(f"  Result: {json.dumps(snap2, indent=2)}")

    dst_data = store.get_container(dst_id)
    dst_idb = dst_data.get("idb", {})
    dst_storage = dst_data.get("storage", {})
    dst_cookies = dst_data.get("cookies", [])

    print(f"\n  Dest: {len(dst_cookies)} cookies, "
          f"{len(dst_storage)} LS origins")
    _print_idb_summary("Dest", dst_idb)

    # ---- Step 6: compare ----
    print("\n" + "=" * 60)
    print("  COMPARISON RESULTS")
    print("=" * 60)

    idb_diffs = _compare_idb(src_idb, dst_idb)
    ls_diffs = _compare_ls(src_storage, dst_storage)

    if not idb_diffs and not ls_diffs:
        print("\n  ✓ PASS: Source and destination data match!")
    else:
        if idb_diffs:
            print(f"\n  IDB differences ({len(idb_diffs)}):")
            for d in idb_diffs:
                print(f"    • {d}")
        if ls_diffs:
            print(f"\n  LocalStorage differences ({len(ls_diffs)}):")
            for d in ls_diffs:
                print(f"    • {d}")
        print(f"\n  ✗ FAIL: {len(idb_diffs)} IDB + {len(ls_diffs)} LS "
              f"differences found")

    # ---- Cleanup ----
    print("\n" + "=" * 60)
    input(">>> Press Enter to clean up test containers and exit...")
    for cid in [src_id, dst_id]:
        try:
            if cid in mgr.hot:
                ctx = mgr.hot.pop(cid)
                from sessions.cdp import CDPSession as _CDP
                try:
                    bs = _CDP.connect_browser(CHROME_PORT)
                    bs.target.dispose_browser_context(ctx)
                    bs.close()
                except Exception:
                    pass
        except Exception:
            pass
    print("  Cleaned up. Done!")


if __name__ == "__main__":
    main()
