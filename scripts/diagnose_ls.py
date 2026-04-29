"""Deep investigation: why localStorage is not defined on Discord tab."""
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
from sessions.cdp import CDPSession

port = 9222

# 1) Check Discord tab with detailed window.localStorage probing
print("=== 1. Discord tab localStorage investigation ===")
info = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5).read())
discord_tabs = [t for t in info if "discord.com" in t.get("url", "") and t["type"] == "page"]

for t in discord_tabs:
    ws = t.get("webSocketDebuggerUrl")
    if not ws:
        continue
    s = CDPSession(ws, timeout=10)
    try:
        checks = [
            ("typeof localStorage", "typeof localStorage"),
            ("typeof window.localStorage", "typeof window.localStorage"),
            ("window.localStorage === undefined", "window.localStorage === undefined"),
            ("'localStorage' in window", "'localStorage' in window"),
            ("typeof window.sessionStorage", "typeof window.sessionStorage"),
            ("typeof window.indexedDB", "typeof window.indexedDB"),
            # Check if we're in the right frame
            ("window === self", "window === self"),
            ("window.top === window", "window.top === window"),
            ("window.location.href", "window.location.href"),
        ]
        for name, expr in checks:
            try:
                val = s.runtime.evaluate(expr, timeout=5)
                print(f"  {name} = {val}")
            except Exception as e:
                print(f"  {name} = EXCEPTION: {e}")
    finally:
        s.close()

# 2) Create a FRESH browser context and test localStorage there
print("\n=== 2. Fresh browser context localStorage test ===")
bs = CDPSession.connect_browser(port)
try:
    ctx = bs.target.create_browser_context()
    print(f"  Created context: {ctx}")
    tid = bs.target.create_target(url="https://discord.com/login", browser_context_id=ctx)
    print(f"  Created tab: {tid}")
finally:
    bs.close()

time.sleep(3)

info2 = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5).read())
tab_info = next((x for x in info2 if x["id"] == tid), None)
if tab_info and tab_info.get("webSocketDebuggerUrl"):
    s2 = CDPSession(tab_info["webSocketDebuggerUrl"], timeout=10)
    try:
        checks2 = [
            ("typeof localStorage", "typeof localStorage"),
            ("typeof window.localStorage", "typeof window.localStorage"),
            ("window.location.href", "window.location.href"),
            ("localStorage.length", "try{localStorage.length}catch(e){'ERR:'+e.message}"),
        ]
        print(f"  Fresh tab URL: {tab_info['url']}")
        for name, expr in checks2:
            try:
                val = s2.runtime.evaluate(expr, timeout=5)
                print(f"  {name} = {val}")
            except Exception as e:
                print(f"  {name} = EXCEPTION: {e}")
    finally:
        s2.close()
else:
    print("  Could not find fresh tab WS URL")

# 3) Now test with about:blank + addScriptToEvaluateOnNewDocument + navigate
# (mimicking our restore flow)
print("\n=== 3. Restore-flow test: about:blank + inject + navigate ===")
bs2 = CDPSession.connect_browser(port)
try:
    tid2 = bs2.target.create_target(url="about:blank", browser_context_id=ctx)
    print(f"  Created blank tab: {tid2}")
finally:
    bs2.close()

time.sleep(1)
info3 = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5).read())
tab3 = next((x for x in info3 if x["id"] == tid2), None)

if tab3 and tab3.get("webSocketDebuggerUrl"):
    s3 = CDPSession(tab3["webSocketDebuggerUrl"], timeout=10)
    try:
        # Add a script (like our restore does)
        s3.page.add_script_to_evaluate_on_new_document(
            "(function(){try{localStorage.setItem('_test','1');}catch(e){console.log('inject-err:'+e);}})();")
        # Navigate to Discord
        s3.page.navigate("https://discord.com/login", wait_for_load=False)
        time.sleep(3)

        checks3 = [
            ("typeof localStorage", "typeof localStorage"),
            ("typeof window.localStorage", "typeof window.localStorage"),
            ("window.location.href", "window.location.href"),
            ("ls.length", "try{localStorage.length}catch(e){'ERR:'+e.message}"),
            ("ls._test", "try{localStorage.getItem('_test')}catch(e){'ERR:'+e.message}"),
        ]
        for name, expr in checks3:
            try:
                val = s3.runtime.evaluate(expr, timeout=5)
                print(f"  {name} = {val}")
            except Exception as e:
                print(f"  {name} = EXCEPTION: {e}")
    finally:
        s3.close()

# Cleanup
print("\n=== Cleanup ===")
bs3 = CDPSession.connect_browser(port)
try:
    bs3.target.dispose_browser_context(ctx)
    print(f"  Disposed context {ctx}")
except Exception as e:
    print(f"  Cleanup error: {e}")
finally:
    bs3.close()
