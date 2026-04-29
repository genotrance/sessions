"""HTTP API server for Sessions."""
from __future__ import annotations

import json
import logging
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .dashboard import DASHBOARD_HTML
from .manager import ContainerManager

log = logging.getLogger("sessions")

DEFAULT_API_PORT = 9999


class _ApiHandler(BaseHTTPRequestHandler):
    manager: ContainerManager = None  # type: ignore
    shutdown_cb = None  # type: ignore
    restart_cb = None  # type: ignore

    def log_message(self, fmt, *args):
        log.debug("http %s", fmt % args if args else fmt)

    def _send_json(self, obj: Any, status: int = 200) -> None:
        data = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (ConnectionAbortedError, BrokenPipeError, ConnectionResetError):
            pass  # client disconnected after headers were sent

    def _send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (ConnectionAbortedError, BrokenPipeError, ConnectionResetError):
            pass  # client disconnected after headers were sent

    def _body(self) -> dict:
        ln = int(self.headers.get("Content-Length", "0") or "0")
        if not ln:
            return {}
        raw = self.rfile.read(ln)
        try:
            return json.loads(raw)
        except Exception:
            return {}

    def _route(self, method: str):
        path = urllib.parse.urlparse(self.path).path
        if method == "GET" and path in ("/", "/index.html", "/dashboard"):
            return self._send_html(DASHBOARD_HTML)
        if method == "GET" and path == "/api/containers":
            return self._send_json(self.manager.status())
        if method == "POST" and path == "/api/containers":
            b = self._body()
            color = b.get("color") or "#3b82f6"
            url = (b.get("url") or "").strip()
            if url:
                return self._send_json(self.manager.create_for_url(url, color))
            name = b.get("name") or "session"
            c = self.manager.create_container(name, color)
            try:
                self.manager.restore(c["id"])
            except Exception:
                pass
            return self._send_json(c)
        if method == "POST" and path == "/api/activate":
            tid = self._body().get("targetId", "")
            return self._send_json(self.manager.activate_tab(tid))
        if method == "POST" and path == "/api/close-tab":
            tid = self._body().get("targetId", "")
            return self._send_json(self.manager.close_tab(tid))
        if method == "POST" and path == "/api/hibernate-all":
            return self._send_json({"results": self.manager.hibernate_all()})
        if method == "POST" and path == "/api/snapshot-all":
            return self._send_json({"results": self.manager.snapshot_all()})
        if method == "POST" and path == "/api/shutdown":
            log.debug("shutdown requested, Referer=%s User-Agent=%s",
                      self.headers.get("Referer", "-"),
                      self.headers.get("User-Agent", "-")[:80])
            results = self.manager.quick_shutdown()
            self._send_json({"shutdown": True, "results": results})
            if self.shutdown_cb:
                threading.Thread(target=self.shutdown_cb, daemon=True).start()
            return
        if method == "POST" and path == "/api/restart":
            self._send_json({"restarting": True})
            log.debug("restart route: restart_cb=%s", self.restart_cb)
            if self.restart_cb:
                self.restart_cb()
            return
        if method == "POST" and path == "/api/clean-default":
            self.manager.clean_default_context()
            return self._send_json({"cleaned": True})
        if method == "POST" and path == "/api/trim-log":
            return self._send_json(self.manager.trim_log())
        m = re.match(r"^/api/containers/([^/]+)(?:/(\w+))?$", path)
        if m:
            cid, action = m.group(1), m.group(2)
            if method == "DELETE" and not action:
                self.manager.delete(cid)
                return self._send_json({"id": cid, "deleted": True})
            if method == "PATCH" and not action:
                b = self._body()
                name = b.get("name")
                if name:
                    return self._send_json(self.manager.rename(cid, name))
                return self._send_json({"error": "nothing to update"}, status=400)
            if method == "DELETE" and action == "tab":
                url = self._body().get("url", "")
                ok = self.manager.store.delete_tab(cid, url)
                return self._send_json({"id": cid, "url": url, "deleted": ok})
            if method == "POST":
                if action == "hibernate":
                    return self._send_json(self.manager.hibernate(cid))
                if action == "restore":
                    return self._send_json(self.manager.restore(cid))
                if action == "open":
                    url = self._body().get("url", "about:blank")
                    tid = self.manager.open_tab(cid, url)
                    return self._send_json({"id": cid, "targetId": tid})
                if action == "clone":
                    new_name = self._body().get("name")
                    res = self.manager.clone(cid, new_name)
                    return self._send_json(res or {"error": "not-found"},
                                           status=200 if res else 404)
                if action == "clean":
                    return self._send_json(self.manager.clean(cid))
        self.send_response(404)
        self.end_headers()

    def _handle(self, method: str):
        try:
            self._route(method)
        except (ConnectionAbortedError, BrokenPipeError, ConnectionResetError):
            pass  # client disconnected mid-response — harmless
        except KeyError as e:
            log.debug("API 404 %s %s: %s", method, self.path, e)
            self._send_json({"error": f"unknown container {e}"}, status=404)
        except Exception as e:
            log.exception("API error %s %s", method, self.path)
            try:
                self._send_json({"error": str(e)}, status=500)
            except (ConnectionAbortedError, BrokenPipeError, ConnectionResetError):
                pass

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PATCH(self):
        self._handle("PATCH")

    def do_DELETE(self):
        self._handle("DELETE")


def make_server(manager: ContainerManager, port: int = DEFAULT_API_PORT) -> ThreadingHTTPServer:
    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer(("127.0.0.1", port), _ApiHandler)
    handler = type("_H", (_ApiHandler,),
                   {"manager": manager, "shutdown_cb": server.shutdown,
                    "restart_cb": None})
    server.RequestHandlerClass = handler
    return server
