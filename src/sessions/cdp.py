"""chrome_cdp.py - Minimal Chrome DevTools Protocol client for ContextDaemon.

Exposes only the CDP surface required by ``context_daemon.py``:
- ``ChromeManager``: launch / reuse / stop a Chrome or Edge instance.
- ``CDPSession``: WebSocket session with domain helpers for the CDP domains
  used by hibernate / restore: Target, Storage, Network, Page, Runtime, Browser.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

import requests
import websocket

IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

# ---------------------------------------------------------------------------
# Browser discovery
# ---------------------------------------------------------------------------

_CHROME_CANDIDATES_WINDOWS = [
    os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
    os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
    os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
]
_CHROME_CANDIDATES_LINUX = [
    "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium-browser", "/usr/bin/chromium", "/snap/bin/chromium",
]
_CHROME_CANDIDATES_MAC = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
]
_EDGE_CANDIDATES_WINDOWS = [
    os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
    os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
]
_EDGE_CANDIDATES_LINUX = [
    "/usr/bin/microsoft-edge", "/usr/bin/microsoft-edge-stable",
]
_EDGE_CANDIDATES_MAC = [
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
]


def find_chrome() -> str | None:
    if IS_WINDOWS:
        candidates = _CHROME_CANDIDATES_WINDOWS
    elif IS_MAC:
        candidates = _CHROME_CANDIDATES_MAC
    else:
        candidates = _CHROME_CANDIDATES_LINUX
    for p in candidates:
        if p and os.path.isfile(p):
            return p
    for name in ("google-chrome", "google-chrome-stable", "chromium-browser",
                 "chromium", "chrome", "chrome.exe"):
        found = shutil.which(name)
        if found:
            return found
    return None


def find_edge() -> str | None:
    if IS_WINDOWS:
        candidates = _EDGE_CANDIDATES_WINDOWS
    elif IS_MAC:
        candidates = _EDGE_CANDIDATES_MAC
    else:
        candidates = _EDGE_CANDIDATES_LINUX
    for p in candidates:
        if p and os.path.isfile(p):
            return p
    for name in ("microsoft-edge", "microsoft-edge-stable", "msedge", "msedge.exe"):
        found = shutil.which(name)
        if found:
            return found
    return None


def find_browser(preference: str = "auto") -> tuple[str | None, str]:
    preference = preference.lower().strip()
    if preference == "edge":
        return find_edge(), "Edge"
    if preference == "chrome":
        return find_chrome(), "Chrome"
    p = find_chrome()
    if p:
        return p, "Chrome"
    p = find_edge()
    if p:
        return p, "Edge"
    return None, "Chrome"


DEFAULT_PORT = 9222


def _default_data_dir() -> str:
    if IS_WINDOWS:
        base = os.environ.get("APPDATA",
                              os.path.join(os.path.expanduser("~"), "AppData", "Roaming"))
        new_dir = os.path.join(base, "Sessions")
        # Migrate from old TEMP location if it exists and new one doesn't
        old_dir = os.path.join(
            os.environ.get("TEMP", os.path.expanduser("~")),
            "context-daemon-profile")
        if os.path.isdir(old_dir) and not os.path.isdir(new_dir):
            try:
                import shutil
                shutil.copytree(old_dir, new_dir)
            except Exception:
                pass
        return new_dir
    elif IS_MAC:
        base = os.path.join(os.path.expanduser("~"), "Library",
                            "Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME",
                              os.path.join(os.path.expanduser("~"),
                                           ".local", "share"))
    return os.path.join(base, "Sessions")


def _default_pid_file() -> str:
    if IS_WINDOWS:
        base = os.environ.get("TEMP", os.path.expanduser("~"))
    else:
        base = os.environ.get("XDG_RUNTIME_DIR",
                              os.environ.get("TMPDIR", "/tmp"))
    return os.path.join(base, "sessions.pid")


CHROME_PATH, _DEFAULT_BROWSER_NAME = find_browser("auto")
USER_DATA_DIR = _default_data_dir()
PID_FILE = _default_pid_file()


# ---------------------------------------------------------------------------
# ChromeManager
# ---------------------------------------------------------------------------

class ChromeManager:
    def __init__(self, port: int = DEFAULT_PORT, chrome_path: str | None = None,
                 user_data_dir: str = USER_DATA_DIR, pid_file: str = PID_FILE,
                 browser_name: str | None = None):
        self.port = port
        self.chrome_path = chrome_path or CHROME_PATH or find_chrome() or find_edge()
        self.user_data_dir = user_data_dir
        self.pid_file = pid_file
        self._proc: subprocess.Popen | None = None
        if browser_name:
            self.browser_name = browser_name
        else:
            self.browser_name = self._detect_browser_name(self.chrome_path)

    @staticmethod
    def _detect_browser_name(path: str | None) -> str:
        if not path:
            return "Chrome"
        lower = path.lower()
        if "msedge" in lower or "microsoft-edge" in lower or "microsoft edge" in lower \
                or "\\edge\\" in lower:
            return "Edge"
        return "Chrome"

    def start(self, headless: bool = False, extra_args: list[str] | None = None,
              start_url: str = "about:blank", timeout: float = 15) -> ChromeManager:
        if self.is_running():
            return self
        if not self.chrome_path or not os.path.isfile(self.chrome_path):
            raise RuntimeError(
                f"Browser binary not found (tried: {self.chrome_path!r}). "
                "Install Chrome or Edge.")
        args = [
            self.chrome_path,
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={self.user_data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-default-apps",
            "--disable-popup-blocking",
            "--disable-translate",
            "--disable-background-networking",
            "--disable-sync",
            "--disable-breakpad",
            "--metrics-recording-only",
            "--remote-allow-origins=*",
        ]
        if headless:
            args.append("--headless=new")
        if not IS_WINDOWS:
            args.append("--disable-dev-shm-usage")
        if extra_args:
            args.extend(extra_args)
        args.append(start_url)
        popen_kwargs: dict[str, Any] = {"stdout": subprocess.DEVNULL,
                                        "stderr": subprocess.DEVNULL}
        if IS_WINDOWS:
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True
        self._proc = subprocess.Popen(args, **popen_kwargs)
        self._write_pid(self._proc.pid)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._cdp_ready():
                return self
            time.sleep(0.3)
        raise RuntimeError(f"{self.browser_name} did not become ready within {timeout}s")

    def stop(self, force: bool = False) -> None:
        if not force:
            try:
                ws_url = self._browser_ws_url()
                if ws_url:
                    ws = websocket.create_connection(ws_url, timeout=5)
                    ws.send(json.dumps({"id": 1, "method": "Browser.close"}))
                    ws.close()
                    time.sleep(1)
                    if not self._cdp_ready():
                        self._remove_pid()
                        self._proc = None
                        return
            except Exception:
                pass
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        pid = self._read_pid()
        if pid:
            try:
                if IS_WINDOWS:
                    os.kill(pid, signal.SIGTERM)
                else:
                    os.kill(pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
        self._remove_pid()
        self._proc = None

    def is_running(self) -> bool:
        return self._cdp_ready()

    def status(self) -> dict:
        info: dict[str, Any] = {"running": self.is_running(), "port": self.port}
        pid = self._read_pid()
        if pid:
            info["pid"] = pid
        info["browser"] = self._read_browser_name() or self.browser_name
        if info["running"]:
            try:
                info["version"] = self.get_version()
            except Exception:
                pass
        return info

    @property
    def base_url(self) -> str:
        return f"http://localhost:{self.port}"

    def get_version(self) -> dict:
        return requests.get(f"{self.base_url}/json/version", timeout=5).json()

    def list_targets(self) -> list[dict]:
        return requests.get(f"{self.base_url}/json/list", timeout=5).json()

    def browser_ws_url(self) -> str | None:
        return self._browser_ws_url()

    def _cdp_ready(self) -> bool:
        try:
            return requests.get(f"{self.base_url}/json/version", timeout=2).status_code == 200
        except Exception:
            return False

    def _browser_ws_url(self) -> str | None:
        try:
            return self.get_version().get("webSocketDebuggerUrl")
        except Exception:
            return None

    def _write_pid(self, pid: int) -> None:
        try:
            with open(self.pid_file, "w") as f:
                json.dump({"pid": pid, "port": self.port,
                           "browser": self.browser_name}, f)
        except OSError:
            pass

    def _read_pid(self) -> int | None:
        try:
            with open(self.pid_file) as f:
                return json.load(f).get("pid")
        except (OSError, json.JSONDecodeError, KeyError):
            return None

    def _read_browser_name(self) -> str | None:
        try:
            with open(self.pid_file) as f:
                return json.load(f).get("browser")
        except (OSError, json.JSONDecodeError, KeyError):
            return None

    def _remove_pid(self) -> None:
        try:
            os.remove(self.pid_file)
        except OSError:
            pass


def ensure_chrome(port: int = DEFAULT_PORT, headless: bool = False,
                  **kwargs) -> ChromeManager:
    mgr = ChromeManager(port=port, **{k: v for k, v in kwargs.items()
                                      if k in ("chrome_path", "user_data_dir",
                                               "pid_file", "browser_name")})
    if mgr.is_running():
        return mgr
    start_kw = {k: v for k, v in kwargs.items()
                if k in ("extra_args", "start_url", "timeout")}
    return mgr.start(headless=headless, **start_kw)


# ---------------------------------------------------------------------------
# CDPSession
# ---------------------------------------------------------------------------

class CDPError(RuntimeError):
    def __init__(self, code: int, message: str, data: Any = None):
        self.code = code
        self.message = message
        self.data = data
        super().__init__(f"CDP {code}: {message}" + (f" ({data})" if data else ""))


class CDPSession:
    def __init__(self, ws_url: str, timeout: float = 30):
        self.ws_url = ws_url
        self.ws = websocket.create_connection(ws_url, timeout=timeout)
        self._msg_id = 0
        self._events: list[dict] = []
        self._listeners: dict[str, list[Callable]] = {}
        self._lock = threading.Lock()
        self.page = _PageDomain(self)
        self.network = _NetworkDomain(self)
        self.runtime = _RuntimeDomain(self)
        self.browser = _BrowserDomain(self)
        self.target = _TargetDomain(self)
        self.storage = _StorageDomain(self)

    @classmethod
    def connect(cls, port: int = DEFAULT_PORT, target_id: str | None = None) -> CDPSession:
        base = f"http://localhost:{port}"
        targets = requests.get(f"{base}/json/list", timeout=5).json()
        if target_id:
            tgt = next((t for t in targets if t["id"] == target_id), None)
            if not tgt:
                raise ValueError(f"Target {target_id} not found")
        else:
            pages = [t for t in targets if t["type"] == "page"]
            if not pages:
                r = requests.put(f"{base}/json/new?about:blank", timeout=5)
                if r.status_code == 405:
                    r = requests.get(f"{base}/json/new?about:blank", timeout=5)
                tgt = r.json()
            else:
                tgt = pages[0]
        return cls(tgt["webSocketDebuggerUrl"])

    @classmethod
    def connect_browser(cls, port: int = DEFAULT_PORT) -> CDPSession:
        """Connect to the browser-level CDP endpoint (not a tab).
        Required for Target.createBrowserContext / Storage by browserContextId."""
        info = requests.get(f"http://127.0.0.1:{port}/json/version",
                           timeout=(0.5, 5)).json()
        return cls(info["webSocketDebuggerUrl"])

    def _next_id(self) -> int:
        with self._lock:
            self._msg_id += 1
            return self._msg_id

    def send(self, method: str, params: dict | None = None, timeout: float = 30,
             session_id: str | None = None) -> dict:
        msg_id = self._next_id()
        payload: dict[str, Any] = {"id": msg_id, "method": method}
        if params:
            payload["params"] = params
        if session_id:
            payload["sessionId"] = session_id
        self.ws.send(json.dumps(payload))
        deadline = time.time() + timeout
        while time.time() < deadline:
            remaining = max(0.05, deadline - time.time())
            self.ws.settimeout(remaining)
            try:
                raw = self.ws.recv()
            except websocket.WebSocketTimeoutException:
                break
            msg = json.loads(raw)
            if msg.get("id") == msg_id:
                if "error" in msg:
                    err = msg["error"]
                    raise CDPError(err.get("code", -1),
                                   err.get("message", str(err)), err.get("data"))
                return msg.get("result", {})
            self._dispatch_event(msg)
        raise TimeoutError(f"No response for {method} (id={msg_id}) within {timeout}s")

    def on(self, event_name: str, callback: Callable[[dict], Any]) -> None:
        self._listeners.setdefault(event_name, []).append(callback)

    def wait_for_event(self, event_name: str, timeout: float = 30,
                       predicate: Callable[[dict], bool] | None = None) -> dict:
        for i, ev in enumerate(self._events):
            if ev.get("method") == event_name and (predicate is None or predicate(ev)):
                return self._events.pop(i)
        deadline = time.time() + timeout
        while time.time() < deadline:
            remaining = max(0.05, deadline - time.time())
            self.ws.settimeout(remaining)
            try:
                raw = self.ws.recv()
            except websocket.WebSocketTimeoutException:
                break
            msg = json.loads(raw)
            if msg.get("method") == event_name and (predicate is None or predicate(msg)):
                return msg
            self._dispatch_event(msg)
        raise TimeoutError(f"Timed out waiting for {event_name}")

    def _dispatch_event(self, msg: dict) -> None:
        method = msg.get("method")
        if not method:
            return
        self._events.append(msg)
        for cb in self._listeners.get(method, []):
            try:
                cb(msg)
            except Exception:
                pass

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# ---------------------------------------------------------------------------
# Domain helpers (only what ContextDaemon uses)
# ---------------------------------------------------------------------------

class _DomainBase:
    _domain = ""

    def __init__(self, session: CDPSession):
        self._s = session

    def enable(self, **params) -> dict:
        return self._s.send(f"{self._domain}.enable", params or None)

    def disable(self) -> dict:
        return self._s.send(f"{self._domain}.disable")


class _PageDomain(_DomainBase):
    _domain = "Page"

    def navigate(self, url: str, wait_for_load: bool = True,
                 timeout: float = 30, session_id: str | None = None) -> dict:
        self._s.send("Page.enable", session_id=session_id)
        res = self._s.send("Page.navigate", {"url": url}, session_id=session_id)
        if wait_for_load:
            try:
                self._s.wait_for_event("Page.loadEventFired", timeout=timeout)
            except TimeoutError:
                pass
        return res

    def add_script_to_evaluate_on_new_document(self, source: str,
                                               session_id: str | None = None) -> dict:
        return self._s.send("Page.addScriptToEvaluateOnNewDocument",
                            {"source": source}, session_id=session_id)


class _NetworkDomain(_DomainBase):
    _domain = "Network"

    def get_all_cookies(self, session_id: str | None = None) -> list[dict]:
        return self._s.send("Network.getAllCookies",
                            session_id=session_id).get("cookies", [])

    def set_cookies(self, cookies: list[dict],
                    session_id: str | None = None) -> dict:
        return self._s.send("Network.setCookies", {"cookies": cookies},
                            session_id=session_id)

    def clear_browser_cookies(self, session_id: str | None = None) -> dict:
        return self._s.send("Network.clearBrowserCookies", session_id=session_id)


class _RuntimeDomain(_DomainBase):
    _domain = "Runtime"

    def evaluate(self, expression: str, return_by_value: bool = True,
                 await_promise: bool = False, session_id: str | None = None,
                 timeout: float = 30, **kwargs) -> Any:
        params: dict[str, Any] = {
            "expression": expression,
            "returnByValue": return_by_value,
            "awaitPromise": await_promise,
            **kwargs,
        }
        result = self._s.send("Runtime.evaluate", params,
                              session_id=session_id, timeout=timeout)
        if "exceptionDetails" in result:
            exc = result["exceptionDetails"]
            raise CDPError(-1, f"JS exception: {exc.get('text', str(exc))}")
        remote = result.get("result", {})
        if return_by_value:
            if remote.get("type") == "undefined":
                return None
            return remote.get("value", remote)
        return remote


class _BrowserDomain(_DomainBase):
    _domain = "Browser"

    def enable(self, **p) -> dict:
        return {}

    def disable(self) -> dict:
        return {}

    def get_version(self) -> dict:
        return self._s.send("Browser.getVersion")

    def close(self) -> dict:
        return self._s.send("Browser.close")

    def get_window_for_target(self, target_id: str) -> dict:
        return self._s.send("Browser.getWindowForTarget", {"targetId": target_id})

    def set_window_bounds(self, window_id: int, bounds: dict) -> dict:
        return self._s.send("Browser.setWindowBounds",
                            {"windowId": window_id, "bounds": bounds})


class _TargetDomain(_DomainBase):
    _domain = "Target"

    def get_targets(self, *, timeout: float | None = None) -> list[dict]:
        kw: dict = {}
        if timeout is not None:
            kw["timeout"] = timeout
        return self._s.send("Target.getTargets", **kw).get("targetInfos", [])

    def create_target(self, url: str = "about:blank",
                      browser_context_id: str | None = None,
                      **kwargs) -> str:
        params: dict[str, Any] = {"url": url, **kwargs}
        if browser_context_id:
            params["browserContextId"] = browser_context_id
        return self._s.send("Target.createTarget", params).get("targetId", "")

    def close_target(self, target_id: str) -> bool:
        return self._s.send("Target.closeTarget",
                            {"targetId": target_id}).get("success", False)

    def activate_target(self, target_id: str) -> dict:
        return self._s.send("Target.activateTarget", {"targetId": target_id})

    def attach_to_target(self, target_id: str, flatten: bool = True) -> str:
        return self._s.send("Target.attachToTarget", {
            "targetId": target_id, "flatten": flatten,
        }).get("sessionId", "")

    def detach_from_target(self, session_id: str) -> dict:
        return self._s.send("Target.detachFromTarget", {"sessionId": session_id})

    def create_browser_context(self, **kwargs) -> str:
        return self._s.send("Target.createBrowserContext",
                            kwargs or None).get("browserContextId", "")

    def dispose_browser_context(self, browser_context_id: str) -> dict:
        return self._s.send("Target.disposeBrowserContext",
                            {"browserContextId": browser_context_id})

    def get_browser_contexts(self) -> list[str]:
        return self._s.send("Target.getBrowserContexts").get("browserContextIds", [])


class _StorageDomain(_DomainBase):
    _domain = "Storage"

    def get_cookies(self, browser_context_id: str | None = None,
                    timeout: float = 30) -> list[dict]:
        params = {"browserContextId": browser_context_id} if browser_context_id else None
        return self._s.send("Storage.getCookies", params, timeout=timeout).get("cookies", [])

    def set_cookies(self, cookies: list[dict],
                    browser_context_id: str | None = None) -> dict:
        params: dict[str, Any] = {"cookies": cookies}
        if browser_context_id:
            params["browserContextId"] = browser_context_id
        return self._s.send("Storage.setCookies", params)

    def clear_cookies(self, browser_context_id: str | None = None) -> dict:
        params = {"browserContextId": browser_context_id} if browser_context_id else None
        return self._s.send("Storage.clearCookies", params)


# ---------------------------------------------------------------------------
# CLI (minimal - just for debugging the browser lifecycle)
# ---------------------------------------------------------------------------

def _cli():
    import argparse
    ap = argparse.ArgumentParser(prog="chrome_cdp")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("start").add_argument("--headless", action="store_true")
    sub.add_parser("stop")
    sub.add_parser("status")
    args = ap.parse_args()
    mgr = ChromeManager()
    if args.cmd == "start":
        mgr.start(headless=args.headless)
        print(json.dumps(mgr.status(), indent=2))
    elif args.cmd == "stop":
        mgr.stop()
        print("stopped")
    else:
        print(json.dumps(mgr.status(), indent=2))


if __name__ == "__main__":
    _cli()
