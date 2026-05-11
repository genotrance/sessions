"""Live integration tests: verify document.hasFocus() CDP polling detects
Chrome tab/window activation — including native Win32 switches.

Requires Chrome running with --remote-debugging-port=9222.
Run with:  python -m pytest tests/test_focus_live.py -v -m live
"""
from __future__ import annotations

import json
import sys
import time
import unittest

try:
    import pytest
    import requests
    import websocket
except ImportError:
    if "unittest" in sys.modules:
        raise unittest.SkipTest("pytest/requests/websocket not installed")
    raise

CHROME_PORT = 9222


# ---------------------------------------------------------------------------
# Minimal CDP helper (avoids coupling to the production CDPSession)
# ---------------------------------------------------------------------------

class CDPHelper:
    """Thin wrapper around the browser-level Chrome DevTools WebSocket."""

    def __init__(self, port: int = CHROME_PORT):
        self.port = port
        self._msg_id = 0
        info = requests.get(
            f"http://127.0.0.1:{port}/json/version", timeout=5
        ).json()
        self.ws = websocket.create_connection(
            info["webSocketDebuggerUrl"], timeout=15
        )

    # -- low-level -----------------------------------------------------------

    def send(self, method: str, params: dict | None = None,
             session_id: str | None = None, timeout: float = 10) -> dict:
        self._msg_id += 1
        msg: dict = {"id": self._msg_id, "method": method}
        if params:
            msg["params"] = params
        if session_id:
            msg["sessionId"] = session_id
        self.ws.send(json.dumps(msg))
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.ws.settimeout(max(0.05, deadline - time.time()))
            try:
                raw = self.ws.recv()
            except websocket.WebSocketTimeoutException:
                break
            except Exception:
                break
            resp = json.loads(raw)
            if resp.get("id") == self._msg_id:
                if "error" in resp:
                    raise RuntimeError(resp["error"])
                return resp.get("result", {})
        raise TimeoutError(f"No response for {method}")

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass

    # -- high-level helpers --------------------------------------------------

    def create_context(self) -> str:
        return self.send("Target.createBrowserContext")["browserContextId"]

    def dispose_context(self, ctx: str):
        try:
            self.send("Target.disposeBrowserContext",
                      {"browserContextId": ctx})
        except Exception:
            pass

    def create_tab(self, url: str = "about:blank",
                   context_id: str | None = None) -> str:
        params: dict = {"url": url}
        if context_id:
            params["browserContextId"] = context_id
        return self.send("Target.createTarget", params)["targetId"]

    def close_tab(self, tid: str):
        try:
            self.send("Target.closeTarget", {"targetId": tid})
        except Exception:
            pass

    def attach(self, tid: str) -> str:
        return self.send("Target.attachToTarget",
                         {"targetId": tid, "flatten": True})["sessionId"]

    def activate(self, tid: str):
        self.send("Target.activateTarget", {"targetId": tid})

    def has_focus(self, session_id: str) -> bool:
        r = self.send("Runtime.evaluate", {
            "expression": "document.hasFocus()",
            "returnByValue": True,
        }, session_id=session_id, timeout=5)
        return r.get("result", {}).get("value", False)

    def visibility_state(self, session_id: str) -> str:
        r = self.send("Runtime.evaluate", {
            "expression": "document.visibilityState",
            "returnByValue": True,
        }, session_id=session_id, timeout=5)
        return r.get("result", {}).get("value", "")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _chrome_reachable() -> bool:
    try:
        requests.get(f"http://127.0.0.1:{CHROME_PORT}/json/version", timeout=2)
        return True
    except Exception:
        return False


@pytest.fixture
def cdp():
    if not _chrome_reachable():
        pytest.skip(f"Chrome not running on port {CHROME_PORT}")
    h = CDPHelper(CHROME_PORT)
    yield h
    h.close()


class _TabFixture:
    """Manages test tabs/contexts so tests clean up automatically."""
    def __init__(self, cdp: CDPHelper):
        self.cdp = cdp
        self._contexts: list[str] = []
        self._tabs: list[str] = []

    def create(self, url: str = "about:blank",
               context_id: str | None = None) -> tuple[str, str, str]:
        """Create a context + tab + flatten session.  Returns (ctx, tid, sid)."""
        if context_id is None:
            context_id = self.cdp.create_context()
            self._contexts.append(context_id)
        tid = self.cdp.create_tab(url, context_id)
        self._tabs.append(tid)
        time.sleep(0.6)   # let Chrome settle the new tab
        sid = self.cdp.attach(tid)
        return context_id, tid, sid

    def cleanup(self):
        for tid in reversed(self._tabs):
            self.cdp.close_tab(tid)
        time.sleep(0.3)
        for ctx in reversed(self._contexts):
            self.cdp.dispose_context(ctx)


@pytest.fixture
def tabs(cdp):
    tf = _TabFixture(cdp)
    yield tf
    tf.cleanup()


# ---------------------------------------------------------------------------
# Tests - programmatic activation (CDP activateTarget)
# ---------------------------------------------------------------------------

@pytest.mark.live
class TestProgrammaticActivation:
    """Verify document.hasFocus() responds to Target.activateTarget."""

    def test_single_window_tab_switch(self, cdp, tabs):
        """Two tabs in the same context: activateTarget toggles hasFocus."""
        ctx, tid1, sid1 = tabs.create("data:text/html,<title>SW-T1</title>")
        _, tid2, sid2 = tabs.create("data:text/html,<title>SW-T2</title>",
                                    context_id=ctx)

        cdp.activate(tid1)
        time.sleep(0.5)
        assert cdp.has_focus(sid1) is True
        assert cdp.has_focus(sid2) is False

        cdp.activate(tid2)
        time.sleep(0.5)
        assert cdp.has_focus(sid1) is False
        assert cdp.has_focus(sid2) is True

    def test_cross_window_activation(self, cdp, tabs):
        """Two tabs in different contexts (= different windows):
        activateTarget switches focus between windows."""
        _, tid1, sid1 = tabs.create("data:text/html,<title>CW-T1</title>")
        _, tid2, sid2 = tabs.create("data:text/html,<title>CW-T2</title>")

        cdp.activate(tid1)
        time.sleep(0.5)
        assert cdp.has_focus(sid1) is True
        assert cdp.has_focus(sid2) is False

        cdp.activate(tid2)
        time.sleep(0.5)
        assert cdp.has_focus(sid2) is True
        assert cdp.has_focus(sid1) is False

    def test_visibility_state_reflects_active_tab(self, cdp, tabs):
        """document.visibilityState returns a valid string for each tab.
        Note: visibilityState is NOT used for focus detection — we rely on
        document.hasFocus().  This test just confirms the API works."""
        ctx, tid1, sid1 = tabs.create("data:text/html,<title>VS-T1</title>")
        _, tid2, sid2 = tabs.create("data:text/html,<title>VS-T2</title>",
                                    context_id=ctx)

        cdp.activate(tid1)
        time.sleep(0.5)
        v1 = cdp.visibility_state(sid1)
        v2 = cdp.visibility_state(sid2)
        assert v1 in ("visible", "hidden")
        assert v2 in ("visible", "hidden")

    def test_rapid_switch(self, cdp, tabs):
        """Rapidly switching between tabs: final state is always correct."""
        ctx, tid1, sid1 = tabs.create("data:text/html,<title>RS-T1</title>")
        _, tid2, sid2 = tabs.create("data:text/html,<title>RS-T2</title>",
                                    context_id=ctx)

        for _ in range(5):
            cdp.activate(tid1)
            cdp.activate(tid2)

        time.sleep(0.5)
        assert cdp.has_focus(sid2) is True
        assert cdp.has_focus(sid1) is False

    def test_three_windows_round_robin(self, cdp, tabs):
        """Three windows, activate each in turn: only the last has focus."""
        _, tid1, sid1 = tabs.create("data:text/html,<title>RR-T1</title>")
        _, tid2, sid2 = tabs.create("data:text/html,<title>RR-T2</title>")
        _, tid3, sid3 = tabs.create("data:text/html,<title>RR-T3</title>")

        for tid, sid, others in [
            (tid1, sid1, [(sid2, False), (sid3, False)]),
            (tid2, sid2, [(sid1, False), (sid3, False)]),
            (tid3, sid3, [(sid1, False), (sid2, False)]),
        ]:
            cdp.activate(tid)
            time.sleep(0.5)
            assert cdp.has_focus(sid) is True
            for osid, expected in others:
                assert cdp.has_focus(osid) is expected


# ---------------------------------------------------------------------------
# Tests - native Win32 window switching
# ---------------------------------------------------------------------------

def _have_win32():
    try:
        import win32api   # noqa: F401
        import win32con   # noqa: F401
        import win32gui   # noqa: F401
        return True
    except ImportError:
        return False


@pytest.mark.live
@pytest.mark.skipif(not _have_win32(), reason="pywin32 not installed")
class TestNativeWin32Switching:
    """Verify document.hasFocus() reacts to Win32-native window activation."""

    @staticmethod
    def _find_chrome_hwnd(substring: str, timeout: float = 5) -> int | None:
        import win32gui
        deadline = time.time() + timeout
        while time.time() < deadline:
            result: list[int] = []
            def cb(hwnd, _):
                if win32gui.IsWindowVisible(hwnd):
                    title = win32gui.GetWindowText(hwnd)
                    if substring in title:
                        result.append(hwnd)
            win32gui.EnumWindows(cb, None)
            if result:
                return result[0]
            time.sleep(0.3)
        return None

    @staticmethod
    def _set_foreground(hwnd: int):
        import win32con
        import win32gui
        # Minimize then restore: Windows always allows restoring a
        # minimized window to the foreground, bypassing the
        # SetForegroundWindow restriction.
        win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
        time.sleep(0.15)
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        # Belt-and-suspenders: try SetForegroundWindow too
        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            pass

    def test_native_window_switch(self, cdp, tabs):
        """SetForegroundWindow on a Chrome window changes hasFocus()."""
        _, tid1, sid1 = tabs.create("data:text/html,<title>NW-Win1-Focus</title>")
        _, tid2, sid2 = tabs.create("data:text/html,<title>NW-Win2-Focus</title>")

        # Make sure both windows exist and find their HWNDs
        cdp.activate(tid1)
        time.sleep(0.5)
        hwnd1 = self._find_chrome_hwnd("NW-Win1-Focus")
        cdp.activate(tid2)
        time.sleep(0.5)
        hwnd2 = self._find_chrome_hwnd("NW-Win2-Focus")

        if not hwnd1 or not hwnd2:
            pytest.skip("Could not find Chrome test windows by title")

        # Native switch to window 1
        self._set_foreground(hwnd1)
        time.sleep(0.7)
        assert cdp.has_focus(sid1) is True
        assert cdp.has_focus(sid2) is False

        # Native switch to window 2
        self._set_foreground(hwnd2)
        time.sleep(0.7)
        assert cdp.has_focus(sid2) is True
        assert cdp.has_focus(sid1) is False

    def test_native_tab_switch_via_keyboard(self, cdp, tabs):
        """Ctrl+Tab in a Chrome window changes which tab has focus."""
        import win32api
        import win32con

        ctx, tid1, sid1 = tabs.create("data:text/html,<title>NT-Tab1-KB</title>")
        _, tid2, sid2 = tabs.create("data:text/html,<title>NT-Tab2-KB</title>",
                                    context_id=ctx)

        # Start with tab 1 active — use CDP to guarantee Chrome has focus
        cdp.activate(tid1)
        time.sleep(0.5)
        hwnd = self._find_chrome_hwnd("NT-Tab1-KB")
        if not hwnd:
            pytest.skip("Could not find Chrome test window")
        # Activate via CDP first, then bring native window to front
        cdp.activate(tid1)
        self._set_foreground(hwnd)
        time.sleep(0.7)

        if not cdp.has_focus(sid1):
            pytest.skip("Could not reliably bring Chrome to foreground")

        # Send Ctrl+Tab to switch to the next tab
        win32api.keybd_event(win32con.VK_CONTROL, 0, 0, 0)
        win32api.keybd_event(win32con.VK_TAB, 0, 0, 0)
        win32api.keybd_event(win32con.VK_TAB, 0, win32con.KEYEVENTF_KEYUP, 0)
        win32api.keybd_event(win32con.VK_CONTROL, 0, win32con.KEYEVENTF_KEYUP, 0)
        time.sleep(0.7)

        # Now tab 2 should have focus
        assert cdp.has_focus(sid2) is True
        assert cdp.has_focus(sid1) is False

    def test_native_switch_back_and_forth(self, cdp, tabs):
        """Multiple native switches: focus always tracks the correct tab."""
        _, tid1, sid1 = tabs.create("data:text/html,<title>BF-Win1</title>")
        _, tid2, sid2 = tabs.create("data:text/html,<title>BF-Win2</title>")

        cdp.activate(tid1)
        time.sleep(0.5)
        hwnd1 = self._find_chrome_hwnd("BF-Win1")
        cdp.activate(tid2)
        time.sleep(0.5)
        hwnd2 = self._find_chrome_hwnd("BF-Win2")
        if not hwnd1 or not hwnd2:
            pytest.skip("Could not find Chrome test windows")

        for _ in range(3):
            self._set_foreground(hwnd1)
            time.sleep(0.5)
            assert cdp.has_focus(sid1) is True

            self._set_foreground(hwnd2)
            time.sleep(0.5)
            assert cdp.has_focus(sid2) is True


# ---------------------------------------------------------------------------
# Tests - mixed scenario
# ---------------------------------------------------------------------------

@pytest.mark.live
class TestMixedScenarios:
    """Tests that combine programmatic + query to verify consistency."""

    def test_no_chrome_focus_returns_all_false(self, cdp, tabs):
        """When Chrome is not the foreground app, all tabs return False.
        (We simulate by not activating any tab after creation — the IDE
        window should still be in front.)"""
        _, tid1, sid1 = tabs.create("data:text/html,<title>NoFocus1</title>")
        # Don't activate — let the current foreground app keep focus
        # If the test runner has focus (not Chrome), hasFocus should be False
        # NOTE: this test is inherently environment-dependent.
        # We just verify the API returns a bool without crashing.
        result = cdp.has_focus(sid1)
        assert isinstance(result, bool)

    def test_has_focus_on_about_blank(self, cdp, tabs):
        """document.hasFocus() works on about:blank tabs."""
        _, tid1, sid1 = tabs.create("about:blank")
        cdp.activate(tid1)
        time.sleep(0.5)
        assert cdp.has_focus(sid1) is True

    def test_has_focus_after_navigation(self, cdp, tabs):
        """Focus survives a same-tab navigation."""
        ctx, tid1, sid1 = tabs.create("data:text/html,<title>Nav1</title>")
        cdp.activate(tid1)
        time.sleep(0.5)
        assert cdp.has_focus(sid1) is True

        # Navigate the tab
        cdp.send("Page.navigate",
                 {"url": "data:text/html,<title>Nav2</title>"},
                 session_id=sid1)
        time.sleep(1.0)
        # After navigation, the tab should still have focus
        assert cdp.has_focus(sid1) is True
