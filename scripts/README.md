# scripts/

These are **live-browser integration scripts** — they require a running Chrome
instance with remote debugging enabled on port 9222 and are **not** part of the
automated unit-test suite (`tests/`).

The unit tests in `tests/` use fakes and run offline. These scripts talk to a
real browser to verify or diagnose things that can only be observed in a live
environment.

Run any script with:

```
python scripts/<script>.py
```

---

## Verification scripts

Scripts that exercise the full production pipeline and exit 0/1 like a test.
Run these after significant changes to the snapshot/restore path to confirm
end-to-end correctness against a real browser.

### `verify_full_roundtrip.py`
**Full session state roundtrip** — the most comprehensive live test.

Exercises the exact same code path used in production:
1. Creates a browser context, opens `example.com`, seeds cookies + localStorage
   + IndexedDB (including binary types: `ArrayBuffer`, `Date`, compound keyPath,
   multi-entry indexes).
2. Calls `_collect_state` (real snapshot path).
3. Disposes the context (simulates hibernate).
4. Calls `_open_tab_with_storage` (real restore path) in a new context.
5. Verifies all three storage mechanisms survived intact.

Exits 0 if all checks pass. Use this after any change to `manager.py`'s
snapshot or restore logic.

### `verify_idb_roundtrip.py`
**IndexedDB dump + restore fidelity** — focused IDB-only verification.

Seeds a `verifydb` database with binary data (`ArrayBuffer`, `Uint8Array`,
`Date`, compound keyPath, autoIncrement store, multi-entry index), dumps it via
`IDB_DUMP_JS`, wipes it, restores via `build_restore_script`, then byte-compares
every field. Use this after changes to `src/sessions/idb.py`.

### `roundtrip_test.py`
**End-to-end snapshot → clone → restore test with WhatsApp (or any live site).**

The master integration test for the full session hibernation pipeline.  Prompts
you to log into a site (default: WhatsApp), then:

1. Snapshots the source container (cookies + localStorage + every IndexedDB).
2. Clones the saved state into a fresh destination container.
3. Restores the destination (injects the scaffolding + per-DB scripts, opens a
   tab) and waits 45 s for the site to initialize.
4. Snapshots the destination and diffs it against the source: cookies, LS keys,
   and IDB row counts per store.

Captures the browser console during restore so you can see the
`[IDB restore] …` progress and every `BLOCKED …` line from the monkey-patched
destructive ops.  Use this to validate any change to `idb.py`, the scaffolding,
the DOMStorage layer, or the restore orchestration in `manager.py`.

### `probe_cryptokey.py`
**WebCrypto CryptoKey export/import capability probe.**

Runs six self-contained tests in a live Chrome tab: AES-GCM, HMAC, RSA-OAEP,
ECDH extractable roundtrips via `exportKey('jwk')` + `importKey('jwk')`,
confirms non-extractable keys correctly throw on export, and confirms IDB
natively stores non-extractable CryptoKeys (which WhatsApp relies on, and which
cannot be copied across browser profiles by design).

Used to prove browser behavior before implementing the codec's CryptoKey
support.  Rerun if you want to confirm WebCrypto semantics on a new Chrome
version.

### `probe_codec_cryptokey.py`
**End-to-end probe of our CryptoKey codec roundtrip.**

Uses the real `_IDB_CODEC_JS` encoder/decoder from `src/sessions/idb.py` and
runs six tests in a live Chrome tab: AES-GCM encrypt/decrypt across roundtrip,
HMAC sign/verify across roundtrip, ECDH `deriveKey` on a restored private key
(the exact op that used to fail for WhatsApp), nested rows containing
`CryptoKey[]` + `Date`, non-extractable key placeholder safety, and a full
IDB `put` → `get` → `decrypt` with a restored key.

Use after any edit to the codec (`_encV` / `_decV` / `_decVAsync` /
`_expandBlob`) to confirm crypto roundtrips are still sound.

### `verify_dom_storage_roundtrip.py`
**DOMStorage read/write bypass verification.**

Writes to `example.com` localStorage via `DOMStorage.setDOMStorageItem`, then
simulates Discord's anti-bot deletion (`delete Window.prototype.localStorage`),
and confirms `DOMStorage.getDOMStorageItems` still reads the data correctly.
Use this to confirm the Discord fix is intact after any CDP layer changes.

### `verify_discord_capture.py`
**Live Discord tab localStorage capture check.**

Requires an open Discord tab. Reads localStorage via both the new
`DOMStorage.getDOMStorageItems` path and the old `Runtime.evaluate` path,
and prints the key counts for each. Use this to quickly confirm the Discord
fix is working in a live session (expected: DOMStorage shows 40+ keys, old
path shows 0).

---

## Diagnostic scripts

Scripts used to investigate a specific bug. Kept here as reference in case the
same issue recurs or a similar site needs debugging.

### `diagnose_discord.py`
**Discord tab JS environment probe.**

Probes an open Discord tab for: origin, `localStorage` accessibility,
`indexedDB.databases()`, service worker registrations, and cookie state.
Originally used to confirm that Discord deletes `localStorage` from
`Window.prototype` as an anti-automation measure (root cause of the
`ls_keys=0` bug).

### `diagnose_ls.py`
**Deep localStorage availability investigation.**

Three-stage probe: (1) checks existing Discord tab JS environment, (2) opens a
fresh browser context at `discord.com/login` and tests localStorage there, (3)
simulates the restore flow (`about:blank` + `addScriptToEvaluateOnNewDocument`
+ navigate) to see whether injected localStorage survives. Used to rule out
alternative causes before the DOMStorage fix was identified.

### `diagnose_headers.py`
**Discord HTTP response headers and CSP probe.**

Creates a fresh context, navigates to `example.com` and `discord.com/login`,
and inspects: `typeof localStorage`, CSP meta tags, `crossOriginIsolated`,
`isSecureContext`, and the `Window.prototype.localStorage` descriptor chain.
Used to rule out CSP/header-level causes of the storage blockage.

### `test_dom_storage.py`
**DOMStorage CDP domain smoke test against a live Discord tab.**

Directly calls `DOMStorage.getDOMStorageItems` and `DOMStorage.setDOMStorageItem`
on an open Discord tab to confirm the CDP domain bypasses Discord's JS-level
localStorage deletion. This was the proof-of-concept script that validated the
fix before it was implemented in `manager.py`.

---

## Relationship to `tests/`

| | `tests/` | `scripts/` |
|---|---|---|
| Runs offline | ✅ | ❌ (needs Chrome on :9222) |
| Part of CI | ✅ | ❌ |
| Uses fakes | ✅ | ❌ (real browser) |
| Purpose | Regression guard | Live verification / diagnosis |

When a bug is fixed, the regression test goes in `tests/`. The script that
helped diagnose or verify the fix stays here as a reference tool.
