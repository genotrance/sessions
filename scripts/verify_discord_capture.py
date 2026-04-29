"""Simulate what _collect_state will now do for the live Discord tab."""
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
from sessions.cdp import CDPSession

port = 9222
info = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5).read())
discord_tabs = [t for t in info if "discord.com" in t.get("url", "") and t["type"] == "page"]

if not discord_tabs:
    print("[!] No Discord tab found")
    sys.exit(1)

tab = discord_tabs[0]
print(f"Tab: {tab['id']}")
print(f"URL: {tab['url']}")

ws = tab.get("webSocketDebuggerUrl")
s = CDPSession(ws, timeout=10)

try:
    # Step 1: Get origin (same as before — this still works)
    origin = s.runtime.evaluate("window.location.origin", timeout=5)
    print(f"\nOrigin: {origin}")

    # Step 2: NEW — read localStorage via DOMStorage
    result = s.send("DOMStorage.getDOMStorageItems", {
        "storageId": {"securityOrigin": origin, "isLocalStorage": True}
    }, timeout=5)
    entries = result.get("entries", [])
    print(f"\nDOMStorage localStorage: {len(entries)} entries")
    for k, v in entries[:15]:
        vstr = v if len(v) < 80 else v[:80] + "..."
        print(f"  {k} = {vstr}")
    if len(entries) > 15:
        print(f"  ... and {len(entries) - 15} more")

    # Step 3: Check if any auth-related keys exist
    keys = [k for k, v in entries]
    auth_keys = [k for k in keys if any(x in k.lower() for x in ["token", "auth", "session", "user"])]
    print(f"\nAuth-related keys: {auth_keys}")

    # Step 4: OLD path would have shown 0 — confirm
    old_result = s.runtime.evaluate(
        "try{JSON.stringify(Object.fromEntries(Object.entries(localStorage)))}catch(e){null}",
        timeout=5)
    print(f"\nOLD Runtime.evaluate path: {old_result!r}")
    print(f"  (This is what was being returned before — null/empty = 0 keys)")

    print(f"\n=== SUMMARY ===")
    print(f"OLD method: 0 keys (localStorage deleted from JS)")
    print(f"NEW method: {len(entries)} keys via CDP DOMStorage")

finally:
    s.close()
