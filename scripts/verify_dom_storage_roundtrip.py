"""Verify the DOMStorage-based read works correctly with real Chrome.

This simulates what _collect_state now does: reads localStorage via
DOMStorage.getDOMStorageItems instead of Runtime.evaluate.
"""
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
from sessions.cdp import CDPSession

port = 9222

print("=== DOMStorage Round-Trip Verification ===\n")

# Step 1: Create a context, open a page, set localStorage via CDP DOMStorage
bs = CDPSession.connect_browser(port)
try:
    ctx = bs.target.create_browser_context()
    print(f"1. Created context: {ctx}")
    tid = bs.target.create_target(url="about:blank", browser_context_id=ctx)
    print(f"   Created tab: {tid}")
finally:
    bs.close()

time.sleep(1)
info = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5).read())
tab = next(x for x in info if x["id"] == tid)
s = CDPSession(tab["webSocketDebuggerUrl"], timeout=10)

try:
    # Navigate to a real page
    s.page.navigate("https://example.com/", wait_for_load=False)
    time.sleep(2)

    # Write test data via DOMStorage.setDOMStorageItem
    origin = "https://example.com"
    sid = {"securityOrigin": origin, "isLocalStorage": True}
    test_data = {
        "token": "fake-auth-token-abc123",
        "user_settings": json.dumps({"theme": "dark", "locale": "en-US"}),
        "cache_version": "42",
    }
    for k, v in test_data.items():
        s.send("DOMStorage.setDOMStorageItem", {
            "storageId": sid, "key": k, "value": v,
        }, timeout=5)
    print(f"2. Wrote {len(test_data)} keys via DOMStorage.setDOMStorageItem")

    # Read back via DOMStorage.getDOMStorageItems (new collection path)
    result = s.send("DOMStorage.getDOMStorageItems", {"storageId": sid}, timeout=5)
    entries = {k: v for k, v in result.get("entries", [])}
    print(f"3. Read back via DOMStorage.getDOMStorageItems: {len(entries)} entries")
    for k, v in entries.items():
        expected = test_data.get(k)
        status = "OK" if v == expected else f"MISMATCH (expected {expected!r})"
        print(f"   {k}: {status}")

    # Also verify JS can see the values
    js_check = s.runtime.evaluate(
        "JSON.stringify({token: localStorage.getItem('token'), "
        "cache: localStorage.getItem('cache_version')})",
        timeout=5)
    print(f"4. JS Runtime.evaluate sees: {js_check}")

finally:
    s.close()

# Step 2: Simulate "delete localStorage from prototype" (like Discord does)
# and verify DOMStorage still works
print(f"\n--- Simulating Discord anti-bot: delete localStorage from Window.prototype ---\n")

info2 = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5).read())
tab2 = next(x for x in info2 if x["id"] == tid)
s2 = CDPSession(tab2["webSocketDebuggerUrl"], timeout=10)

try:
    # Delete localStorage from prototype (like Discord does)
    s2.runtime.evaluate("delete Window.prototype.localStorage", timeout=5)
    print("5. Deleted Window.prototype.localStorage")

    # JS can no longer see it
    js_type = s2.runtime.evaluate("typeof localStorage", timeout=5)
    print(f"   JS typeof localStorage: {js_type}")

    # But DOMStorage STILL works
    result2 = s2.send("DOMStorage.getDOMStorageItems", {
        "storageId": {"securityOrigin": origin, "isLocalStorage": True}
    }, timeout=5)
    entries2 = {k: v for k, v in result2.get("entries", [])}
    print(f"6. DOMStorage.getDOMStorageItems still returns: {len(entries2)} entries")
    for k in test_data:
        status = "OK" if entries2.get(k) == test_data[k] else "MISMATCH"
        print(f"   {k}: {status}")

    # And DOMStorage.setDOMStorageItem still works
    s2.send("DOMStorage.setDOMStorageItem", {
        "storageId": {"securityOrigin": origin, "isLocalStorage": True},
        "key": "post_delete_key", "value": "still_works",
    }, timeout=5)
    result3 = s2.send("DOMStorage.getDOMStorageItems", {
        "storageId": {"securityOrigin": origin, "isLocalStorage": True}
    }, timeout=5)
    post = {k: v for k, v in result3.get("entries", [])}
    print(f"7. Write after delete: {'OK' if post.get('post_delete_key') == 'still_works' else 'FAIL'}")

finally:
    s2.close()

# Cleanup
bs3 = CDPSession.connect_browser(port)
try:
    bs3.target.dispose_browser_context(ctx)
    print(f"\n8. Cleaned up context {ctx}")
except Exception as e:
    print(f"\nCleanup error: {e}")
finally:
    bs3.close()

print("\n=== DONE ===")
