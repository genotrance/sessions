"""Pure utility functions for cookie handling and URL normalization."""
from __future__ import annotations

import json
import os
import re
import urllib.parse

from . import cdp

_VALID_COOKIE_KEYS = {
    "name", "value", "url", "domain", "path", "secure", "httpOnly",
    "sameSite", "expires", "priority", "sameParty", "sourceScheme", "sourcePort",
    "partitionKey",
}


def clean_cookie(c: dict) -> dict:
    """Filter cookie dict to keys accepted by Storage.setCookies."""
    out = {k: v for k, v in c.items() if k in _VALID_COOKIE_KEYS}
    # Storage.setCookies requires either url or domain+path
    if "url" not in out and "domain" not in out:
        return {}
    # Synthesize url from domain+path+secure — setCookies is more reliable with url
    if "url" not in out and "domain" in out:
        domain = out["domain"].lstrip(".")
        scheme = "https" if out.get("secure") else "http"
        path = out.get("path", "/")
        out["url"] = f"{scheme}://{domain}{path}"
    # __Host- cookies must be host-only (no Domain attribute).  CDP returns
    # them with a domain field, but setCookies will create a domain cookie
    # if both url and domain are present, which Chrome then rejects because
    # __Host- prefix requires host-only.  Remove domain, keep url.
    name = out.get("name", "")
    if name.startswith("__Host-") and "url" in out:
        out.pop("domain", None)
    # Session cookies: CDP returns expires <= 0, but setCookies interprets
    # that as a Unix timestamp (1969/1970), making the cookie immediately
    # expired.  Remove expires so setCookies creates a session cookie.
    if out.get("expires") is not None and out["expires"] <= 0:
        del out["expires"]
    return out


def origins_from_cookies(cookies: list[dict]) -> list[str]:
    seen = set()
    out = []
    for c in cookies:
        d = (c.get("domain") or "").lstrip(".")
        if not d:
            continue
        scheme = "https" if c.get("secure") else "http"
        origin = f"{scheme}://{d}"
        if origin not in seen:
            seen.add(origin)
            out.append(origin)
    return out


_FALLBACK_SEARCH_URL = "https://www.google.com/search?q={searchTerms}"
_LIKELY_URL_RE = re.compile(
    r"^(?:[a-z][a-z0-9+\-.]*://|www\.)"
    r"|(?:[a-zA-Z0-9\-]+\.(?:com|org|net|io|dev|app|ai|co|gov|edu|uk|de|fr|jp|ca)(/|$))",
    re.IGNORECASE,
)


def _get_chrome_search_template() -> str:
    """Read the default search engine URL template from Chrome's Preferences."""
    try:
        prefs_path = os.path.join(cdp.USER_DATA_DIR, "Default", "Preferences")
        with open(prefs_path, encoding="utf-8") as f:
            prefs = json.loads(f.read())
        tpl = prefs.get("default_search_provider_data", {}).get(
            "template_url", "")
        if tpl and "{searchTerms}" in tpl:
            return tpl
    except Exception:
        pass
    return _FALLBACK_SEARCH_URL


def build_search_url(query: str) -> str:
    """Build a search URL using Chrome's configured search engine."""
    tpl = _get_chrome_search_template()
    encoded = urllib.parse.quote_plus(query)
    return tpl.replace("{searchTerms}", encoded)


def normalize_url(text: str) -> str:
    """Turn user input into a navigable URL.
    If it looks like a URL/domain, add https:// if missing.
    Otherwise treat it as a search query using Chrome's default engine."""
    text = text.strip()
    if not text:
        return "about:blank"
    if "://" in text:
        return text
    if _LIKELY_URL_RE.match(text):
        return "https://" + text
    return build_search_url(text)


def domain_of(url: str) -> str | None:
    try:
        if "://" not in url:
            url = "https://" + url
        p = urllib.parse.urlparse(url)
        host = p.hostname or ""
        return host or None
    except Exception:
        return None


def origin_of(url: str) -> str | None:
    try:
        p = urllib.parse.urlparse(url)
        if p.scheme and p.netloc:
            return f"{p.scheme}://{p.netloc}"
    except Exception:
        pass
    return None
