# Changelog

All notable changes to this project will be documented in this file.

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
