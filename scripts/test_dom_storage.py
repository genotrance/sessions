"""Test CDP DOMStorage domain — can it bypass Discord's JS-level localStorage deletion?"""
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
from sessions.cdp import CDPSession

port = 9222

# Find the existing Discord tab
info = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5).read())
discord_tabs = [t for t in info if "discord.com" in t.get("url", "") and t["type"] == "page"]

if not discord_tabs:
    print("[!] No Discord tab found")
    sys.exit(1)

tab = discord_tabs[0]
print(f"Discord tab: {tab['id']}")
print(f"URL: {tab['url']}")

ws = tab.get("webSocketDebuggerUrl")
if not ws:
    print("[!] No WebSocket URL")
    sys.exit(1)

s = CDPSession(ws, timeout=10)
try:
    # 1) Confirm JS localStorage is broken
    js_check = s.runtime.evaluate("typeof localStorage", timeout=5)
    print(f"\nJS typeof localStorage: {js_check}")

    # 2) Try DOMStorage.getDOMStorageItems via CDP
    storage_id = {"securityOrigin": "https://discord.com", "isLocalStorage": True}
    try:
        result = s.send("DOMStorage.getDOMStorageItems", {"storageId": storage_id}, timeout=5)
        entries = result.get("entries", [])
        print(f"\nDOMStorage.getDOMStorageItems: {len(entries)} entries")
        for k, v in entries[:10]:
            vstr = v if len(v) < 80 else v[:80] + "..."
            print(f"  {k} = {vstr}")
        if len(entries) > 10:
            print(f"  ... and {len(entries) - 10} more")
    except Exception as e:
        print(f"\nDOMStorage.getDOMStorageItems ERROR: {e}")

    # 3) Try to WRITE via DOMStorage.setDOMStorageItem
    try:
        s.send("DOMStorage.setDOMStorageItem", {
            "storageId": storage_id,
            "key": "_cdp_test",
            "value": "hello_from_cdp"
        }, timeout=5)
        print("\nDOMStorage.setDOMStorageItem: OK (wrote _cdp_test)")
    except Exception as e:
        print(f"\nDOMStorage.setDOMStorageItem ERROR: {e}")

    # 4) Read it back via DOMStorage
    try:
        result2 = s.send("DOMStorage.getDOMStorageItems", {"storageId": storage_id}, timeout=5)
        entries2 = result2.get("entries", [])
        test_entry = next((v for k, v in entries2 if k == "_cdp_test"), None)
        print(f"Read back _cdp_test via DOMStorage: {test_entry}")
    except Exception as e:
        print(f"Read back error: {e}")

    # 5) Check sessionStorage too
    ss_id = {"securityOrigin": "https://discord.com", "isLocalStorage": False}
    try:
        result3 = s.send("DOMStorage.getDOMStorageItems", {"storageId": ss_id}, timeout=5)
        entries3 = result3.get("entries", [])
        print(f"\nSessionStorage entries: {len(entries3)}")
        for k, v in entries3[:5]:
            vstr = v if len(v) < 80 else v[:80] + "..."
            print(f"  {k} = {vstr}")
    except Exception as e:
        print(f"\nSessionStorage ERROR: {e}")

    # 6) Clean up test key
    try:
        s.send("DOMStorage.removeDOMStorageItem", {
            "storageId": storage_id,
            "key": "_cdp_test"
        }, timeout=5)
    except Exception:
        pass

finally:
    s.close()
