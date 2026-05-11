# Sessions

**Isolated browser profiles that remember everything.**

Sessions lets you run multiple isolated browsing profiles in a single Chrome (or Edge) window. Each session keeps its own cookies, logins, localStorage, and IndexedDB — so you can stay signed in to several accounts at once, or keep work and personal browsing completely separate.

When you're done, hibernate a session to free memory. Its tabs, cookies, and site data are saved to disk and restored exactly where you left off — even after a reboot.

## Why?

Chrome's built-in profiles are heavyweight. Incognito windows forget everything when closed. Extensions like containers are limited and can't save IndexedDB or restore tabs with full state.

Sessions fills the gap: lightweight isolated contexts with full state persistence, managed from a simple dashboard.

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

- **+ New** — Creates a new isolated session. A fresh Chrome window opens where you can navigate to any site, log in, and use it normally.
- **Click a tab** in the dashboard to focus it in Chrome. For hibernated sessions, clicking a tab restores the session and opens that URL.

### Managing sessions

- **Hibernate** (pause icon) — Saves the session's tabs, cookies, and site data, then closes its browser window to free memory.
- **Restore** (play icon) — Brings a hibernated session back with all its tabs and logins intact.
- **Move tab** (scissors icon) — Cut a tab from one session and paste it into another. Hover over a tab to see the scissors icon; click it, then click the paste icon on any other session. Works between active and hibernated sessions — cookies, localStorage, and IndexedDB for that tab's origin are transferred. Press Escape to cancel.
- **Delete** (trash icon) — Permanently removes a session and all its saved data.
- **Clean** — Wipes cookies, localStorage, and IndexedDB for a session. For running sessions, data is cleared in the live browser so it behaves like a fresh session without needing to restart.
- **Restart** — Restarts the Sessions backend while keeping Chrome running.
- **Quit** — Saves all sessions and shuts down both the backend and Chrome.

### Bulk operations

Use the checkboxes next to each session (or the select-all checkbox) to select multiple sessions, then use the bulk action bar to **Restore**, **Hibernate**, **Clean**, or **Delete** them all at once.

### Dashboard layout

The dashboard splits into two columns — **Active** sessions on the left and **Hibernated** on the right. Each session has a green (active) or yellow (hibernated) left border for quick identification.

### Search and keyboard navigation

Use the search box to filter sessions by name, URL, or tab title. Search results are also split into Active/Hibernated columns. Arrow keys navigate hot results first then cold, Enter activates the focused item, and Escape clears the search.

### Right-click menu

Right-click any session for a context menu with **Restore**, **Hibernate**, **Clone**, **Clean**, and **Delete** options.

## Features

- **Isolated profiles** — Each session has its own cookies, localStorage, IndexedDB, and browsing context.
- **Full state persistence** — Sessions are saved to a local SQLite database including cookies, storage, and open tabs.
- **Auto-save** — Running sessions are periodically snapshotted so nothing is lost if Chrome crashes.
- **Auto-hibernate** — Closing a browser window automatically hibernates the session.
- **Move tabs between sessions** — Cut a tab from any session and paste it into another, transferring cookies, localStorage, and IndexedDB for that origin.
- **Tab restoration** — Hibernated sessions restore all tabs with their original URLs and site data.
- **Crash recovery** — If Chrome dies unexpectedly, Sessions restarts it and restores your work. Bulk operations in progress (e.g. hibernate) are honoured — sessions you asked to hibernate stay hibernated.
- **Cross-platform** — Works on Windows, macOS, and Linux with Chrome or Edge.
- **Two-column dashboard** — Active sessions on the left, hibernated on the right, each with a colored left border.
- **Dashboard UI** — A lightweight web dashboard for managing sessions from any tab.
- **Keyboard hotkey** — Press `Win+/` (Windows) or `Ctrl+/` (macOS/Linux) to open the dashboard instantly. Disable with `--no-hotkey` if it conflicts with other software.

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
