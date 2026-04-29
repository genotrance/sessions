"""End-to-end probe: verify our _IDB_CODEC_JS roundtrips CryptoKey correctly.

Encodes CryptoKey via our dump-path codec (exportKey + __t:'CK' marker),
JSON-serializes, parses back, then runs our scaffolding's _decVAsync to
reconstruct the CryptoKey.  Finally uses the reconstructed key to decrypt
ciphertext produced by the original key — proving the roundtrip is
cryptographically sound.

Requires Chrome running on 127.0.0.1:9222.
Run: ``..\\test\\python\\python.exe tests\\probe_codec_cryptokey.py``
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.sessions.cdp import CDPSession  # noqa: E402
from src.sessions.idb import (  # noqa: E402
    _IDB_CODEC_JS, build_restore_scaffolding,
)


_JS = r"""
(async function(){
  // --- Pull in our actual encoder/decoder ---
  __CODEC__
  var results = [];
  function rec(name, ok, detail){ results.push({name:name, ok:!!ok, detail:detail||''}); }

  // Reuse the real dump-path CryptoKey encoder (mirrors _expandBlob branch)
  async function encCK(v){
    if (!(self.CryptoKey && v instanceof CryptoKey)) return _encV(v);
    var _alg=v.algorithm, _u=Array.from(v.usages||[]), _ex=v.extractable;
    if (!_ex) return {__t:'CK', ne:true, alg:_alg, u:_u};
    var _jwk = await crypto.subtle.exportKey('jwk', v);
    return {__t:'CK', jwk:_jwk, alg:_alg, u:_u, ex:_ex};
  }

  // --- Test 1: AES-GCM extractable roundtrip via our codec ---
  try {
    var k = await crypto.subtle.generateKey({name:'AES-GCM', length:256}, true, ['encrypt','decrypt']);
    var iv = crypto.getRandomValues(new Uint8Array(12));
    var ct = await crypto.subtle.encrypt({name:'AES-GCM', iv:iv}, k, new TextEncoder().encode('hello'));
    // Encode -> JSON -> decode
    var enc = await encCK(k);
    var json = JSON.stringify(enc);
    var parsed = JSON.parse(json);
    var k2 = await _decVAsync(parsed);
    var ok = k2 instanceof CryptoKey;
    var pt = await crypto.subtle.decrypt({name:'AES-GCM', iv:iv}, k2, ct);
    rec('AES-GCM codec roundtrip + decrypt', ok && new TextDecoder().decode(pt)==='hello');
  } catch(e) { rec('AES-GCM codec roundtrip + decrypt', false, String(e)); }

  // --- Test 2: HMAC roundtrip via our codec ---
  try {
    var h = await crypto.subtle.generateKey({name:'HMAC', hash:'SHA-256', length:256}, true, ['sign','verify']);
    var sig = await crypto.subtle.sign('HMAC', h, new TextEncoder().encode('msg'));
    var enc = await encCK(h);
    var h2 = await _decVAsync(JSON.parse(JSON.stringify(enc)));
    var ok = await crypto.subtle.verify('HMAC', h2, sig, new TextEncoder().encode('msg'));
    rec('HMAC codec roundtrip + verify', ok);
  } catch(e) { rec('HMAC codec roundtrip + verify', false, String(e)); }

  // --- Test 3: ECDH private key + deriveKey after codec roundtrip ---
  try {
    var a = await crypto.subtle.generateKey({name:'ECDH', namedCurve:'P-256'}, true, ['deriveKey','deriveBits']);
    var b = await crypto.subtle.generateKey({name:'ECDH', namedCurve:'P-256'}, true, ['deriveKey','deriveBits']);
    var encPriv = await encCK(a.privateKey);
    var aPriv2 = await _decVAsync(JSON.parse(JSON.stringify(encPriv)));
    var derived = await crypto.subtle.deriveKey(
      {name:'ECDH', public: b.publicKey}, aPriv2,
      {name:'AES-GCM', length:256}, true, ['encrypt','decrypt']);
    rec('ECDH deriveKey after codec roundtrip', derived instanceof CryptoKey);
  } catch(e) { rec('ECDH deriveKey after codec roundtrip', false, String(e)); }

  // --- Test 4: Nested row: {id, keys:[CryptoKey, CryptoKey]} roundtrip ---
  try {
    var k1 = await crypto.subtle.generateKey({name:'AES-GCM', length:128}, true, ['encrypt','decrypt']);
    var k2 = await crypto.subtle.generateKey({name:'AES-GCM', length:128}, true, ['encrypt','decrypt']);
    var row = {id: 'row1', keys: [k1, k2], created: new Date(2024, 0, 1)};
    // Manually walk and encode (mirrors _expandBlob)
    async function expand(v){
      if (v === null || typeof v !== 'object') return _encV(v);
      if (self.CryptoKey && v instanceof CryptoKey) return await encCK(v);
      if (v instanceof Date) return _encV(v);
      if (Array.isArray(v)) { var o=[]; for (var x of v) o.push(await expand(x)); return o; }
      if (v.constructor === Object) {
        var o={}; for (var k in v) o[k] = await expand(v[k]); return o;
      }
      return _encV(v);
    }
    var enc = await expand(row);
    var json = JSON.stringify(enc);
    var parsed = JSON.parse(json);
    var restored = await _decVAsync(parsed);
    var ok = restored.id === 'row1'
          && restored.keys.length === 2
          && restored.keys[0] instanceof CryptoKey
          && restored.keys[1] instanceof CryptoKey
          && restored.created instanceof Date
          && restored.created.getTime() === new Date(2024, 0, 1).getTime();
    rec('Nested row with CryptoKeys + Date roundtrip', ok,
        'keys[0] is '+(restored.keys[0]&&restored.keys[0].constructor.name)
      + ', created is '+(restored.created&&restored.created.constructor.name));
  } catch(e) { rec('Nested row with CryptoKeys + Date roundtrip', false, String(e)); }

  // --- Test 5: Non-extractable key produces a placeholder, not a crash ---
  try {
    var kn = await crypto.subtle.generateKey({name:'AES-GCM', length:256}, false, ['encrypt','decrypt']);
    var enc = await encCK(kn);
    var restored = await _decVAsync(JSON.parse(JSON.stringify(enc)));
    // Should yield null (with console warning)
    rec('Non-extractable key yields null placeholder', enc.ne === true && restored === null);
  } catch(e) { rec('Non-extractable key yields null placeholder', false, String(e)); }

  // --- Test 6: Full IDB row with CryptoKey persists through IDB put/get ---
  try {
    await new Promise(function(res){var r=indexedDB.deleteDatabase('_probe_codec');r.onsuccess=res;r.onerror=res;r.onblocked=res;});
    var db = await new Promise(function(res,rej){
      var r=indexedDB.open('_probe_codec',1);
      r.onupgradeneeded=function(e){e.target.result.createObjectStore('keys',{keyPath:'id'});};
      r.onsuccess=function(e){res(e.target.result);};
      r.onerror=function(e){rej(e.target.error);};
    });
    var k = await crypto.subtle.generateKey({name:'AES-GCM', length:256}, true, ['encrypt','decrypt']);
    var origRow = {id:'master', key:k};
    // Encode
    var enc = {id: origRow.id, key: await encCK(origRow.key)};
    var serialized = JSON.stringify(enc);
    // Decode + put into IDB
    var decoded = await _decVAsync(JSON.parse(serialized));
    await new Promise(function(res,rej){
      var tx=db.transaction('keys','readwrite');
      tx.objectStore('keys').put(decoded);
      tx.oncomplete=res; tx.onerror=function(e){rej(e.target.error);};
    });
    // Read back and use
    var row2 = await new Promise(function(res,rej){
      var tx=db.transaction('keys','readonly');
      var r=tx.objectStore('keys').get('master');
      r.onsuccess=function(){res(r.result);};
      r.onerror=function(e){rej(e.target.error);};
    });
    var iv = crypto.getRandomValues(new Uint8Array(12));
    var ct = await crypto.subtle.encrypt({name:'AES-GCM', iv:iv}, k, new TextEncoder().encode('payload'));
    var pt = await crypto.subtle.decrypt({name:'AES-GCM', iv:iv}, row2.key, ct);
    rec('Full IDB roundtrip with CryptoKey in row',
        row2.key instanceof CryptoKey && new TextDecoder().decode(pt) === 'payload');
    db.close();
    await new Promise(function(res){var r=indexedDB.deleteDatabase('_probe_codec');r.onsuccess=res;r.onerror=res;r.onblocked=res;});
  } catch(e) { rec('Full IDB roundtrip with CryptoKey in row', false, String(e)); }

  return JSON.stringify(results, null, 2);
})()
"""


def main() -> int:
    with urllib.request.urlopen("http://127.0.0.1:9222/json/list", timeout=5) as r:
        targets = json.loads(r.read())
    page = next((t for t in targets if t.get("type") == "page"
                 and t.get("url", "").startswith(("http", "data:"))), None)
    if page is None:
        page = next((t for t in targets if t.get("type") == "page"), None)
    if page is None:
        print("No page target found", file=sys.stderr)
        return 2

    # Build the test JS: inject the real codec (encoder + both decoders).
    # The scaffolding wraps the codec in an IIFE, so we extract _IDB_CODEC_JS
    # directly.  Both _encV and _decVAsync must be in scope.
    js = _JS.replace("__CODEC__", _IDB_CODEC_JS)

    sess = CDPSession(page["webSocketDebuggerUrl"], timeout=30)
    try:
        result = sess.send(
            "Runtime.evaluate",
            {"expression": js, "awaitPromise": True, "returnByValue": True},
            timeout=60,
        )
        if "exceptionDetails" in result:
            print("JS exception:", json.dumps(result["exceptionDetails"], indent=2))
            return 3
        out = result["result"]["value"]
        print(out)
        data = json.loads(out)
        fails = [r for r in data if not r["ok"]]
        if fails:
            print(f"\n{len(fails)}/{len(data)} tests FAILED")
            return 1
        print(f"\n{len(data)}/{len(data)} tests PASSED")
        return 0
    finally:
        sess.close()


if __name__ == "__main__":
    sys.exit(main())
