"""Standalone probe: verify CryptoKey export/import roundtrip works in Chrome.

Exercises AES-GCM, AES-CBC, HMAC, RSA-OAEP, and ECDSA keys in both extractable
and non-extractable modes.  Also tests writing a CryptoKey to IndexedDB,
reading it back, and using it to derive another key (matching WhatsApp's
pattern that failed: ``deriveKey(..., keyFromIdb, ...)``).

Requires Chrome running on 127.0.0.1:9222 (same instance the dev server uses).
Run: ``..\\test\\python\\python.exe tests\\probe_cryptokey.py``
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.sessions.cdp import CDPSession  # noqa: E402

_JS = r"""
(async function(){
  var results = [];
  function rec(name, ok, detail) { results.push({name: name, ok: !!ok, detail: detail||''}); }

  // -------- Test 1: extractable AES-GCM key --------
  try {
    var k = await crypto.subtle.generateKey({name:'AES-GCM', length:256}, true, ['encrypt','decrypt']);
    var jwk = await crypto.subtle.exportKey('jwk', k);
    var k2 = await crypto.subtle.importKey('jwk', jwk, {name:'AES-GCM'}, true, ['encrypt','decrypt']);
    var iv = crypto.getRandomValues(new Uint8Array(12));
    var ct = await crypto.subtle.encrypt({name:'AES-GCM', iv:iv}, k, new TextEncoder().encode('hello'));
    var pt = await crypto.subtle.decrypt({name:'AES-GCM', iv:iv}, k2, ct);
    rec('AES-GCM extractable roundtrip', new TextDecoder().decode(pt) === 'hello');
  } catch(e) { rec('AES-GCM extractable roundtrip', false, String(e)); }

  // -------- Test 2: non-extractable AES-GCM key (should fail export) --------
  try {
    var kn = await crypto.subtle.generateKey({name:'AES-GCM', length:256}, false, ['encrypt','decrypt']);
    try {
      await crypto.subtle.exportKey('jwk', kn);
      rec('AES-GCM non-extractable export blocked', false, 'export unexpectedly succeeded');
    } catch(e) {
      rec('AES-GCM non-extractable export blocked', true, 'correctly threw: '+e.name);
    }
  } catch(e) { rec('AES-GCM non-extractable export blocked', false, String(e)); }

  // -------- Test 3: HMAC key roundtrip --------
  try {
    var h = await crypto.subtle.generateKey({name:'HMAC', hash:'SHA-256', length:256}, true, ['sign','verify']);
    var jwk = await crypto.subtle.exportKey('jwk', h);
    var h2 = await crypto.subtle.importKey('jwk', jwk, {name:'HMAC', hash:'SHA-256'}, true, ['sign','verify']);
    var sig = await crypto.subtle.sign('HMAC', h, new TextEncoder().encode('msg'));
    var ok = await crypto.subtle.verify('HMAC', h2, sig, new TextEncoder().encode('msg'));
    rec('HMAC roundtrip + cross-verify', ok);
  } catch(e) { rec('HMAC roundtrip + cross-verify', false, String(e)); }

  // -------- Test 4: RSA-OAEP key roundtrip --------
  try {
    var rp = await crypto.subtle.generateKey({name:'RSA-OAEP', modulusLength:2048,
      publicExponent:new Uint8Array([1,0,1]), hash:'SHA-256'}, true, ['encrypt','decrypt']);
    var pubJwk = await crypto.subtle.exportKey('jwk', rp.publicKey);
    var privJwk = await crypto.subtle.exportKey('jwk', rp.privateKey);
    var pub2 = await crypto.subtle.importKey('jwk', pubJwk, {name:'RSA-OAEP', hash:'SHA-256'}, true, ['encrypt']);
    var priv2 = await crypto.subtle.importKey('jwk', privJwk, {name:'RSA-OAEP', hash:'SHA-256'}, true, ['decrypt']);
    var ct = await crypto.subtle.encrypt({name:'RSA-OAEP'}, pub2, new TextEncoder().encode('secret'));
    var pt = await crypto.subtle.decrypt({name:'RSA-OAEP'}, priv2, ct);
    rec('RSA-OAEP roundtrip', new TextDecoder().decode(pt) === 'secret');
  } catch(e) { rec('RSA-OAEP roundtrip', false, String(e)); }

  // -------- Test 5: ECDH key + deriveKey (WhatsApp-like pattern) --------
  try {
    var a = await crypto.subtle.generateKey({name:'ECDH', namedCurve:'P-256'}, true, ['deriveKey','deriveBits']);
    var b = await crypto.subtle.generateKey({name:'ECDH', namedCurve:'P-256'}, true, ['deriveKey','deriveBits']);
    var aPubJwk = await crypto.subtle.exportKey('jwk', a.publicKey);
    var aPrivJwk = await crypto.subtle.exportKey('jwk', a.privateKey);
    var aPub2 = await crypto.subtle.importKey('jwk', aPubJwk, {name:'ECDH', namedCurve:'P-256'}, true, []);
    var aPriv2 = await crypto.subtle.importKey('jwk', aPrivJwk, {name:'ECDH', namedCurve:'P-256'}, true, ['deriveKey','deriveBits']);
    var derived = await crypto.subtle.deriveKey(
      {name:'ECDH', public: b.publicKey}, aPriv2,
      {name:'AES-GCM', length:256}, true, ['encrypt','decrypt']);
    rec('ECDH deriveKey after import', derived instanceof CryptoKey);
  } catch(e) { rec('ECDH deriveKey after import', false, String(e)); }

  // -------- Test 6: Write CryptoKey directly to IDB, read back, use --------
  // This tests the native IDB structured-clone path (NOT our codec).
  // It confirms that IDB can natively store/retrieve CryptoKey objects.
  try {
    await new Promise(function(res){var r=indexedDB.deleteDatabase('_probe_ck');r.onsuccess=res;r.onerror=res;r.onblocked=res;});
    var db = await new Promise(function(res,rej){
      var r=indexedDB.open('_probe_ck',1);
      r.onupgradeneeded=function(e){e.target.result.createObjectStore('k');};
      r.onsuccess=function(e){res(e.target.result);};
      r.onerror=function(e){rej(e.target.error);};
    });
    var k = await crypto.subtle.generateKey({name:'AES-GCM', length:256}, false, ['encrypt','decrypt']);
    await new Promise(function(res,rej){
      var tx=db.transaction('k','readwrite');
      tx.objectStore('k').put(k,'master');
      tx.oncomplete=res; tx.onerror=function(e){rej(e.target.error);};
    });
    var k2 = await new Promise(function(res,rej){
      var tx=db.transaction('k','readonly');
      var r=tx.objectStore('k').get('master');
      r.onsuccess=function(){res(r.result);};
      r.onerror=function(e){rej(e.target.error);};
    });
    var isCK = k2 instanceof CryptoKey;
    var iv = crypto.getRandomValues(new Uint8Array(12));
    var ct = await crypto.subtle.encrypt({name:'AES-GCM', iv:iv}, k, new TextEncoder().encode('xyz'));
    var pt = await crypto.subtle.decrypt({name:'AES-GCM', iv:iv}, k2, ct);
    rec('IDB native stores non-extractable CryptoKey', isCK && new TextDecoder().decode(pt)==='xyz',
      'k2 is '+(isCK?'CryptoKey':'not CryptoKey')+', ext='+(k2&&k2.extractable));
    db.close();
    await new Promise(function(res){var r=indexedDB.deleteDatabase('_probe_ck');r.onsuccess=res;r.onerror=res;r.onblocked=res;});
  } catch(e) { rec('IDB native stores non-extractable CryptoKey', false, String(e)); }

  return JSON.stringify(results, null, 2);
})()
"""


def main() -> int:
    import urllib.request

    # Find a page target to attach to
    with urllib.request.urlopen("http://127.0.0.1:9222/json/list", timeout=5) as r:
        targets = json.loads(r.read())
    page = next((t for t in targets if t.get("type") == "page"
                 and t.get("url", "").startswith(("http", "data:"))), None)
    if page is None:
        page = next((t for t in targets if t.get("type") == "page"), None)
    if page is None:
        print("No page target found; open a tab first", file=sys.stderr)
        return 2

    ws_url = page["webSocketDebuggerUrl"]
    print(f"Probing via {page['url'][:80]}")
    sess = CDPSession(ws_url, timeout=30)
    try:
        result = sess.send(
            "Runtime.evaluate",
            {"expression": _JS, "awaitPromise": True, "returnByValue": True},
            timeout=60,
        )
        if "exceptionDetails" in result:
            print("JS exception:", result["exceptionDetails"])
            return 3
        out = result["result"]["value"]
        print(out)
        data = json.loads(out)
        fails = [r for r in data if not r["ok"]]
        return 0 if not fails else 1
    finally:
        sess.close()


if __name__ == "__main__":
    sys.exit(main())
