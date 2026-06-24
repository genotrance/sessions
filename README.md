# Sessions

**Isolated browser profiles that remember everything.**

Sessions lets you run multiple isolated browsing sessions in Chrome (or Edge). Each session keeps its own cookies, logins, localStorage, and IndexedDB — so you can stay signed in to several accounts at once, or keep work and personal browsing completely separate.

Two types of sessions:

- **Session** — A full Chrome profile with persistent login, extensions, and passkey support. Best for sites that need long-lived authentication (Gmail, Okta, etc.).
- **Lite Session** — A lightweight CDP browser context with snapshotted state. Fast to create and low on resources, ideal for casual browsing.

When you're done, hibernate a session to free memory. Its tabs and data are saved and restored exactly where you left off — even after a reboot.

## Why?

Chrome's built-in profiles are heavyweight and hard to manage. Incognito windows forget everything when closed. Extensions like containers are limited and can't save IndexedDB or restore tabs with full state.

Sessions fills the gap: a hybrid of full Chrome profiles and lightweight isolated contexts, all managed from a simple dashboard.

## Quick Start

**Requirements:** Python 3.10+ and Chrome (or Edge) installed.

### Install from PyPI

```bash
pip install sessions
```

### Run

```bash
sessions start
```

A dashboard opens in your browser at [http://localhost:9999](http://localhost:9999).

### Stop

```bash
sessions stop
```

All open sessions are automatically saved before shutdown.

### Install from source

```bash
pip install -e .
```

## How to Use

### Creating and browsing

- **+Session** (blue button) — Creates a full Chrome profile session. Supports persistent login, extensions, and passkeys. Chrome populates the profile on first launch. Note: profile windows appear as separate taskbar entries on Windows — this is inherent to Chrome's profile system.
- **+Lite Session** (green button) — Creates a lightweight isolated session, ideal for casual browsing.
- **Click a tab** in the dashboard to focus it in Chrome. For hibernated sessions, clicking a tab restores the session and opens that URL.

### Managing sessions

- **Hibernate** (pause icon) — Saves the session's tabs, cookies, and site data, then closes its browser window to free memory.
- **Restore** (play icon) — Brings a hibernated session back with all its tabs and logins intact.
- **Move tab** (scissors icon) — Cut a tab from one session and paste it into another. Hover over a tab to see the scissors icon; click it, then click the paste icon on any other session. Works between active and hibernated sessions — cookies, localStorage, and IndexedDB for that tab's origin are transferred. Press Escape to cancel.
- **Delete** (trash icon) — Permanently removes a session and all its saved data.
- **Clean** — Wipes cookies, localStorage, and IndexedDB for a session. For running sessions, data is cleared in the live browser so it behaves like a fresh session without needing to restart.
- **Auto-clean on tab close** — Closing a tab in a Session (profile-backed) automatically clears that site's cookies, localStorage, and IndexedDB from the profile, so no data lingers from briefly visited sites.
- **Restart** — Restarts the Sessions backend while keeping Chrome running.
- **Quit** — Saves all sessions and shuts down both the backend and Chrome.

### Bulk operations

Use the checkboxes next to each session (or the select-all checkbox) to select multiple sessions, then use the bulk action bar to **Restore**, **Hibernate**, **Clean**, or **Delete** them all at once.

### Dashboard layout

The dashboard splits into two columns — **Active** sessions on the left and **Hibernated** on the right.

Lite Sessions have a **green** left border; Sessions (profile-backed) have a **blue** left border.

### Search and keyboard navigation

Use the search box to filter sessions by name, URL, or tab title. Search results are also split into Active/Hibernated columns. Arrow keys navigate hot results first then cold, Enter activates the focused item, and Escape clears the search.

### Right-click menu

Right-click any session for a context menu with **Restore**, **Hibernate**, **Clone**, **Clean**, and **Delete** options.

## Features

- **Two session types** — Full profile sessions for persistent login and extensions, plus lightweight Lite Sessions for casual browsing.
- **Session isolation** — Each session has its own cookies, localStorage, IndexedDB, and browsing context.
- **Full state persistence** — Lite Sessions save cookies, storage, and tabs to SQLite. Sessions use Chrome's native profile storage.
- **Auto-save** — Running sessions are periodically snapshotted so nothing is lost if Chrome crashes.
- **Auto-hibernate** — Closing a browser window automatically hibernates the session.
- **Move tabs between sessions** — Cut a tab from any session and paste it into another. Works across session types.
- **Tab restoration** — Hibernated sessions restore all tabs with their original URLs and site data.
- **Crash recovery** — If Chrome dies unexpectedly, Sessions restarts it and restores your work. Profile sessions are re-launched and Lite Sessions are restored from snapshots.
- **Unique profile icons** — Each Session (profile-backed) gets a distinct Chrome avatar icon so windows are easy to tell apart in the taskbar and Alt-Tab. Renaming a session updates the icon label.
- **Cross-platform** — Works on Windows, macOS, and Linux with Chrome or Edge.
- **Two-column dashboard** — Active sessions on the left, hibernated on the right. Green borders for Lite Sessions, blue for Sessions.
- **Dashboard UI** — A lightweight web dashboard for managing sessions from any tab.
- **Keyboard hotkey** — Press `Win+/` (Windows) or `Ctrl+/` (macOS/Linux) to open the dashboard instantly. Disable with `--no-hotkey` if it conflicts with other software.
- **Chrome diagnostics** — Chrome native logs (`chrome_debug.log`), stderr capture (`chrome_stderr.log`), and crash dumps (`crashes/`) are written to the user data directory for troubleshooting. Logs are rotated on restart (keeping 3 previous generations) so crash-era diagnostics are preserved.

## Options

| Flag | Description |
|---|---|
| `--api-port PORT` | Dashboard/API port (default: 9999) |
| `--browser-port PORT` | Chrome DevTools port (default: 9222) |
| `--headless` | Run Chrome without a visible window |
| `--no-browser-open` | Don't open the dashboard on startup |
| `--debug` | Write detailed logs to `debug.log` |
| `--foreground` | Run in the foreground instead of daemonizing |
| `--no-hotkey` | Disable the global keyboard shortcut |

## Development

```bash
pip install -e ".[dev]"
pytest
```

Lint with [Ruff](https://docs.astral.sh/ruff/):

```bash
ruff check src/ tests/
```

### Publishing to PyPI

Releases are published automatically via GitHub Actions when a new release is created on GitHub. To publish manually:

```bash
python -m build
twine upload dist/*
```

## License

[MIT](LICENSE)
