# Changelog

## 4.0.6 — 2026-09-06

### Fixed

- Show the current release's notes once after a manual download as well as
  after an automatic update.

## 4.0.5 — 2026-09-06

### Added

- Added a startup update check for packaged releases. New releases download,
  verify against GitHub's published SHA-256 digest, install, and relaunch
  automatically before the main window opens.
- Shows the applied release's short changelog in the next welcome screen.

## 4.0.4 — 2026-09-06

### Changed

- Moved quick capture and encryption controls above Settings and placed the
  release label beside the Settings button.

## 4.0.3 — 2026-09-06

### Added

- Added a sidebar toggle for choosing encrypted `.asenc` or plaintext `.md`
  storage for new AutoScript recordings. Encryption remains enabled by default.

## 4.0.2 — 2026-09-06

### Changed

- Added a subtle release-version label beside Open Folder so users can identify the installed executable.

## 4.0.1 — 2026-09-06

### Fixed

- Corrected Windows DPAPI secret handling in the packaged application so the
  Discord plugin and AutoScript can safely use the same local protection code.
- Preserved compatibility with bot tokens and transcript tokens saved by earlier
  releases.
