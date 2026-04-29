"""IndexedDB dump and restore helpers for session hibernation.

Handles the full structured-clone type set that IDB supports (ArrayBuffer,
TypedArrays, Date, RegExp, Map, Set, Blob, CryptoKey), which plain
JSON.stringify silently corrupts.  This is why WhatsApp/Discord used to
fail with "database error" after restore — their rows contain binary data,
Dates, and CryptoKey objects that were being turned into empty objects.

The restore pipeline injects a scaffolding script via
``addScriptToEvaluateOnNewDocument`` that:

1. Monkey-patches destructive IDB and localStorage APIs on the main thread
   (``deleteDatabase``, ``deleteObjectStore``, ``clear``, ``delete``,
   ``cursor.delete``, ``Storage.removeItem``, ``Storage.clear``) for a 60s
   protection window while the restored site initializes.
2. Wraps ``Worker`` / ``SharedWorker`` constructors so the same IDB
   blockers also run inside workers (which have their own prototype chain
   and would otherwise wipe worker-managed databases like ``fts-storage``).
3. Queues page-initiated ``indexedDB.open`` calls until our restore writes
   complete, preventing races where the site opens a DB mid-restore.

Security considerations:

- **Private key material**: extractable CryptoKeys are exported as JWK and
  stored in the snapshot file.  The snapshot DB lives under %APPDATA%
  (Windows) or the user's home dir (Linux/Mac) with user-only ACLs.
  Non-extractable keys cannot be exported by design and are stored as
  ``{__t:'CK', ne:true}`` placeholders with a clear console warning on
  dump.  Apps relying on non-extractable keys (e.g., WhatsApp's
  ``wawc_db_enc``) cannot be fully restored across browser profiles;
  this is the browser's security boundary.
- **Worker blob URLs**: the Worker wrapper builds a ``Blob`` from static
  blocker JS (no user-supplied data is interpolated).  Sites whose CSP
  forbids blob-scheme workers will reject the wrapper; we fall back to
  constructing the original ``Worker(url, options)`` directly.  No
  additional attack surface is introduced.
- **Monkey-patch scope**: every patch reverts on protection expiry (60s
  default) so long-lived page behavior is unaffected.
"""
from __future__ import annotations

import json

# ---------------------------------------------------------------------------
# Codec JS — shared by both dump and restore scripts
# ---------------------------------------------------------------------------

_IDB_CODEC_JS = r"""
function _b64enc(u8){
  var s='';
  for(var i=0;i<u8.length;i++)s+=String.fromCharCode(u8[i]);
  return btoa(s);
}
function _b64dec(str){
  var bin=atob(str);var u=new Uint8Array(bin.length);
  for(var i=0;i<bin.length;i++)u[i]=bin.charCodeAt(i);
  return u;
}
function _encV(v,seen){
  if(v===null)return null;
  if(v===undefined)return {__t:'U'};
  var t=typeof v;
  if(t==='number'||t==='string'||t==='boolean')return v;
  if(t==='bigint')return {__t:'BI',v:v.toString()};
  if(t!=='object')return null;
  seen=seen||new WeakSet();
  if(seen.has(v))return null;
  try{seen.add(v);}catch(e){}
  if(v instanceof Date)return {__t:'D',v:v.getTime()};
  if(v instanceof RegExp)return {__t:'R',s:v.source,f:v.flags};
  if(v instanceof ArrayBuffer)return {__t:'AB',v:_b64enc(new Uint8Array(v))};
  if(typeof SharedArrayBuffer!=='undefined'&&v instanceof SharedArrayBuffer)
    return {__t:'AB',v:_b64enc(new Uint8Array(v))};
  if(ArrayBuffer.isView(v)){
    var name=v.constructor.name;
    var u8=new Uint8Array(v.buffer,v.byteOffset,v.byteLength);
    return {__t:'TA',n:name,v:_b64enc(u8)};
  }
  if(v instanceof Map){
    var es=[];v.forEach(function(val,key){es.push([_encV(key,seen),_encV(val,seen)]);});
    return {__t:'M',v:es};
  }
  if(v instanceof Set){
    var arr=[];v.forEach(function(val){arr.push(_encV(val,seen));});
    return {__t:'S',v:arr};
  }
  if(Array.isArray(v)){
    var out=new Array(v.length);
    for(var i=0;i<v.length;i++)out[i]=_encV(v[i],seen);
    return out;
  }
  // Plain object — skip non-cloneable objects (functions, CryptoKey, etc.)
  var proto=Object.getPrototypeOf(v);
  if(proto!==null&&proto!==Object.prototype)return null;
  var r={};
  for(var k in v){
    if(Object.prototype.hasOwnProperty.call(v,k)){
      try{r[k]=_encV(v[k],seen);}catch(e){r[k]=null;}
    }
  }
  return r;
}
function _decV(v){
  if(v===null||v===undefined)return v;
  if(typeof v!=='object')return v;
  if(Array.isArray(v)){var out=new Array(v.length);
    for(var i=0;i<v.length;i++)out[i]=_decV(v[i]);return out;}
  var t=v.__t;
  if(t==='U')return undefined;
  if(t==='BI')return BigInt(v.v);
  if(t==='D')return new Date(v.v);
  if(t==='R')return new RegExp(v.s,v.f);
  if(t==='AB')return _b64dec(v.v).buffer;
  if(t==='TA'){
    var u8=_b64dec(v.v);
    var Ctor=self[v.n]||Uint8Array;
    if(v.n==='Uint8Array')return u8;
    if(v.n==='DataView')return new DataView(u8.buffer);
    var bpe=Ctor.BYTES_PER_ELEMENT||1;
    return new Ctor(u8.buffer,0,u8.byteLength/bpe);
  }
  if(t==='Bl'){var u=_b64dec(v.v);
    return new Blob([u],{type:v.m||''});}
  if(t==='M'){var m=new Map();
    for(var j=0;j<v.v.length;j++)m.set(_decV(v.v[j][0]),_decV(v.v[j][1]));
    return m;}
  if(t==='S'){var s=new Set();
    for(var k=0;k<v.v.length;k++)s.add(_decV(v.v[k]));return s;}
  var r={};
  for(var kk in v){if(Object.prototype.hasOwnProperty.call(v,kk))r[kk]=_decV(v[kk]);}
  return r;
}
// Async decoder — additionally resolves {__t:'CK'} CryptoKey markers via
// SubtleCrypto.importKey.  Use when rows may contain CryptoKey values.
async function _decVAsync(v){
  if(v===null||v===undefined)return v;
  if(typeof v!=='object')return v;
  if(Array.isArray(v)){
    var out=new Array(v.length);
    for(var i=0;i<v.length;i++)out[i]=await _decVAsync(v[i]);
    return out;
  }
  var t=v.__t;
  if(t==='CK'){
    if(v.ne||!v.jwk){
      console.warn('[IDB restore] non-extractable CryptoKey placeholder; value lost');
      return null;
    }
    try{
      return await crypto.subtle.importKey(
        'jwk',v.jwk,v.alg,v.ex!==false,v.u||[]);
    }catch(e){
      console.warn('[IDB restore] CryptoKey importKey failed:',e&&e.message||e);
      return null;
    }
  }
  if(t==='M'){
    var m=new Map();
    for(var j=0;j<v.v.length;j++){
      m.set(await _decVAsync(v.v[j][0]),await _decVAsync(v.v[j][1]));
    }
    return m;
  }
  if(t==='S'){
    var s=new Set();
    for(var k2=0;k2<v.v.length;k2++)s.add(await _decVAsync(v.v[k2]));
    return s;
  }
  // Other leaf markers (U, BI, D, R, AB, TA, Bl) cannot contain CryptoKeys.
  if(t)return _decV(v);
  // Plain object — recurse so nested CryptoKeys resolve.
  var r={};
  for(var kk in v){
    if(Object.prototype.hasOwnProperty.call(v,kk))r[kk]=await _decVAsync(v[kk]);
  }
  return r;
}
"""

# ---------------------------------------------------------------------------
# Worker-side IDB blocker — injected into Web Workers to prevent them from
# wiping IndexedDB data after restore.  The ``_until`` variable is defined
# by the caller as an absolute ms timestamp.  Blocks destructive ops
# (deleteDatabase, deleteObjectStore, clear, delete, cursor.delete) while
# Date.now() < _until.  Uses ``self`` (available in both window & worker
# contexts) so the same code runs in a Worker.
# ---------------------------------------------------------------------------

_WORKER_IDB_BLOCKER_JS = r"""
var _wlogged=0;
var _wlog=function(m){if(_wlogged++<50)console.log('[Worker IDB]',m);};
var _active=function(){return Date.now()<_until;};
if(self.indexedDB){
  var _oD=self.indexedDB.deleteDatabase.bind(self.indexedDB);
  self.indexedDB.deleteDatabase=function(n){
    if(_active()){_wlog('BLOCKED deleteDatabase: '+n);
      var r={readyState:'done',result:undefined,error:null,_evts:{},
        addEventListener:function(t,f){(this._evts[t]=this._evts[t]||[]).push(f);},
        removeEventListener:function(t,f){this._evts[t]=(this._evts[t]||[]).filter(function(x){return x!==f;});},
        dispatchEvent:function(){return true;}};
      setTimeout(function(){var ev={target:r,type:'success'};
        if(r.onsuccess)r.onsuccess(ev);
        (r._evts.success||[]).forEach(function(f){f(ev);});},0);
      return r;
    }
    return _oD(n);
  };
}
if(self.IDBObjectStore){
  var _oC=self.IDBObjectStore.prototype.clear;
  self.IDBObjectStore.prototype.clear=function(){
    if(_active()){_wlog('BLOCKED clear on '+this.name);
      var r={readyState:'done',result:undefined,error:null,source:this,transaction:this.transaction};
      Promise.resolve().then(function(){if(r.onsuccess)r.onsuccess({target:r,type:'success'});});
      return r;
    }
    return _oC.call(this);
  };
  var _oDR=self.IDBObjectStore.prototype.delete;
  self.IDBObjectStore.prototype.delete=function(k){
    if(_active()){_wlog('BLOCKED delete on '+this.name);
      var r={readyState:'done',result:undefined,error:null,source:this,transaction:this.transaction};
      Promise.resolve().then(function(){if(r.onsuccess)r.onsuccess({target:r,type:'success'});});
      return r;
    }
    return _oDR.call(this,k);
  };
}
if(self.IDBDatabase){
  var _oDS=self.IDBDatabase.prototype.deleteObjectStore;
  self.IDBDatabase.prototype.deleteObjectStore=function(n){
    if(_active()){_wlog('BLOCKED deleteObjectStore: '+this.name+'/'+n);return;}
    return _oDS.call(this,n);
  };
}
if(self.IDBCursor){
  var _oCD=self.IDBCursor.prototype.delete;
  self.IDBCursor.prototype.delete=function(){
    if(_active()){_wlog('BLOCKED cursor.delete');
      var r={readyState:'done',result:undefined,error:null};
      Promise.resolve().then(function(){if(r.onsuccess)r.onsuccess({target:r,type:'success'});});
      return r;
    }
    return _oCD.call(this);
  };
}
console.log('[Worker IDB] blocker installed, active until '+new Date(_until).toISOString());
"""

# ---------------------------------------------------------------------------
# Quick metadata-only script — returns [{n: name, v: version}, ...]
# Used to enumerate databases before per-database dumps.
# ---------------------------------------------------------------------------

IDB_LIST_JS = r"""(async function(){
  try {
    if (!indexedDB.databases) return JSON.stringify([]);
    const dbs = await indexedDB.databases();
    return JSON.stringify(dbs.map(function(d){return {n:d.name,v:d.version};}));
  } catch(e) { return JSON.stringify([]); }
})()"""

# ---------------------------------------------------------------------------
# Per-database dump — dumps a single database by name.
# Much more resilient than the monolithic dump because one slow/blocked
# database doesn't prevent the others from being captured.
# ---------------------------------------------------------------------------


def build_single_db_dump_js(db_name: str) -> str:
    """Return a JS async IIFE that dumps a single IndexedDB database."""
    escaped = json.dumps(db_name)
    return (
        "(async function(){" + _IDB_CODEC_JS + ""
        "try{"
        "var name=" + escaped + ";"
        "var db;try{db=await new Promise(function(res,rej){"
        "var r=indexedDB.open(name);"
        "r.onerror=function(){rej(r.error);};"
        "r.onblocked=function(){rej(new Error('blocked'));};"
        "r.onsuccess=function(){res(r.result);};});"
        "}catch(e){return JSON.stringify({});}"
        "var dbData={_meta:{version:db.version}};"
        "var storeNames=Array.from(db.objectStoreNames);"
        "for(var si=0;si<storeNames.length;si++){"
        "var storeName=storeNames[si];"
        "try{"
        "var tx=db.transaction(storeName,'readonly');"
        "var store=tx.objectStore(storeName);"
        "var rawRows=await new Promise(function(res,rej){"
        "var r=store.getAll();r.onerror=function(){rej(r.error);};"
        "r.onsuccess=function(){res(r.result);};});"
        "var rawKeys;try{rawKeys=await new Promise(function(res,rej){"
        "var r=store.getAllKeys();r.onerror=function(){rej(r.error);};"
        "r.onsuccess=function(){res(r.result);};});"
        "}catch(e){rawKeys=rawRows.map(function(_,i){return i;});}"
        "async function _expandBlob(v,depth){"
        "if(depth>20)return null;"
        "if(v&&typeof v==='object'){"
        "if(v instanceof Blob){var buf=await v.arrayBuffer();"
        "return {__t:'Bl',m:v.type||'',v:_b64enc(new Uint8Array(buf))};}"
        "if(self.CryptoKey&&v instanceof CryptoKey){"
        "var _alg=v.algorithm,_u=Array.from(v.usages||[]),_ex=v.extractable;"
        "if(!_ex){console.warn("
        "'[IDB dump] non-extractable CryptoKey cannot be exported',"
        "_alg&&_alg.name);"
        "return {__t:'CK',ne:true,alg:_alg,u:_u};}"
        "try{var _jwk=await crypto.subtle.exportKey('jwk',v);"
        "return {__t:'CK',jwk:_jwk,alg:_alg,u:_u,ex:_ex};"
        "}catch(e){console.warn('[IDB dump] CryptoKey export failed:',"
        "e&&e.message||e);"
        "return {__t:'CK',ne:true,alg:_alg,u:_u,err:String(e)};}}"
        "if(Array.isArray(v)){var out2=new Array(v.length);"
        "for(var i=0;i<v.length;i++)out2[i]=await _expandBlob(v[i],depth+1);return out2;}"
        "if(v.constructor===Object){var out3={};"
        "for(var k in v)if(Object.prototype.hasOwnProperty.call(v,k)){"
        "out3[k]=await _expandBlob(v[k],depth+1);}return out3;}}"
        "return v;}"
        "var encRows=[];"
        "for(var ri=0;ri<rawRows.length;ri++){"
        "try{encRows.push(_encV(await _expandBlob(rawRows[ri],0)));}catch(e){encRows.push(null);}}"
        "var encKeys=rawKeys.map(function(k){try{return _encV(k);}catch(e){return null;}});"
        "var keyPath=store.keyPath;"
        "var indexes=[];"
        "var idxNames=Array.from(store.indexNames);"
        "for(var ii=0;ii<idxNames.length;ii++){try{"
        "var idx=store.index(idxNames[ii]);"
        "indexes.push({name:idx.name,keyPath:idx.keyPath,"
        "unique:idx.unique,multiEntry:idx.multiEntry});}catch(e){}}"
        "dbData[storeName]={rows:encRows,keys:encKeys,"
        "keyPath:(keyPath==null||keyPath==='')?null:keyPath,"
        "autoIncrement:store.autoIncrement,indexes:indexes};"
        "}catch(e){}}"
        "try{db.close();}catch(e){}"
        "return JSON.stringify(dbData);"
        "}catch(e){return JSON.stringify({});}"
        "})()"
    )


# ---------------------------------------------------------------------------
# Monolithic dump script — kept for backward-compat and tests.
# Prefer per-database dumps (build_single_db_dump_js) in _collect_state.
# ---------------------------------------------------------------------------

IDB_DUMP_JS = "(async function(){" + _IDB_CODEC_JS + r"""
  try {
    if (!indexedDB.databases) return JSON.stringify({});
    const dbs = await indexedDB.databases();
    const out = {};
    for (const info of dbs) {
      const name = info.name;
      if (!name) continue;
      let db;
      try {
        db = await new Promise((res,rej)=>{
          const r=indexedDB.open(name);
          r.onerror=()=>rej(r.error);
          r.onblocked=()=>rej(new Error('blocked'));
          r.onsuccess=()=>res(r.result);
        });
      } catch(e) { continue; }
      const dbData = {_meta: {version: db.version}};
      for (const storeName of Array.from(db.objectStoreNames)) {
        try {
          const tx = db.transaction(storeName, 'readonly');
          const store = tx.objectStore(storeName);
          const rawRows = await new Promise((res,rej)=>{
            const r=store.getAll(); r.onerror=()=>rej(r.error);
            r.onsuccess=()=>res(r.result);
          });
          let rawKeys;
          try {
            rawKeys = await new Promise((res,rej)=>{
              const r=store.getAllKeys(); r.onerror=()=>rej(r.error);
              r.onsuccess=()=>res(r.result);
            });
          } catch(e) { rawKeys = rawRows.map((_,i)=>i); }
          async function _expandBlob(v, depth) {
            if (depth > 20) return null;
            if (v && typeof v === 'object') {
              if (v instanceof Blob) {
                const buf = await v.arrayBuffer();
                return {__t:'Bl', m: v.type||'', v: _b64enc(new Uint8Array(buf))};
              }
              if (self.CryptoKey && v instanceof CryptoKey) {
                const _alg = v.algorithm;
                const _u = Array.from(v.usages||[]);
                const _ex = v.extractable;
                if (!_ex) {
                  console.warn('[IDB dump] non-extractable CryptoKey cannot be exported', _alg && _alg.name);
                  return {__t:'CK', ne:true, alg:_alg, u:_u};
                }
                try {
                  const _jwk = await crypto.subtle.exportKey('jwk', v);
                  return {__t:'CK', jwk:_jwk, alg:_alg, u:_u, ex:_ex};
                } catch(e) {
                  console.warn('[IDB dump] CryptoKey export failed:', e && e.message || e);
                  return {__t:'CK', ne:true, alg:_alg, u:_u, err: String(e)};
                }
              }
              if (Array.isArray(v)) {
                const out2 = new Array(v.length);
                for (let i=0;i<v.length;i++) out2[i] = await _expandBlob(v[i], depth+1);
                return out2;
              }
              if (v.constructor === Object) {
                const out2 = {};
                for (const k in v) if (Object.prototype.hasOwnProperty.call(v,k)) {
                  out2[k] = await _expandBlob(v[k], depth+1);
                }
                return out2;
              }
            }
            return v;
          }
          const encRows = [];
          for (const r of rawRows) {
            try { encRows.push(_encV(await _expandBlob(r, 0))); }
            catch(e) { encRows.push(null); }
          }
          const encKeys = rawKeys.map(k => { try { return _encV(k); } catch(e) { return null; } });
          const keyPath = store.keyPath;
          const indexes = [];
          for (const idxName of Array.from(store.indexNames)) {
            try {
              const idx = store.index(idxName);
              indexes.push({name: idx.name, keyPath: idx.keyPath,
                unique: idx.unique, multiEntry: idx.multiEntry});
            } catch(e) {}
          }
          dbData[storeName] = {rows: encRows, keys: encKeys,
            keyPath: (keyPath==null||keyPath==="") ? null : keyPath,
            autoIncrement: store.autoIncrement, indexes};
        } catch(e) {}
      }
      try { db.close(); } catch(e) {}
      out[name] = dbData;
    }
    return JSON.stringify(out);
  } catch(e) { return JSON.stringify({__error: String(e)}); }
})()"""

# ---------------------------------------------------------------------------
# Restore script builder
# ---------------------------------------------------------------------------


def build_restore_script(idb_by_db: dict) -> str:
    """Return a JS IIFE that fully restores IndexedDB databases from a snapshot.

    idb_by_db: { dbName: { _meta: {version: int},
                            storeName: {rows, keys, keyPath, autoIncrement,
                                        indexes: [{name, keyPath, unique, multiEntry}]}
                          } }

    The script:
    1. Deletes the existing database so onupgradeneeded always fires.
    2. Opens at the saved version so the app sees the correct version.
    3. Recreates every object store with its full keyPath (including compound
       array key paths) and autoIncrement setting.
    4. Recreates every index with unique/multiEntry flags.
    5. Decodes rows (binary data, Dates, Maps, Sets) and inserts them with
       their original keys.

    It blocks the page's own indexedDB.open calls by monkey-patching
    indexedDB.open until restoration completes, preventing race conditions.
    """
    payload = json.dumps(idb_by_db)
    return (
        "(function(){"
        "try{"
        + _IDB_CODEC_JS +
        "var _idb=" + payload + ";"
        "var _dbNames=Object.keys(_idb);"
        "console.log('[IDB restore] starting',_dbNames.length,'databases:',_dbNames.join(', '));"
        "if(!_dbNames.length)return;"
        # Block the page's own DB opens until we finish restoring
        "var _pending=_dbNames.length;"
        "var _origOpen=indexedDB.open.bind(indexedDB);"
        "var _origDel=indexedDB.deleteDatabase.bind(indexedDB);"
        "var _unblock=function(){indexedDB.open=_origOpen;indexedDB.deleteDatabase=_origDel;"
        "console.log('[IDB restore] unblocked, all done');};"
        "var _done=function(dbN){_pending--;console.log('[IDB restore] db done:',dbN,'remaining:',_pending);"
        "if(_pending<=0)_unblock();};"
        # Safety timeout: unblock after 15s even if restoration is incomplete
        "setTimeout(function(){if(_pending>0){"
        "console.warn('[IDB restore] TIMEOUT: forcing unblock, pending=',_pending);"
        "_pending=0;_unblock();}},15000);"
        "indexedDB.open=function(n,v){"
        "if(_pending<=0)return _origOpen(n,v);"
        # Return a request-like object that waits for restore to finish.
        # Must support both .onsuccess-style AND addEventListener() so that
        # sites like WhatsApp (which use the EventTarget API) get callbacks.
        "var fakeReq={readyState:'pending',_evts:{},"
        "addEventListener:function(t,fn){(this._evts[t]=this._evts[t]||[]).push(fn);},"
        "removeEventListener:function(t,fn){"
        "this._evts[t]=(this._evts[t]||[]).filter(function(f){return f!==fn;});},"
        "dispatchEvent:function(){return true;}};"
        "var _q=function(){var r=_origOpen(n,v);"
        "r.onsuccess=function(e){fakeReq.result=e.target.result;fakeReq.readyState='done';"
        "var ev={target:fakeReq,type:'success'};"
        "if(fakeReq.onsuccess)fakeReq.onsuccess(ev);"
        "(fakeReq._evts.success||[]).forEach(function(f){f(ev);});};"
        "r.onerror=function(e){fakeReq.error=e.target.error;fakeReq.readyState='done';"
        "var ev={target:fakeReq,type:'error'};"
        "if(fakeReq.onerror)fakeReq.onerror(ev);"
        "(fakeReq._evts.error||[]).forEach(function(f){f(ev);});};"
        "r.onupgradeneeded=function(e){"
        "if(fakeReq.onupgradeneeded)fakeReq.onupgradeneeded(e);"
        "(fakeReq._evts.upgradeneeded||[]).forEach(function(f){f(e);});};};"
        "var _check=setInterval(function(){if(_pending<=0){clearInterval(_check);_q();}},50);"
        "return fakeReq;"
        "};"
        # Restore each database
        "_dbNames.forEach(function(dbName){"
        "var dbData=_idb[dbName];"
        "var meta=dbData._meta||{};"
        "var ver=meta.version||1;"
        # Delete first to guarantee onupgradeneeded fires with clean state
        "var dr=_origDel(dbName);"
        "var _called=false;"
        # Async: pre-decode rows (so nested CryptoKey markers resolve via
        # importKey) BEFORE we open the readwrite transaction.
        "var _afterDelete=async function(){"
        "if(_called)return;_called=true;"
        "var sNames=Object.keys(dbData).filter(function(s){return s!=='_meta';});"
        "var decoded={},_total=0;"
        "for(var si=0;si<sNames.length;si++){"
        "var sName=sNames[si],s=dbData[sName];"
        "var rows=s.rows||[],keys=s.keys||[];"
        "var dRows=new Array(rows.length);"
        "for(var i=0;i<rows.length;i++){"
        "_total++;"
        "try{dRows[i]=[await _decVAsync(rows[i]),"
        "await _decVAsync(keys[i]),true];"
        "}catch(ex){dRows[i]=[null,null,false];}}"
        "decoded[sName]={rows:dRows,keyPath:s.keyPath};}"
        "var req=_origOpen(dbName,ver);"
        "req.onupgradeneeded=function(e){"
        "var db=e.target.result;"
        "sNames.forEach(function(sName){"
        "var s=dbData[sName];"
        "var opts={};"
        "if(s.keyPath!==null&&s.keyPath!==undefined)opts.keyPath=s.keyPath;"
        "opts.autoIncrement=!!s.autoIncrement;"
        "try{var os=db.createObjectStore(sName,opts);"
        "var idxs=s.indexes||[];"
        "idxs.forEach(function(idx){"
        "try{os.createIndex(idx.name,idx.keyPath,"
        "{unique:!!idx.unique,multiEntry:!!idx.multiEntry});}catch(ex){}"
        "});}catch(ex){}"
        "});"
        "};"
        "req.onsuccess=function(e){"
        "var db=e.target.result;"
        "var liveStores=sNames.filter(function(sName){"
        "return db.objectStoreNames.contains(sName);});"
        "if(!liveStores.length){db.close();_done(dbName);return;}"
        "var _errs=0,_ok=0;"
        "var tx=db.transaction(liveStores,'readwrite');"
        "liveStores.forEach(function(sName){"
        "try{"
        "var os=tx.objectStore(sName);"
        "var dInfo=decoded[sName],dRows=dInfo.rows;"
        "var hasKp=dInfo.keyPath!==null&&dInfo.keyPath!==undefined;"
        "for(var i=0;i<dRows.length;i++){"
        "var dd=dRows[i];"
        "if(!dd[2]){_errs++;continue;}"
        "try{var row=dd[0],key=dd[1],pr;"
        "if(hasKp){pr=os.put(row);}"
        "else{pr=os.put(row,key!==undefined?key:i);}"
        "pr.onerror=function(ev){_errs++;if(ev&&ev.preventDefault)ev.preventDefault();};"
        "pr.onsuccess=function(){_ok++;};"
        "}catch(ex){_errs++;}"
        "}"
        "}catch(ex){console.warn('[IDB restore] store error in',dbName+'/'+sName,ex);}"
        "});"
        "tx.oncomplete=function(){console.log('[IDB restore]',dbName,'committed:',_ok+'/'+_total,'ok,',_errs,'errors');"
        "db.close();_done(dbName);};"
        "tx.onerror=function(ev){console.warn('[IDB restore] tx error for',dbName,ev);"
        "if(ev&&ev.preventDefault)ev.preventDefault();};"
        "tx.onabort=function(){console.warn('[IDB restore] tx abort for',dbName,'ok=',_ok,'errs=',_errs);db.close();_done(dbName);};"
        "};"
        "req.onerror=function(ev){console.warn('[IDB restore] open error for',dbName,ev);_done(dbName);};"
        "};"
        "dr.onsuccess=_afterDelete;"
        "dr.onerror=_afterDelete;"
        "dr.onblocked=function(){setTimeout(_afterDelete,100);};"
        "});"
        "}catch(e){console.error('idb restore failed:',e);}"
        "})()"
    )


# ---------------------------------------------------------------------------
# Per-database restore (split injection to avoid giant CDP messages)
# ---------------------------------------------------------------------------


def build_restore_scaffolding(db_count: int, timeout_ms: int = 30000) -> str:
    """Return a JS IIFE that sets up the IDB restore scaffolding.

    Registers shared state on ``window.__idbR`` for per-database restore
    scripts injected via separate ``addScriptToEvaluateOnNewDocument`` calls.
    Must be registered BEFORE any per-database scripts.
    """
    protect_ms = 60000  # block destructive ops for 60s after restore
    return (
        "(function(){"
        "try{"
        + _IDB_CODEC_JS +
        "window.__idbRL=[];"
        "var R=window.__idbR={"
        "pending:" + str(int(db_count)) + ","
        "origOpen:indexedDB.open.bind(indexedDB),"
        "origDel:indexedDB.deleteDatabase.bind(indexedDB),"
        "origClear:IDBObjectStore.prototype.clear,"
        "origDelRow:IDBObjectStore.prototype.delete,"
        "origDelStore:IDBDatabase.prototype.deleteObjectStore,"
        "origCursorDel:(window.IDBCursor&&IDBCursor.prototype.delete)||null,"
        "origLsRemove:(window.Storage&&Storage.prototype.removeItem)||null,"
        "origLsClear:(window.Storage&&Storage.prototype.clear)||null,"
        "decV:_decV,"
        "decVAsync:_decVAsync,"
        "dbReady:{},"
        "dbPending:{},"
        "_protectUntil:Date.now()+" + str(protect_ms) + ","
        "counts:{delRow:0,delStore:0,cursorDel:0,lsRm:0,lsClr:0},"
        "log:function(){var a=Array.prototype.slice.call(arguments);console.log.apply(console,a);window.__idbRL.push(a.join(' '));},"
        "warn:function(){var a=Array.prototype.slice.call(arguments);console.warn.apply(console,a);window.__idbRL.push('WARN: '+a.join(' '));}"
        "};"
        "var _unblock=function(){"
        "indexedDB.open=R.origOpen;"
        "R.log('[IDB restore] unblocked, all done (" + str(protect_ms // 1000) + "s protection active)');"
        "setTimeout(function(){"
        "indexedDB.deleteDatabase=R.origDel;"
        "IDBObjectStore.prototype.clear=R.origClear;"
        "IDBObjectStore.prototype.delete=R.origDelRow;"
        "IDBDatabase.prototype.deleteObjectStore=R.origDelStore;"
        "if(R.origCursorDel)IDBCursor.prototype.delete=R.origCursorDel;"
        "if(R.origLsRemove)Storage.prototype.removeItem=R.origLsRemove;"
        "if(R.origLsClear)Storage.prototype.clear=R.origLsClear;"
        "if(R.origWorker)window.Worker=R.origWorker;"
        "if(R.origSharedWorker)window.SharedWorker=R.origSharedWorker;"
        "console.log('[IDB restore] protection ended. Blocked counts:',JSON.stringify(R.counts));"
        "if(window.__idbRL)window.__idbRL.push('[IDB restore] protection ended counts='+JSON.stringify(R.counts));"
        "delete window.__idbR;"
        "}," + str(protect_ms) + ");"
        "};"
        "R.done=function(dbN){"
        "R.pending--;R.log('[IDB restore] db done:',dbN,'remaining:',R.pending);"
        "if(R.pending<=0)_unblock();"
        "};"
        # Safety timeout
        "setTimeout(function(){if(R.pending>0){"
        "R.warn('[IDB restore] TIMEOUT: forcing unblock, pending=',R.pending);"
        "R.pending=0;_unblock();"
        "}}," + str(int(timeout_ms)) + ");"
        # Monkey-patch indexedDB.deleteDatabase to block and return fake success
        "indexedDB.deleteDatabase=function(n){"
        "R.log('[IDB restore] BLOCKED deleteDatabase:',n);"
        "var fakeReq={readyState:'pending',result:undefined,error:null,_evts:{},"
        "addEventListener:function(t,fn){(this._evts[t]=this._evts[t]||[]).push(fn);},"
        "removeEventListener:function(t,fn){"
        "this._evts[t]=(this._evts[t]||[]).filter(function(f){return f!==fn;});},"
        "dispatchEvent:function(){return true;}};"
        "setTimeout(function(){fakeReq.readyState='done';"
        "var ev={target:fakeReq,type:'success'};"
        "if(fakeReq.onsuccess)fakeReq.onsuccess(ev);"
        "(fakeReq._evts.success||[]).forEach(function(f){f(ev);});"
        "},0);"
        "return fakeReq;"
        "};"
        # Monkey-patch IDBObjectStore.prototype.clear to no-op during protection
        "IDBObjectStore.prototype.clear=function(){"
        "if(R._protectUntil>Date.now()||R.pending>0){"
        "R.log('[IDB restore] BLOCKED clear on',this.name);"
        "var fakeReq={readyState:'done',result:undefined,error:null,source:this,transaction:this.transaction};"
        "var self=this;Promise.resolve().then(function(){"
        "if(fakeReq.onsuccess)fakeReq.onsuccess({target:fakeReq,type:'success'});});"
        "return fakeReq;}"
        "return R.origClear.call(this);"
        "};"
        # Monkey-patch IDBObjectStore.prototype.delete (per-row delete)
        "IDBObjectStore.prototype.delete=function(key){"
        "if(R._protectUntil>Date.now()||R.pending>0){"
        "R.counts.delRow++;"
        "if(R.counts.delRow<=10)R.log('[IDB restore] BLOCKED delete on',this.name,'key=',key);"
        "else if(R.counts.delRow===11)R.log('[IDB restore] BLOCKED delete (suppressing further logs)');"
        "var fakeReq={readyState:'done',result:undefined,error:null,source:this,transaction:this.transaction};"
        "Promise.resolve().then(function(){"
        "if(fakeReq.onsuccess)fakeReq.onsuccess({target:fakeReq,type:'success'});});"
        "return fakeReq;}"
        "return R.origDelRow.call(this,key);"
        "};"
        # Monkey-patch IDBDatabase.prototype.deleteObjectStore (called in onupgradeneeded)
        "IDBDatabase.prototype.deleteObjectStore=function(name){"
        "if(R._protectUntil>Date.now()||R.pending>0){"
        "R.counts.delStore++;"
        "R.log('[IDB restore] BLOCKED deleteObjectStore:',this.name+'/'+name);"
        "return;}"
        "return R.origDelStore.call(this,name);"
        "};"
        # Monkey-patch IDBCursor.prototype.delete (cursor-based per-row delete)
        "if(R.origCursorDel){"
        "IDBCursor.prototype.delete=function(){"
        "if(R._protectUntil>Date.now()||R.pending>0){"
        "R.counts.cursorDel++;"
        "if(R.counts.cursorDel<=5)R.log('[IDB restore] BLOCKED cursor.delete');"
        "var fakeReq={readyState:'done',result:undefined,error:null};"
        "Promise.resolve().then(function(){"
        "if(fakeReq.onsuccess)fakeReq.onsuccess({target:fakeReq,type:'success'});});"
        "return fakeReq;}"
        "return R.origCursorDel.call(this);"
        "};}"
        # Monkey-patch localStorage.removeItem (some migration flags live here)
        "if(R.origLsRemove){"
        "Storage.prototype.removeItem=function(key){"
        "if(R._protectUntil>Date.now()||R.pending>0){"
        "R.counts.lsRm++;"
        "if(R.counts.lsRm<=20)R.log('[IDB restore] BLOCKED localStorage.removeItem:',key);"
        "return undefined;}"
        "return R.origLsRemove.call(this,key);"
        "};}"
        # Monkey-patch localStorage.clear
        "if(R.origLsClear){"
        "Storage.prototype.clear=function(){"
        "if(R._protectUntil>Date.now()||R.pending>0){"
        "R.counts.lsClr++;"
        "R.log('[IDB restore] BLOCKED localStorage.clear');"
        "return undefined;}"
        "return R.origLsClear.call(this);"
        "};}"
        # Monkey-patch Worker / SharedWorker so workers also get the IDB blocker.
        # WhatsApp uses Web Workers that manage their own DBs (fts-storage,
        # jobs-storage, lru-media-storage-idb, offd-storage).  Worker threads
        # have their own indexedDB object and prototype chain, so main-thread
        # patches do NOT apply.  We wrap the worker URL in a blob that first
        # installs the same blocking patches inside the worker, then loads the
        # original script via importScripts (classic) or import() (module).
        "var _WBP=" + json.dumps(_WORKER_IDB_BLOCKER_JS) + ";"
        "var _wrapWorker=function(OrigCtor){"
        "return function(scriptURL,options){"
        "try{"
        "var until=(R._protectUntil&&R._protectUntil>Date.now())"
        "?R._protectUntil:(Date.now()+120000);"
        "var absUrl=new URL(scriptURL,self.location.href).href;"
        "var isModule=options&&options.type==='module';"
        "var loader=isModule"
        "?('import('+JSON.stringify(absUrl)+').catch(function(e){console.error(\"[Worker IDB] module import failed\",e);});')"
        ":('importScripts('+JSON.stringify(absUrl)+');');"
        "var code='(function(){var _until='+until+';'+_WBP+'})();'+loader;"
        "var blob=new Blob([code],{type:'application/javascript'});"
        "var blobUrl=URL.createObjectURL(blob);"
        "R.log('[IDB restore] wrapped worker:',absUrl,'until=',new Date(until).toISOString());"
        "return new OrigCtor(blobUrl,options);"
        "}catch(e){"
        "R.warn('[IDB restore] worker wrap failed:',e&&e.message?e.message:e);"
        "return new OrigCtor(scriptURL,options);"
        "}"
        "};"
        "};"
        "try{"
        "var OW=window.Worker;"
        "if(OW){"
        "var WW=_wrapWorker(OW);"
        "WW.prototype=OW.prototype;"
        "window.Worker=WW;"
        "R.origWorker=OW;"
        "}"
        "var OSW=window.SharedWorker;"
        "if(OSW){"
        "var WSW=_wrapWorker(OSW);"
        "WSW.prototype=OSW.prototype;"
        "window.SharedWorker=WSW;"
        "R.origSharedWorker=OSW;"
        "}"
        "}catch(e){R.warn('[IDB restore] worker ctor patch failed:',e);}"
        # Monkey-patch indexedDB.open to queue page-initiated opens
        "indexedDB.open=function(n,v){"
        "if(R.pending<=0)return R.origOpen(n,v);"
        "var fakeReq={readyState:'pending',_evts:{},"
        "addEventListener:function(t,fn){(this._evts[t]=this._evts[t]||[]).push(fn);},"
        "removeEventListener:function(t,fn){"
        "this._evts[t]=(this._evts[t]||[]).filter(function(f){return f!==fn;});},"
        "dispatchEvent:function(){return true;}};"
        "var _q=function(){var r=R.origOpen(n,v);"
        "r.onsuccess=function(e){fakeReq.result=e.target.result;fakeReq.readyState='done';"
        "var ev={target:fakeReq,type:'success'};"
        "if(fakeReq.onsuccess)fakeReq.onsuccess(ev);"
        "(fakeReq._evts.success||[]).forEach(function(f){f(ev);});};"
        "r.onerror=function(e){fakeReq.error=e.target.error;fakeReq.readyState='done';"
        "var ev={target:fakeReq,type:'error'};"
        "if(fakeReq.onerror)fakeReq.onerror(ev);"
        "(fakeReq._evts.error||[]).forEach(function(f){f(ev);});};"
        "r.onupgradeneeded=function(e){"
        "if(fakeReq.onupgradeneeded)fakeReq.onupgradeneeded(e);"
        "(fakeReq._evts.upgradeneeded||[]).forEach(function(f){f(e);});};};"
        "var _check=setInterval(function(){if(R.pending<=0){clearInterval(_check);_q();}},50);"
        "return fakeReq;"
        "};"
        "R.log('[IDB restore] scaffolding ready, expecting',R.pending,'databases');"
        "}catch(e){console.error('[IDB restore] scaffolding failed:',e);}"
        "})()"
    )


def build_restore_single_db_inject(db_name: str, db_data: dict) -> str:
    """Return a JS IIFE that restores a single IndexedDB database.

    Requires ``window.__idbR`` to have been set up by
    :func:`build_restore_scaffolding`.
    """
    payload = json.dumps(db_data)
    safe_name = json.dumps(db_name)
    return (
        "(function(){"
        "var R=window.__idbR;"
        "if(!R){console.warn('[IDB restore] no scaffolding for'," + safe_name + ");return;}"
        "var dbName=" + safe_name + ";"
        "var dbData=" + payload + ";"
        "var meta=dbData._meta||{};"
        "var ver=meta.version||1;"
        "R.log('[IDB restore] restoring',dbName,'ver='+ver);"
        "var dr=R.origDel(dbName);"
        "var _called=false;"
        "var _afterDelete=async function(){"
        "if(_called)return;_called=true;"
        # Pre-decode all rows (async — resolves nested CryptoKey markers).
        # Done BEFORE we open the DB/transaction because IDB transactions
        # auto-commit on microtask drain and awaiting mid-tx aborts them.
        "var decoded={};"
        "var _total=0;"
        "var sNames=Object.keys(dbData).filter(function(s){return s!=='_meta';});"
        "for(var si=0;si<sNames.length;si++){"
        "var sName=sNames[si];var s=dbData[sName];"
        "var rows=s.rows||[],keys=s.keys||[];"
        "var dRows=new Array(rows.length);"
        "for(var i=0;i<rows.length;i++){"
        "_total++;"
        "try{dRows[i]=[await R.decVAsync(rows[i]),"
        "await R.decVAsync(keys[i]),true];"
        "}catch(ex){dRows[i]=[null,null,false];}}"
        "decoded[sName]={rows:dRows,keyPath:s.keyPath};}"
        "var req=R.origOpen(dbName,ver);"
        "req.onupgradeneeded=function(e){"
        "var db=e.target.result;"
        "sNames.forEach(function(sName){"
        "var s=dbData[sName];"
        "var opts={};"
        "if(s.keyPath!==null&&s.keyPath!==undefined)opts.keyPath=s.keyPath;"
        "opts.autoIncrement=!!s.autoIncrement;"
        "try{var os=db.createObjectStore(sName,opts);"
        "var idxs=s.indexes||[];"
        "idxs.forEach(function(idx){"
        "try{os.createIndex(idx.name,idx.keyPath,"
        "{unique:!!idx.unique,multiEntry:!!idx.multiEntry});}catch(ex){}"
        "});}catch(ex){}"
        "});"
        "};"
        "req.onsuccess=function(e){"
        "var db=e.target.result;"
        "var liveStores=sNames.filter(function(sName){"
        "return db.objectStoreNames.contains(sName);});"
        "if(!liveStores.length){db.close();R.done(dbName);return;}"
        "var _errs=0,_ok=0;"
        "var tx=db.transaction(liveStores,'readwrite');"
        "liveStores.forEach(function(sName){"
        "try{"
        "var os=tx.objectStore(sName);"
        "var dInfo=decoded[sName];var dRows=dInfo.rows;"
        "var hasKp=dInfo.keyPath!==null&&dInfo.keyPath!==undefined;"
        "for(var i=0;i<dRows.length;i++){"
        "var dd=dRows[i];"
        "if(!dd[2]){_errs++;continue;}"
        "try{var row=dd[0],key=dd[1],pr;"
        "if(hasKp){pr=os.put(row);}"
        "else{pr=os.put(row,key!==undefined?key:i);}"
        "pr.onerror=function(ev){_errs++;if(ev&&ev.preventDefault)ev.preventDefault();};"
        "pr.onsuccess=function(){_ok++;};"
        "}catch(ex){_errs++;}"
        "}"
        "}catch(ex){R.warn('[IDB restore] store error in',dbName+'/'+sName,ex);}"
        "});"
        "tx.oncomplete=function(){R.log('[IDB restore]',dbName,'committed:',_ok+'/'+_total,'ok,',_errs,'errors');"
        "db.close();R.done(dbName);};"
        "tx.onerror=function(ev){R.warn('[IDB restore] tx error for',dbName,ev&&ev.target&&ev.target.error?ev.target.error.name:'?');"
        "if(ev&&ev.preventDefault)ev.preventDefault();};"
        "tx.onabort=function(ev){R.warn('[IDB restore] tx ABORT for',dbName,'ok=',_ok,'errs=',_errs,'reason=',ev&&ev.target&&ev.target.error?ev.target.error.name:'unknown');db.close();R.done(dbName);};"
        "};"
        "req.onerror=function(ev){R.warn('[IDB restore] open error for',dbName,ev&&ev.target&&ev.target.error?ev.target.error.name:'?');R.done(dbName);};"
        "};"
        "dr.onsuccess=_afterDelete;"
        "dr.onerror=_afterDelete;"
        "dr.onblocked=function(){setTimeout(_afterDelete,100);};"
        "})()"
    )


# ---------------------------------------------------------------------------
# Chunked per-store restore (avoids large CDP messages for big databases)
# ---------------------------------------------------------------------------

_MAX_CHUNK_BYTES = 500_000  # target max ~500 KB per injected script


def build_restore_db_schema(db_name: str, db_data: dict,
                            pending_chunks: int) -> str:
    """Return JS IIFE that creates one DB's schema (stores + indexes).

    Sets ``R.dbReady[dbName] = true`` on success so that data-chunk scripts
    can start inserting rows.  If *pending_chunks* is 0 (DB has no rows),
    the schema script itself calls ``R.done(dbName)``.
    """
    safe_name = json.dumps(db_name)
    meta = db_data.get("_meta", {})
    ver = int(meta.get("version", 1))

    # Build store + index creation JS
    store_parts: list[str] = []
    for sname, sval in db_data.items():
        if sname == "_meta" or not isinstance(sval, dict):
            continue
        safe_sname = json.dumps(sname)
        opts: list[str] = []
        kp = sval.get("keyPath")
        if kp is not None:
            opts.append("keyPath:" + json.dumps(kp))
        opts.append(
            "autoIncrement:" + ("true" if sval.get("autoIncrement") else "false")
        )
        idx_js = ""
        for idx in sval.get("indexes", []):
            idx_js += (
                "try{os.createIndex(" + json.dumps(idx["name"]) + ","
                + json.dumps(idx["keyPath"]) + ","
                "{unique:" + ("true" if idx.get("unique") else "false") + ","
                "multiEntry:" + ("true" if idx.get("multiEntry") else "false")
                + "});}catch(ex){}"
            )
        store_parts.append(
            "try{var os=db.createObjectStore("
            + safe_sname + ",{" + ",".join(opts) + "});"
            + idx_js + "}catch(ex){}"
        )
    stores_js = "".join(store_parts)

    return (
        "(function(){"
        "var R=window.__idbR;"
        "if(!R){console.warn('[IDB restore] no scaffolding for',"
        + safe_name + ");return;}"
        "var dbName=" + safe_name + ";"
        "R.dbPending[dbName]=" + str(int(pending_chunks)) + ";"
        "R.dbReady[dbName]=false;"
        "R.log('[IDB restore] schema',dbName,'ver=" + str(ver)
        + " chunks='+R.dbPending[dbName]);"
        "var dr=R.origDel(dbName);"
        "var _called=false;"
        "var _ad=function(){"
        "if(_called)return;_called=true;"
        "var req=R.origOpen(dbName," + str(ver) + ");"
        "req.onupgradeneeded=function(e){"
        "var db=e.target.result;" + stores_js + "};"
        "req.onsuccess=function(e){"
        "e.target.result.close();"
        "R.dbReady[dbName]=true;"
        "R.log('[IDB restore] schema ready for',dbName);"
        "if(R.dbPending[dbName]<=0)R.done(dbName);"
        "};"
        "req.onerror=function(e){"
        "R.warn('[IDB restore] schema failed',dbName,"
        "e&&e.target&&e.target.error?e.target.error.name:'?');"
        "R.done(dbName);};"
        "};"
        "dr.onsuccess=_ad;"
        "dr.onerror=_ad;"
        "dr.onblocked=function(){setTimeout(_ad,100);};"
        "})()"
    )


def build_restore_store_chunk(db_name: str, store_name: str,
                              has_key_path: bool,
                              rows: list, keys: list) -> str:
    """Return JS IIFE that inserts one chunk of rows into one store.

    Polls ``R.dbReady[dbName]`` before proceeding.  Decrements
    ``R.dbPending[dbName]`` and calls ``R.done`` when all chunks complete.
    """
    safe_db = json.dumps(db_name)
    safe_sn = json.dumps(store_name)
    payload = json.dumps({"r": rows, "k": keys})
    kp_js = "true" if has_key_path else "false"
    n = len(rows)

    return (
        "(function(){"
        "var R=window.__idbR;if(!R)return;"
        "var dbN=" + safe_db + ",sN=" + safe_sn
        + ",d=" + payload + ",kp=" + kp_js + ";"
        "var _finish=function(){R.dbPending[dbN]--;"
        "if(R.dbPending[dbN]<=0)R.done(dbN);};"
        "var _go=async function(){"
        "if(!R.dbReady[dbN]){setTimeout(_go,50);return;}"
        # Pre-decode rows asynchronously so nested CryptoKey markers resolve
        # to real CryptoKey objects BEFORE we open the IDB transaction
        # (transactions auto-commit on microtask drain, so awaiting mid-tx
        # would silently abort the commit).
        "var rows=d.r,keys=d.k;"
        "var decoded=new Array(rows.length);"
        "for(var i=0;i<rows.length;i++){"
        "try{decoded[i]=[await R.decVAsync(rows[i]),"
        "await R.decVAsync(keys[i]),true];"
        "}catch(ex){decoded[i]=[null,null,false];}}"
        "var req=R.origOpen(dbN);"
        "req.onsuccess=function(e){"
        "var db=e.target.result;"
        "if(!db.objectStoreNames.contains(sN)){"
        "db.close();"
        "R.warn('[IDB restore]',dbN+'/'+sN,'store missing');"
        "_finish();return;}"
        "var tx=db.transaction([sN],'readwrite');"
        "var os=tx.objectStore(sN);"
        "var _ok=0,_er=0;"
        "for(var i=0;i<decoded.length;i++){"
        "var dd=decoded[i];"
        "if(!dd[2]){_er++;continue;}"
        "try{var row=dd[0],key=dd[1],pr;"
        "if(kp){pr=os.put(row);}else{pr=os.put(row,key!==undefined?key:i);}"
        "pr.onerror=function(ev){_er++;if(ev&&ev.preventDefault)ev.preventDefault();};"
        "pr.onsuccess=function(){_ok++;};"
        "}catch(ex){_er++;}}"
        "tx.oncomplete=function(){"
        "R.log('[IDB restore]',dbN+'/'+sN,_ok+'/" + str(n) + "ok',_er+'errs');"
        "db.close();_finish();};"
        "tx.onerror=function(ev){if(ev&&ev.preventDefault)ev.preventDefault();};"
        "tx.onabort=function(){"
        "R.warn('[IDB restore]',dbN+'/'+sN,'ABORT ok='+_ok,'errs='+_er);"
        "db.close();_finish();};"
        "};"
        "req.onerror=function(){"
        "R.warn('[IDB restore] chunk open failed',dbN+'/'+sN);"
        "_finish();};"
        "};"
        "_go();"
        "})()"
    )


def build_restore_db_scripts(db_name: str, db_data: dict,
                             max_chunk_bytes: int = _MAX_CHUNK_BYTES,
                             ) -> list[str]:
    """Split one database's restore into schema + per-store chunk scripts.

    For small databases (< *max_chunk_bytes* total), falls back to a single
    :func:`build_restore_single_db_inject` script.  For large databases,
    returns ``[schema_script, chunk1, chunk2, ...]`` where each chunk
    handles one batch of rows for one object store.
    """
    total_size = len(json.dumps(db_data))
    if total_size < max_chunk_bytes:
        return [build_restore_single_db_inject(db_name, db_data)]

    # Enumerate data chunks (store_name, has_key_path, rows, keys)
    chunks: list[tuple[str, bool, list, list]] = []
    for sname, sval in db_data.items():
        if sname == "_meta" or not isinstance(sval, dict):
            continue
        rows = sval.get("rows", [])
        keys = list(sval.get("keys", []))
        if not rows:
            continue
        # Pad keys to same length as rows
        while len(keys) < len(rows):
            keys.append(None)
        has_kp = sval.get("keyPath") is not None

        # Split rows into size-bounded chunks
        c_rows: list = []
        c_keys: list = []
        c_size = 0
        for i in range(len(rows)):
            r_size = len(json.dumps(rows[i])) + len(json.dumps(keys[i]))
            if c_size + r_size > max_chunk_bytes and c_rows:
                chunks.append((sname, has_kp, c_rows, c_keys))
                c_rows, c_keys, c_size = [], [], 0
            c_rows.append(rows[i])
            c_keys.append(keys[i])
            c_size += r_size
        if c_rows:
            chunks.append((sname, has_kp, c_rows, c_keys))

    scripts: list[str] = [
        build_restore_db_schema(db_name, db_data, len(chunks))
    ]
    for sname, has_kp, c_rows, c_keys in chunks:
        scripts.append(
            build_restore_store_chunk(db_name, sname, has_kp, c_rows, c_keys)
        )
    return scripts
