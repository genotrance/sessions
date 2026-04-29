"""Diagnose WhatsApp restoration failure by capturing console output.

This script:
1. Opens a fresh context
2. Injects the IDB restore scaffolding
3. Captures all console.log/warn/error messages during restore
4. Reports which databases were deleted or cleared

Requires Chrome running on 127.0.0.1:9222.
Run: ``..\\test\\python\\python.exe scripts\\diagnose_whatsapp_restore.py``
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.sessions.cdp import CDPSession  # noqa: E402
from src.sessions.idb import (  # noqa: E402
    build_restore_scaffolding,
    build_restore_single_db_inject,
)


def main() -> int:
    # Get a page target
    with urllib.request.urlopen("http://127.0.0.1:9222/json/list", timeout=5) as r:
        targets = json.loads(r.read())
    page = next((t for t in targets if t.get("type") == "page"
                 and t.get("url", "").startswith(("http", "data:"))), None)
    if page is None:
        page = next((t for t in targets if t.get("type") == "page"), None)
    if page is None:
        print("No page target found", file=sys.stderr)
        return 2

    sess = CDPSession(page["webSocketDebuggerUrl"], timeout=30)
    try:
        # Enable console message logging
        console_messages = []
        def on_console_message(msg):
            if msg.get("method") == "Runtime.consoleAPICalled":
                params = msg.get("params", {})
                args = params.get("args", [])
                msg_type = params.get("type", "log")
                text_parts = []
                for arg in args:
                    if arg.get("type") == "string":
                        text_parts.append(arg.get("value", ""))
                    elif arg.get("type") == "number":
                        text_parts.append(str(arg.get("value", "")))
                    else:
                        text_parts.append(str(arg))
                text = " ".join(text_parts)
                console_messages.append((msg_type, text))
                print(f"[{msg_type.upper()}] {text}")

        sess.on("Runtime.consoleAPICalled", on_console_message)

        # Enable Runtime domain
        sess.send("Runtime.enable")

        # Inject scaffolding
        print("\n=== Injecting IDB restore scaffolding ===\n")
        scaffolding = build_restore_scaffolding(60000)  # 60s protection
        sess.runtime.evaluate(scaffolding, timeout=5)

        # Simulate a small WhatsApp-like restore: create wawc_db_enc with a CryptoKey
        print("\n=== Injecting test database (wawc_db_enc with CryptoKey) ===\n")
        
        # Create a test database with a CryptoKey
        test_db_inject = """
        (async function(){
            var R = window.__idbR;
            if (!R) { console.warn('[TEST] No scaffolding'); return; }
            
            console.log('[TEST] Starting test database restore');
            
            // Simulate wawc_db_enc with a CryptoKey
            var dbName = 'wawc_db_enc';
            var req = R.origOpen(dbName, 1);
            
            req.onupgradeneeded = function(e) {
                var db = e.target.result;
                try {
                    db.createObjectStore('keys', {keyPath: 'id'});
                    console.log('[TEST] Created keys store');
                } catch(ex) {
                    console.warn('[TEST] Failed to create store:', ex);
                }
            };
            
            req.onsuccess = async function(e) {
                var db = e.target.result;
                console.log('[TEST] Database opened');
                
                // Generate a test CryptoKey
                try {
                    var key = await crypto.subtle.generateKey(
                        {name: 'AES-GCM', length: 256},
                        true,  // extractable
                        ['encrypt', 'decrypt']
                    );
                    console.log('[TEST] Generated test CryptoKey');
                    
                    // Try to put it in the database
                    var tx = db.transaction(['keys'], 'readwrite');
                    var os = tx.objectStore('keys');
                    var pr = os.put({id: 'test-key', key: key});
                    
                    pr.onsuccess = function() {
                        console.log('[TEST] Successfully stored CryptoKey');
                    };
                    
                    pr.onerror = function(ev) {
                        console.warn('[TEST] Failed to store CryptoKey:', ev.target.error);
                    };
                    
                    tx.oncomplete = function() {
                        console.log('[TEST] Transaction complete');
                        db.close();
                    };
                    
                    tx.onerror = function(ev) {
                        console.warn('[TEST] Transaction error:', ev.target.error);
                        db.close();
                    };
                } catch(ex) {
                    console.error('[TEST] Error:', ex);
                    db.close();
                }
            };
            
            req.onerror = function(ev) {
                console.error('[TEST] Failed to open database:', ev.target.error);
            };
        })()
        """
        
        sess.runtime.evaluate(test_db_inject, await_promise=True, timeout=10)
        
        # Wait for messages to arrive
        print("\n=== Waiting for console messages ===\n")
        time.sleep(2)
        
        # Check what databases exist
        print("\n=== Checking final database state ===\n")
        check_dbs = """
        (async function(){
            if (!indexedDB.databases) return JSON.stringify([]);
            const dbs = await indexedDB.databases();
            return JSON.stringify(dbs.map(d => d.name));
        })()
        """
        
        dbs = sess.runtime.evaluate(check_dbs, await_promise=True, timeout=5)
        print(f"Databases after restore: {dbs}")
        
        # Summary
        print("\n=== Console Message Summary ===\n")
        blocked_count = sum(1 for t, m in console_messages if "BLOCKED" in m)
        error_count = sum(1 for t, m in console_messages if t in ("error", "warn"))
        print(f"Total messages: {len(console_messages)}")
        print(f"Errors/Warnings: {error_count}")
        print(f"Blocked operations: {blocked_count}")
        
        # Check for CryptoKey-related messages
        crypto_msgs = [m for t, m in console_messages if "CryptoKey" in m or "export" in m.lower()]
        if crypto_msgs:
            print(f"\nCryptoKey-related messages:")
            for msg in crypto_msgs:
                print(f"  - {msg}")
        
        return 0
    finally:
        sess.close()


if __name__ == "__main__":
    sys.exit(main())
