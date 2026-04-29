"""Diagnose why Discord tab shows 0 localStorage."""
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
from sessions.cdp import CDPSession

info = json.loads(urllib.request.urlopen("http://127.0.0.1:9222/json/list", timeout=5).read())
discord_tabs = [t for t in info if "discord.com" in t.get("url", "")]

if not discord_tabs:
    print("[!] No Discord tabs found")
    sys.exit(1)

for t in discord_tabs:
    tid = t["id"]
    url = t["url"]
    ttype = t["type"]
    print(f"Tab: {tid}")
    print(f"  URL: {url}")
    print(f"  Type: {ttype}")

    ws = t.get("webSocketDebuggerUrl")
    if not ws or ttype != "page":
        print("  (skipped - no ws or not a page)")
        continue

    s = CDPSession(ws, timeout=10)
    try:
        checks = [
            ("origin",        "window.location.origin"),
            ("href",          "window.location.href"),
            ("title",         "document.title"),
            ("readyState",    "document.readyState"),
            ("bodyLen",       "document.body ? document.body.innerHTML.length : -1"),
            ("ls.length",     "try{localStorage.length}catch(e){'ERR:'+e.message}"),
            ("ls.keys",       "try{JSON.stringify(Object.keys(localStorage).slice(0,20))}catch(e){'ERR:'+e.message}"),
            ("has_token",     "try{localStorage.getItem('token')!==null}catch(e){'ERR:'+e.message}"),
            ("cookie.len",    "try{document.cookie.length}catch(e){'ERR:'+e.message}"),
            ("cookie(200)",   "try{document.cookie.substring(0,200)}catch(e){'ERR:'+e.message}"),
        ]
        for name, expr in checks:
            try:
                val = s.runtime.evaluate(expr, timeout=5)
                print(f"  {name}: {val}")
            except Exception as e:
                print(f"  {name}: EXCEPTION: {e}")

        # Async checks
        async_checks = [
            ("idb.databases", "(async()=>{try{const dbs=await indexedDB.databases();return JSON.stringify(dbs.map(d=>d.name));}catch(e){return 'ERR:'+e.message;}})()"),
            ("sw.regs",       "(async()=>{try{const r=await navigator.serviceWorker.getRegistrations();return JSON.stringify(r.map(x=>x.scope));}catch(e){return 'ERR:'+e.message;}})()"),
        ]
        for name, expr in async_checks:
            try:
                val = s.runtime.evaluate(expr, await_promise=True, timeout=5)
                print(f"  {name}: {val}")
            except Exception as e:
                print(f"  {name}: EXCEPTION: {e}")
    finally:
        s.close()
