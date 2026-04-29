"""Check Discord's response headers and compare with a site where localStorage works."""
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
from sessions.cdp import CDPSession

port = 9222

bs = CDPSession.connect_browser(port)
try:
    ctx = bs.target.create_browser_context()
    print(f"Context: {ctx}")

    # Test 1: Check a known-good site
    tid1 = bs.target.create_target(url="about:blank", browser_context_id=ctx)
finally:
    bs.close()

time.sleep(1)
info = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5).read())
tab1 = next(x for x in info if x["id"] == tid1)
s1 = CDPSession(tab1["webSocketDebuggerUrl"], timeout=10)

try:
    # Enable network to capture headers
    s1.send("Network.enable", {})

    headers_captured = {}

    def capture_headers(params):
        url = params.get("response", {}).get("url", "")
        hdrs = params.get("response", {}).get("headers", {})
        if url and ("discord.com" in url or "example.com" in url):
            headers_captured[url] = hdrs

    s1._event_handlers = {"Network.responseReceived": capture_headers}

    # Navigate to example.com first
    s1.page.navigate("https://example.com/", wait_for_load=False)
    time.sleep(2)

    ls_test = s1.runtime.evaluate("typeof localStorage", timeout=5)
    print(f"\n=== example.com ===")
    print(f"  typeof localStorage: {ls_test}")
    ls_len = s1.runtime.evaluate("try{localStorage.length}catch(e){'ERR:'+e}", timeout=5)
    print(f"  localStorage.length: {ls_len}")
finally:
    s1.close()

# Now test Discord in the same context
bs2 = CDPSession.connect_browser(port)
try:
    tid2 = bs2.target.create_target(url="about:blank", browser_context_id=ctx)
finally:
    bs2.close()

time.sleep(1)
info2 = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5).read())
tab2 = next(x for x in info2 if x["id"] == tid2)
s2 = CDPSession(tab2["webSocketDebuggerUrl"], timeout=10)

try:
    # Enable network
    s2.send("Network.enable", {})

    response_headers = {}

    def on_response(msg):
        if msg.get("method") == "Network.responseReceived":
            params = msg.get("params", {})
            resp = params.get("response", {})
            url = resp.get("url", "")
            if "discord.com" in url and "/login" in url:
                response_headers.update(resp.get("headers", {}))

    s2.page.navigate("https://discord.com/login", wait_for_load=False)
    time.sleep(4)

    print(f"\n=== discord.com/login ===")
    ls_test2 = s2.runtime.evaluate("typeof localStorage", timeout=5)
    print(f"  typeof localStorage: {ls_test2}")

    # Try to get the response headers via CDP
    # Use Network.getResponseBody or check captured events
    # Let's use a simpler approach: check page's CSP
    csp = s2.runtime.evaluate(
        "document.querySelector('meta[http-equiv=\"Content-Security-Policy\"]')?.content || 'none'",
        timeout=5)
    print(f"  CSP meta tag: {csp}")

    # Check if page is sandboxed
    sandbox = s2.runtime.evaluate("document.featurePolicy ? 'yes' : 'no'", timeout=5)
    print(f"  featurePolicy: {sandbox}")

    # Check origin
    origin = s2.runtime.evaluate("window.origin", timeout=5)
    print(f"  window.origin: {origin}")

    # Check if page is cross-origin isolated
    coi = s2.runtime.evaluate("window.crossOriginIsolated", timeout=5)
    print(f"  crossOriginIsolated: {coi}")

    # Check isSecureContext
    secure = s2.runtime.evaluate("window.isSecureContext", timeout=5)
    print(f"  isSecureContext: {secure}")

    # Check navigator.storage
    storage_persist = s2.runtime.evaluate(
        "(async()=>{try{return await navigator.storage.persisted();}catch(e){return 'ERR:'+e.message;}})()",
        await_promise=True, timeout=5)
    print(f"  storage.persisted: {storage_persist}")

    # Check if Discord JS is overriding localStorage
    proto = s2.runtime.evaluate(
        "Object.getOwnPropertyDescriptor(window.__proto__, 'localStorage')?.get ? 'has getter' : 'no getter'",
        timeout=5)
    print(f"  Window.prototype localStorage getter: {proto}")

    # Check the Window prototype chain
    proto2 = s2.runtime.evaluate(
        "Object.getOwnPropertyDescriptor(Window.prototype, 'localStorage')?.get ? 'has getter' : 'no getter'",
        timeout=5)
    print(f"  Window.prototype localStorage descriptor: {proto2}")

    # Check if Discord deleted it
    proto3 = s2.runtime.evaluate(
        """
        (function(){
            try {
                var desc = Object.getOwnPropertyDescriptor(Window.prototype, 'localStorage');
                if (!desc) return 'no descriptor on Window.prototype';
                if (desc.get) {
                    try { return 'getter exists, calling: ' + typeof desc.get.call(window); }
                    catch(e) { return 'getter throws: ' + e.message; }
                }
                return 'descriptor exists but no getter: ' + JSON.stringify(Object.keys(desc));
            } catch(e) { return 'error: ' + e.message; }
        })()
        """,
        timeout=5)
    print(f"  Deep localStorage probe: {proto3}")

finally:
    s2.close()

# Cleanup
bs3 = CDPSession.connect_browser(port)
try:
    bs3.target.dispose_browser_context(ctx)
    print(f"\nDisposed context {ctx}")
except Exception as e:
    print(f"Cleanup error: {e}")
finally:
    bs3.close()
