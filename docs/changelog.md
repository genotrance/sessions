# Changelog

All notable changes to this project will be documented in this file.

## [0.1.4] - 2026-05-21

### Fixed

- **Sleep/wake duplicate windows**: After resuming from sleep, Chrome windows were duplicated because the crash recovery mechanism mistook a sluggish-but-alive Chrome for a dead process. Fixed by: (1) detecting sleep/wake via wall-clock gaps in the watcher loop and suppressing crash recovery for 15s afterward, (2) increasing the crash detection threshold from 2 to 5 consecutive failures, (3) making HTTP reachability checks retry 3× with longer timeouts (1s+5s instead of 0.5s+2s), (4) adding a 6-second grace period in `recover_chrome` to verify Chrome is truly dead before force-killing it, and (5) using `reconnect_to_existing` after restart to deduplicate windows restored by Chrome's own session restore.
- **Debug log location**: Debug log now writes to `sessions_debug.log` in the system temp directory (cross-platform) instead of the code directory, preventing permission issues and clutter.

## [0.1.3] - 2026-05-12

### Fixed

- **Dashboard column width responsiveness**: Two-column layout now dynamically adjusts to match the width of the search bar and top header at all window sizes using `minmax(0, 1fr)` grid columns.

## [0.1.2] - 2026-05-11

### Added

- **Two-column dashboard layout**: Active sessions on the left, hibernated on the right. Search results also split into two columns. Keyboard navigation goes through hot sessions first, then cold.
- **Move tab between sessions**: Cut a tab from one session and paste it into another. Works for any combination of active and hibernated sessions — tab data (cookies, localStorage, IndexedDB) is transferred along with it. Source tab is only removed after successful insertion into the destination to prevent data loss.
- **Paste-mode click targets**: In paste mode, clicking any tab in a destination session triggers the paste — no need to find empty space.
- **Action icon separators**: Visual separator between safe actions (hibernate, cut) and destructive actions (delete, close) to prevent accidental clicks.

### Fixed

- **Hot→hot move race condition**: Source tab is now closed under a lock before opening in the destination, preventing the background watcher from hibernating the source session mid-move.
- **Cold→hot source tab not removed**: Source tab removal now uses `save_hibernation` (full rewrite) instead of `delete_tab` which could silently fail.
- **Move tab doesn't steal focus**: Moved tabs open in the background (`background: true`) so the dashboard stays in the foreground.
- **Hibernate/delete only on first-tab hover**: Session-level action icons now only appear when hovering the first tab row, not any tab in the session.
- **Gmail/Google re-login after restart**: `__Host-` prefixed cookies (critical for Google authentication) were silently rejected during restore because they were set with a `domain` attribute. Chrome requires `__Host-` cookies to be host-only. Session cookies (no expiry) were also lost because CDP's `expires: -1` was passed through to `setCookies`, which interpreted it as a 1969 timestamp.
- **Empty session row overflow**: Hibernate/delete buttons no longer extend beyond the session box.
- **Trim log keeps last 500 lines**: The "Trim Log" button now keeps the last 500 lines instead of clearing the file.
- **Dashboard width**: Dashboard UI now uses 90% of screen width for the two-column layout.

## [0.1.1] - 2026-05-08

### Fixed

- **Crash-safe bulk operations**: Bulk hibernate, clean, and delete now send all selected session IDs in a single API call. User intent is recorded in the database before any Chrome work begins, so if Chrome crashes mid-operation, sessions you asked to hibernate stay hibernated instead of being restored.
- **Clean wipes live browser data**: Cleaning a running session now clears cookies, localStorage, and IndexedDB from the actual Chrome tabs (not just the backend database), so the session behaves like a fresh start without needing to restart it.

## [0.1.0] - 2026-04-29

### Added

- **Dashboard hotkey**: Win+/ (Windows) or Ctrl+/ (macOS/Linux) to instantly open the sessions dashboard and focus the search box
- **Session management**: Create, restore, hibernate, and delete browser sessions with full state preservation (cookies, localStorage, IndexedDB)
- **Tab search**: Search across all saved tabs by title and URL in the dashboard
- **Bulk operations**: Select multiple sessions for batch hibernation, cleaning, or deletion
- **Session isolation**: Each session runs in its own browser context with complete isolation of cookies, storage, and site data
- **State preservation**: Automatic snapshots of session state (cookies, localStorage, IndexedDB) every 30 seconds
- **Crash recovery**: Automatic reconnection to existing sessions if the daemon restarts
- **Dashboard UI**: Web-based interface for managing sessions, tabs, and state
- **HTTP API**: RESTful API for programmatic session management
- **Daemon mode**: Background service for persistent session management
- **Cross-platform support**: Windows, macOS, and Linux

### Fixed

- Dashboard search box now auto-focuses when activated via Win+/ hotkey
